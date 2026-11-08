import contextlib
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "internal"))
import filebrowser_manage as manage
import filebrowser_setup as setup


class FakeAPI:
    def __init__(self, users):
        self.users = users

    def json(self, path):
        return self.users


class ManageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.manager = manage.Manager(self.project)
        self.manager.runtime_directories()
        (self.project / "credentials").mkdir()
        self.manager.admin_csv.write_text("username,password\naccount_manager,Long test password123!\n")
        self.manager.deployment.database.write_bytes(b"original database")
        self.manager.deployment.image = "test-image"
        self.manager.deployment.uid = self.manager.deployment.gid = os.getuid()
        self.manager.deployment.base_url = "http://127.0.0.1:8000"
        self.admin = {"id": 1, "username": "account_manager", "scope": "/", "perm": {"admin": True}}
        self.second = {"id": 2, "username": "second_admin", "scope": "/", "perm": {"admin": True}}
        self.alice = {"id": 3, "username": "alice", "scope": "/users/alice", "perm": {"admin": False}}
        self.bob = {"id": 4, "username": "bob", "scope": "/users/bob", "perm": {"admin": False}}
        self.api = FakeAPI([self.admin, self.second, self.alice, self.bob])
        for name in ("alice", "bob"):
            folder = self.manager.deployment.data_dir / "users" / name
            folder.mkdir(parents=True)
            (folder / "private.txt").write_text(name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_admin_csv_supports_multiple_administrators(self):
        self.manager.admin_csv.write_text("username,password\nsecond_admin,Second password123!\naccount_manager,Manager password123!\n")
        records = manage.read_admins(self.manager.admin_csv)
        self.assertEqual([row[0] for row in records], ["account_manager", "second_admin"])

    def test_duplicate_roles_rejected(self):
        with self.assertRaisesRegex(setup.SetupError, "both"):
            manage.validate_roles([("Alice", "password")], [("alice", "password")])

    def test_scope_rejects_traversal_and_symlinks(self):
        for value in ("/../outside", "/users/../outside", "/users\\outside", "/users/\n"):
            with self.subTest(value=value), self.assertRaises(setup.SetupError):
                self.manager.scope_path(value)
        (self.manager.deployment.data_dir / "link").symlink_to(self.project, target_is_directory=True)
        with self.assertRaisesRegex(setup.SetupError, "symbolic"):
            self.manager.scope_path("/link/credentials")

    def test_two_confirmations_are_mandatory(self):
        for responses in (["no"], ["FIRST", "no"]):
            with self.subTest(responses=responses), patch.object(manage, "prompt", side_effect=responses):
                with self.assertRaises(manage.Cancelled):
                    manage.confirm_twice("FIRST", "SECOND", "warning")
        with patch.object(manage, "prompt", side_effect=["FIRST", "SECOND"]):
            manage.confirm_twice("FIRST", "SECOND", "warning")

    def test_noninteractive_destructive_prompt_is_rejected(self):
        with patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(setup.SetupError, "interactive"):
                manage.prompt("Confirm? ")

    def test_delete_without_data_retains_files(self):
        with patch.object(self.manager, "connect", return_value=(self.api, "account_manager", "secret")), patch.object(manage, "prompt", side_effect=["alice", "n", "DELETE alice", "CONFIRM ACCOUNT alice"]), patch.object(self.manager, "mutate_deletions") as mutate:
            self.manager.delete_accounts()
        self.assertEqual(mutate.call_args.args[0], [self.alice])
        self.assertEqual(mutate.call_args.args[1], [])
        self.assertTrue((self.manager.deployment.data_dir / "users/alice/private.txt").exists())

    def test_root_admin_data_warns_whole_branch_twice(self):
        with patch.object(self.manager, "connect", return_value=(self.api, "account_manager", "secret")), patch.object(manage, "prompt", side_effect=["account_manager", "y", "DELETE account_manager", "DELETE DATA /"]), patch.object(self.manager, "mutate_deletions") as mutate, contextlib.redirect_stdout(io.StringIO()) as output:
            self.manager.delete_accounts()
        self.assertGreaterEqual(output.getvalue().count("WHOLE BRANCH"), 2)
        self.assertIn("OTHER ACCOUNTS", output.getvalue())
        self.assertEqual(mutate.call_args.args[1], ["/"])

    def test_last_admin_is_protected(self):
        self.api.users.remove(self.second)
        with patch.object(self.manager, "connect", return_value=(self.api, "account_manager", "secret")), patch.object(manage, "prompt", return_value="account_manager"), patch.object(self.manager, "mutate_deletions") as mutate:
            with self.assertRaisesRegex(setup.SetupError, "last administrator"):
                self.manager.delete_accounts()
        mutate.assert_not_called()

    def test_cancelled_clear_never_mutates(self):
        with patch.object(self.manager, "connect", return_value=(self.api, "account_manager", "secret")), patch.object(manage, "prompt", side_effect=["CLEAR DEFAULT USERS", "no"]), patch.object(self.manager, "mutate_deletions") as mutate:
            with self.assertRaises(manage.Cancelled):
                self.manager.delete_accounts(bulk=True)
        mutate.assert_not_called()

    def test_bulk_clear_selects_only_nonadmins(self):
        with patch.object(self.manager, "connect", return_value=(self.api, "account_manager", "secret")), patch.object(manage, "prompt", side_effect=["CLEAR DEFAULT USERS", "DELETE DEFAULT USER DATA"]), patch.object(self.manager, "mutate_deletions") as mutate:
            self.manager.delete_accounts(bulk=True)
        self.assertEqual(mutate.call_args.args[0], [self.alice, self.bob])
        self.assertEqual(mutate.call_args.args[1], ["/users/alice", "/users/bob"])

    def test_bulk_clear_rejects_branch_containing_admin_scope(self):
        self.alice["scope"] = "/"
        with patch.object(self.manager, "connect", return_value=(self.api, "account_manager", "secret")):
            with self.assertRaisesRegex(setup.SetupError, "administrator"):
                self.manager.delete_accounts(bulk=True)

    def test_dry_run_clear_and_hard_clear_do_not_prompt_or_mutate(self):
        with patch.object(self.manager, "connect", return_value=(self.api, "account_manager", "secret")), patch.object(manage, "prompt") as prompt, patch.object(self.manager, "mutate_deletions") as mutate:
            self.manager.delete_accounts(bulk=True, dry_run=True)
            self.manager.hard_clear(dry_run=True)
        prompt.assert_not_called()
        mutate.assert_not_called()

    def test_failure_restores_quarantined_files_and_database(self):
        def cli(arguments):
            self.manager.deployment.database.write_bytes(b"changed database")
            raise setup.SetupError("CLI failed")
        with patch.object(self.manager.deployment, "command", return_value=""), patch.object(self.manager, "cli", side_effect=cli):
            with self.assertRaisesRegex(setup.SetupError, "CLI failed"):
                self.manager.mutate_deletions([self.alice], ["/users/alice"], "test")
        self.assertEqual(self.manager.deployment.database.read_bytes(), b"original database")
        self.assertEqual((self.manager.deployment.data_dir / "users/alice/private.txt").read_text(), "alice")
        self.assertTrue((self.manager.deployment.data_dir / "users/bob/private.txt").exists())

    def test_deleting_folder_does_not_follow_descendant_symlink(self):
        outside = self.project / "outside.txt"
        outside.write_text("keep")
        (self.manager.deployment.data_dir / "users/alice/external").symlink_to(outside)
        with patch.object(self.manager.deployment, "command", return_value=""), patch.object(self.manager, "cli", return_value="ID Username\n1 account_manager\n"), patch.object(self.manager, "wait_health"):
            self.manager.mutate_deletions([self.alice], ["/users/alice"], "test")
        self.assertFalse((self.manager.deployment.data_dir / "users/alice").exists())
        self.assertEqual(outside.read_text(), "keep")
        self.assertTrue((self.manager.deployment.data_dir / "users/bob/private.txt").exists())

    def test_hard_clear_removes_database_and_data_but_retains_credentials(self):
        with patch.object(self.manager.deployment, "command", return_value="") as command:
            self.manager.mutate_deletions([], ["/"], "test-hard", hard=True)
        self.assertFalse(self.manager.deployment.database.exists())
        self.assertEqual(list(self.manager.deployment.data_dir.iterdir()), [])
        self.assertTrue(self.manager.admin_csv.exists())
        self.assertTrue(list(self.manager.deployment.backups.glob("*/filebrowser.db")))
        self.assertFalse(any("up" in call.args[0] for call in command.call_args_list))

    def test_public_entrypoint_help_from_another_working_directory(self):
        result = subprocess.run(["bash", str(ROOT / "run_script.sh")], cwd=self.project,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--setup", "--dry-run", "--create-users", "--delete-account", "--clear-users-and-data", "--hard-clear"):
            self.assertIn(flag, result.stdout)
        self.assertIn("WHOLE BRANCH", result.stdout)

    def test_dry_run_and_import_csv_file_modes(self):
        self.manager.user_csv.write_text("username,password\nalice,Alice long password123!\n")
        for dry_run, expected in ((True, 0o644), (False, 0o600)):
            self.manager.admin_csv.chmod(0o644)
            self.manager.user_csv.chmod(0o644)
            with patch.object(self.manager, "connect", return_value=(self.api, "account_manager", "secret")), patch.object(manage.users_api, "preflight", return_value=[]), patch.object(manage.users_api, "execute_plan"):
                self.manager.setup_and_users(False, True, dry_run)
            self.assertEqual(self.manager.admin_csv.stat().st_mode & 0o777, expected)
            self.assertEqual(self.manager.user_csv.stat().st_mode & 0o777, expected)

    def test_help_does_not_write_python_cache_files(self):
        checkout = self.project / "checkout"
        checkout.mkdir()
        shutil.copytree(ROOT / "internal", checkout / "internal", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "run_script.sh", checkout / "run_script.sh")
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONDONTWRITEBYTECODE"}
        result = subprocess.run(["bash", str(checkout / "run_script.sh")], env=environment,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(checkout.rglob("__pycache__")))

    def test_destructive_flags_cannot_be_combined_with_setup(self):
        result = subprocess.run(["bash", str(ROOT / "run_script.sh"), "--hard-clear", "--setup"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
