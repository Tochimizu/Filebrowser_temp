#!/usr/bin/env python3
"""Create isolated File Browser accounts from a username,password CSV."""
import argparse
import csv
import getpass
import json
import os
import posixpath
import re
import sys
from urllib import error, parse, request


class ImportError_(Exception):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    # Never forward credentials or X-Auth tokens to a redirect destination.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base):
        self.base = base.rstrip("/") + "/api"
        self.token = None
        self.opener = request.build_opener(NoRedirect())

    def call(self, method, path, data=None, missing_ok=False):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Auth"] = self.token
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
        req = request.Request(self.base + path, data=body,
                              headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as response:
                return response.read()
        except error.HTTPError as exc:
            exc.close()
            if missing_ok and exc.code == 404:
                return None
            # Response bodies can echo secrets; do not print them.
            raise ImportError_(f"{method} {path}: HTTP {exc.code}. "
                               "Check credentials, permissions, password policy and server logs.") from None
        except (error.URLError, TimeoutError, OSError):
            raise ImportError_(f"{method} {path}: connection failed or timed out. "
                               "Check the server before retrying; a timed-out write may have succeeded.") from None

    def json(self, path):
        try:
            return json.loads(self.call("GET", path))
        except (ValueError, UnicodeError):
            raise ImportError_(f"GET {path}: expected JSON; check --base-url.") from None

    def login(self, username, password):
        raw = self.call("POST", "/login",
                        {"username": username, "password": password, "recaptcha": ""})
        try:
            token = raw.decode("ascii").strip()
        except UnicodeError:
            token = ""
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise ImportError_("Login did not return a raw JWT. Check --base-url and authentication settings.")
        self.token = token


def read_csv(path, delimiter):
    records = []
    seen = set()
    with open(path, encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=delimiter, strict=True)
        header = reader.fieldnames
        if not header or len(header) != len(set(header)) or not {"username", "password"} <= set(header):
            raise ImportError_("CSV must have unique column headers including username,password.")
        for row in reader:
            line = reader.line_num
            if None in row or any(value is None for value in row.values()):
                raise ImportError_(f"CSV line {line}: wrong number of columns.")
            username, password = row["username"], row["password"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}", username) or ".." in username:
                raise ImportError_(f"CSV line {line}: username must start with a letter/digit and use "
                                   "only letters, digits, _, -, ., @ (1-64 characters; no '..').")
            if not password:
                raise ImportError_(f"CSV line {line}: password is empty.")
            if username.casefold() in seen:
                raise ImportError_(f"CSV line {line}: duplicate username (case-insensitive).")
            seen.add(username.casefold())
            records.append((username, password))
    if not records:
        raise ImportError_("CSV contains no users.")
    return records


def scope_path(scope):
    if not isinstance(scope, str) or "\\" in scope:
        raise ImportError_("Server returned an invalid user scope.")
    return posixpath.normpath("/" + scope.lstrip("/")).casefold()


def overlapping(first, second):
    return (first == second or first == "/" or second == "/" or
            first.startswith(second + "/") or second.startswith(first + "/"))


PERMISSIONS = {
    "admin": False, "execute": False, "share": False,
    "create": True, "rename": True, "modify": True, "delete": True, "download": True,
}


def verify_user(user, scope):
    if scope_path(user.get("scope")) != scope_path(scope):
        raise ImportError_("Account scope differs from the requested private folder.")
    permissions = user.get("perm", {})
    if any(permissions.get(key) is not value for key, value in PERMISSIONS.items()):
        raise ImportError_("Account permissions differ from the requested standard-user permissions.")
    if user.get("commands"):
        raise ImportError_("Account has unexpected executable commands.")


def preflight(api, records, admin):
    users = api.json("/users")
    if not isinstance(users, list):
        raise ImportError_("Expected a user list from /api/users.")
    admins = [u for u in users if u.get("username") == admin]
    if len(admins) != 1 or admins[0].get("perm", {}).get("admin") is not True:
        raise ImportError_("The login account must be a File Browser administrator.")
    # Resource paths are relative to the administrator's scope; user scopes
    # in /api/users are relative to the server root. Keep these paths distinct.
    scope_path(admins[0].get("scope"))  # Validate before joining, preserving case.
    admin_scope = posixpath.normpath("/" + admins[0]["scope"].lstrip("/"))
    settings = api.json("/settings")
    minimum = settings.get("minimumPasswordLength", 0)
    if not isinstance(minimum, int) or minimum < 0:
        raise ImportError_("Server returned an invalid minimum password length.")
    plan = []
    for username, password in records:
        scope = posixpath.join(admin_scope, "users", username)
        matches = [u for u in users if u.get("username", "").casefold() == username.casefold()]
        existing = matches[0] if len(matches) == 1 else None
        if matches:
            if len(matches) != 1 or existing.get("username") != username:
                raise ImportError_(f"{username}: conflicting existing username.")
            try:
                verify_user(existing, scope)
            except ImportError_ as exc:
                raise ImportError_(f"{username}: existing user is unsafe to skip: {exc}") from None
        elif len(password) < minimum:
            raise ImportError_(f"{username}: password is shorter than the server minimum ({minimum}).")
        for other in users:
            if other is existing or other.get("perm", {}).get("admin") is True:
                continue
            if overlapping(scope_path(other.get("scope")), scope_path(scope)):
                raise ImportError_(f"{username}: another non-admin account has an overlapping scope. "
                                   "Correct that account's scope before importing.")
        if not existing:
            folder = api.call("GET", "/resources/users/" + username, missing_ok=True)
            if folder is not None:
                raise ImportError_(f"{username}: target folder already exists without a matching safe account. "
                                   "Inspect it manually before assigning its data.")
        plan.append((username, password, scope, bool(existing)))
    # A link at the shared parent could point into unrelated data.
    parent = api.call("GET", "/resources/users", missing_ok=True)
    if parent is not None:
        try:
            info = json.loads(parent)
        except ValueError:
            raise ImportError_("Expected directory metadata for /users.") from None
        if info.get("isDir") is not True or info.get("isSymlink"):
            raise ImportError_("/users must be a real directory, not a file or symbolic link.")
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", help="UTF-8 CSV with username,password headers")
    parser.add_argument("--base-url", default=os.environ.get("FB_BASE_URL", "http://localhost:8000"),
                        help="File Browser URL including any base path (default: http://localhost:8000)")
    parser.add_argument("--admin", default=os.environ.get("FB_ADMIN_USERNAME", "admin"))
    parser.add_argument("--delimiter", choices=[",", ";"], default=",")
    parser.add_argument("--dry-run", action="store_true", help="Validate CSV and server state without creating users/folders")
    args = parser.parse_args(argv)
    url = parse.urlsplit(args.base_url)
    if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ImportError_("--base-url must be an http(s) URL without credentials, query or fragment.")
    records = read_csv(args.csv_file, args.delimiter)
    password = os.environ.pop("FB_ADMIN_PASSWORD", None)
    if password is None:
        if not sys.stdin.isatty():
            raise ImportError_("Set FB_ADMIN_PASSWORD for non-interactive use.")
        password = getpass.getpass(f"File Browser password for {args.admin}: ")
    api = API(args.base_url)
    api.login(args.admin, password)
    plan = preflight(api, records, args.admin)
    execute_plan(api, plan, password, args.dry_run)


def execute_plan(api, plan, password, dry_run=False):
    created = skipped = 0
    for username, user_password, scope, exists in plan:
        if exists:
            print(f"SKIP {username}: already configured for {scope}; password unchanged.")
            skipped += 1
            continue
        if dry_run:
            print(f"WOULD CREATE {username}: scope={scope}")
            continue
        try:
            # /api/users creates the supplied home directory under the server
            # root. No resource write or root-scoped administrator is required.
            data = {
                "username": username, "password": user_password, "scope": scope,
                "locale": "en", "viewMode": "list", "lockPassword": False,
                "perm": PERMISSIONS.copy(), "commands": [], "rules": [],
                "sorting": {"by": "name", "asc": True},
            }
            api.call("POST", "/users",
                     {"what": "user", "which": [], "data": data, "current_password": password})
            saved = [u for u in api.json("/users") if u.get("username") == username]
            if len(saved) != 1:
                raise ImportError_("Could not read back the created account.")
            verify_user(saved[0], scope)
            folder = api.json("/resources/users/" + username)
            if folder.get("isDir") is not True or folder.get("isSymlink"):
                raise ImportError_("Created home is not an accessible real directory.")
        except ImportError_ as exc:
            raise ImportError_(f"{username}: {exc} Import stopped; earlier users/folders remain. "
                               "Inspect the last account and folder before retrying.") from None
        print(f"CREATED {username}: scope={scope}")
        created += 1
    print(f"{'Dry run complete' if dry_run else 'Import complete'}: "
          f"{created} created, {skipped} skipped.")


if __name__ == "__main__":
    try:
        main()
    except (ImportError_, OSError, csv.Error, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nImport interrupted. Inspect server state before retrying.", file=sys.stderr)
        sys.exit(130)
