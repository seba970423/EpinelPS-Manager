import ssl
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from certificate_fixtures import legacy_ca_pair

from epinel_manager.certificates import fingerprint, leaf_certificate, local_certificate, openssl, public_ca_pfx
from epinel_manager.common import ManagedError
from epinel_manager.connection import host_addresses, patch_hosts, tls_probe, wine_prefix
from epinel_manager.locations import copied_location, find_game, locate_installation


class PathsAndRouting(unittest.TestCase):
    def test_dolphin_path_uri_quotes_and_executable_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "friend's game root"
            game = root / "NIKKE/game"
            (game / "nikke_Data/Plugins/x86_64").mkdir(parents=True)
            exe = game / "nikke.exe"
            exe.write_bytes(b"fixture")
            for text in (str(root), str(game), str(exe), root.as_uri(), '"' + str(game) + '"'):
                self.assertEqual(find_game(copied_location(text)), game)

    def test_network_uri_and_multiple_pasted_locations_are_rejected(self):
        for text in ("file://other-computer/game", "/one\n/two"):
            with self.subTest(text=text), self.assertRaises(ManagedError):
                copied_location(text)

    def test_nested_installation_scan_stays_inside_selected_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            selected = base / "selected installation"
            game = selected / "Client/build/game"
            (game / "nikke_Data/Plugins/x86_64").mkdir(parents=True)
            (game / "nikke.exe").write_bytes(b"game fixture")
            launcher = selected / "tools/local launcher"
            (launcher / "intl_service").mkdir(parents=True)
            (launcher / "intl_service/cacert.pem").write_bytes(b"launcher trust")
            outside = base / "another installation"
            (outside / "Launcher").mkdir(parents=True)
            (selected / "unrelated-link").symlink_to(outside, target_is_directory=True)
            installation = locate_installation(selected)
            self.assertEqual(installation.root, selected)
            self.assertEqual(installation.game, game)
            self.assertEqual(installation.launchers, (launcher,))

    def test_multiple_nested_games_require_a_more_specific_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("one", "two"):
                game = root / name / "game"
                (game / "nikke_Data/Plugins/x86_64").mkdir(parents=True)
                (game / "nikke.exe").write_bytes(b"game fixture")
            with self.assertRaisesRegex(ManagedError, "multiple NIKKE installations"):
                locate_installation(root)

    def test_launcher_log_directories_are_not_detected_as_launchers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "NIKKE/game"
            (game / "nikke_Data/Plugins/x86_64").mkdir(parents=True)
            (game / "nikke.exe").write_bytes(b"game fixture")
            launcher = root / "Launcher"
            log = launcher / "log/launcher"
            log.mkdir(parents=True)
            # Old releases may have written public CA files into that false match.
            (log / "myCA.pem").write_bytes(b"old misplaced public certificate")
            self.assertEqual(locate_installation(root).launchers, (launcher,))

    def test_hosts_repair_preserves_other_names_and_is_idempotent(self):
        original = b"127.0.0.1 localhost\n::1 localhost ip6-localhost\n192.0.2.44 li-sg.intlgame.com unrelated.local # keep comment\n"
        result = patch_hosts(original)
        self.assertEqual(host_addresses(result, "li-sg.intlgame.com"), ["127.0.0.1"])
        self.assertEqual(host_addresses(result, "unrelated.local"), ["192.0.2.44"])
        self.assertIn(b"keep comment", result)
        self.assertIn(b"::1 localhost ip6-localhost", result)
        self.assertEqual(result, patch_hosts(result))
        self.assertNotIn(b"127.0.0.1 cloud.nikke-kr.com", result)

    def test_windows_prefix_is_derived_from_selected_drive_c(self):
        game = Path("/home/user/Bottles/prefixes/bottle-id/drive_c/NIKKE/NIKKE/game")
        self.assertEqual(wine_prefix(game), Path("/home/user/Bottles/prefixes/bottle-id"))


class LiveClientTrust(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.ca = local_certificate(cls.root / "private", cls.root / "site.pfx", lambda line: None)
        (cls.root / "ca.pem").write_bytes(cls.ca)
        (cls.root / "leaf.pem").write_bytes(leaf_certificate(cls.root / "site.pfx"))
        (cls.root / "leaf.key").write_bytes(openssl("pkcs12", "-in", cls.root / "site.pfx", "-passin", "pass:", "-nocerts", "-nodes").stdout)
        cls.expected = fingerprint((cls.root / "leaf.pem").read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def server(self, certificate_root=None):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args):
                pass
            def do_GET(self):
                body = b"EpinelPS live SDK TLS fixture"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                # Deliberately keep TLS alive after the complete HTTP body.
                # A health check waiting for EOF incorrectly times out here.
                self.close_connection = False
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        certificate_root = certificate_root or self.root
        context.load_cert_chain(certificate_root / "leaf.pem", certificate_root / "leaf.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_health_check_finishes_without_waiting_for_tls_connection_close(self):
        server = self.server()
        start = time.monotonic()
        tls_probe(self.root / "ca.pem", "global-lobby.nikke-kr.com", self.expected, port=server.server_port)
        self.assertLess(time.monotonic() - start, 2)

    def test_actual_game_bundle_with_wrong_ca_is_rejected(self):
        server = self.server()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrong = local_certificate(root / "private", root / "site.pfx", lambda line: None)
            (root / "game-bundle.pem").write_bytes(wrong)
            with self.assertRaises(ssl.SSLCertVerificationError):
                tls_probe(root / "game-bundle.pem", "li-sg.intlgame.com", self.expected, port=server.server_port, check_http=False)

    def test_live_probe_requires_ca_key_usage_even_on_older_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_ca_pair(root)
            server = self.server(root)
            expected = fingerprint((root / "leaf.pem").read_bytes())
            with self.assertRaisesRegex(ssl.SSLCertVerificationError, "key usage"):
                tls_probe(root / "ca.pem", "global-lobby.nikke-kr.com", expected, port=server.server_port)

    def test_selector_public_pfx_is_cached_and_contains_no_private_key(self):
        cache = self.root / "public-cache"
        first = public_ca_pfx(self.ca, cache)
        second = public_ca_pfx(self.ca, cache)
        self.assertEqual(first, second)
        file = cache / (fingerprint(self.ca) + ".pfx")
        result = openssl("pkcs12", "-in", file, "-passin", "pass:", "-nodes").stdout
        self.assertEqual(fingerprint(result), fingerprint(self.ca))
        self.assertNotIn(b"PRIVATE KEY", result)
