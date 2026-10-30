import copy
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from epinel_manager.common import ManagedError, write_json
from epinel_manager.manager import Manager, main
from epinel_manager.network import Network
from epinel_manager.releases import MANIFEST_PATH, MANIFEST_URL, MANAGER_REPO, bundled_release, parse_manifest


class ReleasePolicy(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(MANIFEST_PATH.read_text())

    def test_stable_uses_only_the_project_manifest_and_release(self):
        network = Network()
        with patch.object(network, "get", return_value=json.dumps(self.document).encode()) as get:
            build = network.approved()
        get.assert_called_once_with(MANIFEST_URL, limit=65536)
        self.assertIn(f"github.com/{MANAGER_REPO}/releases/download/", build.url)
        self.assertNotIn("nightly.link", build.url)
        self.assertIn(f"/actions/runs/{build.run_id}/", build.upstream_url)

    def test_incomplete_or_unsafe_pins_are_rejected(self):
        changes = [
            {"schema": 2}, {"schema": True}, {"channel": "experimental"},
            {"upstream_repository": "someone/else"}, {"minimum_manager_version": "999.0.0"},
            {"minimum_manager_version": "invalid"}, {"sequence": True}, {"sequence": 0},
            {"release": "../../other"}, {"server": None},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ManagedError):
                parse_manifest({**self.document, **change})
        for field, value in (("commit", "main"), ("sha256", "bad"), ("executable_sha256", "bad"),
                             ("run_id", True), ("artifact_id", 0), ("size", -1), ("size", 2 ** 40)):
            document = copy.deepcopy(self.document)
            document["server"][field] = value
            with self.subTest(field=field), self.assertRaises(ManagedError):
                parse_manifest(document)

    def test_invalid_json_never_falls_back_to_upstream_latest(self):
        network = Network()
        with patch.object(network, "get", return_value=b"not json"), patch.object(network, "latest") as latest:
            with self.assertRaises(ManagedError):
                network.approved()
        latest.assert_not_called()


class ChannelSelection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.network = Mock(spec=Network)
        self.messages = []
        self.manager = Manager(Path(self.temp.name), network=self.network, log=self.messages.append)
        self.pin = replace(bundled_release(), executable_digest=hashlib.sha256(b"server fixture").hexdigest())
        self.network.approved.return_value = self.pin
        self.old = self.manager.versions / ("a" * 40)
        self.old.mkdir()
        (self.old / "EpinelPS").write_bytes(b"old server")
        write_json(self.old / ".build.json", {"run_id": self.pin.run_id - 1})
        self.manager.state["active"] = self.old.name
        self.version = self.manager.versions / self.pin.commit
        self.version.mkdir()
        (self.version / "EpinelPS").write_bytes(b"server fixture")

    def test_existing_settings_default_to_stable_and_preserve_preferences(self):
        write_json(self.manager.settings_path, {"auto_update": False, "game_dir": "/remembered/path"})
        manager = Manager(self.manager.root, network=self.network)
        self.assertEqual(manager.settings["update_channel"], "stable")
        self.assertFalse(manager.settings["auto_update"])
        self.assertEqual(manager.settings["game_dir"], "/remembered/path")

    def test_normal_startup_never_queries_upstream_latest(self):
        self.assertEqual(self.manager.candidate(), self.version)
        self.network.approved.assert_called_once()
        self.network.latest.assert_not_called()

    def test_experimental_channel_explicitly_uses_latest(self):
        self.manager.settings["update_channel"] = "experimental"
        self.network.latest.return_value = replace(self.pin, release="", sequence=0, executable_digest="")
        self.assertEqual(self.manager.candidate(), self.version)
        self.network.latest.assert_called_once()
        self.network.approved.assert_not_called()

    def test_missing_pin_keeps_installed_server_without_upstream_fallback(self):
        self.network.approved.side_effect = ManagedError("pin unavailable")
        self.assertEqual(self.manager.candidate(), self.old)
        self.network.latest.assert_not_called()

    def test_modified_active_approved_executable_is_not_started(self):
        self.manager.state["active"] = self.version.name
        (self.version / "EpinelPS").write_bytes(b"modified active executable")
        with self.assertRaises(ManagedError):
            self.manager.candidate()

    def test_offline_and_disabled_updates_do_not_check_either_channel(self):
        self.assertEqual(self.manager.candidate(offline=True), self.old)
        self.manager.settings["auto_update"] = False
        self.assertEqual(self.manager.candidate(), self.old)
        self.network.approved.assert_not_called()
        self.network.latest.assert_not_called()

    def test_newer_or_unknown_install_is_not_automatically_downgraded(self):
        for metadata in ({"run_id": self.pin.run_id + 1}, {}):
            with self.subTest(metadata=metadata):
                write_json(self.old / ".build.json", metadata)
                self.assertEqual(self.manager.candidate(), self.old)
                self.assertIsNone(self.manager.selected_build)
        self.assertTrue(any("--adopt-stable" in message for message in self.messages))

    def test_known_pinned_import_can_be_adopted_without_an_extra_prompt(self):
        (self.old / "EpinelPS").write_bytes(b"server fixture")
        (self.old / ".build.json").unlink()
        self.assertEqual(self.manager.candidate(), self.version)

    def test_explicit_adoption_keeps_checks_and_allows_intended_switch(self):
        write_json(self.old / ".build.json", {"run_id": self.pin.run_id + 1})
        self.manager.adopt_stable = True
        self.assertEqual(self.manager.candidate(), self.version)
        (self.version / "EpinelPS").write_bytes(b"wrong executable")
        self.assertEqual(self.manager.candidate(), self.old)

    def test_explicit_adoption_can_check_pin_when_auto_updates_are_disabled(self):
        self.manager.settings["auto_update"] = False
        self.manager.adopt_stable = True
        self.assertEqual(self.manager.candidate(), self.version)
        self.assertFalse(self.manager.settings["auto_update"])

    def test_older_or_conflicting_approval_cannot_replace_accepted_release(self):
        for accepted in (replace(self.pin, sequence=self.pin.sequence + 1), replace(self.pin, commit="b" * 40),
                         replace(self.pin, digest="b" * 64)):
            with self.subTest(accepted=accepted):
                self.manager.state["stable_release"] = accepted.record()
                self.assertEqual(self.manager.candidate(), self.old)

    def test_missing_published_zip_does_not_download_from_upstream(self):
        (self.version / "EpinelPS").unlink()
        self.version.rmdir()
        self.network.download.side_effect = ManagedError("release not published")
        self.assertEqual(self.manager.candidate(), self.old)
        self.network.download.assert_called_once()
        self.assertEqual(self.network.download.call_args.args[0].url, self.pin.url)
        self.network.latest.assert_not_called()

    def test_explicit_channel_is_remembered_without_repeating_setup(self):
        with patch("epinel_manager.manager.__file__", str(self.manager.root / "epinel_manager/manager.py")), \
                patch.object(Manager, "setup") as setup, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--channel", "experimental", "--status"]), 0)
        setup.assert_not_called()
        saved = json.loads(self.manager.settings_path.read_text())
        self.assertEqual(saved["update_channel"], "experimental")

    def test_stable_adoption_rejects_conflicting_modes(self):
        for argument in ("--offline", "--repair", "--status"):
            with self.subTest(argument=argument), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["--adopt-stable", argument])
            self.assertEqual(error.exception.code, 2)


class ApprovalOrigin(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("prepare_pinned_server", MANIFEST_PATH.parent / "tools/prepare_server.py")
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.build = bundled_release()
        self.run = {"head_sha": self.build.commit, "path": ".github/workflows/dotnet-desktop.yml",
                    "head_branch": "main", "event": "push", "conclusion": "success"}
        self.artifact = {"id": self.build.artifact_id, "name": "EpinelPS_linux_x64", "expired": False,
                         "size_in_bytes": self.build.size, "digest": "sha256:" + self.build.digest,
                         "workflow_run": {"head_sha": self.build.commit}}

    def test_bootstrap_requires_the_exact_successful_push_build_and_artifact(self):
        network = Mock(spec=Network)
        network.json.side_effect = [self.run, {"artifacts": [self.artifact]}]
        self.tool.verify_origin(network, self.build)
        self.assertEqual(network.json.call_count, 2)
        for changed in ({"event": "pull_request"}, {"head_sha": "b" * 40}, {"conclusion": "failure"}):
            network.json.side_effect = [{**self.run, **changed}]
            with self.subTest(run=changed), self.assertRaises(ManagedError):
                self.tool.verify_origin(network, self.build)

    def test_expired_or_changed_origin_cannot_be_bootstrapped(self):
        for changed in ({"expired": True}, {"digest": "sha256:" + "b" * 64}, {"size_in_bytes": 1}):
            network = Mock(spec=Network)
            network.json.side_effect = [self.run, {"artifacts": [{**self.artifact, **changed}]}]
            with self.subTest(artifact=changed), self.assertRaises(ManagedError):
                self.tool.verify_origin(network, self.build)
