import copy
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "internal"))
import filebrowser_setup as setup


PASSWORD = 'a long,"quoted" password $with symbols'
SETTINGS = {
    "signup": False, "createUserDir": True, "userHomeBasePath": "/users",
    "authMethod": "json", "defaults": {"scope": "", "perm": {"admin": False, "execute": False, "share": False}},
}
MANAGER = {"username": "account_manager", "scope": "/",
           "perm": {"admin": True, "execute": False, "share": False}}


class FakeAPI:
    def __init__(self, users=None):
        self.users = copy.deepcopy(users if users is not None else [MANAGER])
        self.settings = copy.deepcopy(SETTINGS)

    def json(self, path):
        return self.users if path == "/users" else self.settings

    def call(self, method, path, data):
        if method == "PUT" and path == "/settings":
            self.settings = data


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.csv = self.project / "admin.csv"
        self.csv.write_text('username,password\naccount_manager,"a long,""quoted"" password $with symbols"\n')
        self.deployment = setup.Deployment(self.project, self.csv)
        self.deployment.database_dir.mkdir(parents=True)
        self.deployment.data_dir.mkdir()
        self.deployment.config_dir.mkdir()
        (self.deployment.config_dir / "settings.json").write_text("{}")
        (self.project / "docker-compose.yml").write_text("services: {}")
        self.deployment.uid = self.deployment.gid = 1000
        self.deployment.image = "filebrowser/filebrowser:v2.63.23"
        self.deployment.base_url = "http://127.0.0.1:8000"

    def tearDown(self):
        self.temp.cleanup()

    def test_csv_preserves_quoted_password(self):
        self.assertEqual(setup.read_admin_csv(self.csv), PASSWORD)

    def test_csv_rejects_wrong_accounts_and_invalid_passwords(self):
        for text in (
            "username,password\nAdel,long enough password\n",
            "username,password\naccount_manager,short\n",
            "username,password\naccount_manager,long enough password\nAdel,another long password\n",
            'username,password\naccount_manager,"long enough\npassword"\n',
            "username,password\naccount_manager,long enough password,extra\n",
        ):
            with self.subTest(text=text):
                self.csv.write_text(text)
                with self.assertRaises(setup.SetupError):
                    setup.read_admin_csv(self.csv)

    def test_manager_and_isolation_defaults_verified(self):
        api = FakeAPI()
        self.assertEqual(len(setup.verify_manager(api, only_manager=True)), 1)
        api.users.append({"username": "student"})
        self.assertEqual(len(setup.verify_manager(api, only_manager=False)), 2)
        with self.assertRaisesRegex(setup.SetupError, "other than"):
            setup.verify_manager(api, only_manager=True)

    def test_unsafe_defaults_are_rejected(self):
        for key, value in (("signup", True), ("createUserDir", False), ("userHomeBasePath", "/")):
            with self.subTest(key=key):
                api = FakeAPI()
                api.settings[key] = value
                with self.assertRaises(setup.SetupError):
                    setup.verify_manager(api, only_manager=True)

    def test_normal_rerun_preserves_database_and_users(self):
        self.deployment.database.write_bytes(b"old database")
        api = FakeAPI([MANAGER, {"username": "student"}])
        with patch.object(self.deployment, "command", return_value=""), patch.object(self.deployment, "stage") as stage, patch.object(setup, "wait_api", return_value=api):
            self.deployment.install(PASSWORD, reset=False)
        stage.assert_not_called()
        self.assertEqual(self.deployment.database.read_bytes(), b"old database")

    def test_staging_failure_keeps_original_database_running(self):
        self.deployment.database.write_bytes(b"old database")
        with patch.object(self.deployment, "command") as command, patch.object(self.deployment, "stage", side_effect=setup.SetupError("stage failed")), patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(setup.SetupError, "stage failed"):
                self.deployment.install(PASSWORD, reset=True)
        self.assertEqual(self.deployment.database.read_bytes(), b"old database")
        self.assertFalse(any("stop" in call.args[0] for call in command.call_args_list))

    def test_reset_backs_up_and_activates_only_manager(self):
        self.deployment.database.write_bytes(b"old database")
        staged = self.deployment.database_dir / ".setup-test.db"
        staged.write_bytes(b"new database")
        with patch.object(self.deployment, "command", return_value=""), patch.object(self.deployment, "stage", return_value=staged), patch.object(setup, "wait_api", return_value=FakeAPI()), patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            self.deployment.install(PASSWORD, reset=True)
        self.assertEqual(self.deployment.database.read_bytes(), b"new database")
        backups = list(self.deployment.backups.glob("*/filebrowser.db"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"old database")
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_activation_failure_restores_previous_database(self):
        self.deployment.database.write_bytes(b"old database")
        staged = self.deployment.database_dir / ".setup-test.db"
        staged.write_bytes(b"new database")
        with patch.object(self.deployment, "command", return_value="") as command, patch.object(self.deployment, "stage", return_value=staged), patch.object(setup, "wait_api", side_effect=setup.SetupError("activation failed")), patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(setup.SetupError, "activation failed"):
                self.deployment.install(PASSWORD, reset=True)
        self.assertEqual(self.deployment.database.read_bytes(), b"old database")
        labels = [call.args[1] for call in command.call_args_list]
        self.assertIn("Restart previous database", labels)

    def test_bootstrap_password_uses_stdin_not_docker_arguments(self):
        calls = []

        def command(args, label, **kwargs):
            calls.append((args, label, kwargs))
            if label == "Initialize staged database":
                container_path = args[args.index("--database") + 1]
                (self.deployment.database_dir / Path(container_path).name).write_bytes(b"staged")
            if label == "Inspect staged server":
                return '[{"NetworkSettings":{"Ports":{"80/tcp":[{"HostPort":"12345"}]}}}]'
            return ""

        with patch.object(self.deployment, "command", side_effect=command), patch.object(setup, "wait_api", return_value=FakeAPI()):
            staged = self.deployment.stage(PASSWORD)
        create = [call for call in calls if call[1] == "Create account_manager"][0]
        self.assertEqual(create[2]["input_text"], PASSWORD + "\n")
        self.assertNotIn(PASSWORD, " ".join(create[0]))
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)

    def test_fresh_checkout_does_not_require_runtime_settings_file(self):
        (self.deployment.config_dir / "settings.json").unlink()
        config = {"services": {"filebrowser": {
            "image": self.deployment.image,
            "volumes": [
                {"type": "bind", "source": str(self.deployment.data_dir), "target": "/srv"},
                {"type": "bind", "source": str(self.deployment.database_dir), "target": "/database"},
                {"type": "bind", "source": str(self.deployment.config_dir), "target": "/config"},
            ],
            "ports": [{"target": 80, "published": "8000"}],
        }}}
        with patch.object(self.deployment, "command", return_value=__import__("json").dumps(config)):
            self.deployment.validate()
        self.assertFalse((self.deployment.config_dir / "settings.json").exists())

    def test_shell_launcher(self):
        result = subprocess.run(["bash", str(ROOT / "internal/filebrowser_setup.sh"), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--reset-users", result.stdout)


if __name__ == "__main__":
    unittest.main()
