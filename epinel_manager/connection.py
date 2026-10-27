"""Local game routing and verification using the game's actual trust bundles."""
from __future__ import annotations

import hashlib
import http.client
import socket
import ssl
from pathlib import Path

from .certificates import DOMAINS
from .common import ManagedError
from .locations import install_root

ROUTE_DOMAINS = tuple(domain for domain in DOMAINS if domain not in {"cloud.nikke-kr.com", "na.fleetlogd.com"})
PROBE_DOMAINS = ("li-sg.intlgame.com", "aws-na.intlgame.com", "global-lobby.nikke-kr.com")
SELECTOR_START = "# begin ServerSelector entries"
SELECTOR_END = "# end ServerSelector entries"
MANAGED_START = "# begin EpinelPS Managed entries"
MANAGED_END = "# end EpinelPS Managed entries"


def wine_prefix(game: Path) -> Path | None:
    for parent in (game, *game.parents):
        if parent.name.lower() == "drive_c":
            return parent.parent
    root = install_root(game)
    candidate = root / "wine_prefix"
    return candidate if (candidate / "drive_c").is_dir() else None


def bundles(game: Path, launchers: list[Path]) -> list[Path]:
    directories = [game / "nikke_Data/Plugins/x86_64"]
    root = install_root(game)
    for base in [root, *launchers]:
        directories.extend([base / "intl_service", base / "Launcher/intl_service", base / "launcher/intl_service"])
    result = []
    for directory in directories:
        for name in ("intl_cacert.pem", "cacert.pem"):
            path = directory / name
            if path.is_file() and path.resolve() not in result:
                result.append(path.resolve())
    return result


def selector_directories(game: Path, launchers: list[Path]) -> list[Path]:
    result = set(launchers)
    root = install_root(game)
    if any((root / name).exists() for name in ("ServerSelector.Desktop.exe", "myCA.pem", "myCA.pfx")):
        result.add(root)
    return sorted(result)


def patch_hosts(original: bytes) -> bytes:
    try:
        text = original.decode("utf-8")
    except UnicodeError as error:
        raise ManagedError("The hosts file is not UTF-8; it was left unchanged.") from error
    markers = {SELECTOR_START, SELECTOR_END, MANAGED_START, MANAGED_END}
    retained = []
    for line in text.splitlines():
        if line.strip() in markers:
            continue
        body, separator, comment = line.partition("#")
        fields = body.split()
        if len(fields) >= 2:
            remaining = [name for name in fields[1:] if name.lower() not in ROUTE_DOMAINS]
            if len(remaining) != len(fields[1:]):
                if remaining:
                    line = fields[0] + "\t" + " ".join(remaining) + (" #" + comment if separator else "")
                elif separator:
                    line = "#" + comment
                else:
                    continue
        retained.append(line)
    block = "\n".join([SELECTOR_START, MANAGED_START,
                       *("127.0.0.1 " + domain for domain in ROUTE_DOMAINS),
                       MANAGED_END, SELECTOR_END])
    return ("\n".join(retained).rstrip("\r\n") + "\n\n" + block + "\n").encode()


def host_addresses(content: bytes, name: str) -> list[str]:
    result = []
    for line in content.decode("utf-8").splitlines():
        fields = line.partition("#")[0].split()
        if len(fields) > 1 and name.lower() in [value.lower() for value in fields[1:]]:
            result.append(fields[0])
    return result


def tls_probe(cafile: Path, hostname: str, expected_leaf: str, *, port=443, check_http=True) -> None:
    context = ssl.create_default_context(cafile=str(cafile))
    # Match modern Python defaults on older installations too. Invalid CA
    # structure must be repaired, not accepted on only some machines.
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    context.set_alpn_protocols(["http/1.1"])
    with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
        with context.wrap_socket(raw, server_hostname=hostname) as connection:
            if hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest() != expected_leaf:
                raise ManagedError("The server is presenting a different certificate than its configured site.pfx.")
            if check_http:
                connection.sendall((f"GET / HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n").encode())
                # Parse HTTP framing instead of waiting indefinitely for TLS EOF.
                # Kestrel can finish a body before it finishes closing TLS.
                with http.client.HTTPResponse(connection) as response:
                    response.begin()
                    body = response.read(65536)
                    if response.status != 200 or b"EpinelPS" not in body:
                        raise ManagedError(f"Unexpected server response: HTTP {response.status}.")
