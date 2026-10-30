import fcntl
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from epinel_manager.common import ManagedError, atomic_write, write_json

MODULE = Path(__file__).resolve().parent.parent / "tools/apply_update.py"
SPEC = importlib.util.spec_from_file_location("epinel_application_update", MODULE)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


class ApplicationUpdate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = self.root / "new application", self.root / "existing managed folder"
        self.source.mkdir()
        self.target.mkdir()
        (self.source / "EpinelPS").write_bytes(b"new application")
        (self.source / "Start-EpinelPS.sh").write_bytes(b"new start script")
        (self.source / "epinel_manager").mkdir()
        (self.source / "epinel_manager/manager.py").write_bytes(b"new manager code")
        (self.source / "server-release.json").write_bytes(b'{"new": "project server pin"}')
        shutil.copytree(MODULE.parent.parent / "epinel_manager/bin", self.source / "epinel_manager/bin")
        (self.target / "EpinelPS").write_bytes(b"old application")
        write_json(self.target / ".epinel/state.json", {"active": "imported", "rejected": ["b" * 40]})
        write_json(self.target / ".epinel/data/db.json", {"progress": "do not replace this"})
        # An accidentally present source save directory must never be imported.
        write_json(self.source / ".epinel/data/db.json", {"progress": "wrong source save"})
        self.state = (self.target / ".epinel/state.json").read_bytes()
        self.save = (self.target / ".epinel/data/db.json").read_bytes()

    def test_application_update_preserves_existing_saves_and_state(self):
        result = updater.apply_update(self.source, self.target)
        self.assertEqual(result, self.target)
        self.assertEqual((self.target / "EpinelPS").read_bytes(), b"new application")
        self.assertEqual((self.target / ".epinel/state.json").read_bytes(), self.state)
        self.assertEqual((self.target / ".epinel/data/db.json").read_bytes(), self.save)
        self.assertTrue((self.target / "epinel_manager/bin/EpinelPS.Managed.StartupHook.dll").is_file())
        self.assertTrue((self.target / "Start-EpinelPS.sh").stat().st_mode & 0o111)
        self.assertEqual((self.target / "server-release.json").read_bytes(), (self.source / "server-release.json").read_bytes())

    def test_running_manager_blocks_application_update(self):
        with (self.target / ".epinel/manager.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ManagedError):
                updater.apply_update(self.source, self.target)
        self.assertEqual((self.target / "EpinelPS").read_bytes(), b"old application")

    def test_failed_application_update_restores_application_and_keeps_saves(self):
        failed = False
        def writer(path, content, mode=None):
            nonlocal failed
            if path == self.target / "epinel_manager/manager.py" and not failed:
                failed = True
                raise OSError("simulated disk write failure")
            atomic_write(path, content, mode)
        with patch.object(updater, "atomic_write", side_effect=writer), self.assertRaises(OSError):
            updater.apply_update(self.source, self.target)
        self.assertEqual((self.target / "EpinelPS").read_bytes(), b"old application")
        self.assertEqual((self.target / ".epinel/data/db.json").read_bytes(), self.save)
