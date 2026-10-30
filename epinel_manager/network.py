from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from . import __version__
from .common import ManagedError
from .releases import ARTIFACT, MANIFEST_URL, NIGHTLY, REPO, Build, parse_manifest

WORKFLOW = ".github/workflows/dotnet-desktop.yml"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
RETRY_ERRORS = (OSError, urllib.error.URLError, http.client.HTTPException)


class Network:
    """Socket inactivity limits, rather than a timeout for the whole download."""
    def __init__(self, log=print, *, timeout=30, attempts=4, opener=None):
        self.log, self.timeout, self.attempts = log, timeout, attempts
        self.opener = opener or urllib.request.urlopen

    def open(self, url, headers=None):
        request = urllib.request.Request(url, headers={
            "User-Agent": f"EpinelPS-Managed/{__version__}",
            "Accept": "application/vnd.github+json" if url.startswith(API) else "*/*",
            "Accept-Encoding": "identity",
            **(headers or {}),
        })
        return self.opener(request, timeout=self.timeout)

    def get(self, url: str, limit=8 * 1024 * 1024) -> bytes:
        for attempt in range(self.attempts):
            try:
                with self.open(url) as response:
                    data = response.read(limit + 1)
                    if len(data) > limit:
                        raise ManagedError("The upstream response is unexpectedly large.")
                    return data
            except RETRY_ERRORS as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                    raise ManagedError(f"Upstream returned HTTP {exc.code}.") from exc
                if attempt + 1 == self.attempts:
                    raise ManagedError(f"Cannot reach upstream: {exc}") from exc
                self.log(f"Network interrupted; retrying ({attempt + 1}/{self.attempts - 1})…")
                time.sleep(min(2 ** attempt, 8))
        raise AssertionError("unreachable")

    def json(self, url):
        try:
            return json.loads(self.get(url))
        except ValueError as exc:
            raise ManagedError("Upstream did not return valid JSON.") from exc

    def latest(self) -> Build:
        # The numeric run URL is immutable. A moving /main download can fetch a
        # different commit from the one displayed or whose CA was selected.
        runs = self.json(f"{API}/repos/{REPO}/actions/runs?branch=main&status=success&event=push&per_page=40")
        for run in runs.get("workflow_runs", []):
            if (run.get("path") != WORKFLOW or run.get("head_branch") != "main"
                    or run.get("event") != "push" or run.get("conclusion") != "success"):
                continue
            commit = run.get("head_sha", "")
            if not re.fullmatch(r"[a-f0-9]{40}", commit):
                continue
            metadata = self.json(f"{API}/repos/{REPO}/actions/runs/{int(run['id'])}/artifacts?per_page=100")
            for artifact in metadata.get("artifacts", []):
                if artifact.get("name") != ARTIFACT or artifact.get("expired"):
                    continue
                digest = artifact.get("digest", "")
                if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                    raise ManagedError("GitHub did not publish a SHA-256 digest; keeping the installed build.")
                origin = artifact.get("workflow_run", {})
                if origin.get("head_sha", commit) != commit:
                    raise ManagedError("The artifact and workflow commit do not match.")
                return Build(commit, int(run["id"]), int(artifact["id"]), digest[7:], int(artifact["size_in_bytes"]))
        raise ManagedError("No downloadable successful Linux build is available.")

    def approved(self) -> Build:
        # Only the project-owned pin decides the stable version. Its ZIP is
        # published to this project's Releases after CI, never fetched from main.
        try:
            document = json.loads(self.get(MANIFEST_URL, limit=65536))
        except ValueError as exc:
            raise ManagedError("The project did not return a valid server pin; keeping the installed build.") from exc
        return parse_manifest(document)

    def download(self, build: Build, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(".part")
        if destination.exists() and sha256(destination) == build.digest:
            return destination
        for attempt in range(self.attempts):
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > build.size:
                partial.unlink()
                offset = 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with self.open(build.url, headers) as response:
                    status = response.status
                    if status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if not match or int(match[1]) != offset or int(match[3]) != build.size:
                            partial.unlink(missing_ok=True)
                            raise OSError("Invalid resume response")
                    elif status == 200:
                        offset = 0  # This server does not support resuming.
                    else:
                        raise OSError(f"Unexpected HTTP status {status}")
                    start, last_report, done = time.monotonic(), 0.0, offset
                    with partial.open("ab" if offset else "wb") as output:
                        while True:
                            # read1 exposes partial progress before a large buffer fills.
                            chunk = response.read1(128 * 1024)
                            if not chunk:
                                break
                            done += len(chunk)
                            if done > build.size:
                                raise ManagedError("Download exceeds the size published by GitHub.")
                            output.write(chunk)
                            now = time.monotonic()
                            if now - last_report >= 2:
                                speed = (done - offset) / max(now - start, .01)
                                percent = done * 100 / max(build.size, 1)
                                self.log(f"Downloading {percent:.1f}% · {done / 1048576:.1f}/{build.size / 1048576:.1f} MiB · {speed / 1048576:.2f} MiB/s")
                                last_report = now
                        output.flush()
                        os.fsync(output.fileno())
                if partial.stat().st_size != build.size:
                    raise OSError("The connection ended before the download completed")
                if sha256(partial) != build.digest:
                    partial.unlink()
                    raise OSError("SHA-256 verification failed; discarding the download")
                os.replace(partial, destination)
                self.log("Download complete; GitHub SHA-256 verified.")
                return destination
            except RETRY_ERRORS as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    if exc.code == 416:
                        if partial.exists() and partial.stat().st_size == build.size and sha256(partial) == build.digest:
                            os.replace(partial, destination)
                            return destination
                        partial.unlink(missing_ok=True)
                    elif exc.code not in (408, 429, 500, 502, 503, 504):
                        raise ManagedError(f"Download returned HTTP {exc.code}; installed files are unchanged.") from exc
                if attempt + 1 == self.attempts:
                    raise ManagedError(f"Download failed after retries: {exc}") from exc
                self.log(f"Download interrupted; retrying ({attempt + 1}/{self.attempts - 1})…")
                time.sleep(min(2 ** attempt, 8))
        raise AssertionError("unreachable")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract(archive: Path, destination: Path) -> None:
    try:
        _extract(archive, destination)
    except (zipfile.BadZipFile, EOFError, RuntimeError) as exc:
        raise ManagedError("The update archive is corrupt or unsupported.") from exc


def _extract(archive: Path, destination: Path) -> None:
    """Reject traversal, links, duplicate entries and oversized archives."""
    with zipfile.ZipFile(archive) as zipped:
        seen, size = set(), 0
        for entry in zipped.infolist():
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                    or not path.parts or str(path) in seen or stat.S_ISLNK(mode)
                    or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))):
                raise ManagedError("The update archive contains an unsafe path or file.")
            seen.add(str(path))
            size += entry.file_size
            if size > 3 * 1024 ** 3 or len(seen) > 30000:
                raise ManagedError("The update archive exceeds extraction limits.")
        destination.mkdir(parents=True, exist_ok=False)
        try:
            for entry in zipped.infolist():
                target = destination / entry.filename
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zipped.open(entry) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(0o700 if entry.filename == "EpinelPS" else 0o600)
            required = ("EpinelPS", "site.pfx", "appsettings.json", "gameconfig.json", "gameversion.json", "log4net.config")
            if not all((destination / name).is_file() for name in required):
                raise ManagedError("The archive is missing required server files.")
        except BaseException:
            shutil.rmtree(destination)
            raise
