from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import platform
import re
import shutil
import signal
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .certificates import (TrustTransaction, ensure_certificate, fingerprint,
                           leaf_certificate, patch_bundle, public_ca_pfx, validate)
from .common import ManagedError, atomic_write, read_json, write_json
from .network import Network, extract, sha256

from .locations import (LOCATION_REVISION, confirmed_installation, copied_location,
                        find_game, installation_for_game, locate_installation)
from .ports import PortConflict, check_ports
from .connection import (PROBE_DOMAINS, ROUTE_DOMAINS, bundles, host_addresses,
                         patch_hosts, selector_directories, tls_probe, wine_prefix)

STARTUP_REVISION = 2


class InstalledReleaseMismatch(ManagedError):
    """An already active approved executable changed; do not silently run it."""


def pick_directory(title: str, start: Path | None = None) -> Path | None:
    start = start or Path.home()
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        for program, arguments in (
            ("kdialog", ["--title", title, "--getexistingdirectory", str(start)]),
            ("zenity", ["--file-selection", "--directory", "--title=" + title, "--filename=" + str(start) + "/"]),
        ):
            if shutil.which(program):
                result = subprocess.run([program, *arguments], capture_output=True, text=True)
                return Path(result.stdout.strip()).expanduser().resolve() if result.returncode == 0 and result.stdout.strip() else None
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            result = filedialog.askdirectory(title=title, initialdir=start)
            root.destroy()
            return Path(result).resolve() if result else None
        except Exception:
            # A headless Tk build or absent display must not suppress the terminal picker.
            pass
    if sys.stdin.isatty():
        print(title + " (paste a folder path, or leave blank to cancel):")
        value = input("> ").strip().strip('"')
        return Path(value).expanduser().resolve() if value else None
    return None


def scan_directories(roots: list[Path], max_depth=4, budget=12000):
    """Bounded discovery in common install locations; no full-drive scan."""
    ignored = {"node_modules", ".git", ".cache", ".epinel", "cache", "drive_c", "wwwroot", "nikke_Data"}
    pending, seen = [(root, 0) for root in roots if root.is_dir()], set()
    while pending and budget > 0:
        directory, depth = pending.pop()
        if directory in seen or directory.is_symlink():
            continue
        seen.add(directory)
        budget -= 1
        yield directory
        if depth >= max_depth:
            continue
        try:
            pending.extend((child, depth + 1) for child in directory.iterdir()
                           if child.name not in ignored and child.is_dir() and not child.is_symlink())
        except OSError:
            continue


def game_directory(directory: Path) -> Path | None:
    return find_game(directory)


def process_using(executable: Path) -> bool:
    executable = executable.resolve()
    for process in Path("/proc").glob("[0-9]*/exe"):
        try:
            if process.resolve() == executable:
                return True
        except OSError:
            continue
    return False


def process_identity(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError, ValueError):
        return None


def is_owned_startup(pid: int, identity: str, executable: Path) -> bool:
    if process_identity(pid) != identity:
        return False
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        expected = os.fsencode(str(executable))
        return expected in command[:2]
    except OSError:
        return False


def game_is_running() -> bool:
    for entry in Path("/proc").glob("[0-9]*/comm"):
        try:
            name = entry.read_text().strip().lower()
            if name in {"nikke.exe", "nikke_launcher.", "nikke_launcher", "nikke_launcher.exe", "epinelpslaunche", "serverselector."}:
                return True
        except OSError:
            continue
    return False


def sqlite_backup(source: Path, destination: Path) -> None:
    # The SQLite backup API includes committed WAL content in a consistent snapshot.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5) as original:
        with sqlite3.connect(destination) as backup:
            original.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ManagedError("The SQLite backup failed its integrity check.")
    destination.chmod(0o600)


class Manager:
    def __init__(self, root: Path, *, network=None, log=print):
        self.root = root.resolve()
        self.private = self.root / ".epinel"
        self.private.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.private.chmod(0o700)
        self.data = self.private / "data"
        self.data.mkdir(exist_ok=True)
        self.versions = self.private / "versions"
        self.versions.mkdir(exist_ok=True)
        self.log = log
        self.network = network or Network(log)
        self.settings_path, self.state_path = self.private / "settings.json", self.private / "state.json"
        self.settings = {"auto_update": True, "update_channel": "stable", "startup_timeout": 900,
                         "launcher_dirs": [], **read_json(self.settings_path)}
        self.state = read_json(self.state_path)
        self.child = None
        self.ready = False
        self.selected_build = None
        self.adopt_stable = False

    def save_settings(self):
        write_json(self.settings_path, self.settings)

    def active(self) -> Path | None:
        name = self.state.get("active")
        if not name or not re.fullmatch(r"[a-f0-9]{40}|imported", name):
            return None
        path = self.versions / name
        return path if (path / "EpinelPS").is_file() else None

    def lock(self):
        self.lockfile = (self.private / "manager.lock").open("a+")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ManagedError("EpinelPS Managed is already running in this folder.") from exc

    def discovery_roots(self):
        home = Path.home()
        return list(dict.fromkeys([self.root.parent, home / "Desktop", home / "Downloads",
                                  home / "Games", home / "games", home / "Applications",
                                  home / ".local/share/EpinelPS", home / ".local/share/EpinelPSLauncher"]))

    def import_existing(self, source: Path):
        source = source.expanduser().resolve()
        if self.active():
            raise ManagedError("An installation is already imported; its saves have not been replaced.")
        if not (source / "EpinelPS").is_file() or not (source / "gamecommon.json").is_file():
            raise ManagedError("Choose the existing server folder containing EpinelPS and gamecommon.json.")
        if source == self.root or self.private in source.parents:
            raise ManagedError("Choose your original server folder, outside this managed bundle.")
        if process_using(source / "EpinelPS"):
            raise ManagedError("Close the existing EpinelPS server before importing its saves.")
        self.log(f"Importing the existing server and saves from {source}")
        staging = self.versions / ".importing"
        shutil.rmtree(staging, ignore_errors=True)
        excluded = {"db.json", "epinelps.db", "epinelps.db-wal", "epinelps.db-shm", "cache", "logs", ".git", ".epinel", "Server_Update", "Server.zip"}
        shutil.copytree(source, staging, ignore=lambda directory, names: excluded.intersection(names) if Path(directory) == source else [])
        try:
            configuration = read_json(source / "appsettings.json", {"ConnectionStrings": {}})
            connections = configuration.setdefault("ConnectionStrings", {})
            connection_type = connections.get("EpinelPSConnectionType", "sqlite").lower()
            if connection_type != "sqlite":
                raise ManagedError("This build supports managed save backups for SQLite installations. Your original database is unchanged.")
            raw = connections.get("EpinelPSConnection", 'Data Source="(startupDirectory)/epinelps.db"')
            match = re.search(r'(?:Data Source|Filename)\s*=\s*(?:"([^"]+)"|([^;]+))', raw, re.I)
            if not match:
                raise ManagedError("Cannot determine the existing SQLite database path.")
            database = Path((match[1] or match[2]).replace("(startupDirectory)", str(source)))
            if not database.is_absolute():
                database = source / database
            if database.exists():
                sqlite_backup(database.resolve(), self.data / "epinelps.db")
            for name in ("db.json", "gamecommon.json"):
                if (source / name).exists():
                    shutil.copy2(source / name, self.data / name)
            connections["EpinelPSConnectionType"] = "sqlite"
            escaped_database = str(self.data / "epinelps.db").replace('"', '""')
            connections["EpinelPSConnection"] = f'Data Source="{escaped_database}"'
            write_json(self.data / "appsettings.json", configuration)
            if (source / "cache").is_dir():
                self.log("Importing cached assets (using hard links where possible)…")
                def copy_cache(src, dst):
                    try:
                        os.link(src, dst)
                    except OSError:
                        shutil.copy2(src, dst)
                    return dst
                shutil.copytree(source / "cache", self.data / "cache", copy_function=copy_cache, dirs_exist_ok=True)
            destination = self.versions / "imported"
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(staging, destination)
            self.link_data(destination)
            self.state = {"active": "imported", "previous": None, "rejected": []}
            write_json(self.state_path, self.state)
            self.settings["imported_from"] = str(source)
            self.save_settings()
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def setup(self, explicit_game=None, explicit_launchers=None):
        if not self.active():
            directories = list(scan_directories(self.discovery_roots()))
            servers = [path for path in directories if path != self.root
                       and (path / "EpinelPS").is_file() and (path / "gamecommon.json").is_file()]
            source = servers[0] if len(servers) == 1 else pick_directory("Choose your existing EpinelPS server folder")
            if not source:
                raise ManagedError("Setup cancelled. The original server and saves are unchanged.")
            self.import_existing(source)
        old_game = self.settings.get("game_dir", "")
        reselect = (self.settings.get("location_revision", 0) < LOCATION_REVISION
                    or not old_game or not game_directory(Path(old_game)))
        if explicit_game:
            installation = locate_installation(copied_location(str(explicit_game)))
        elif reselect:
            installation = confirmed_installation(old_game)
        else:
            remembered_root = self.settings.get("install_dir")
            installation = installation_for_game(Path(old_game), Path(remembered_root) if remembered_root else None)
        selected = installation.game
        extras = self.settings.get("launcher_extra_dirs", self.settings.get("launcher_dirs", [])) if str(selected) == old_game else []
        extras = [Path(path).resolve() for path in extras if Path(path).is_dir()]
        if explicit_launchers is not None:
            extras = []
            for raw in explicit_launchers:
                path = copied_location(str(raw))
                path = path.parent if path.is_file() else path
                if not path.is_dir():
                    raise ManagedError("The selected launcher location does not exist: " + str(path))
                extras.append(path.resolve())
        extras = sorted(set(extras) - set(installation.launchers))
        launchers = sorted({*installation.launchers, *extras})
        self.settings.update(install_dir=str(installation.root), game_dir=str(selected),
                             launcher_dirs=[str(path) for path in launchers], launcher_extra_dirs=[str(path) for path in extras],
                             location_revision=LOCATION_REVISION, launcher_setup_done=True, manage_hosts=True)
        self.save_settings()
        self.log("Selected game: " + str(selected))
        for launcher in launchers:
            self.log("Selected launcher: " + str(launcher))
        if prefix := wine_prefix(selected):
            self.log("Wine/Bottles prefix: " + str(prefix))

    def link_data(self, version):
        # v0.1 linked the JSON save but omitted the SQLite file. Upstream's early
        # GameContext.CreateNew() opens BaseDirectory/epinelps.db before it reads
        # configuration. Preserve any detached v0.1 files before repairing aliases.
        detached = [version / name for name in ("epinelps.db", "epinelps.db-wal", "epinelps.db-shm")
                    if (version / name).exists() and not (version / name).is_symlink()]
        if detached:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
            archive = self.private / "detached-databases" / (version.name + "-" + stamp)
            archive.mkdir(parents=True)
            for path in detached:
                os.replace(path, archive / path.name)
            self.log("Preserved a detached v0.1 database before linking the canonical save store.")
        for name in ("db.json", "epinelps.db", "gamecommon.json", "appsettings.json", "cache", "logs"):
            target = self.data / name
            if name in ("cache", "logs"):
                target.mkdir(exist_ok=True)
            link = version / name
            if link.is_symlink() or link.is_file():
                link.unlink()
            elif link.is_dir():
                shutil.rmtree(link)
            link.symlink_to(target, target_is_directory=name in ("cache", "logs"))

    def candidate(self, offline=False):
        self.selected_build = None
        active = self.active()
        if offline or (not self.settings["auto_update"] and not self.adopt_stable):
            return active
        channel = self.settings["update_channel"]
        if channel not in ("stable", "experimental"):
            raise ManagedError("Unknown update channel. Use --channel stable or --channel experimental.")
        self.log("Checking this project's approved server release…" if channel == "stable" else
                 "Experimental channel: checking upstream's latest successful Linux build…")
        try:
            build = self.network.approved() if channel == "stable" else self.network.latest()
            if channel == "stable":
                accepted = self.state.get("stable_release", {})
                if (build.sequence < accepted.get("sequence", 0) or
                        (build.sequence == accepted.get("sequence") and build.record() != accepted)):
                    raise ManagedError("The server pin is older than, or conflicts with, the accepted release. Keeping the installed build.")
                if active and active.name != build.commit and not self.adopt_stable:
                    installed = read_json(active / ".build.json")
                    run = installed.get("run_id")
                    exact_binary = build.executable_digest and sha256(active / "EpinelPS") == build.executable_digest
                    if not exact_binary and (type(run) is not int or run <= 0 or run > build.run_id):
                        self.log("The installed server is newer than the stable pin, or its version is unknown. "
                                 "Keeping it to avoid an automatic save downgrade. "
                                 "Use --adopt-stable only when you intend to switch to the pinned version; backups remain enabled.")
                        return active
                self.log(f"Approved release: {build.release} · server {build.commit[:12]}")
            if build.commit == self.state.get("active"):
                if build.executable_digest and sha256(active / "EpinelPS") != build.executable_digest:
                    raise InstalledReleaseMismatch("The installed server does not match the approved executable checksum. Restore the verified version before starting it.")
                self.selected_build = build
                self.log(f"Already current: {build.commit[:12]}")
                return active
            if build.commit in self.state.get("rejected", []):
                self.log(f"Keeping the installed build; {build.commit[:12]} previously failed startup.")
                return active
            destination = self.versions / build.commit
            if not destination.exists():
                archive = self.network.download(build, self.private / "downloads" / f"{build.run_id}.zip")
                staging = self.versions / (".stage-" + build.commit)
                shutil.rmtree(staging, ignore_errors=True)
                try:
                    extract(archive, staging)
                    if build.executable_digest and sha256(staging / "EpinelPS") != build.executable_digest:
                        raise ManagedError("The server executable does not match the project's approved checksum.")
                    write_json(staging / ".build.json", build.record())
                    ensure_certificate(staging, self.private / "certificates", self.network, build.commit, self.log)
                    os.replace(staging, destination)
                except BaseException:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
            if build.executable_digest and sha256(destination / "EpinelPS") != build.executable_digest:
                raise ManagedError("The cached server does not match the project's approved executable checksum.")
            self.selected_build = build
            return destination
        except InstalledReleaseMismatch:
            raise
        except (ManagedError, OSError, ValueError) as exc:
            if not active:
                raise
            self.log(f"Update postponed: {exc}\nStarting the installed version instead.")
            return active

    def snapshot(self):
        identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
        snapshot = self.private / "backups" / identifier
        snapshot.mkdir(parents=True)
        files = []
        for source in self.data.iterdir():
            if not source.is_file() or source.name.endswith(("-wal", "-shm")):
                continue
            if source.name == "epinelps.db":
                sqlite_backup(source, snapshot / source.name)
            else:
                shutil.copy2(source, snapshot / source.name)
            files.append(source.name)
        write_json(snapshot / "manifest.json", files)
        return snapshot

    def restore_snapshot(self, snapshot):
        files = read_json(snapshot / "manifest.json", [])
        for path in self.data.iterdir():
            if path.is_file() and path.name not in files:
                path.unlink()
        for name in files:
            atomic_write(self.data / name, (snapshot / name).read_bytes())

    def recover(self):
        pending_file = self.private / "pending.json"
        pending = read_json(pending_file)
        if not pending or pending.get("accepted"):
            transaction = TrustTransaction(self.private / "trust-pending", self.write_trust_file)
            if not pending:
                transaction.restore()
            transaction.commit()
            pending_file.unlink(missing_ok=True)
            return
        version = self.versions / pending["candidate"]
        pid, identity = pending.get("pid"), pending.get("process_identity")
        if pid and identity and is_owned_startup(pid, identity, version / "EpinelPS"):
            self.log("Stopping the unaccepted server left by the interrupted startup…")
            # Verify both Linux start time and executable command before signaling:
            # a recycled PID or an accepted server must never be stopped here.
            def send(sig):
                try:
                    if os.getpgid(pid) == pid:
                        os.killpg(pid, sig)
                    else:
                        os.kill(pid, sig)
                except ProcessLookupError:
                    pass
            send(signal.SIGTERM)
            deadline = time.monotonic() + 5
            while is_owned_startup(pid, identity, version / "EpinelPS") and time.monotonic() < deadline:
                time.sleep(.1)
            if is_owned_startup(pid, identity, version / "EpinelPS"):
                send(signal.SIGKILL)
                deadline = time.monotonic() + 5
                while is_owned_startup(pid, identity, version / "EpinelPS") and time.monotonic() < deadline:
                    time.sleep(.1)
        if process_using(version / "EpinelPS"):
            raise ManagedError("A server from the interrupted launch is still running. Close it before recovery.")
        self.log("Recovering the previous saves and certificate files after an interrupted startup…")
        TrustTransaction(self.private / "trust-pending", self.write_trust_file).restore()
        self.restore_snapshot(self.private / "backups" / pending["snapshot"])
        self.state = pending["state"]
        write_json(self.state_path, self.state)
        pending_file.unlink()
        TrustTransaction(self.private / "trust-pending").commit()

    def preserve_preferences(self, version):
        active = self.active()
        if active and active != version:
            old, new = read_json(active / "gameconfig.json"), read_json(version / "gameconfig.json")
            # Refresh upstream asset URLs, salts and version metadata together.
            # Preserve the user-facing preferences exposed by upstream's config.
            for name in ("EnablePurchaseSimulation", "ArchiveUnlockAll"):
                if name in old:
                    new[name] = old[name]
            write_json(version / "gameconfig.json", new)

    def game_bundles(self):
        game = game_directory(Path(self.settings.get("game_dir", "")))
        if not game:
            raise ManagedError("The selected game is missing; select its location again in setup.")
        launchers = [Path(path) for path in self.settings.get("launcher_dirs", [])]
        return game, launchers, bundles(game, launchers)

    def routing_files(self, game):
        if not self.settings.get("manage_hosts", False):
            return []
        files = [Path(getattr(self, "system_hosts_file", "/etc/hosts"))]
        if prefix := wine_prefix(game):
            # This is derived from the selected game's drive_c ancestor, not a
            # guessed sibling wine_prefix that misses Bottles installations.
            files.append(prefix / "drive_c/windows/system32/drivers/etc/hosts")
        return files

    def trust_changes(self, version, ca):
        game, launchers, paths = self.game_bundles()
        plugins = game / "nikke_Data/Plugins/x86_64"
        if not any(path.parent == plugins.resolve() for path in paths):
            raise ManagedError("The selected game's certificate bundle is missing.")
        changes = {}
        for path in paths:
            changes[path] = patch_bundle(path.read_bytes(), ca.read_bytes())
            self.log("Checking SDK certificate bundle: " + str(path))
        public_pfx = public_ca_pfx(ca.read_bytes(), self.private / "public-ca")
        for directory in selector_directories(game, launchers):
            if not directory.is_dir():
                raise ManagedError("The selected launcher directory is missing: " + str(directory))
            for name in ("myCA.pem", "myCA.cer"):
                changes[directory / name] = ca.read_bytes()
            changes[directory / "myCA.pfx"] = public_pfx
        for path in self.routing_files(game):
            original = path.read_bytes() if path.exists() else b""
            changes[path] = patch_hosts(original)
            self.log("Checking local routing: " + str(path))
        return changes

    def verify_game_connection(self, expected_leaf):
        game, launchers, paths = self.game_bundles()
        report = ["EpinelPS connection checks", "Game: " + str(game),
                  *("Launcher: " + str(path) for path in launchers)]
        try:
            for path in self.routing_files(game):
                content = path.read_bytes()
                for domain in ROUTE_DOMAINS:
                    if host_addresses(content, domain) != ["127.0.0.1"]:
                        raise ManagedError("Local routing did not verify for " + domain + " in " + str(path))
                report.append("Local routing verified: " + str(path))
            for path in paths:
                for domain in PROBE_DOMAINS:
                    tls_probe(path, domain, expected_leaf, port=getattr(self, "health_port", 443), check_http=False)
                report.append("Live SDK TLS verified using: " + str(path))
                self.log("Live SDK TLS verified using: " + str(path))
            report.append("Server leaf SHA-256: " + expected_leaf)
            report.append("Local prerequisites passed; actual game login remains a separate check.")
        except (ssl.SSLError, OSError, ManagedError, http.client.HTTPException) as error:
            report.append("FAILED: " + str(error))
            atomic_write(self.private / "connection-report.txt", ("\n".join(report) + "\n").encode())
            raise ManagedError("Game/launcher connection check failed: " + str(error)) from error
        atomic_write(self.private / "connection-report.txt", ("\n".join(report) + "\n").encode())

    def write_trust_file(self, path, content, mode=None):
        try:
            atomic_write(path, content, mode)
        except PermissionError:
            if not shutil.which("pkexec"):
                raise ManagedError(f"Permission is needed to repair {path}; no system authentication dialog is available.")
            # No shell interpolation: selected public CA files and the local
            # hosts routing plan are the only privileged writes.
            with tempfile.NamedTemporaryFile(prefix="epinel-public-cert-") as temporary:
                temporary.write(content)
                temporary.flush()
                self.log(f"System permission is needed to repair {path}.")
                result = subprocess.run(["pkexec", "/usr/bin/install", "-m", format(mode or 0o644, "o"), "-T", temporary.name, str(path)])
                if result.returncode:
                    raise ManagedError(f"Certificate repair was not authorized for {path}.")

    def check_ports(self):
        check_ports()

    def prepare_ports(self, version):
        self.check_ports()
        setting = Path("/proc/sys/net/ipv4/ip_unprivileged_port_start")
        try:
            needs_capability = os.geteuid() != 0 and int(setting.read_text()) > 80
        except OSError:
            needs_capability = os.geteuid() != 0
        if needs_capability:
            self.enable_low_ports(version)

    def enable_low_ports(self, version):
        binary = version / "EpinelPS"
        getcap, setcap = shutil.which("getcap"), shutil.which("setcap")
        if getcap:
            result = subprocess.run([getcap, str(binary)], capture_output=True, text=True)
            if "cap_net_bind_service=ep" in result.stdout:
                return
        if not setcap or not shutil.which("pkexec"):
            raise ManagedError("The system needs setcap and a polkit authentication agent to allow ports 80/443 without running the server as root.")
        self.log("Allowing the server to use ports 80/443. A system password dialog may appear.")
        if subprocess.run(["pkexec", setcap, "cap_net_bind_service=+ep", str(binary)]).returncode:
            raise ManagedError("Permission to use ports 80/443 was not granted.")

    def tls_ready(self, ca, expected_leaf):
        tls_probe(ca, "global-lobby.nikke-kr.com", expected_leaf, port=getattr(self, "health_port", 443))
        return True

    def stop_child(self):
        if self.child and self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGTERM)
            try:
                self.child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait()

    def server_environment(self, version):
        environment = os.environ.copy()
        escaped = str(self.data / "epinelps.db").replace('"', '""')
        environment["ConnectionStrings__EpinelPSConnection"] = f'Data Source="{escaped}"'
        environment["ConnectionStrings__EpinelPSConnectionType"] = "sqlite"
        environment.pop("EPINEL_MANAGED_SCHEMA_ONLY", None)
        with (version / "EpinelPS").open("rb") as binary:
            native = binary.read(4) == b"\x7fELF"
        if native:
            directory = Path(__file__).resolve().parent / "bin"
            hook = directory / "EpinelPS.Managed.StartupHook.dll"
            manifest = read_json(directory / "startup-hook.json")
            if not hook.is_file() or manifest.get("sha256") != sha256(hook):
                raise ManagedError("The bundled database startup helper is missing or damaged. Re-extract the complete maintenance update.")
            hooks = [str(hook), *[path for path in environment.get("DOTNET_STARTUP_HOOKS", "").split(os.pathsep)
                                  if path and path != str(hook)]]
            environment["DOTNET_STARTUP_HOOKS"] = os.pathsep.join(hooks)
            environment["EPINEL_MANAGED_DATABASE"] = str(self.data / "epinelps.db")
        return environment, native

    def upgrade_startup_state(self):
        if self.state.get("startup_revision", 0) < STARTUP_REVISION:
            if self.state.get("startup_revision", 0) < 1:
                if self.state.get("rejected"):
                    self.log("Database startup fix installed; retrying builds rejected by v0.1 once.")
                self.state["rejected"] = []
            else:
                retry = set()
                for commit in self.state.get("rejected", []):
                    if not re.fullmatch(r"[a-f0-9]{40}", commit):
                        continue
                    version = self.versions / commit
                    ca = version / ".managed-ca.pem"
                    if ca.is_file() and (version / "site.pfx").is_file() and not validate(version / "site.pfx", ca, 30 * 86400):
                        retry.add(commit)
                if retry:
                    self.log("Strict certificate repair installed; retrying builds rejected with invalid certificate pairs once.")
                    self.state["rejected"] = [commit for commit in self.state["rejected"] if commit not in retry]
            self.state["startup_revision"] = STARTUP_REVISION
            write_json(self.state_path, self.state)

    def launch_once(self, version):
        """Return False only for failed startup; accepted sessions never roll back."""
        if process_using(version / "EpinelPS"):
            raise ManagedError("This server executable is already running. Close it before repairing database paths or starting another copy.")
        self.prepare_ports(version)
        self.preserve_preferences(version)
        self.link_data(version)
        commit = version.name if re.fullmatch(r"[a-f0-9]{40}", version.name) else None
        ca = ensure_certificate(version, self.private / "certificates", self.network, commit, self.log)
        changes = self.trust_changes(version, ca)
        needs_repair = any(not path.exists() or path.read_bytes() != content for path, content in changes.items())
        if needs_repair and game_is_running():
            raise ManagedError("Close NIKKE and its game launcher so their certificate files can be repaired safely.")
        snapshot = self.snapshot()
        old_state = dict(self.state)
        pending = {"candidate": version.name, "snapshot": snapshot.name, "state": old_state}
        write_json(self.private / "pending.json", pending)
        trust = TrustTransaction(self.private / "trust-pending", self.write_trust_file)
        fatal = threading.Event()
        port_conflict = threading.Event()
        listening = threading.Event()
        last_health_report = 0.0
        listening_since = None
        self.ready = False
        try:
            self.log(f"Starting EpinelPS ({version.name[:12]})…")
            environment, native = self.server_environment(version)
            database_ready = threading.Event()
            # --headless suppresses upstream's updater, leaving the normal server
            # console and CLI commands available through inherited stdin.
            self.child = subprocess.Popen([str(version / "EpinelPS"), "--headless"], cwd=version,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                          stdin=None if sys.stdin.isatty() else subprocess.PIPE,
                                          env=environment, text=True, errors="replace", bufsize=1, start_new_session=True)
            pending.update(pid=self.child.pid, process_identity=process_identity(self.child.pid))
            write_json(self.private / "pending.json", pending)
            child = self.child
            def output():
                with child.stdout as stream:
                    for line in stream:
                        self.log(line.rstrip("\n"))
                        if "Now listening on:" in line:
                            listening.set()
                        if "address already in use" in line.lower():
                            port_conflict.set()
                            fatal.set()
                        if line.rstrip("\r\n") == "Managed database ready: " + str(self.data / "epinelps.db"):
                            database_ready.set()
                        if any(message in line for message in ("Fatal error:", "Press any key to exit", "An error occurred creating the DB")):
                            fatal.set()
            reader = threading.Thread(target=output, daemon=True)
            reader.start()
            expected_leaf = fingerprint(leaf_certificate(version / "site.pfx"))
            deadline = time.monotonic() + float(self.settings["startup_timeout"])
            while time.monotonic() < deadline:
                if self.child.poll() is not None or fatal.is_set():
                    break
                try:
                    healthy = self.tls_ready(ca, expected_leaf)
                except (ssl.SSLError, OSError, ManagedError, http.client.HTTPException) as error:
                    healthy = False
                    now = time.monotonic()
                    if listening.is_set():
                        listening_since = listening_since or now
                        if now - last_health_report >= 5:
                            self.log("Waiting for server TLS/HTTP verification: " + str(error))
                            lines = ["EpinelPS connection checks", "Phase: waiting for server TLS/HTTP",
                                     "Game: " + self.settings.get("game_dir", ""),
                                     *("Launcher: " + path for path in self.settings.get("launcher_dirs", [])),
                                     "Server leaf SHA-256: " + expected_leaf, "Last error: " + str(error)]
                            atomic_write(self.private / "connection-report.txt", ("\n".join(lines) + "\n").encode())
                            last_health_report = now
                        if now - listening_since > 30:
                            raise ManagedError("The server is listening but its TLS/HTTP check failed: " + str(error)) from error
                if healthy and not fatal.is_set():
                    if native and not database_ready.is_set():
                        raise ManagedError("The server started without confirming managed database migrations; refusing to accept it.")
                    trust.apply(changes)
                    self.verify_game_connection(expected_leaf)
                    self.state = {**old_state, "active": version.name,
                                  "previous": old_state.get("active") if old_state.get("active") != version.name else old_state.get("previous"),
                                  "last_backup": snapshot.name}
                    if (self.selected_build and self.selected_build.release
                            and self.selected_build.commit == version.name):
                        self.state["stable_release"] = self.selected_build.record()
                    write_json(self.state_path, self.state)
                    # Once acceptance is durable, a later crash must NEVER
                    # restore the pre-launch save and erase game progress.
                    write_json(self.private / "pending.json", {**pending, "accepted": True})
                    self.ready = True
                    (self.private / "pending.json").unlink()
                    trust.commit()
                    self.log("Ready: server, selected game/launcher TLS and local routing verified.")
                    try:
                        while self.child.poll() is None:
                            if fatal.wait(.35):
                                self.stop_child()
                                break
                        result = self.child.returncode
                    except KeyboardInterrupt:
                        self.stop_child()
                        result = 0
                    reader.join(timeout=2)
                    if result:
                        self.log(f"The server exited with status {result}. Current saves were retained.")
                    return True
                time.sleep(.35)
            if port_conflict.is_set():
                raise PortConflict("Another process took a server port during startup. Close the other server and start again. This build was not rejected.")
            self.log("The new server did not pass its startup/TLS check.")
            return False
        finally:
            if not self.ready:
                self.stop_child()
                if "reader" in locals():
                    reader.join(timeout=2)
                trust.restore()
                self.restore_snapshot(snapshot)
                self.state = old_state
                write_json(self.state_path, self.state)
                (self.private / "pending.json").unlink(missing_ok=True)
                trust.commit()
            if self.child and self.child.poll() is not None and self.child.stdin:
                self.child.stdin.close()
            if self.child and self.child.poll() is not None and "reader" not in locals() and self.child.stdout:
                self.child.stdout.close()

    def run(self, offline=False):
        self.recover()
        self.check_ports()
        self.upgrade_startup_state()
        active = self.active()
        version = self.candidate(offline)
        if version is None:
            raise ManagedError("Import an existing server to preserve its gamecommon.json and saves.")
        try:
            accepted = self.launch_once(version)
        except PortConflict:
            raise
        except (ManagedError, OSError, subprocess.SubprocessError) as exc:
            if not active or active == version:
                raise
            self.log(f"The update could not be activated: {exc}")
            accepted = False
        if accepted:
            return 0
        if active and active != version:
            rejected = list(dict.fromkeys([*self.state.get("rejected", []), version.name]))
            self.state["rejected"] = rejected
            write_json(self.state_path, self.state)
            self.log("Restored the pre-update saves. Starting the previous server build…")
            if self.launch_once(active):
                return 0
        raise ManagedError("The installed server could not start. Saves were restored; see .epinel/manager.log for the actual server error.")

    def repair(self):
        self.recover()
        version = self.active()
        if not version:
            raise ManagedError("Import your existing server first.")
        if process_using(version / "EpinelPS") or game_is_running():
            raise ManagedError("Close the server, game and launcher before repairing certificates.")
        commit = version.name if re.fullmatch(r"[a-f0-9]{40}", version.name) else None
        ca = ensure_certificate(version, self.private / "certificates", self.network, commit, self.log)
        trust = TrustTransaction(self.private / "trust-pending", self.write_trust_file)
        trust.apply(self.trust_changes(version, ca))
        trust.commit()
        self.log("Certificate files and local routing repaired. Start EpinelPS to verify live game/launcher TLS.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Project-approved EpinelPS updates and certificate repair for Linux x86-64.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--setup", action="store_true", help="Choose one NIKKE install folder and discover its game and launcher")
    parser.add_argument("--import", dest="import_path", type=Path, help="Import an existing server without changing the original")
    parser.add_argument("--game", type=Path, help="Remember a game folder")
    parser.add_argument("--launcher", action="append", type=Path, help="Remember a launcher/selector folder; repeat as needed")
    parser.add_argument("--offline", action="store_true", help="Skip update checks this time")
    parser.add_argument("--repair", action="store_true", help="Repair certificates without starting the server")
    parser.add_argument("--status", action="store_true", help="Show the saved installation and settings")
    parser.add_argument("--retry-update", action="store_true", help="Retry a build previously rejected after failed startup")
    parser.add_argument("--channel", choices=("stable", "experimental"), help="Remember the update channel; stable follows this project's pin")
    parser.add_argument("--adopt-stable", action="store_true", help="Explicitly switch a newer/unknown server to the stable pin, retaining startup backups")
    args = parser.parse_args(argv)
    if args.adopt_stable and (args.channel == "experimental" or args.offline or args.repair or args.status):
        parser.error("--adopt-stable requires an online stable-channel startup")
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "amd64"):
        print("This build supports Linux x86-64.", file=sys.stderr)
        return 1
    if not shutil.which("openssl"):
        print("OpenSSL is required. Install the distribution’s openssl package before starting.", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parent.parent
    log_path = root / ".epinel/manager.log"
    log_path.parent.mkdir(mode=0o700, exist_ok=True)
    # Avoid an indefinitely growing maintenance log; upstream keeps its own logs.
    if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
        os.replace(log_path, log_path.with_suffix(".previous.log"))
    with log_path.open("a", buffering=1) as logfile:
        logfile_path_mode = 0o600
        log_path.chmod(logfile_path_mode)
        def log(message):
            print(message, flush=True)
            logfile.write(datetime.now(timezone.utc).isoformat(timespec="seconds") + " " + str(message) + "\n")
        manager = Manager(root, log=log)
        previous_handlers = {}
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous_handlers[sig] = signal.signal(sig, interrupted)
        try:
            manager.lock()
            log(f"EpinelPS Managed {__version__}")
            if args.channel or args.adopt_stable:
                manager.settings["update_channel"] = args.channel or "stable"
                manager.save_settings()
            manager.adopt_stable = args.adopt_stable
            if args.status:
                print(json.dumps({"state": manager.state, "settings": manager.settings}, indent=2))
                return 0
            manager.recover()
            if args.import_path:
                manager.import_existing(args.import_path)
            if args.setup:
                manager.settings.pop("game_dir", None)
                manager.settings["launcher_dirs"] = []
                manager.settings.pop("launcher_extra_dirs", None)
                manager.settings.pop("install_dir", None)
                manager.settings.pop("launcher_setup_done", None)
                manager.settings.pop("location_revision", None)
            manager.setup(args.game, args.launcher)
            if args.retry_update:
                manager.state["rejected"] = []
                write_json(manager.state_path, manager.state)
            if args.repair:
                manager.repair()
                return 0
            return manager.run(args.offline)
        except KeyboardInterrupt:
            manager.stop_child()
            log("Stopped. Saves retained.")
            return 130
        except (ManagedError, OSError, subprocess.SubprocessError, ValueError) as exc:
            manager.stop_child()
            log(f"Could not complete startup: {exc}")
            return 1
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
            if getattr(manager, "lockfile", None):
                manager.lockfile.close()


if __name__ == "__main__":
    raise SystemExit(main())
