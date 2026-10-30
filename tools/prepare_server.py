#!/usr/bin/env python3
"""Prepare the exact pinned server for CI; never touches an installed game/save."""
import argparse
from dataclasses import replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from epinel_manager.certificates import ensure_certificate
from epinel_manager.common import ManagedError
from epinel_manager.network import API, ARTIFACT, REPO, WORKFLOW, Network, extract, sha256
from epinel_manager.releases import bundled_release


def verify_origin(network, build):
    run = network.json(f"{API}/repos/{REPO}/actions/runs/{build.run_id}")
    if (run.get("head_sha") != build.commit or run.get("path") != WORKFLOW
            or run.get("head_branch") != "main" or run.get("event") != "push"
            or run.get("conclusion") != "success"):
        raise ManagedError("The pinned workflow does not match the approved source commit.")
    metadata = network.json(f"{API}/repos/{REPO}/actions/runs/{build.run_id}/artifacts?per_page=100")
    matching = [a for a in metadata.get("artifacts", []) if a.get("id") == build.artifact_id]
    if len(matching) != 1:
        raise ManagedError("The pinned upstream artifact is unavailable. Use the project's archived release.")
    artifact = matching[0]
    if (artifact.get("name") != ARTIFACT or artifact.get("expired")
            or artifact.get("size_in_bytes") != build.size or artifact.get("digest") != "sha256:" + build.digest
            or artifact.get("workflow_run", {}).get("head_sha") != build.commit):
        raise ManagedError("The upstream artifact does not match the approved size, digest and commit.")


def prepare(output: Path, archive: Path | None = None):
    build = bundled_release()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    network = Network()
    if archive is None:
        archive = output / (ARTIFACT + ".zip")
        try:
            network.download(build, archive)
        except ManagedError:
            # Bootstrap a NEW approval before its release is published. This
            # fallback exists only in the maintainer tool, never normal startup.
            verify_origin(network, build)
            network.download(replace(build, release=""), archive)
    if archive.stat().st_size != build.size or sha256(archive) != build.digest:
        raise ManagedError("The pinned test archive failed its size or SHA-256 check.")
    server = output / "server"
    extract(archive, server)
    if sha256(server / "EpinelPS") != build.executable_digest:
        raise ManagedError("The pinned test executable failed its SHA-256 check.")
    ensure_certificate(server, output / "test-certificates", network, build.commit)
    return build, archive.resolve(), server / "EpinelPS"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path, help="Use an already downloaded archive, still verifying both digests")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    build, archive, executable = prepare(args.output, args.archive)
    print(f"Prepared {build.release}: {build.commit}")
    if args.github_output:
        with args.github_output.open("a") as output:
            output.write(f"archive={archive}\nexecutable={executable}\nrelease={build.release}\ncommit={build.commit}\n")


if __name__ == "__main__":
    try:
        main()
    except (ManagedError, OSError) as exc:
        raise SystemExit(str(exc))
