"""Generate synthetic certificates; never package upstream game/certificate files."""
from pathlib import Path

from epinel_manager.certificates import DOMAINS, openssl


def legacy_ca_pair(root: Path) -> None:
    """A matching, unexpired pair whose CA lacks the required KeyUsage extension."""
    root.mkdir(parents=True, exist_ok=True)
    config = root / "ca.cnf"
    config.write_text("[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ca\n"
                      "[dn]\nCN=Legacy fixture CA\n[ca]\nbasicConstraints=critical,CA:TRUE\nsubjectKeyIdentifier=hash\n")
    openssl("req", "-new", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "365",
            "-config", config, "-keyout", root / "ca.key", "-out", root / "ca.pem")
    openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=global-lobby.nikke-kr.com",
            "-keyout", root / "leaf.key", "-out", root / "leaf.csr")
    extensions = root / "leaf.ext"
    extensions.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                          "extendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid,issuer\nsubjectKeyIdentifier=hash\n"
                          "subjectAltName=" + ",".join("DNS:" + domain for domain in DOMAINS) + "\n")
    openssl("x509", "-req", "-in", root / "leaf.csr", "-CA", root / "ca.pem", "-CAkey", root / "ca.key",
            "-set_serial", "0x1234567890", "-days", "365", "-sha256", "-extfile", extensions, "-out", root / "leaf.pem")
    openssl("pkcs12", "-export", "-inkey", root / "leaf.key", "-in", root / "leaf.pem", "-certfile", root / "ca.pem",
            "-out", root / "site.pfx", "-passout", "pass:")
