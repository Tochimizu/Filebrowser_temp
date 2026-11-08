#!/usr/bin/env python3
"""The implementation behind run_script.sh; use the shell entry point."""
import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
import textwrap
import time
import uuid
from urllib import error, parse, request

import filebrowser_import_users as users_api
import filebrowser_setup as setup

PROJECT = setup.PROJECT
ADMIN_PERMISSIONS = dict(users_api.PERMISSIONS, admin=True)


class Cancelled(Exception):
    pass


def canonical_scope(value):
    if not isinstance(value, str) or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise setup.SetupError("Invalid folder scope returned by the server.")
    if ".." in value.split("/"):
        raise setup.SetupError("Refusing a scope containing '..'.")
    return posixpath.normpath("/" + value.lstrip("/"))


def contains(parent, child):
    return parent == "/" or child == parent or child.startswith(parent.rstrip("/") + "/")


def prompt(message):
    if not sys.stdin.isatty():
        raise setup.SetupError("Destructive actions require an interactive terminal. Use --dry-run to preview.")
    return input(message)


def confirm_twice(first, second, warning):
    print(warning)
    if prompt(f"Type {first!r} to continue: ") != first:
        raise Cancelled()
    print("FINAL WARNING: " + warning)
    if prompt(f"Type {second!r} to confirm: ") != second:
        raise Cancelled()


def read_admins(path):
    records = users_api.read_csv(path, ",")
    for username, password in records:
        if len(password) < 12 or len(password.encode("utf-8")) > 72:
            raise setup.SetupError(f"{username}: administrator password must be at least 12 characters and at most 72 UTF-8 bytes.")
        if any(c in password for c in "\r\n\0"):
            raise setup.SetupError(f"{username}: administrator passwords cannot contain newlines or NUL.")
    return sorted(records, key=lambda row: row[0] != setup.USERNAME)


def validate_roles(admins, students):
    duplicates = {name.casefold() for name, _ in admins} & {name.casefold() for name, _ in students}
    if duplicates:
        raise setup.SetupError("The same username cannot appear in both administrator and default-user CSVs.")
    for name, password in students:
        if "\0" in password or len(password.encode("utf-8")) > 72:
            raise setup.SetupError(f"{name}: user password contains NUL or exceeds 72 UTF-8 bytes.")


class Manager:
    def __init__(self, project=PROJECT):
        self.deployment = setup.Deployment(project, Path(project) / "credentials/admin.csv")
        self.project = self.deployment.project
        self.admin_csv = self.project / "credentials/admin.csv"
        self.user_csv = self.project / "credentials/user.csv"

    def connect(self, admins):
        for name, password in admins:
            api = users_api.API(self.deployment.base_url)
            try:
                api.login(name, password)
                current = api.json("/users")
            except users_api.ImportError_:
                continue
            account = next((user for user in current if user.get("username") == name), None)
            if account and account.get("perm", {}).get("admin") is True and canonical_scope(account.get("scope")) == "/":
                return api, name, password
        raise setup.SetupError("No root-scoped File Browser administrator from credentials/admin.csv could log in. "
                               "Check the running service and CSV passwords, or use --setup for a new installation.")

    def runtime_directories(self):
        for path in (self.deployment.database_dir, self.deployment.data_dir, self.deployment.config_dir):
            path.mkdir(parents=True, exist_ok=True)
        path = self.deployment.config_dir / "settings.json"
        if not path.exists():
            path.write_text(json.dumps(setup.DEFAULT_SERVER_CONFIG, indent=2) + "\n")

    def plan_admins(self, api, records):
        existing = api.json("/users")
        minimum = api.json("/settings").get("minimumPasswordLength", 12)
        result = []
        for name, password in records:
            matches = [user for user in existing if user.get("username", "").casefold() == name.casefold()]
            if len(password) < minimum:
                raise setup.SetupError(f"{name}: administrator password is shorter than the server minimum ({minimum}).")
            if matches:
                user = matches[0]
                if (len(matches) != 1 or user.get("username") != name or user.get("perm") != ADMIN_PERMISSIONS
                        or canonical_scope(user.get("scope")) != "/" or user.get("commands")):
                    raise setup.SetupError(f"{name}: existing account conflicts with the requested administrator role/scope.")
                check = users_api.API(self.deployment.base_url)
                check.login(name, password)
            result.append((name, password, bool(matches)))
        return result

    def create_admins(self, api, plan, acting_password, dry_run):
        for name, password, exists in plan:
            if exists:
                print(f"SKIP ADMIN {name}: already configured; password verified.")
            elif dry_run:
                print(f"WOULD CREATE ADMIN {name}: admin=true, scope=/ (all served folders).")
            else:
                data = {"username": name, "password": password, "scope": "/",
                        "locale": "en", "viewMode": "list", "lockPassword": False,
                        "perm": ADMIN_PERMISSIONS.copy(), "commands": [], "rules": [],
                        "sorting": {"by": "name", "asc": True}}
                api.call("POST", "/users", {"what": "user", "which": [], "data": data,
                                           "current_password": acting_password})
                check = users_api.API(self.deployment.base_url)
                check.login(name, password)
                account = next(user for user in api.json("/users") if user["username"] == name)
                if account.get("perm") != ADMIN_PERMISSIONS or canonical_scope(account.get("scope")) != "/":
                    raise setup.SetupError(f"{name}: saved administrator settings do not match.")
                print(f"CREATED ADMIN {name}: scope=/.")

    def setup_and_users(self, do_setup, do_users, dry_run):
        admins = read_admins(self.admin_csv)
        students = users_api.read_csv(self.user_csv, ",") if do_users else []
        validate_roles(admins, students)
        if not dry_run:
            self.admin_csv.chmod(0o600)
            if do_users:
                self.user_csv.chmod(0o600)
        if not self.deployment.database.exists():
            if not do_setup:
                raise setup.SetupError("No database exists. Run --setup first or combine --setup --create-users.")
            for name, password in students:
                if len(password) < 12:
                    raise setup.SetupError(f"{name}: new-user password is shorter than 12 characters.")
                target = self.scope_path("/users/" + name)
                if target.exists():
                    raise setup.SetupError(f"{name}: an unassigned folder already exists at {target}.")
            if dry_run:
                self.deployment.command(["docker", "info", "--format", "{{.ServerVersion}}"], "Check Docker availability")
                for name, _ in admins:
                    print(f"WOULD CREATE ADMIN {name}: admin=true, scope=/.")
                for name, _ in students:
                    print(f"WOULD CREATE USER {name}: scope=/users/{name}.")
                print("CSV and local folder checks passed. A fresh server would be initialized; live API checks require --setup.")
                return
            self.runtime_directories()
            primary, password = admins[0]
            self.deployment.install(password, reset=False, username=primary)
        elif do_setup and not dry_run:
            self.deployment.command(self.deployment.compose + ["up", "-d", "--no-deps", "filebrowser"], "Start File Browser")
        api, acting_admin, acting_password = self.connect(admins)
        admin_plan = self.plan_admins(api, admins) if do_setup else []
        user_plan = users_api.preflight(api, students, acting_admin) if do_users else []
        # All requested CSVs and server conflicts are checked before any account write.
        if do_setup:
            self.create_admins(api, admin_plan, acting_password, dry_run)
        if do_users:
            users_api.execute_plan(api, user_plan, acting_password, dry_run)
        print("Validation complete; no accounts or files changed." if dry_run else "Requested account setup/import complete.")

    def scope_path(self, scope):
        root = self.deployment.data_dir
        normalized = canonical_scope(scope)
        target = root / normalized.lstrip("/")
        for part in [root, *list(target.parents)]:
            if part == root.parent:
                break
            if part.is_symlink():
                raise setup.SetupError("Refusing a scope reached through a symbolic link.")
        if target.is_symlink():
            raise setup.SetupError("Refusing a symbolic-link scope.")
        root_real, target_real = root.resolve(), target.resolve()
        if not target_real.is_relative_to(root_real):
            raise setup.SetupError("Refusing a scope outside this project's served data directory.")
        return target

    def snapshot(self, users):
        return sorted((user["id"], user["username"], user.get("scope"), user.get("perm", {}).get("admin"))
                      for user in users)

    def backup(self, reason):
        folder = self.deployment.backups
        folder.mkdir(mode=0o700, exist_ok=True)
        folder.chmod(0o700)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + reason + "-" + uuid.uuid4().hex[:8]
        backup = folder / name
        backup.mkdir(mode=0o700)
        database = self.deployment.database
        if database.exists():
            pending = backup / "filebrowser.db.partial"
            shutil.copy2(database, pending)
            pending.chmod(0o600)
            os.replace(pending, backup / "filebrowser.db")
        print(f"Private database backup: {backup.relative_to(self.project)}")
        return backup

    def cli(self, arguments):
        command = self.deployment.container_args() + ["--rm", "--network", "none",
                  "--entrypoint", "/bin/filebrowser", self.deployment.image,
                  "--database", "/database/filebrowser.db"] + arguments
        return self.deployment.command(command, "Update offline accounts")

    def wait_health(self):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                with request.urlopen(self.deployment.base_url + "/health", timeout=2) as response:
                    if response.status == 200:
                        return
            except (error.URLError, OSError, TimeoutError):
                pass
            time.sleep(0.5)
        raise setup.SetupError("The service did not become healthy after account changes.")

    def mutate_deletions(self, selected, scopes, reason, *, hard=False):
        # The service is stopped before accounts or data change. Files first move
        # to private quarantine, allowing rollback before irreversible cleanup.
        scopes = sorted(set(scopes), key=lambda value: (len(Path(canonical_scope(value)).parts), value))
        roots = []
        for scope in scopes:
            path = self.scope_path(scope)
            if not any(path.is_relative_to(parent) for parent in roots):
                roots.append(path)
        if not shutil.rmtree.avoids_symlink_attacks:
            raise setup.SetupError("This platform lacks safe directory deletion support; run under WSL/Linux.")
        self.deployment.command(self.deployment.compose + ["stop", "filebrowser"], "Stop File Browser for deletion")
        backup = None
        moved = []
        try:
            backup = self.backup(reason)
            quarantine = backup / "pending-data-removal"
            quarantine.mkdir(mode=0o700)
            for root in roots:
                self.scope_path("/" + str(root.relative_to(self.deployment.data_dir)))
                sources = list(root.iterdir()) if root == self.deployment.data_dir and root.exists() else [root]
                for source in sources:
                    if not source.exists() and not source.is_symlink():
                        continue
                    destination = quarantine / str(len(moved))
                    if not destination.resolve().is_relative_to(backup.resolve()):
                        raise setup.SetupError("Invalid private quarantine destination.")
                    os.replace(source, destination)
                    moved.append((source, destination))
            if hard:
                if self.deployment.database.exists():
                    self.deployment.database.unlink()
            else:
                for user in selected:
                    self.cli(["users", "rm", str(user["id"])])
                remaining = self.cli(["users", "ls"])
                remaining_ids = {int(line.split()[0]) for line in remaining.splitlines()
                                 if line.split() and line.split()[0].isdigit()}
                if remaining_ids & {user["id"] for user in selected}:
                    raise setup.SetupError("An account selected for deletion is still present.")
                self.deployment.command(self.deployment.compose + ["up", "-d", "--no-deps", "filebrowser"], "Restart File Browser")
                self.wait_health()
        except BaseException:
            # Stop any replacement process before restoring database and directories.
            self.deployment.command(self.deployment.compose + ["stop", "filebrowser"], "Stop failed account change")
            for source, destination in reversed(moved):
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)
            if backup and (backup / "filebrowser.db").exists():
                restore = self.deployment.database_dir / (".restore-" + uuid.uuid4().hex + ".db")
                shutil.copy2(backup / "filebrowser.db", restore)
                os.replace(restore, self.deployment.database)
            if self.deployment.database.exists():
                self.deployment.command(self.deployment.compose + ["up", "-d", "--no-deps", "filebrowser"], "Restore previous deployment")
            raise
        try:
            shutil.rmtree(quarantine)
        except OSError:
            raise setup.SetupError(f"Account changes succeeded, but some quarantined files could not be erased: {quarantine}.") from None
        if hard:
            print("Hard clear complete: all accounts, sessions, shares and served files removed. Service stopped. Run --setup to restart.")
        else:
            print(f"Deleted {len(selected)} account(s). " + ("Selected folder branches and files removed." if scopes else "All files retained."))

    def delete_accounts(self, bulk=False, dry_run=False):
        api, _, _ = self.connect(read_admins(self.admin_csv))
        current = api.json("/users")
        for user in current:
            canonical_scope(user.get("scope"))
        if bulk:
            selected = [user for user in current if user.get("perm", {}).get("admin") is not True]
            if not selected:
                print("No default (non-admin) users exist. No files changed.")
                return
            scopes = [canonical_scope(user["scope"]) for user in selected]
            kept = [user for user in current if user not in selected]
            if any(contains(scope, canonical_scope(user["scope"])) for scope in scopes for user in kept):
                raise setup.SetupError("A default user's folder contains an administrator's scope. Bulk deletion refused; inspect --delete-account instead.")
            for user in selected:
                print(f"REMOVE DEFAULT USER {user['username']!r}: {self.scope_path(user['scope'])}")
            warning = "DELETE ALL DEFAULT USERS AND THEIR FOLDER BRANCHES, including every descendant file. Administrators remain."
            tokens = ("CLEAR DEFAULT USERS", "DELETE DEFAULT USER DATA")
        else:
            print("Existing accounts:")
            for user in current:
                print(f"  {user['username']!r}: {'ADMIN' if user.get('perm', {}).get('admin') else 'USER'}, scope={user['scope']!r}")
            name = prompt("Exact account username to delete: ")
            selected = [user for user in current if user["username"] == name]
            if not selected:
                raise setup.SetupError("No account with that exact username exists.")
            user = selected[0]
            scope = canonical_scope(user["scope"])
            warning = f"Delete account {name!r}."
            if user.get("perm", {}).get("admin") or scope == "/" or name.replace("-", "_").casefold() == "account_manager":
                print("WARNING: administrator account. Deleting its scoped data may erase the WHOLE BRANCH, including other users' files.")
            if user.get("perm", {}).get("admin") and sum(u.get("perm", {}).get("admin") is True for u in current) <= 1:
                raise setup.SetupError("File Browser protects its last administrator. Add another administrator with --setup, or use --hard-clear.")
            answer = prompt("Also remove this account's scoped folder and ALL descendant files? [y/N]: ").strip().lower()
            if answer not in ("", "n", "no", "y", "yes"):
                raise setup.SetupError("Answer yes or no. Nothing was changed.")
            scopes = [scope] if answer in ("y", "yes") else []
            if scopes:
                print(f"EXACT FOLDER BRANCH: {self.scope_path(scope)}")
                affected = [u["username"] for u in current if u not in selected and contains(scope, canonical_scope(u["scope"]))]
                warning += f" DELETE THE WHOLE BRANCH {scope!r} and all descendants. Deleted files cannot be recovered from the database backup."
                if affected:
                    print("OTHER ACCOUNTS WHOSE FOLDERS WILL BE ERASED: " + ", ".join(repr(name) for name in affected))
                    warning += " Other accounts remain but their files in this branch will be lost."
            else:
                print("All scoped folders and files will be retained.")
            tokens = (f"DELETE {name}", f"DELETE DATA {scope}" if scopes else f"CONFIRM ACCOUNT {name}")
        for scope in scopes:
            self.scope_path(scope)  # Validate every affected path before confirmation.
        if dry_run:
            print("DRY RUN: " + warning + " No changes made.")
            return
        confirm_twice(*tokens, warning)
        if self.snapshot(api.json("/users")) != self.snapshot(current):
            raise setup.SetupError("Accounts changed during confirmation. Rerun and review the new deletion plan.")
        self.mutate_deletions(selected, scopes, "clear-users" if bulk else "delete-account")

    def hard_clear(self, dry_run=False):
        target = self.scope_path("/")
        warning = (f"HARD CLEAR: remove EVERY account, including account_manager and all administrators, "
                   f"plus the WHOLE BRANCH {target} and every file below it. "
                   "Shares/sessions/settings are reset and the service stays stopped. "
                   "Credentials, configuration and private database backups are retained; deleted data has no backup.")
        print(warning)
        if dry_run:
            print("DRY RUN: no accounts, files or containers changed.")
            return
        confirm_twice("HARD CLEAR", "DELETE ALL ACCOUNTS AND FILES", warning)
        self.mutate_deletions([], ["/"], "hard-clear", hard=True)



def print_details():
    """Render the maintained README as a readable, plain-text terminal guide."""
    document = (PROJECT / "README.md").read_text(encoding="utf-8")
    in_code = False
    for line in document.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            print()
            continue
        if in_code:
            print("    " + line)
            continue
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", line)
        line = line.replace("**", "").replace("`", "")
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            print(title.upper())
            print("=" * len(title))
        elif line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if all(re.fullmatch(r"[-: ]+", cell) for cell in cells):
                continue
            if cells[0] != "Flag":
                print("  " + cells[0])
                width = max(50, min(100, shutil.get_terminal_size(fallback=(80, 24)).columns))
                print(textwrap.fill(" | ".join(cells[1:]), width=width,
                                    initial_indent="    ", subsequent_indent="    "))
                print()
        else:
            print(line)
    print("\nDocumentation only: no credentials read, containers accessed, or accounts/files changed.")

def parser():
    result = argparse.ArgumentParser(
        prog="bash ./run_script.sh",
        description="Manage this File Browser project only through: bash ./run_script.sh",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  bash ./run_script.sh --details
  bash ./run_script.sh --setup
  bash ./run_script.sh --dry-run
  bash ./run_script.sh --setup --create-users --dry-run
  bash ./run_script.sh --create-users
  bash ./run_script.sh --delete-account
  bash ./run_script.sh --clear-users-and-data
  bash ./run_script.sh --hard-clear

CSV headers: username,password. Credentials are read automatically; passwords
are never displayed. Destructive commands require an interactive terminal and
TWO typed confirmations. Administrator/root-scope data deletion can erase the
WHOLE BRANCH, including other users' files. Database backups do not back up data.
No flags prints this help and performs no action.""")
    result.add_argument("--details", action="store_true", help="Print the full terminal guide: CSV format, workflows, folder scopes, all flags, confirmations and recovery. Documentation only.")
    result.add_argument("--setup", action="store_true", help="Read credentials/admin.csv; initialize the service and create/verify ALL listed administrators with shared scope /.")
    result.add_argument("--dry-run", action="store_true", help="Preview selected actions without changes. Alone: validate BOTH CSVs and check whether all accounts can be created.")
    result.add_argument("--create-users", action="store_true", help="Read credentials/user.csv; create default users in /users/<username>, restricted to their own folders.")
    destructive = result.add_mutually_exclusive_group()
    destructive.add_argument("--delete-account", action="store_true", help="Ask which account to delete and whether to erase its whole folder branch; require two confirmations.")
    destructive.add_argument("--clear-users-and-data", action="store_true", help="Delete ALL non-admin accounts and their folders/files after two confirmations; retain administrators.")
    destructive.add_argument("--hard-clear", action="store_true", help="Delete ALL accounts (even account_manager), the database and ALL served data after two confirmations; service stops.")
    return result


@contextmanager
def management_lock(manager):
    directory = manager.deployment.database_dir
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".setup.lock"
    if lock_path.is_symlink():
        raise setup.SetupError("Refusing a symbolic-link management lock.")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise setup.SetupError("Another project management command is running.") from None
        yield


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.details:
        if any((args.setup, args.create_users, args.dry_run, args.delete_account, args.clear_users_and_data, args.hard_clear)):
            cli.error("--details is documentation only; run it separately from account actions.")
        print_details()
        return
    destructive = args.delete_account or args.clear_users_and_data or args.hard_clear
    if destructive and (args.setup or args.create_users):
        cli.error("Run destructive actions separately from --setup/--create-users.")
    cli.print_help()
    if not any((args.setup, args.create_users, args.dry_run, destructive)):
        return
    if args.dry_run and not any((args.setup, args.create_users, destructive)):
        args.setup = args.create_users = True
    manager = Manager()
    manager.deployment.validate()

    def dispatch():
        if args.hard_clear:
            manager.hard_clear(args.dry_run)
        elif args.delete_account or args.clear_users_and_data:
            manager.delete_accounts(args.clear_users_and_data, args.dry_run)
        else:
            manager.setup_and_users(args.setup, args.create_users, args.dry_run)

    if args.dry_run:
        dispatch()
    else:
        with management_lock(manager):
            dispatch()


if __name__ == "__main__":
    try:
        main()
    except Cancelled:
        print("Cancelled. No accounts or data were changed.")
    except (setup.SetupError, users_api.ImportError_, OSError, ValueError, KeyError, csv.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled/interrupted. If changes had started, inspect service state before retrying.", file=sys.stderr)
        sys.exit(130)
