#!/usr/bin/env python3
"""Exercise run_script.sh in a disposable Compose project, never the live project."""
import csv
import json
import os
from pathlib import Path
import pty
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "internal"))
from filebrowser_import_users import API, ImportError_

PASSWORD = "Disposable manager password123!"
ADMINS = [("account_manager", PASSWORD), ("co_admin", PASSWORD)]
STUDENTS = [("alice", "Alice disposable password123!"), ("bob", "Bob disposable password123!")]


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["username", "password"])
        writer.writerows(rows)


def invoke(project, flags, *, answers=None, code=0):
    command = ["bash", str(project / "run_script.sh"), *flags]
    if answers is None:
        result = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=120)
        output, returncode = result.stdout + result.stderr, result.returncode
    else:
        master, slave = pty.openpty()
        process = subprocess.Popen(command, cwd=project, stdin=slave, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        os.close(slave)
        try:
            os.write(master, ("\n".join(answers) + "\n").encode())
            output, _ = process.communicate(timeout=120)
            returncode = process.returncode
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
    assert returncode == code, f"{flags}: exit {returncode}\n{output}"
    return output


def login(base, name="account_manager"):
    api = API(base)
    api.login(name, PASSWORD)
    return api


def names(api):
    return {user["username"] for user in api.json("/users")}


def main():
    with tempfile.TemporaryDirectory(prefix="filebrowser-management-") as directory:
        project = Path(directory).resolve()
        assert project.is_relative_to(Path(tempfile.gettempdir()).resolve())
        shutil.copytree(ROOT / "internal", project / "internal", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "run_script.sh", project / "run_script.sh")
        (project / "credentials").mkdir()
        write_csv(project / "credentials/admin.csv", ADMINS)
        write_csv(project / "credentials/user.csv", STUDENTS)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        compose = {
            "name": "filebrowser-management-" + uuid.uuid4().hex[:12],
            "services": {"filebrowser": {
                "image": "filebrowser/filebrowser:v2.63.23",
                "environment": {"PUID": str(os.getuid()), "PGID": str(os.getgid())},
                "volumes": ["./admin/data:/srv", "./admin/database:/database", "./admin/config:/config"],
                "ports": [f"127.0.0.1:{port}:80"],
            }},
        }
        # JSON is valid YAML, allowing a dependency-free disposable Compose file.
        (project / "docker-compose.yml").write_text(json.dumps(compose))
        try:
            invoke(project, ["--dry-run"])
            assert not (project / "admin").exists(), "Dry run created runtime directories."
            invoke(project, ["--setup", "--create-users"])
            manager = login(base)
            assert names(manager) == {"account_manager", "co_admin", "alice", "bob"}
            assert "2 skipped" in invoke(project, ["--setup", "--create-users"])
            invoke(project, ["--dry-run"])
            print("PASS: public setup, multiple administrators, CSV import, rerun, dry-run.")

            # All CSV conflict checks must precede creation of even a new admin.
            write_csv(project / "credentials/admin.csv", ADMINS + [("new_admin", PASSWORD)])
            write_csv(project / "credentials/user.csv", [("short_user", "short")])
            invoke(project, ["--setup", "--create-users"], code=1)
            assert "new_admin" not in names(manager)
            write_csv(project / "credentials/admin.csv", ADMINS)
            write_csv(project / "credentials/user.csv", STUDENTS)

            data = project / "admin/data"
            for user in ("alice", "bob"):
                (data / "users" / user / "private.txt").write_text(user + " private")
            invoke(project, ["--delete-account"], answers=["alice", "n", "DELETE alice", "CONFIRM ACCOUNT alice"])
            manager = login(base)
            assert "alice" not in names(manager)
            assert (data / "users/alice/private.txt").exists()
            invoke(project, ["--delete-account"], answers=["bob", "y", "DELETE bob", "DELETE DATA /users/bob"])
            manager = login(base)
            assert "bob" not in names(manager)
            assert not (data / "users/bob").exists()
            assert (data / "users/alice/private.txt").exists()
            print("PASS: individual deletion with and without scoped data.")

            write_csv(project / "credentials/user.csv", [("carol", PASSWORD), ("dave", PASSWORD)])
            invoke(project, ["--create-users"])
            outside = project / "outside.txt"
            outside.write_text("outside must survive")
            (data / "users/carol/outside-link").symlink_to(outside)
            (data / "project.txt").write_text("shared project file")
            invoke(project, ["--clear-users-and-data"], answers=["no"])
            invoke(project, ["--clear-users-and-data"], answers=["CLEAR DEFAULT USERS", "no"])
            manager = login(base)
            assert {"carol", "dave"} <= names(manager)
            invoke(project, ["--clear-users-and-data", "--dry-run"])
            assert (data / "users/carol").exists()
            invoke(project, ["--clear-users-and-data"], answers=["CLEAR DEFAULT USERS", "DELETE DEFAULT USER DATA"])
            manager = login(base)
            assert names(manager) == {"account_manager", "co_admin"}
            assert not (data / "users/carol").exists() and not (data / "users/dave").exists()
            assert outside.read_text() == "outside must survive"
            assert (data / "project.txt").exists()
            assert (data / "users/alice/private.txt").exists()
            print("PASS: both cancellations, bulk non-admin cleanup, symlink safety, retained admins/orphan data.")

            cancelled = invoke(project, ["--delete-account"], answers=["account_manager", "y", "DELETE account_manager", "no"])
            assert "WHOLE BRANCH" in cancelled
            assert (data / "project.txt").exists()
            deleted = invoke(project, ["--delete-account"], answers=["account_manager", "y", "DELETE account_manager", "DELETE DATA /"])
            assert "OTHER ACCOUNTS" in deleted
            co_admin = login(base, "co_admin")
            assert names(co_admin) == {"co_admin"}
            assert list(data.iterdir()) == []
            invoke(project, ["--delete-account"], answers=["co_admin"], code=1)
            print("PASS: administrator whole-branch warning, root data deletion, last-admin protection.")

            (data / "clear-me.txt").write_text("hard clear")
            invoke(project, ["--hard-clear"], answers=["HARD CLEAR", "no"])
            assert (data / "clear-me.txt").exists()
            old_api = login(base, "co_admin")
            invoke(project, ["--hard-clear"], answers=["HARD CLEAR", "DELETE ALL ACCOUNTS AND FILES"])
            assert not (project / "admin/database/filebrowser.db").exists()
            assert list(data.iterdir()) == []
            assert (project / "credentials/admin.csv").exists()
            assert (project / "admin/config/settings.json").exists()
            assert list((project / ".backups").glob("*/filebrowser.db"))
            write_csv(project / "credentials/user.csv", STUDENTS)
            invoke(project, ["--setup", "--create-users"])
            assert names(login(base)) == {"account_manager", "co_admin", "alice", "bob"}
            try:
                old_api.json("/users")
            except ImportError_:
                pass
            else:
                raise AssertionError("Pre-hard-clear session survived the new database.")
            print("PASS: hard-clear cancellation, full reset, fresh setup, old-session invalidation.")

            # Bootstrap also supports an administrator CSV without account_manager.
            invoke(project, ["--hard-clear"], answers=["HARD CLEAR", "DELETE ALL ACCOUNTS AND FILES"])
            write_csv(project / "credentials/admin.csv", [("other_manager", PASSWORD)])
            invoke(project, ["--setup"])
            assert names(login(base, "other_manager")) == {"other_manager"}
            print("PASS: setup supports arbitrary administrator usernames.")
            print("Management smoke test passed. Only the disposable project was changed.")
        finally:
            subprocess.run(["docker", "compose", "--project-directory", str(project),
                            "-f", str(project / "docker-compose.yml"), "down", "--remove-orphans"],
                           capture_output=True, timeout=60)


if __name__ == "__main__":
    main()
