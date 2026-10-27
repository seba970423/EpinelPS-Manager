from __future__ import annotations

import hashlib
import os
import re
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path

from .common import ManagedError, atomic_write, read_json, write_json
from .network import RAW, REPO

PEM = re.compile(rb"-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----", re.S)
BEGIN = b"# BEGIN EPINELPS MANAGED CA"
END = b"# END EPINELPS MANAGED CA"
DOMAINS = ("global-lobby.nikke-kr.com", "jp-lobby.nikke-kr.com", "us-lobby.nikke-kr.com",
           "kr-lobby.nikke-kr.com", "sea-lobby.nikke-kr.com", "hmt-lobby.nikke-kr.com",
           "cloud.nikke-kr.com", "aws-na-dr.intlgame.com", "sg-vas.intlgame.com",
           "aws-na.intlgame.com", "common-web.intlgame.com", "li-sg.intlgame.com",
           "data-aws-na.intlgame.com", "na-community.playerinfinite.com",
           "www.jupiterlauncher.com", "na.fleetlogd.com")


def openssl(*arguments, input=None, check=True):
    result = subprocess.run(["openssl", *map(str, arguments)], input=input,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if check and result.returncode:
        # Do not print private material or arbitrary command output.
        raise ManagedError(f"Certificate operation failed (openssl {arguments[0]}).")
    return result


def fingerprint(pem: bytes) -> str:
    matches = PEM.findall(pem)
    if len(matches) != 1:
        raise ManagedError("Expected exactly one public CA certificate.")
    try:
        der = ssl.PEM_cert_to_DER_cert(matches[0].decode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise ManagedError("The CA certificate is malformed.") from exc
    return hashlib.sha256(der).hexdigest()


def leaf_certificate(pfx: Path) -> bytes:
    return openssl("pkcs12", "-in", pfx, "-passin", "pass:", "-clcerts", "-nokeys").stdout


def public_ca_pfx(ca: bytes, cache: Path) -> bytes:
    """Windows ServerSelector expects myCA.pfx; export a PUBLIC-only PKCS#12."""
    identity = fingerprint(ca)
    cache.mkdir(parents=True, exist_ok=True)
    stored = cache / (identity + ".pfx")
    if stored.exists():
        result = openssl("pkcs12", "-in", stored, "-passin", "pass:", "-nokeys", check=False)
        if result.returncode == 0 and fingerprint(result.stdout) == identity:
            return stored.read_bytes()
    with tempfile.TemporaryDirectory(dir=cache) as directory:
        directory = Path(directory)
        atomic_write(directory / "ca.pem", ca)
        openssl("pkcs12", "-export", "-nokeys", "-in", directory / "ca.pem", "-out", directory / "ca.pfx", "-passout", "pass:")
        atomic_write(stored, (directory / "ca.pfx").read_bytes())
    return stored.read_bytes()


def validate(pfx: Path, ca: Path, minimum_seconds=0) -> bool:
    """Validate a strict X.509 chain, times, hostnames and private-key match."""
    try:
        fingerprint(ca.read_bytes())
        with tempfile.TemporaryDirectory(prefix="epinel-cert-check-") as directory:
            directory = Path(directory)
            leaf = directory / "leaf.pem"
            atomic_write(leaf, leaf_certificate(pfx))
            if openssl("x509", "-in", ca, "-checkend", minimum_seconds, "-noout", check=False).returncode:
                return False
            if openssl("x509", "-in", leaf, "-checkend", minimum_seconds, "-noout", check=False).returncode:
                return False
            for hostname in DOMAINS:
                if openssl("verify", "-x509_strict", "-CAfile", ca, "-purpose", "sslserver", "-verify_hostname", hostname, leaf, check=False).returncode:
                    return False
            key = openssl("pkcs12", "-in", pfx, "-passin", "pass:", "-nocerts", "-nodes").stdout
            public_key = openssl("pkey", "-pubout", input=key).stdout
            public_cert = openssl("x509", "-in", leaf, "-pubkey", "-noout").stdout
            return public_key == public_cert
    except (ManagedError, OSError, subprocess.SubprocessError):
        return False


def local_certificate(store: Path, destination: Path, log=print) -> bytes:
    """A local fallback when upstream ships an expired or inconsistent pair."""
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca, key = store / "root.pem", store / "root.key"
    rotate = not (ca.exists() and key.exists())
    if not rotate:
        try:
            rotate = bool(openssl("x509", "-in", ca, "-checkend", 60 * 86400, "-noout", check=False).returncode)
            if not rotate:
                rotate = (openssl("pkey", "-in", key, "-pubout").stdout
                          != openssl("x509", "-in", ca, "-pubkey", "-noout").stdout)
        except ManagedError:
            rotate = True
    if rotate:
        # Recovery also handles termination between these two atomic writes:
        # the next startup detects a missing or mismatched key pair.
        with tempfile.TemporaryDirectory(dir=store, prefix="root-") as temporary:
            temporary = Path(temporary)
            openssl("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-days", "3650",
                    "-keyout", temporary / "root.key", "-out", temporary / "root.pem",
                    "-subj", "/CN=EpinelPS Managed Local CA",
                    "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign")
            atomic_write(key, (temporary / "root.key").read_bytes(), 0o600)
            atomic_write(ca, (temporary / "root.pem").read_bytes(), 0o600)
    with tempfile.TemporaryDirectory(dir=store, prefix="issue-") as directory:
        directory = Path(directory)
        leaf_key, csr, leaf = directory / "leaf.key", directory / "leaf.csr", directory / "leaf.pem"
        extensions = directory / "leaf.ext"
        extensions.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=" + ",".join("DNS:" + domain for domain in DOMAINS) + "\n")
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", leaf_key,
                "-out", csr, "-subj", "/CN=global-lobby.nikke-kr.com")
        openssl("x509", "-req", "-in", csr, "-CA", ca, "-CAkey", key, "-set_serial",
                "0x" + os.urandom(16).hex(), "-out", leaf, "-days", "365", "-sha256", "-extfile", extensions)
        pfx = directory / "site.pfx"
        openssl("pkcs12", "-export", "-inkey", leaf_key, "-in", leaf, "-certfile", ca,
                "-out", pfx, "-passout", "pass:")
        if not validate(pfx, ca, 30 * 86400):
            raise ManagedError("Generated certificates failed validation.")
        atomic_write(destination, pfx.read_bytes())
    log("Issued a valid server certificate using this installation’s private local CA.")
    return ca.read_bytes()


def ensure_certificate(version: Path, private: Path, network, commit: str | None, log=print) -> Path:
    pfx, ca = version / "site.pfx", version / ".managed-ca.pem"
    if ca.exists() and validate(pfx, ca, 30 * 86400):
        log("Server certificate: valid, correct hostnames and matching private key.")
        return ca
    upstream = None
    if commit:
        try:
            upstream = network.get(f"{RAW}/{REPO}/{commit}/ServerSelector/myCA.pem", limit=65536)
        except ManagedError as exc:
            log(f"Could not fetch the pinned upstream CA: {exc}")
    if upstream:
        with tempfile.TemporaryDirectory() as directory:
            check_ca = Path(directory) / "ca.pem"
            atomic_write(check_ca, upstream)
            if validate(pfx, check_ca, 30 * 86400):
                atomic_write(ca, upstream)
                log("Upstream certificate and CA match and are valid.")
                return ca
    # Imported installations may include a valid CA beside the existing server.
    imported = version / "myCA.pem"
    if imported.exists() and validate(pfx, imported, 30 * 86400):
        atomic_write(ca, imported.read_bytes())
        return ca
    log("Upstream certificate is missing, mismatched, non-compliant or nearing expiry; repairing locally.")
    atomic_write(ca, local_certificate(private, pfx, log))
    return ca


def patch_bundle(original: bytes, ca: bytes) -> bytes:
    """Replace only our blocks and the old selector's explicitly marked patch."""
    fingerprint(ca)
    if original.count(BEGIN) != original.count(END):
        raise ManagedError("An incomplete managed CA block needs recovery before this bundle can be changed.")
    legacy_heading = rb"(?:Good SSL Ca\r?\n=+\r?\n)?"
    original = re.sub(legacy_heading + re.escape(BEGIN) + rb"\r?\n.*?" + re.escape(END) + rb"\r?\n?", b"", original, flags=re.S)
    # Unlike upstream's substring truncation, retain all unrelated trailing data.
    original = re.sub(rb"(?m)^Good SSL Ca\r?\n=+\r?\n[ \t]*" + PEM.pattern + rb"[\r\n]*",
                      b"", original, flags=re.S)
    body = original.rstrip(b"\r\n")
    certificate = PEM.findall(ca)[0]
    # Retain upstream's UI label for compatibility, but never use that label as
    # proof of certificate validity. Put it BEFORE our block: upstream's Official
    # switch truncates at this label and must not leave a dangling BEGIN marker.
    result = body + b"\n\nGood SSL Ca\n===============================\n" + BEGIN + b"\n" + certificate + b"\n" + END + b"\n"
    if sum(fingerprint(block) == fingerprint(ca) for block in PEM.findall(result)) < 1:
        raise ManagedError("Certificate bundle verification failed.")
    return result


class TrustTransaction:
    """Journal every target before the first write, including across process death."""
    def __init__(self, directory: Path, writer=atomic_write):
        self.directory, self.writer = directory, writer

    def apply(self, changes: dict[Path, bytes]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        records = []
        for path, content in changes.items():
            path = path.resolve()
            if path.exists() and path.read_bytes() == content:
                continue
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
            previous = self.directory / str(len(records))
            exists = path.exists()
            if exists:
                atomic_write(previous, path.read_bytes())
            records.append({"path": str(path), "exists": exists, "mode": mode, "backup": previous.name})
        write_json(self.directory / "journal.json", records)
        try:
            # Use resolved keys from the journal so symlinked bundle targets stay intact.
            resolved_changes = {path.resolve(): content for path, content in changes.items()}
            for record in records:
                target = Path(record["path"])
                self.writer(target, resolved_changes[target], record["mode"])
                if target.read_bytes() != resolved_changes[target]:
                    raise ManagedError(f"Could not verify repaired certificate file: {target}")
        except BaseException:
            self.restore()
            raise

    def restore(self) -> None:
        for record in reversed(read_json(self.directory / "journal.json", [])):
            path = Path(record["path"])
            if record["exists"]:
                self.writer(path, (self.directory / record["backup"]).read_bytes(), record["mode"])
            else:
                path.unlink(missing_ok=True)

    def commit(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)
