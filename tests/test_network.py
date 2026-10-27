import hashlib
import io
import socket
import stat
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from epinel_manager.common import ManagedError
from epinel_manager.network import Build, Network, extract


class Downloads(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "update.zip"
        self.body = bytes(range(256)) * 2048
        self.requests = []
        self.mode = "normal"
        test = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def do_GET(self):
                offset = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
                test.requests.append(offset)
                if test.mode == "404":
                    self.send_error(404)
                    return
                if offset == len(test.body):
                    self.send_error(416)
                    return
                if test.mode == "ignore-range":
                    offset = 0
                self.send_response(206 if offset else 200)
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(test.body)-1}/{len(test.body)}")
                if test.mode != "unknown-length":
                    self.send_header("Content-Length", str(len(test.body) - offset))
                self.send_header("Connection", "close")
                self.end_headers()
                if test.mode == "disconnect" and len(test.requests) == 1:
                    self.wfile.write(test.body[:131072])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                else:
                    self.wfile.write(test.body[offset:])

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(thread.join, 2)
        self.build = Build("a" * 40, 1, 1, hashlib.sha256(self.body).hexdigest(), len(self.body))
        def opener(request, timeout):
            replacement = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/download", headers=dict(request.header_items()))
            return urllib.request.urlopen(replacement, timeout=timeout)
        self.net = Network(lambda x: None, timeout=2, opener=opener)
        self.sleep = patch("epinel_manager.network.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_streams_without_content_length(self):
        self.mode = "unknown-length"
        self.net.download(self.build, self.path)
        self.assertEqual(self.path.read_bytes(), self.body)

    def test_interrupted_download_resumes(self):
        self.mode = "disconnect"
        self.net.download(self.build, self.path)
        self.assertEqual(self.requests, [0, 131072])
        self.assertEqual(self.path.read_bytes(), self.body)

    def test_server_ignoring_range_restarts_cleanly(self):
        self.mode = "ignore-range"
        self.path.with_suffix(".part").write_bytes(self.body[:100])
        self.net.download(self.build, self.path)
        self.assertEqual(self.path.read_bytes(), self.body)

    def test_complete_partial_416_is_verified(self):
        self.path.with_suffix(".part").write_bytes(self.body)
        self.net.download(self.build, self.path)
        self.assertEqual(self.path.read_bytes(), self.body)

    def test_bad_digest_never_replaces_installed_file(self):
        self.path.write_bytes(b"existing verified download")
        bad = Build("a" * 40, 1, 1, "0" * 64, len(self.body))
        with self.assertRaises(ManagedError):
            self.net.download(bad, self.path)
        self.assertEqual(self.path.read_bytes(), b"existing verified download")

    def test_http_error_body_is_not_installed(self):
        self.mode = "404"
        with self.assertRaises(ManagedError):
            self.net.download(self.build, self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.requests), 1)

    def test_run_and_ca_are_pinned_to_same_commit(self):
        responses = [
            {"workflow_runs": [
                {"path": ".github/workflows/dotnet-desktop.yml", "head_sha": "b" * 40, "id": 3, "head_branch": "main", "event": "pull_request", "conclusion": "success"},
                {"path": ".github/workflows/dotnet-desktop.yml", "head_sha": "a" * 40, "id": 2, "head_branch": "main", "event": "push", "conclusion": "success"}]},
            {"artifacts": [{"name": "EpinelPS_linux_x64", "id": 7, "expired": False, "size_in_bytes": 5, "digest": "sha256:" + "c" * 64, "workflow_run": {"head_sha": "a" * 40}}]},
        ]
        with patch.object(self.net, "json", side_effect=responses):
            build = self.net.latest()
        self.assertEqual(build.commit, "a" * 40)
        self.assertIn("/actions/runs/2/", build.url)


class Extraction(unittest.TestCase):
    def test_corrupt_archive_has_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.zip").write_bytes(b"not a ZIP")
            with self.assertRaises(ManagedError):
                extract(root / "bad.zip", root / "stage")

    def test_traversal_and_symlinks_rejected(self):
        for name, mode in [("../escape", stat.S_IFREG | 0o644), ("site.pfx", stat.S_IFLNK | 0o777), ("/absolute", stat.S_IFREG | 0o644)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with zipfile.ZipFile(root / "bad.zip", "w") as archive:
                    entry = zipfile.ZipInfo(name)
                    entry.external_attr = mode << 16
                    archive.writestr(entry, b"bad")
                with self.assertRaises(ManagedError):
                    extract(root / "bad.zip", root / "stage")
                self.assertFalse((root / "stage").exists())

    def test_missing_package_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root / "bad.zip", "w") as archive:
                archive.writestr("EpinelPS", b"incomplete")
            with self.assertRaises(ManagedError):
                extract(root / "bad.zip", root / "stage")
            self.assertFalse((root / "stage").exists())
