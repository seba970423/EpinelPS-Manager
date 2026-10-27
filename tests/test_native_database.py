"""Real upstream .NET/EF regression tests; provide EPINEL_TEST_SERVER to run."""
import base64
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from epinel_manager.manager import Manager


@unittest.skipUnless(os.environ.get("EPINEL_TEST_SERVER"), "Set EPINEL_TEST_SERVER to an upstream Linux server executable")
class NativeDatabase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="epinel-native-regression-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = Manager(self.root / "managed user's folder", log=lambda message: None)
        self.version = self.manager.versions / ("a" * 40)
        self.version.mkdir()
        (self.version / "gameconfig.json").write_text(json.dumps({"TargetVersion": "regression-test"}))
        source = Path(os.environ["EPINEL_TEST_SERVER"]).resolve()
        try:
            os.link(source, self.version / "EpinelPS")
        except OSError:
            shutil.copy2(source, self.version / "EpinelPS")
        (self.version / "EpinelPS").chmod(0o700)
        self.fixture = {
            "DbVersion": 5,
            "LauncherTokenKey": base64.b64encode(b"L" * 32).decode(),
            "EncryptionTokenKey": base64.b64encode(b"E" * 32).decode(),
            "Users": [{"ID": 188898165, "Username": "regression@example.invalid", "Nickname": "saved-user",
                       "LastNormalStageCleared": 6000002, "Triggers": [
                           {"Type": 1, "ConditionId": 4, "CreatedAt": 1234, "Value": 1}]}],
        }
        (self.manager.data / "db.json").write_text(json.dumps(self.fixture))
        self.manager.link_data(self.version)

    def run_migrations(self):
        environment, native = self.manager.server_environment(self.version)
        self.assertTrue(native)
        environment["EPINEL_MANAGED_SCHEMA_ONLY"] = "1"
        environment["DOTNET_BUNDLE_EXTRACT_BASE_DIR"] = str(self.root / "runtime-extraction")
        result = subprocess.run([str(self.version / "EpinelPS"), "--headless"], cwd=self.version,
                                env=environment, capture_output=True, text=True, timeout=60)
        self.output = result.stdout + result.stderr
        return result

    def test_real_migrations_create_triggers_before_saved_user_reconciliation(self):
        result = self.run_migrations()
        self.assertEqual(result.returncode, 0, self.output)
        with sqlite3.connect(self.manager.data / "epinelps.db") as database:
            tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"Users", "SdkUsers", "Triggers", "__EFMigrationsHistory"}.issubset(tables))
            self.assertEqual(database.execute("SELECT ID,Nickname FROM Users").fetchall(), [(188898165, "saved-user")])
            self.assertEqual(database.execute("SELECT UserId,Value FROM Triggers").fetchall(), [(188898165, 1)])
        saved = json.loads((self.manager.data / "db.json").read_text())
        self.assertEqual(saved["Users"][0]["LastNormalStageCleared"], 6000002)
        self.assertIn("Managed database ready:", self.output)
        self.assertNotIn("no such table", self.output)

    def test_real_migrations_are_idempotent_and_preserve_existing_rows(self):
        self.assertEqual(self.run_migrations().returncode, 0, self.output)
        with sqlite3.connect(self.manager.data / "epinelps.db") as database:
            database.execute("CREATE TABLE RegressionProgress(Value TEXT)")
            database.execute("INSERT INTO RegressionProgress VALUES('keep this progress')")
        self.assertEqual(self.run_migrations().returncode, 0, self.output)
        with sqlite3.connect(self.manager.data / "epinelps.db") as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM Users").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM Triggers").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT Value FROM RegressionProgress").fetchone()[0], "keep this progress")

    def test_real_database_corruption_fails_before_json_save_is_changed(self):
        before = (self.manager.data / "db.json").read_bytes()
        (self.manager.data / "epinelps.db").write_bytes(b"this is not a SQLite database")
        result = self.run_migrations()
        self.assertEqual(result.returncode, 78, self.output)
        self.assertIn("managed database preparation failed", self.output)
        self.assertEqual((self.manager.data / "db.json").read_bytes(), before)

    def test_real_native_entrypoint_runs_only_after_database_preparation(self):
        # No gamecommon.json is present: upstream Main exits at its normal missing
        # asset check, after the helper has completed its real schema migration.
        # Remove the intentionally dangling import alias too: some .NET versions
        # report that a dangling symlink exists even though its target does not.
        (self.version / "gamecommon.json").unlink()
        environment, _ = self.manager.server_environment(self.version)
        environment["DOTNET_BUNDLE_EXTRACT_BASE_DIR"] = str(self.root / "runtime-extraction")
        result = subprocess.run([str(self.version / "EpinelPS"), "--headless"], cwd=self.version,
                                env=environment, capture_output=True, text=True, timeout=60)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertLess(output.index("Managed database ready:"), output.index("EpinelPS v"))
        self.assertIn("gamecommon.json does not exist", output)
