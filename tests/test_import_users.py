import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("importer", ROOT / "internal/filebrowser_import_users.py")
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.csv = Path(self.temp.name) / "users.csv"
        self.csv.write_text('username,password\nalice,"a long,quoted password"\nbob,another long password\n')
        self.users = [{"username": "admin", "scope": ".", "perm": {"admin": True}}]
        self.folders = set()
        self.writes = []
        self.requests = []
        self.corrupt_scope = False
        self.fail_create = False
        self.symlink_parent = False
        self.missing_home = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, data=None):
                self.send_response(status)
                self.end_headers()
                if data is not None:
                    self.wfile.write(json.dumps(data).encode())

            def do_GET(self):
                owner.requests.append(self.path)
                if self.headers.get("X-Auth") != "header.payload.signature":
                    self.respond(403)
                elif self.path == "/api/users":
                    self.respond(200, owner.users)
                elif self.path == "/api/settings":
                    self.respond(200, {"minimumPasswordLength": 12})
                elif self.path.startswith("/api/resources"):
                    relative = self.path.removeprefix("/api/resources")
                    admin_scope = owner.users[0]["scope"]
                    folder = __import__("posixpath").normpath("/" + admin_scope.lstrip("/") + "/" + relative.lstrip("/"))
                    is_link = owner.symlink_parent and relative == "/users"
                    self.respond(200, {"isDir": True, "isSymlink": is_link}) if folder in owner.folders else self.respond(404)
                else:
                    self.respond(404)

            def do_POST(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/api/login":
                    credentials = json.loads(data)
                    if credentials != {"username": "admin", "password": "admin secret", "recaptcha": ""}:
                        self.respond(403)
                        return
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"header.payload.signature")
                    return
                if self.headers.get("X-Auth") != "header.payload.signature":
                    self.respond(403)
                    return
                owner.writes.append((self.path, data))
                if self.path == "/api/users":
                    payload = json.loads(data)
                    if owner.fail_create:
                        self.respond(400, {"error": "sensitive response"})
                        return
                    if payload.get("current_password") != "admin secret" or payload.get("what") != "user" or payload.get("which") != []:
                        self.respond(400)
                        return
                    user = copy.deepcopy(payload["data"])
                    if not owner.missing_home:
                        folder = user["scope"]
                        owner.folders.update({folder, folder.rsplit("/", 1)[0]})
                    if owner.corrupt_scope:
                        user["scope"] = "/"
                    owner.users.append(user)
                    self.respond(201)
                else:
                    self.respond(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def run_import(self, *args):
        with patch.dict(os.environ, {"FB_ADMIN_PASSWORD": "admin secret"}), contextlib.redirect_stdout(io.StringIO()) as output:
            importer.main([str(self.csv), "--base-url", self.url, *args])
        return output.getvalue()

    def test_create_private_accounts_and_safe_rerun(self):
        output = self.run_import()
        self.assertIn("2 created", output)
        alice, bob = self.users[1:]
        self.assertEqual(alice["password"], "a long,quoted password")
        self.assertEqual(alice["scope"], "/users/alice")
        self.assertEqual(bob["scope"], "/users/bob")
        for user in (alice, bob):
            for key in ("admin", "execute", "share"):
                self.assertIs(user["perm"][key], False)
            self.assertEqual(user["commands"], [])
        previous_writes = len(self.writes)
        self.csv.write_text("username,password\nalice,replacement password\nbob,replacement password\n")
        self.assertIn("2 skipped", self.run_import())
        self.assertEqual(len(self.writes), previous_writes)
        self.assertEqual(alice["password"], "a long,quoted password")

    def test_dry_run_has_no_writes(self):
        self.assertIn("WOULD CREATE alice", self.run_import("--dry-run"))
        self.assertEqual(self.writes, [])

    def test_csv_validation_before_server_access(self):
        for content in (
            "username,password\n../bob,secret\n",
            "username,password\nalice,secret\nALICE,other\n",
            "username,password\nalice,\n",
            "username,password\nalice,one,extra\n",
            "name,password\nalice,secret\n",
            'username,password\nalice,"unterminated\n',
        ):
            with self.subTest(content=content):
                self.csv.write_text(content)
                with self.assertRaises((importer.ImportError_, __import__("csv").Error)):
                    self.run_import()
                self.assertEqual(self.requests, [])
                self.assertEqual(self.writes, [])

    def test_semicolon_bom_crlf_and_exact_password(self):
        self.csv.write_bytes(b'\xef\xbb\xbfusername;password\r\nalice;"  long;password ""quoted""  "\r\n')
        self.run_import("--delimiter", ";")
        self.assertEqual(self.users[1]["password"], '  long;password "quoted"  ')

    def test_overlapping_existing_non_admin_is_rejected(self):
        self.users.append({"username": "legacy", "scope": "/", "perm": {"admin": False}})
        with self.assertRaisesRegex(importer.ImportError_, "overlapping"):
            self.run_import()
        self.assertEqual(self.writes, [])

    def test_unsafe_existing_csv_account_is_rejected(self):
        self.users.append({"username": "alice", "scope": "/", "perm": importer.PERMISSIONS.copy()})
        with self.assertRaisesRegex(importer.ImportError_, "unsafe"):
            self.run_import()
        self.assertEqual(self.writes, [])

    def test_existing_unassigned_folder_is_rejected(self):
        self.folders.add("/users/alice")
        with self.assertRaisesRegex(importer.ImportError_, "folder already exists"):
            self.run_import()
        self.assertEqual(self.writes, [])

    def test_symlink_parent_is_rejected(self):
        self.folders.add("/users")
        self.symlink_parent = True
        with self.assertRaisesRegex(importer.ImportError_, "symbolic link"):
            self.run_import()
        self.assertEqual(self.writes, [])

    def test_nonroot_administrator_creates_inside_own_scope(self):
        self.users[0]["scope"] = "/Teachers/Adel"
        self.assertIn("scope=/Teachers/Adel/users/alice", self.run_import())
        self.assertEqual(self.users[1]["scope"], "/Teachers/Adel/users/alice")
        self.assertEqual(self.users[2]["scope"], "/Teachers/Adel/users/bob")
        self.assertIn("/Teachers/Adel/users/alice", self.folders)
        self.assertNotIn("/users/alice", self.folders)
        self.assertEqual([path for path, body in self.writes], ["/api/users", "/api/users"])
        self.assertIn("/api/resources/users/alice", self.requests)
        self.assertNotIn("/api/resources/Teachers/Adel/users/alice", self.requests)
        self.assertIn("2 skipped", self.run_import())

    def test_nonroot_dry_run_has_no_writes(self):
        self.users[0]["scope"] = "/Teachers/Adel"
        self.assertIn("scope=/Teachers/Adel/users/alice", self.run_import("--dry-run"))
        self.assertEqual(self.writes, [])

    def test_nonroot_existing_unassigned_folder_is_rejected(self):
        self.users[0]["scope"] = "/Teachers/Adel"
        self.folders.add("/Teachers/Adel/users/alice")
        with self.assertRaisesRegex(importer.ImportError_, "folder already exists"):
            self.run_import()
        self.assertEqual(self.writes, [])

    def test_nonroot_parent_symlink_is_rejected(self):
        self.users[0]["scope"] = "/Teachers/Adel"
        self.folders.add("/Teachers/Adel/users")
        self.symlink_parent = True
        with self.assertRaisesRegex(importer.ImportError_, "symbolic link"):
            self.run_import()
        self.assertEqual(self.writes, [])

    def test_nonroot_overlapping_non_admin_is_rejected(self):
        self.users[0]["scope"] = "/Teachers/Adel"
        self.users.append({"username": "legacy", "scope": "/Teachers", "perm": {"admin": False}})
        with self.assertRaisesRegex(importer.ImportError_, "overlapping"):
            self.run_import()
        self.assertEqual(self.writes, [])

    def test_regular_account_is_rejected(self):
        self.users[0]["perm"]["admin"] = False
        with self.assertRaisesRegex(importer.ImportError_, "administrator"):
            self.run_import()
        self.assertEqual(self.writes, [])

    def test_missing_home_stops_import(self):
        self.missing_home = True
        with self.assertRaisesRegex(importer.ImportError_, "HTTP 404"):
            self.run_import()
        self.assertEqual(len(self.users), 2)

    def test_existing_command_execution_is_rejected(self):
        self.users.append({"username": "alice", "scope": "/users/alice",
                           "perm": dict(importer.PERMISSIONS, execute=True)})
        with self.assertRaisesRegex(importer.ImportError_, "permissions differ"):
            self.run_import()
        self.assertEqual(self.writes, [])

    def test_short_password_rejected_before_writes(self):
        self.csv.write_text("username,password\nalice,short\n")
        with self.assertRaisesRegex(importer.ImportError_, "server minimum"):
            self.run_import()
        self.assertEqual(self.writes, [])

    def test_saved_scope_mismatch_stops_import(self):
        self.corrupt_scope = True
        with self.assertRaisesRegex(importer.ImportError_, "scope differs"):
            self.run_import()
        self.assertEqual(len(self.users), 2)  # Bob was never created.

    def test_http_failure_stops_without_echoing_secrets(self):
        self.fail_create = True
        with self.assertRaisesRegex(importer.ImportError_, "HTTP 400") as raised:
            self.run_import()
        self.assertNotIn("sensitive response", str(raised.exception))
        self.assertNotIn("admin secret", str(raised.exception))
        self.assertEqual(len(self.users), 1)
        self.assertEqual(len(self.writes), 1)

    def test_shell_entrypoint(self):
        result = subprocess.run(["bash", str(ROOT / "internal/filebrowser_import_users.sh"), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--dry-run", result.stdout)


if __name__ == "__main__":
    unittest.main()
