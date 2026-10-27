import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from certificate_fixtures import legacy_ca_pair

from epinel_manager.certificates import (BEGIN, END, PEM, TrustTransaction, ensure_certificate,
                                        fingerprint, leaf_certificate, local_certificate,
                                        openssl, patch_bundle, validate)
from epinel_manager.common import ManagedError, atomic_write


class Certificates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixture.name)
        cls.ca = local_certificate(cls.root / "private", cls.root / "site.pfx", lambda x: None)
        (cls.root / "ca.pem").write_bytes(cls.ca)
        legacy_ca_pair(cls.root / "legacy")

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def test_real_chain_and_all_game_hostnames_validate(self):
        self.assertTrue(validate(self.root / "site.pfx", self.root / "ca.pem", 30 * 86400))

    def test_mismatched_authority_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = local_certificate(root / "private", root / "site.pfx", lambda x: None)
            (root / "wrong.pem").write_bytes(other)
            self.assertFalse(validate(self.root / "site.pfx", root / "wrong.pem"))

    def test_matching_unexpired_ca_without_key_usage_is_rejected(self):
        legacy = self.root / "legacy"
        # Reproduce the gap: OpenSSL's ordinary verification accepts this pair.
        self.assertEqual(openssl("verify", "-CAfile", legacy / "ca.pem", "-purpose", "sslserver",
                                 "-verify_hostname", "global-lobby.nikke-kr.com", legacy / "leaf.pem", check=False).returncode, 0)
        self.assertFalse(validate(legacy / "site.pfx", legacy / "ca.pem", 30 * 86400))

    def test_cached_and_pinned_legacy_ca_are_repaired_automatically(self):
        legacy = self.root / "legacy"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (".managed-ca.pem", "myCA.pem"):
                (root / name).write_bytes((legacy / "ca.pem").read_bytes())
            (root / "site.pfx").write_bytes((legacy / "site.pfx").read_bytes())
            messages = []
            network = SimpleNamespace(get=lambda *args, **kwargs: (legacy / "ca.pem").read_bytes())
            ca = ensure_certificate(root, root / "private", network, "a" * 40, messages.append)
            self.assertNotEqual(fingerprint(ca.read_bytes()), fingerprint((legacy / "ca.pem").read_bytes()))
            self.assertTrue(validate(root / "site.pfx", ca, 30 * 86400))
            self.assertTrue(any("non-compliant" in message for message in messages))
            # Later launches keep the valid local CA rather than returning to upstream's broken pair.
            original = ca.read_bytes()
            self.assertEqual(ensure_certificate(root, root / "private", None, None, messages.append).read_bytes(), original)

    def test_expired_certificate_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = openssl("pkcs12", "-in", self.root / "site.pfx", "-passin", "pass:", "-nodes", "-nocerts").stdout
            (root / "key.pem").write_bytes(key)
            (root / "leaf.pem").write_bytes(leaf_certificate(self.root / "site.pfx"))
            openssl("x509", "-in", root / "leaf.pem", "-signkey", root / "key.pem", "-days", "-1", "-out", root / "expired.pem")
            openssl("pkcs12", "-export", "-inkey", root / "key.pem", "-in", root / "expired.pem", "-out", root / "expired.pfx", "-passout", "pass:")
            self.assertFalse(validate(root / "expired.pfx", self.root / "ca.pem"))

    def test_local_ca_survives_leaf_renewal(self):
        before = fingerprint(self.ca)
        with tempfile.TemporaryDirectory() as directory:
            next_ca = local_certificate(self.root / "private", Path(directory) / "new.pfx", lambda x: None)
        self.assertEqual(fingerprint(next_ca), before)

    def test_broken_local_private_key_is_repaired_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca = local_certificate(root / "private", root / "site.pfx", lambda x: None)
            (root / "private/root.key").write_bytes(b"broken key")
            renewed = local_certificate(root / "private", root / "site.pfx", lambda x: None)
            self.assertNotEqual(fingerprint(ca), fingerprint(renewed))
            self.assertTrue(validate(root / "site.pfx", root / "private/root.pem"))

    def test_repairs_legacy_blocks_preserving_unrelated_certificates(self):
        original = b"Vendor prefix\n" + self.ca + b"\nGood SSL Ca\n===============================\n" + self.ca + b"\nDo not lose this suffix\n" + self.ca
        result = patch_bundle(original, self.ca)
        self.assertIn(b"Do not lose this suffix", result)
        self.assertEqual(result.count(b"Good SSL Ca"), 1)
        self.assertEqual(result.count(BEGIN), 1)
        self.assertEqual(len(PEM.findall(result)), 3)
        self.assertEqual(result, patch_bundle(result, self.ca))

    def test_upstream_official_switch_does_not_leave_a_broken_managed_block(self):
        result = patch_bundle(b"Vendor trust\n", self.ca)
        # The unmodified launcher removes everything from its old UI label onward.
        official = result[:result.index(b"Good SSL Ca")]
        self.assertNotIn(BEGIN, official)
        self.assertEqual(patch_bundle(official, self.ca), result)

    def test_incomplete_managed_block_is_not_silently_removed(self):
        with self.assertRaises(ManagedError):
            patch_bundle(b"Vendor trust\n" + BEGIN + b"\npartial", self.ca)

    def test_valid_imported_certificate_needs_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "site.pfx").write_bytes((self.root / "site.pfx").read_bytes())
            (root / "myCA.pem").write_bytes(self.ca)
            ca = ensure_certificate(root, root / "private", None, None, lambda x: None)
            self.assertEqual(fingerprint(ca.read_bytes()), fingerprint(self.ca))

    def test_trust_transaction_restores_all_files_on_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = root / "a", root / "b"
            a.write_bytes(b"original a")
            b.write_bytes(b"original b")
            count = 0
            def writer(path, data, mode=None):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("simulated disk error")
                atomic_write(path, data, mode)
            with self.assertRaises(OSError):
                TrustTransaction(root / "journal", writer).apply({a: b"new a", b: b"new b"})
            self.assertEqual(a.read_bytes(), b"original a")
            self.assertEqual(b.read_bytes(), b"original b")

    def test_symlinked_bundle_keeps_its_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, link = root / "real", root / "link"
            target.write_bytes(b"original")
            link.symlink_to(target)
            tx = TrustTransaction(root / "journal")
            tx.apply({link: b"repaired"})
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_bytes(), b"repaired")
            tx.restore()
            self.assertEqual(target.read_bytes(), b"original")
