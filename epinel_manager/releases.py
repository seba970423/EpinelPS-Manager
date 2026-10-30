"""Project-owned server pins; successful upstream builds are not approvals."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .common import ManagedError, read_json

REPO = "EpinelPS/EpinelPS"
ARTIFACT = "EpinelPS_linux_x64"
NIGHTLY = "https://nightly.link"
MANAGER_REPO = "seba970423/EpinelPS-Manager"
MANIFEST_URL = f"https://raw.githubusercontent.com/{MANAGER_REPO}/main/server-release.json"
MANIFEST_PATH = Path(__file__).resolve().parent.parent / "server-release.json"


@dataclass(frozen=True)
class Build:
    commit: str
    run_id: int
    artifact_id: int
    digest: str
    size: int
    release: str = ""
    sequence: int = 0
    executable_digest: str = ""

    @property
    def upstream_url(self) -> str:
        return f"{NIGHTLY}/{REPO}/actions/runs/{self.run_id}/{ARTIFACT}.zip"

    @property
    def url(self) -> str:
        if self.release:
            return f"https://github.com/{MANAGER_REPO}/releases/download/{self.release}/{ARTIFACT}.zip"
        return self.upstream_url

    def record(self) -> dict:
        return {"commit": self.commit, "run_id": self.run_id, "artifact_id": self.artifact_id,
                "sha256": self.digest, "size": self.size, "release": self.release,
                "sequence": self.sequence, "executable_sha256": self.executable_digest}


def parse_manifest(document) -> Build:
    """Reject incomplete pins, arbitrary download URLs and unsupported formats."""
    if not isinstance(document, dict):
        raise ManagedError("The project's server pin must be a JSON object.")
    if type(document.get("schema")) is not int or document["schema"] != 1:
        raise ManagedError("This server pin needs a newer manager. Update EpinelPS Managed first.")
    if document.get("channel") != "stable" or document.get("upstream_repository") != REPO:
        raise ManagedError("The project's server pin has an unsupported channel or upstream repository.")
    minimum = document.get("minimum_manager_version", "")
    if not isinstance(minimum, str) or not re.fullmatch(r"\d+\.\d+\.\d+", minimum):
        raise ManagedError("The server pin has an invalid minimum manager version.")
    if tuple(map(int, minimum.split("."))) > tuple(map(int, __version__.split("."))):
        raise ManagedError(f"This server pin requires manager {minimum} or later. Update the manager first.")
    release = document.get("release", "")
    sequence = document.get("sequence")
    if (not isinstance(release, str) or not re.fullmatch(r"server-\d{4}\.\d{2}\.\d{2}\.\d+", release)
            or type(sequence) is not int or sequence < 1):
        raise ManagedError("The server pin has an invalid release identity.")
    server = document.get("server")
    if not isinstance(server, dict):
        raise ManagedError("The server pin is missing build metadata.")
    for name, length in (("commit", 40), ("sha256", 64), ("executable_sha256", 64)):
        value = server.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{" + str(length) + r"}", value):
            raise ManagedError(f"The server pin has an invalid {name}.")
    for name in ("run_id", "artifact_id", "size"):
        if type(server.get(name)) is not int or server[name] <= 0:
            raise ManagedError(f"The server pin has an invalid {name}.")
    if server["size"] > 1024 ** 3:
        raise ManagedError("The pinned archive exceeds the download size limit.")
    return Build(server["commit"], server["run_id"], server["artifact_id"], server["sha256"],
                 server["size"], release, sequence, server["executable_sha256"])


def bundled_release() -> Build:
    return parse_manifest(read_json(MANIFEST_PATH))
