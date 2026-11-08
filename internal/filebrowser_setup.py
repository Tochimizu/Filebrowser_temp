#!/usr/bin/env python3
"""Bootstrap account_manager from CSV, with an explicit option to reset all accounts."""
import argparse
import csv
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from urllib import error, request

from filebrowser_import_users import API, ImportError_, scope_path

PROJECT = Path(__file__).resolve().parents[1]
USERNAME = "account_manager"
MINIMUM_PASSWORD_LENGTH = 12
DEFAULT_SERVER_CONFIG = {
    "port": 80, "baseURL": "", "address": "0.0.0.0", "log": "stdout",
    "database": "/database/filebrowser.db", "root": "/srv",
    "disableExec": True, "followExternalSymlinks": False,
}


class SetupError(Exception):
    pass


def read_admin_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        header = reader.fieldnames
        if not header or len(header) != len(set(header)) or not {"username", "password"} <= set(header):
            raise SetupError("Administrator CSV requires unique username,password headers.")
        rows = list(reader)
    if len(rows) != 1:
        raise SetupError("Administrator CSV must contain exactly one account_manager row.")
    row = rows[0]
    if None in row or any(value is None for value in row.values()):
        raise SetupError("Administrator CSV has the wrong number of columns.")
    if row["username"] != USERNAME:
        raise SetupError("Administrator CSV username must be account_manager.")
    password = row["password"]
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        raise SetupError("Administrator password must have at least 12 characters.")
    if any(char in password for char in "\r\n\0"):
        raise SetupError("Administrator password must not contain newlines or NUL characters.")
    return password


def run(command, *, cwd, label, input_text=None, timeout=120):
    try:
        result = subprocess.run(command, cwd=cwd, input=input_text, text=True,
                                capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError(f"{label} could not complete. Check Docker and its logs.") from None
    if result.returncode:
        # Never echo output from credential-bearing commands.
        raise SetupError(f"{label} failed (exit {result.returncode}). Check Docker and its logs.")
    return result.stdout


def wait_api(base_url, password, username=USERNAME):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with request.urlopen(base_url + "/health", timeout=2) as response:
                if response.status == 200:
                    api = API(base_url)
                    api.login(username, password)
                    return api
        except (error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    raise SetupError("File Browser did not become ready within 45 seconds.")


def verify_manager(api, *, only_manager, username=USERNAME):
    users = api.json("/users")
    if not isinstance(users, list):
        raise SetupError("Expected a File Browser user list.")
    managers = [user for user in users if user.get("username") == username]
    if len(managers) != 1:
        raise SetupError(f"{username} is missing or duplicated.")
    manager = managers[0]
    if manager.get("perm", {}).get("admin") is not True or scope_path(manager.get("scope")) != "/":
        raise SetupError(f"{username} must be an administrator with shared scope '/'.")
    if manager.get("perm", {}).get("execute") is not False or manager.get("perm", {}).get("share") is not False:
        raise SetupError(f"{username} command execution and public sharing must be disabled.")
    if only_manager and len(users) != 1:
        raise SetupError(f"Reset verification failed: accounts other than {username} remain.")
    settings = api.json("/settings")
    defaults = settings.get("defaults", {})
    if (settings.get("signup") is not False or settings.get("createUserDir") is not True
            or settings.get("userHomeBasePath") != "/users"
            or settings.get("authMethod") != "json"
            or defaults.get("scope") != ""
            or any(defaults.get("perm", {}).get(key) is not False for key in ("admin", "execute", "share"))):
        raise SetupError("Server settings do not match the automatic setup's isolation defaults.")
    return users


class Deployment:
    def __init__(self, project, csv_file):
        self.project = Path(project).resolve()
        self.csv_file = Path(csv_file).resolve()
        self.database_dir = self.project / "admin/database"
        self.database = self.database_dir / "filebrowser.db"
        self.data_dir = self.project / "admin/data"
        self.config_dir = self.project / "admin/config"
        self.backups = self.project / ".backups"
        self.compose = ["docker", "compose", "--project-directory", str(self.project),
                        "-f", str(self.project / "docker-compose.yml")]

    def command(self, args, label, **kwargs):
        return run(args, cwd=self.project, label=label, **kwargs)

    def validate(self):
        for path in (self.project / "admin", self.database_dir, self.data_dir, self.config_dir, self.backups):
            if path.is_symlink() or not path.resolve().is_relative_to(self.project):
                raise SetupError("Deployment directories must be real directories inside this project.")
        if self.database.is_symlink():
            raise SetupError("Refusing to replace a symbolic-link database.")
        if self.csv_file.is_relative_to(self.data_dir.resolve()):
            raise SetupError("Administrator credentials must be outside the served data directory.")
        raw = self.command(self.compose + ["config", "--format", "json"], "Read Compose configuration")
        service = json.loads(raw)["services"]["filebrowser"]
        self.image = service["image"]
        mounts = {item["target"]: item for item in service.get("volumes", [])}
        for destination, expected in (("/srv", self.data_dir), ("/database", self.database_dir), ("/config", self.config_dir)):
            mount = mounts.get(destination, {})
            if mount.get("type") != "bind" or Path(mount.get("source", "")).resolve() != expected.resolve():
                raise SetupError("Compose must bind this project's admin/data, admin/database and admin/config.")
        config_path = self.config_dir / "settings.json"
        if config_path.is_symlink():
            raise SetupError("settings.json must not be a symbolic link.")
        settings = json.loads(config_path.read_text()) if config_path.exists() else DEFAULT_SERVER_CONFIG.copy()
        if settings.get("database") != "/database/filebrowser.db" or settings.get("root") != "/srv":
            raise SetupError("settings.json must use database /database/filebrowser.db and root /srv.")
        if settings.get("port") != 80 or settings.get("baseURL", "") != "":
            raise SetupError("This setup requires container port 80 and an empty baseURL.")
        environment = service.get("environment", {})
        self.uid = int(environment.get("PUID", 1000))
        self.gid = int(environment.get("PGID", 1000))
        ports = [p for p in service.get("ports", []) if p.get("target") == 80 and p.get("published")]
        if len(ports) != 1:
            raise SetupError("Compose must publish exactly one HTTP port for target 80.")
        self.base_url = "http://127.0.0.1:" + str(ports[0]["published"])

    def container_args(self):
        return ["docker", "run", "--user", f"{self.uid}:{self.gid}",
                "--mount", f"type=bind,src={self.database_dir},dst=/database",
                "--mount", f"type=bind,src={self.data_dir},dst=/srv"]

    def stage(self, password, username=USERNAME):
        stage_id = uuid.uuid4().hex
        stage_path = self.database_dir / f".setup-{stage_id}.db"
        container_database = "/database/" + stage_path.name
        cli = self.container_args() + ["--rm", "--network", "none", "--entrypoint", "/bin/filebrowser", self.image,
                                      "--database", container_database]
        container_name = "filebrowser-setup-" + stage_id
        try:
            self.command(cli + [
                "config", "init", "--root", "/srv", "--address", "0.0.0.0", "--port", "80",
                "--auth.method", "json", "--signup=false", "--createUserDir=true", "--scope", "",
                "--disableExec=true", "--followExternalSymlinks=false", "--minimumPasswordLength", "12",
                "--perm.admin=false", "--perm.execute=false", "--perm.share=false",
            ], "Initialize staged database")
            # Pass the password through stdin, never a Docker command argument or environment.
            shell = ('IFS= read -r manager_password; exec /bin/filebrowser --database "$1" '
                     'users add "$2" "$manager_password" --scope / --perm.admin=true '
                     '--perm.execute=false --perm.share=false')
            command = self.container_args() + ["--rm", "--interactive", "--network", "none",
                      "--entrypoint", "/bin/sh", self.image, "-eu", "-c", shell, "setup", container_database, username]
            self.command(command, f"Create {username}", input_text=password + "\n")
            self.command(self.container_args() + [
                "--detach", "--name", container_name, "--publish", "127.0.0.1::80",
                "--entrypoint", "/bin/filebrowser", self.image,
                "--database", container_database, "--root", "/srv", "--address", "0.0.0.0", "--port", "80",
                "--disableExec=true", "--followExternalSymlinks=false",
            ], "Start staged verification server")
            details = json.loads(self.command(["docker", "inspect", container_name], "Inspect staged server"))[0]
            port = details["NetworkSettings"]["Ports"]["80/tcp"][0]["HostPort"]
            api = wait_api("http://127.0.0.1:" + port, password, username=username)
            settings = api.json("/settings")
            settings.update(signup=False, createUserDir=True, userHomeBasePath="/users")
            settings["defaults"]["scope"] = ""
            for key in ("admin", "execute", "share"):
                settings["defaults"]["perm"][key] = False
            api.call("PUT", "/settings", settings)
            verify_manager(api, only_manager=True, username=username)
        except BaseException:
            self.command(["docker", "rm", "--force", container_name], "Remove staged server") if self.container_exists(container_name) else None
            if stage_path.exists():
                stage_path.unlink()
            raise
        else:
            self.command(["docker", "rm", "--force", container_name], "Stop staged verification server")
            stage_path.chmod(0o600)
            return stage_path

    def container_exists(self, name):
        result = subprocess.run(["docker", "inspect", name], cwd=self.project,
                                capture_output=True, text=True)
        return result.returncode == 0

    def install(self, password, reset, username=USERNAME):
        if self.database.exists() and not reset:
            self.command(self.compose + ["up", "-d", "--no-deps", "filebrowser"], "Start File Browser")
            api = wait_api(self.base_url, password, username=username)
            users = verify_manager(api, only_manager=False, username=username)
            print(f"Setup already configured: {username} verified; {len(users)} account(s) retained.")
            return
        image_check = subprocess.run(["docker", "image", "inspect", self.image],
                                     cwd=self.project, capture_output=True, text=True)
        if image_check.returncode:
            self.command(["docker", "pull", self.image], "Pull configured Docker image", timeout=300)
        print("Preparing and verifying a fresh database before replacing the live database.")
        stage_path = self.stage(password, username=username)
        backup = None
        replaced = False
        stop_attempted = False
        try:
            stop_attempted = True
            self.command(self.compose + ["stop", "filebrowser"], "Stop File Browser")
            if self.database.exists():
                self.backups.mkdir(mode=0o700, exist_ok=True)
                self.backups.chmod(0o700)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
                backup_dir = self.backups / stamp
                backup_dir.mkdir(mode=0o700)
                backup = backup_dir / "filebrowser.db"
                shutil.copy2(self.database, backup)
                backup.chmod(0o600)
                for source in (self.config_dir / "settings.json", self.project / "docker-compose.yml"):
                    shutil.copy2(source, backup_dir / source.name)
                print(f"Previous database saved outside the served folders: {backup.relative_to(self.project)}")
            os.replace(stage_path, self.database)
            replaced = True
            self.command(self.compose + ["up", "-d", "--no-deps", "filebrowser"], "Start clean File Browser")
            api = wait_api(self.base_url, password, username=username)
            verify_manager(api, only_manager=True, username=username)
            print(f"Setup complete: {username} is the only account; all previous users, shares and sessions were reset.")
            print(f"Login: {self.base_url} (password from {self.csv_file.name}).")
        except BaseException:
            if replaced:
                self.command(self.compose + ["stop", "filebrowser"], "Stop failed deployment")
                if backup:
                    shutil.copy2(backup, self.database)
                    self.command(self.compose + ["up", "-d", "--no-deps", "filebrowser"], "Restart previous database")
                    print("Setup failed; restored the previous database.", file=sys.stderr)
                else:
                    self.database.unlink()
                    print("Setup failed; no previous database existed. Service remains stopped.", file=sys.stderr)
            elif stop_attempted:
                self.command(self.compose + ["up", "-d", "--no-deps", "filebrowser"], "Restart unchanged deployment")
            raise
        finally:
            if stage_path.exists():
                stage_path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("admin_csv", nargs="?", type=Path, default=PROJECT / "credentials/admin.csv")
    parser.add_argument("--reset-users", action="store_true",
                        help="Replace the database, removing ALL users, shares, settings and active sessions")
    parser.add_argument("--dry-run", action="store_true", help="Validate setup inputs without changing containers or files")
    args = parser.parse_args(argv)
    deployment = Deployment(PROJECT, args.admin_csv)
    password = read_admin_csv(deployment.csv_file)
    deployment.validate()
    if args.dry_run:
        action = "RESET all accounts and create" if args.reset_users else "Initialize or verify"
        print(f"DRY RUN: {action} account_manager, admin=true, scope=/, image={deployment.image}.")
        print("Uploaded files are preserved. No containers or files changed.")
        return
    for path in (deployment.database_dir, deployment.data_dir, deployment.config_dir):
        path.mkdir(parents=True, exist_ok=True)
    deployment.csv_file.chmod(0o600)
    with (deployment.database_dir / ".setup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("Another setup process is running.") from None
        config_path = deployment.config_dir / "settings.json"
        if not config_path.exists():
            config_path.write_text(json.dumps(DEFAULT_SERVER_CONFIG, indent=2) + "\n")
        deployment.install(password, args.reset_users)


if __name__ == "__main__":
    try:
        main()
    except (SetupError, ImportError_, OSError, ValueError, KeyError, csv.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup interrupted.", file=sys.stderr)
        sys.exit(130)
