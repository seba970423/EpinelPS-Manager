import json
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from certificate_fixtures import legacy_ca_pair

from epinel_manager.certificates import TrustTransaction, local_certificate, openssl
from epinel_manager.common import ManagedError, read_json, write_json
from epinel_manager.manager import STARTUP_REVISION, Manager, is_owned_startup, process_identity, sqlite_backup
from epinel_manager.locations import installation_for_game
from epinel_manager.ports import PortConflict


class ManagedStartup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.fixture_path = Path(cls.fixture.name)
        cls.ca = local_certificate(cls.fixture_path / "private", cls.fixture_path / "site.pfx", lambda x: None)
        cls.key = openssl("pkcs12", "-in", cls.fixture_path / "site.pfx", "-passin", "pass:", "-nocerts", "-nodes").stdout
        cls.leaf = openssl("pkcs12", "-in", cls.fixture_path / "site.pfx", "-passin", "pass:", "-clcerts", "-nokeys").stdout

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / "friend's game/NIKKE/game"
        self.plugins = self.game / "nikke_Data/Plugins/x86_64"
        self.plugins.mkdir(parents=True)
        (self.game / "nikke.exe").write_bytes(b"game fixture")
        self.bundle = self.plugins / "intl_cacert.pem"
        self.bundle.write_bytes(b"Original vendor trust\n")
        self.launcher = self.root / "friend's launcher"
        self.launcher.mkdir()
        self.messages = []
        self.manager = Manager(self.root / "managed folder's copy", log=self.log)
        self.manager.settings.update({"game_dir": str(self.game), "launcher_dirs": [str(self.launcher)], "startup_timeout": 5})
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            self.manager.health_port = port.getsockname()[1]
        self.manager.prepare_ports = lambda version: None
        self.manager.check_ports = lambda: None
        self.addCleanup(self.manager.stop_child)
        write_json(self.manager.data / "db.json", {"progress": "original"})
        write_json(self.manager.data / "gamecommon.json", {"fixture": True})
        write_json(self.manager.data / "appsettings.json", {"ConnectionStrings": {"EpinelPSConnectionType": "sqlite", "EpinelPSConnection": f'Data Source="{self.manager.data / "epinelps.db"}"'}})
        with sqlite3.connect(self.manager.data / "epinelps.db") as db:
            db.execute("CREATE TABLE progress(value TEXT)")
            db.execute("INSERT INTO progress VALUES ('original')")
        self.write_new_progress = False

    def log(self, message):
        self.messages.append(message)
        if message.startswith("Ready:"):
            if self.write_new_progress:
                write_json(self.manager.data / "db.json", {"progress": "played after update"})
                with sqlite3.connect(self.manager.data / "epinelps.db") as db:
                    db.execute("UPDATE progress SET value='played after update'")
            self.manager.stop_child()

    def make_version(self, character, fail=False):
        version = self.manager.versions / (character * 40)
        version.mkdir()
        write_json(version / "gameconfig.json", {"TargetVersion": "test", "ArchiveUnlockAll": False})
        (version / "site.pfx").write_bytes((self.fixture_path / "site.pfx").read_bytes())
        (version / ".managed-ca.pem").write_bytes(self.ca)
        (version / ".leaf.pem").write_bytes(self.leaf)
        (version / ".key.pem").write_bytes(self.key)
        script = "#!/usr/bin/env python3\n" + textwrap.dedent(f"""
            import json, pathlib, sqlite3, ssl, time
            from http.server import HTTPServer, BaseHTTPRequestHandler
            if {fail!r}:
                pathlib.Path('db.json').write_text(json.dumps({{'progress':'bad migration'}}))
                configuration = json.loads(pathlib.Path('appsettings.json').read_text())
                path = configuration['ConnectionStrings']['EpinelPSConnection'].split('"')[1]
                with sqlite3.connect(path) as db:
                    db.execute("UPDATE progress SET value='bad migration'")
                print('Fatal error: simulated failed migration', flush=True)
                time.sleep(30)
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args): pass
                def do_GET(self):
                    payload=b'EpinelPS integration fixture'
                    self.send_response(200)
                    self.send_header('Content-Length',str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
            server=HTTPServer(('127.0.0.1',{self.manager.health_port}),Handler)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain('.leaf.pem','.key.pem')
            server.socket=context.wrap_socket(server.socket,server_side=True)
            server.serve_forever()
        """)
        (version / "EpinelPS").write_text(script)
        (version / "EpinelPS").chmod(0o700)
        return version

    def set_active(self, version):
        self.manager.state = {"active": version.name, "rejected": []}
        write_json(self.manager.state_path, self.manager.state)
        self.manager.link_data(version)

    def progress(self):
        with sqlite3.connect(self.manager.data / "epinelps.db") as db:
            return read_json(self.manager.data / "db.json")["progress"], db.execute("SELECT value FROM progress").fetchone()[0]

    def test_failed_update_restores_json_and_sqlite_and_launches_previous(self):
        old, bad = self.make_version("a"), self.make_version("b", fail=True)
        self.set_active(old)
        self.manager.candidate = lambda offline: bad
        self.assertEqual(self.manager.run(), 0)
        self.assertEqual(self.progress(), ("original", "original"))
        self.assertEqual(self.manager.state["active"], old.name)
        self.assertIn(bad.name, self.manager.state["rejected"])
        self.assertIn(b"BEGIN EPINELPS MANAGED CA", self.bundle.read_bytes())
        self.assertEqual((self.launcher / "myCA.pem").read_bytes(), self.ca)
        self.assertTrue(any("Starting the previous" in item for item in self.messages))

    def test_accepted_session_never_restores_away_new_progress(self):
        old, new = self.make_version("a"), self.make_version("b")
        self.set_active(old)
        self.manager.candidate = lambda offline: new
        self.write_new_progress = True
        self.assertEqual(self.manager.run(), 0)
        self.assertEqual(self.progress(), ("played after update", "played after update"))
        self.assertEqual(self.manager.state["active"], new.name)
        self.manager.recover()
        self.assertEqual(self.progress(), ("played after update", "played after update"))

    def test_pending_interrupted_launch_recovers_saves_and_trust(self):
        old, new = self.make_version("a"), self.make_version("b")
        self.set_active(old)
        original = dict(self.manager.state)
        snapshot = self.manager.snapshot()
        write_json(self.manager.data / "db.json", {"progress": "interrupted migration"})
        with sqlite3.connect(self.manager.data / "epinelps.db") as db:
            db.execute("UPDATE progress SET value='interrupted migration'")
        TrustTransaction(self.manager.private / "trust-pending").apply({self.bundle: b"interrupted trust change"})
        write_json(self.manager.private / "pending.json", {"candidate": new.name, "snapshot": snapshot.name, "state": original})
        self.manager.recover()
        self.assertEqual(self.progress(), ("original", "original"))
        self.assertEqual(self.bundle.read_bytes(), b"Original vendor trust\n")

    def test_durable_acceptance_marker_prevents_stale_rollback(self):
        old = self.make_version("a")
        self.set_active(old)
        snapshot = self.manager.snapshot()
        write_json(self.manager.data / "db.json", {"progress": "later progress"})
        write_json(self.manager.private / "pending.json", {"candidate": old.name, "snapshot": snapshot.name, "state": self.manager.state, "accepted": True})
        self.manager.recover()
        self.assertEqual(read_json(self.manager.data / "db.json")["progress"], "later progress")

    def test_interrupted_unaccepted_child_is_stopped_automatically(self):
        old, bad = self.make_version("a"), self.make_version("b", fail=True)
        self.set_active(old)
        self.manager.link_data(bad)
        snapshot = self.manager.snapshot()
        child = subprocess.Popen([str(bad / "EpinelPS"), "--headless"], cwd=bad,
                                 stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            # Some container runners mount /proc from a different PID namespace.
            # Keep real signal/child recovery while supplying its kernel identity.
            identity = "test-start-time"
            self.assertIn("Fatal error:", child.stdout.readline())
            write_json(self.manager.private / "pending.json", {"candidate": bad.name, "snapshot": snapshot.name,
                       "state": self.manager.state, "pid": child.pid, "process_identity": identity})
            with patch("epinel_manager.manager.is_owned_startup", side_effect=lambda *args: child.poll() is None):
                self.manager.recover()
            child.wait(timeout=3)
            self.assertEqual(self.progress(), ("original", "original"))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()

    def test_kernel_identity_handles_spaces_in_process_name_and_reused_pids(self):
        stat = "42 (EpinelPS with spaces) S " + " ".join(["0"] * 18 + ["9000"])
        with patch.object(Path, "read_text", return_value=stat):
            self.assertEqual(process_identity(42), "9000")
        expected = Path("/a folder/EpinelPS")
        with patch("epinel_manager.manager.process_identity", return_value="9000"), patch.object(Path, "read_bytes", return_value=b"python3\0/a folder/EpinelPS\0--headless\0"):
            self.assertTrue(is_owned_startup(42, "9000", expected))
            self.assertFalse(is_owned_startup(42, "older-process", expected))

    def test_consistent_backup_includes_committed_wal_data(self):
        path = self.manager.data / "epinelps.db"
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("UPDATE progress SET value='in WAL'")
            db.commit()
            self.assertTrue(Path(str(path) + "-wal").exists())
            snapshot = self.manager.snapshot()
            with sqlite3.connect(snapshot / "epinelps.db") as copied:
                self.assertEqual(copied.execute("SELECT value FROM progress").fetchone()[0], "in WAL")

    def test_permission_failure_does_not_promote_candidate_or_lose_saves(self):
        old, new = self.make_version("a"), self.make_version("b")
        self.set_active(old)
        self.manager.candidate = lambda offline: new
        writer = self.manager.write_trust_file
        calls = 0
        def fail_once(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ManagedError("simulated cancelled authentication")
            return writer(*args)
        self.manager.write_trust_file = fail_once
        self.assertEqual(self.manager.run(), 0)
        self.assertEqual(self.manager.state["active"], old.name)
        self.assertEqual(self.progress(), ("original", "original"))

    def test_offline_update_failure_keeps_installed_version(self):
        old = self.make_version("a")
        self.set_active(old)
        with patch.object(self.manager.network, "approved", side_effect=ManagedError("offline")):
            self.assertEqual(self.manager.candidate(), old)

    def test_stable_release_is_recorded_only_after_successful_startup(self):
        from epinel_manager.releases import bundled_release
        from dataclasses import replace
        version = self.make_version("a")
        self.set_active(version)
        pin = replace(bundled_release(), commit=version.name)
        self.manager.selected_build = pin
        self.assertTrue(self.manager.launch_once(version))
        self.assertEqual(self.manager.state["stable_release"], pin.record())

    def test_failed_stable_startup_preserves_previous_approval_and_saves(self):
        from epinel_manager.releases import bundled_release
        from dataclasses import replace
        old, bad = self.make_version("a"), self.make_version("b", fail=True)
        self.set_active(old)
        previous = replace(bundled_release(), commit=old.name)
        self.manager.state["stable_release"] = previous.record()
        self.manager.selected_build = replace(previous, commit=bad.name, sequence=previous.sequence + 1)
        self.assertFalse(self.manager.launch_once(bad))
        self.assertEqual(self.manager.state["stable_release"], previous.record())
        self.assertEqual(self.progress(), ("original", "original"))

    def test_import_does_not_modify_original_files(self):
        # A separate manager imports a traditional server layout, including both saves.
        source = self.root / "original server's folder"
        source.mkdir()
        (source / "EpinelPS").write_bytes(b"fixture original executable")
        (source / "EpinelPS").chmod(0o700)
        write_json(source / "gamecommon.json", {"fixture": True})
        write_json(source / "db.json", {"save": "original user"})
        write_json(source / "gameconfig.json", {"TargetVersion": "test"})
        write_json(source / "appsettings.json", {"ConnectionStrings": {"EpinelPSConnection": 'Data Source="(startupDirectory)/epinelps.db"', "EpinelPSConnectionType": "sqlite"}})
        with sqlite3.connect(source / "epinelps.db") as db:
            db.execute("CREATE TABLE Triggers(id INTEGER)")
        (source / "cache").mkdir()
        (source / "cache/asset").write_bytes(b"cached asset")
        before = {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()}
        manager = Manager(self.root / "import destination", log=lambda x: None)
        manager.import_existing(source)
        after = {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()}
        self.assertEqual(before, after)
        self.assertTrue((manager.active() / "db.json").is_symlink())
        self.assertEqual(read_json(manager.data / "db.json"), {"save": "original user"})
        self.assertIn(str(manager.data), read_json(manager.data / "appsettings.json")["ConnectionStrings"]["EpinelPSConnection"])
        self.assertEqual((manager.data / "cache/asset").read_bytes(), b"cached asset")

    def test_early_upstream_database_path_uses_the_canonical_save(self):
        version = self.make_version("a")
        with sqlite3.connect(self.manager.data / "epinelps.db") as database:
            database.execute("CREATE TABLE Triggers(Id INTEGER PRIMARY KEY, Value INTEGER)")
            database.execute("INSERT INTO Triggers VALUES(1, 42)")
        (version / "epinelps.db").write_bytes(b"detached v0.1 database")
        (version / "epinelps.db-wal").write_bytes(b"detached WAL")
        self.manager.link_data(version)
        self.assertTrue((version / "epinelps.db").is_symlink())
        self.assertEqual((version / "epinelps.db").resolve(), self.manager.data / "epinelps.db")
        # This reproduces upstream's early BaseDirectory lookup, ignoring the
        # environment/configuration connection string exactly as GameContext did.
        with sqlite3.connect(version / "epinelps.db") as early_context:
            self.assertEqual(early_context.execute("SELECT Value FROM Triggers").fetchone()[0], 42)
        archives = list((self.manager.private / "detached-databases").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / "epinelps.db").read_bytes(), b"detached v0.1 database")
        self.assertEqual((archives[0] / "epinelps.db-wal").read_bytes(), b"detached WAL")
        self.manager.link_data(version)
        self.assertEqual(len(list((self.manager.private / "detached-databases").iterdir())), 1)

    def test_old_rejections_are_retried_once_after_database_fix(self):
        version = self.make_version("a")
        self.set_active(version)
        self.manager.state["rejected"] = ["b" * 40]
        self.manager.upgrade_startup_state()
        self.assertEqual(self.manager.state["rejected"], [])
        self.manager.state["rejected"] = ["b" * 40]
        self.manager.upgrade_startup_state()
        self.assertEqual(self.manager.state["rejected"], ["b" * 40])

    def test_certificate_fix_retries_only_invalid_pairs_once(self):
        old, invalid, valid = self.make_version("a"), self.make_version("b"), self.make_version("c")
        legacy = self.root / "legacy"
        legacy_ca_pair(legacy)
        (invalid / "site.pfx").write_bytes((legacy / "site.pfx").read_bytes())
        (invalid / ".managed-ca.pem").write_bytes((legacy / "ca.pem").read_bytes())
        self.set_active(old)
        self.manager.state.update(startup_revision=1, rejected=[invalid.name, valid.name])
        self.manager.upgrade_startup_state()
        self.assertEqual(self.manager.state["rejected"], [valid.name])
        self.manager.state["rejected"].append(invalid.name)
        self.manager.upgrade_startup_state()
        self.assertEqual(self.manager.state["rejected"], [valid.name, invalid.name])

    def test_running_native_server_blocks_database_path_changes(self):
        version = self.make_version("a")
        detached = version / "epinelps.db"
        detached.write_bytes(b"database still in use")
        with patch("epinel_manager.manager.process_using", return_value=True), self.assertRaises(ManagedError):
            self.manager.launch_once(version)
        self.assertEqual(detached.read_bytes(), b"database still in use")
        self.assertFalse((self.manager.private / "detached-databases").exists())

    def test_setup_explicitly_asks_for_game_even_when_old_auto_path_is_valid(self):
        version = self.make_version("a")
        self.set_active(version)
        with patch("epinel_manager.manager.confirmed_installation", return_value=installation_for_game(self.game)) as choose, patch("epinel_manager.manager.scan_directories", side_effect=AssertionError("Do not scan other installations")):
            self.manager.setup()
            choose.assert_called_once()
        self.assertEqual(self.manager.settings["game_dir"], str(self.game))
        self.assertTrue(self.manager.settings["manage_hosts"])
        with patch("epinel_manager.manager.confirmed_installation", side_effect=AssertionError("Must remember explicit choice")):
            self.manager.setup()

    def test_cancelled_installation_selection_does_not_replace_remembered_paths(self):
        version = self.make_version("a")
        self.set_active(version)
        original = dict(self.manager.settings)
        with patch("epinel_manager.locations.ask_location", return_value=None), self.assertRaises(ManagedError):
            self.manager.setup()
        self.assertEqual(self.manager.settings, original)

    def test_one_installation_prompt_finds_game_launchers_and_remembers_them(self):
        version = self.make_version("a")
        self.set_active(version)
        root = self.game.parent.parent
        launcher = root / "Launcher"
        launcher.mkdir()
        (root / "ServerSelector.Desktop.exe").write_bytes(b"selector fixture")
        with patch("epinel_manager.locations.ask_location", return_value='"' + str(root) + '"') as prompt:
            self.manager.setup()
            prompt.assert_called_once()
        self.assertEqual(self.manager.settings["game_dir"], str(self.game))
        self.assertEqual(self.manager.settings["install_dir"], str(root))
        self.assertIn(str(launcher), self.manager.settings["launcher_dirs"])
        self.assertIn(str(root), self.manager.settings["launcher_dirs"])
        # Previously explicit extra launcher folders remain available on upgrade.
        self.assertIn(str(self.launcher), self.manager.settings["launcher_dirs"])
        with patch("epinel_manager.locations.ask_location", side_effect=AssertionError("No repeat prompts")):
            self.manager.setup()

    def test_working_v012_paths_upgrade_without_another_prompt(self):
        version = self.make_version("a")
        self.set_active(version)
        self.manager.settings["location_revision"] = 1
        self.manager.save_settings()
        with patch("epinel_manager.locations.ask_location", side_effect=AssertionError("No forced re-selection")):
            self.manager.setup()
        self.assertEqual(self.manager.settings["game_dir"], str(self.game))
        self.assertIn(str(self.launcher), self.manager.settings["launcher_dirs"])

    def test_port_conflict_stops_before_download_without_rejecting_build(self):
        version = self.make_version("a")
        self.set_active(version)
        before = dict(self.manager.state)
        self.manager.check_ports = lambda: (_ for _ in ()).throw(PortConflict("Port 443 is already in use"))
        with patch.object(self.manager, "candidate", side_effect=AssertionError("Must not download")), self.assertRaises(PortConflict):
            self.manager.run()
        self.assertEqual(self.manager.state, before)
        self.assertFalse((self.manager.private / "backups").exists())

    def test_port_conflict_during_launch_never_rejects_or_falls_back(self):
        old, new = self.make_version("a"), self.make_version("b")
        self.set_active(old)
        self.manager.state["startup_revision"] = STARTUP_REVISION
        before = dict(self.manager.state)
        self.manager.candidate = lambda offline: new
        with patch.object(self.manager, "launch_once", side_effect=PortConflict("Port 443 was taken")) as launch, self.assertRaises(PortConflict):
            self.manager.run()
        launch.assert_called_once_with(new)
        self.assertEqual(self.manager.state, before)

    def test_child_bind_failure_restores_saves_without_rejecting_or_fallback(self):
        old, new = self.make_version("a"), self.make_version("b", fail=True)
        self.set_active(old)
        self.manager.state["startup_revision"] = STARTUP_REVISION
        executable = new / "EpinelPS"
        executable.write_text(executable.read_text().replace(
            "Fatal error: simulated failed migration",
            "Fatal error: Failed to bind to address https://0.0.0.0:443: address already in use."))
        self.manager.candidate = lambda offline: new
        with self.assertRaises(PortConflict):
            self.manager.run()
        self.assertEqual(self.progress(), ("original", "original"))
        self.assertEqual(self.manager.state["rejected"], [])
        self.assertEqual(self.manager.state["active"], old.name)
        self.assertFalse(any("Starting the previous" in message for message in self.messages))

    def test_port_check_precedes_database_and_certificate_changes(self):
        version = self.make_version("a")
        detached = version / "epinelps.db"
        detached.write_bytes(b"database still in use")
        with patch.object(self.manager, "prepare_ports", side_effect=PortConflict("Port 443 is already in use")), self.assertRaises(PortConflict):
            self.manager.launch_once(version)
        self.assertEqual(detached.read_bytes(), b"database still in use")
        self.assertFalse((self.manager.private / "detached-databases").exists())

    def test_explicit_bottles_root_repairs_launcher_bundles_and_public_selector_pfx(self):
        from epinel_manager.certificates import fingerprint, openssl
        from epinel_manager.connection import wine_prefix
        root = self.root / "Bottles/prefixes/a-real-bottle/drive_c/NIKKE"
        game = root / "NIKKE/game"
        plugins = game / "nikke_Data/Plugins/x86_64"
        plugins.mkdir(parents=True)
        (game / "nikke.exe").write_bytes(b"game fixture")
        (plugins / "intl_cacert.pem").write_bytes(b"old game trust\n")
        launcher = root / "Launcher"
        (launcher / "intl_service").mkdir(parents=True)
        old_bundle = launcher / "intl_service/intl_cacert.pem"
        old_bundle.write_bytes(b"old launcher trust\n")
        (root / "ServerSelector.Desktop.exe").write_bytes(b"old Windows selector")
        (root / "myCA.pfx").write_bytes(b"old certificate kit")
        prefix = root.parent
        hosts = prefix.parent / "test-system-hosts"
        hosts.write_bytes(b"127.0.0.1 localhost\n192.0.2.99 li-sg.intlgame.com another-app.local # preserve alias\n")
        self.manager.system_hosts_file = hosts
        self.manager.settings.update(game_dir=str(game), launcher_dirs=[str(launcher)], manage_hosts=True)
        version = self.make_version("a")
        ca = version / ".managed-ca.pem"
        plan = self.manager.trust_changes(version, ca)
        self.assertIn(old_bundle.resolve(), plan)
        self.assertIn(root / "myCA.pfx", plan)
        self.assertEqual(wine_prefix(game), root.parent.parent)
        self.assertIn(root.parent.parent / "drive_c/windows/system32/drivers/etc/hosts", plan)
        TrustTransaction(self.manager.private / "test-connection").apply(plan)
        self.assertIn(b"BEGIN EPINELPS MANAGED CA", old_bundle.read_bytes())
        extracted = openssl("pkcs12", "-in", root / "myCA.pfx", "-passin", "pass:", "-nokeys").stdout
        self.assertEqual(fingerprint(extracted), fingerprint(self.ca))
        self.assertIn(b"another-app.local", hosts.read_bytes())
        self.assertIn(b"127.0.0.1 li-sg.intlgame.com", hosts.read_bytes())

    def test_native_server_receives_verified_migration_hook_and_shared_database(self):
        version = self.make_version("a")
        (version / "EpinelPS").write_bytes(b"\x7fELF" + b"fixture")
        with patch.dict("os.environ", {"EPINEL_MANAGED_SCHEMA_ONLY": "1", "ConnectionStrings__EpinelPSConnection": "a different database"}):
            environment, native = self.manager.server_environment(version)
        self.assertTrue(native)
        self.assertEqual(environment["EPINEL_MANAGED_DATABASE"], str(self.manager.data / "epinelps.db"))
        self.assertIn("EpinelPS.Managed.StartupHook.dll", environment["DOTNET_STARTUP_HOOKS"])
        self.assertNotIn("EPINEL_MANAGED_SCHEMA_ONLY", environment)
        self.assertIn(str(self.manager.data), environment["ConnectionStrings__EpinelPSConnection"])
