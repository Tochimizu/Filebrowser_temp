#!/usr/bin/env python3
"""Optional Docker smoke test; uses only a disposable database and data directory."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "internal"))
import filebrowser_import_users as importer
import filebrowser_setup as setup

IMAGE = "filebrowser/filebrowser:v2.63.23"
PASSWORD = "Disposable test manager password!123"


def denied(api, path):
    try:
        api.call("GET", path)
    except importer.ImportError_ as exc:
        if not any(f"HTTP {code}" in str(exc) for code in (301, 302, 307, 308, 400, 403, 404)):
            raise
    else:
        raise AssertionError(f"Unexpected access to {path}")


def main():
    with tempfile.TemporaryDirectory(prefix="filebrowser-smoke-") as temporary:
        project = Path(temporary).resolve()
        deployment = setup.Deployment(project, project / "credentials/admin.csv")
        deployment.database_dir.mkdir(parents=True)
        deployment.data_dir.mkdir()
        deployment.uid, deployment.gid = os.getuid(), os.getgid()
        deployment.image = IMAGE
        staged = deployment.stage(PASSWORD)
        name = "filebrowser-smoke-" + uuid.uuid4().hex
        try:
            deployment.command(deployment.container_args() + [
                "--detach", "--name", name, "--publish", "127.0.0.1::80",
                "--entrypoint", "/bin/filebrowser", IMAGE,
                "--database", "/database/" + staged.name, "--root", "/srv",
                "--address", "0.0.0.0", "--port", "80",
                "--disableExec=true", "--followExternalSymlinks=false",
            ], "Start smoke-test server")
            info = json.loads(deployment.command(["docker", "inspect", name], "Inspect smoke-test server"))[0]
            base = "http://127.0.0.1:" + info["NetworkSettings"]["Ports"]["80/tcp"][0]["HostPort"]
            manager = setup.wait_api(base, PASSWORD)
            students = project / "students.csv"
            students.write_text("username,password\nalice,Alice test password123!\nbob,Bob test password123!\n")
            for expected in ("2 created", "2 skipped"):
                with patch.dict(os.environ, {"FB_ADMIN_PASSWORD": PASSWORD}), contextlib.redirect_stdout(io.StringIO()) as output:
                    importer.main([str(students), "--admin", "account_manager", "--base-url", base])
                assert expected in output.getvalue(), output.getvalue()
            alice, bob = importer.API(base), importer.API(base)
            alice.login("alice", "Alice test password123!")
            bob.login("bob", "Bob test password123!")
            alice.call("POST", "/resources/private.txt", "Alice private marker")
            bob.call("POST", "/resources/private.txt", "Bob private marker")
            assert b"Alice private marker" in alice.call("GET", "/raw/private.txt")
            assert b"Bob private marker" in bob.call("GET", "/raw/private.txt")
            assert "bob" not in {item["name"] for item in alice.json("/resources/")["items"]}
            denied(alice, "/users")
            denied(alice, "/raw/%2e%2e/bob/private.txt")
            (deployment.data_dir / "users/alice/escape").symlink_to("../bob", target_is_directory=True)
            denied(alice, "/raw/escape/private.txt")
            assert b"Bob private marker" in bob.call("GET", "/raw/private.txt")
            manager.call("POST", "/users", {
                "what": "user", "which": [], "current_password": PASSWORD,
                "data": {
                    "username": "second_manager", "password": PASSWORD, "scope": "/",
                    "perm": dict(importer.PERMISSIONS, admin=True), "commands": [], "rules": [],
                },
            })
            second = importer.API(base)
            second.login("second_manager", PASSWORD)
            for admin in (manager, second):
                assert {"alice", "bob"} <= {item["name"] for item in admin.json("/resources/users")["items"]}
            print("Docker smoke test passed: CSV import/rerun, private files, traversal and symlink denial, two administrators sharing the project.")
        finally:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True)
            assert project.is_relative_to(Path(tempfile.gettempdir()).resolve())


if __name__ == "__main__":
    main()
