"""Explicit locations copied from Dolphin or another file manager."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .common import ManagedError

LOCATION_REVISION = 1


def copied_location(value: str) -> Path:
    value = value.strip()
    if not value or "\n" in value or "\r" in value or "\0" in value:
        raise ManagedError("Paste one game or launcher location.")
    # Prefer an existing literal filename; apostrophes and spaces need no escaping.
    literal = Path(value).expanduser()
    if literal.exists():
        return literal.resolve()
    if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
        value = value[1:-1]
    if value.startswith("file:"):
        url = urlsplit(value)
        if url.scheme != "file" or url.netloc not in ("", "localhost") or url.query or url.fragment:
            raise ManagedError("Paste a local file location from the file manager.")
        value = unquote(url.path)
    elif "\\ " in value:
        try:
            values = shlex.split(value)
            if len(values) == 1:
                value = values[0]
        except ValueError:
            pass
    return Path(value).expanduser().resolve()


def find_game(location: Path) -> Path | None:
    location = Path(location)
    if location.is_file():
        location = location.parent
    candidates = (location, location / "NIKKE/game", location / "game")
    for candidate in candidates:
        if ((candidate / "nikke.exe").is_file()
                and (candidate / "nikke_Data/Plugins/x86_64").is_dir()):
            return candidate.resolve()
    found = []
    for candidate in installation_directories(location):
        if ((candidate / "nikke.exe").is_file()
                and (candidate / "nikke_Data/Plugins/x86_64").is_dir()):
            found.append(candidate.resolve())
            if len(found) > 1:
                raise ManagedError("This folder contains multiple NIKKE installations. Choose the install folder for the one you use.")
    return found[0] if found else None


def installation_directories(root: Path, max_depth=5, budget=2048):
    """Search only the selected installation; never follow links into other trees."""
    ignored = {"nikke_data", "unity", "sail_files", "cache", "log", "logs", ".git", ".epinel", "windows"}
    pending = deque([(root, 0)])
    while pending and budget > 0:
        directory, depth = pending.popleft()
        budget -= 1
        if not directory.is_dir():
            continue
        yield directory
        if depth >= max_depth:
            continue
        try:
            pending.extend((child, depth + 1) for child in sorted(directory.iterdir())
                           if child.name.lower() not in ignored and not child.is_symlink() and child.is_dir())
        except OSError:
            continue
    if pending:
        raise ManagedError("This folder is too broad to scan. Choose the NIKKE installation folder inside it.")


def install_root(game: Path) -> Path:
    if game.name.lower() == "game" and game.parent.name.lower() == "nikke":
        return game.parent.parent
    if game.name.lower() == "game" and any((game.parent / name).is_dir() for name in ("Launcher", "launcher")):
        return game.parent
    return game


@dataclass(frozen=True)
class Installation:
    root: Path
    game: Path
    launchers: tuple[Path, ...]


def installation_for_game(game: Path, root: Path | None = None) -> Installation:
    game = game.resolve()
    root = root.resolve() if root else install_root(game)
    if not game.is_relative_to(root):
        root = install_root(game)
    launchers = set()
    for directory in installation_directories(root):
        sdk = directory / "intl_service"
        if (directory in {root / "Launcher", root / "launcher"}
                or any((sdk / name).is_file() for name in ("intl_cacert.pem", "cacert.pem"))
                or any((directory / name).is_file() for name in (
                    "ServerSelector.Desktop.exe", "EpinelPSLauncher", "EpinelPSLauncher.exe", "nikke_launcher.exe",
                    "myCA.pem", "myCA.pfx"))):
            launchers.add(directory.resolve())
    return Installation(root, game, tuple(sorted(launchers)))


def locate_installation(location: Path) -> Installation:
    location = location.resolve()
    chosen = location.parent if location.is_file() else location
    game = find_game(chosen)
    if not game:
        raise ManagedError("That folder does not contain nikke.exe and nikke_Data/Plugins/x86_64.")
    root = install_root(game)
    # Keep the user's scan boundary for nonstandard layouts. A direct game/exe
    # selection still finds its conventional sibling Launcher and selector kit.
    if root == game and chosen != game and game.is_relative_to(chosen):
        root = chosen
    return installation_for_game(game, root)


def ask_location(title: str, default="", *, optional=False) -> str | None:
    message = title + "\nUse Copy Location in Dolphin, then paste it here."
    if optional:
        message += "\nLeave blank if you do not use a separate launcher."
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        choices = (("kdialog", ["--title", "EpinelPS Managed", "--inputbox", message, str(default)]),
                   ("zenity", ["--entry", "--title=EpinelPS Managed", "--text=" + message, "--entry-text=" + str(default)]))
        for program, arguments in choices:
            if shutil.which(program):
                result = subprocess.run([program, *arguments], capture_output=True, text=True)
                return result.stdout.strip() if result.returncode == 0 else None
    if sys.stdin.isatty():
        print(message)
        if default:
            print("Current location: " + str(default))
        if optional:
            print("Enter '-' to skip this launcher.")
        answer = input("> ").strip()
        return "" if optional and answer == "-" else answer or str(default)
    return None


def confirmed_installation(default="") -> Installation:
    for attempt in range(3):
        answer = ask_location("Where is NIKKE installed? Paste its install folder. The game and launcher will be found inside it.", default)
        if answer is None:
            raise ManagedError("Game selection cancelled; the remembered location was not replaced.")
        try:
            return locate_installation(copied_location(answer))
        except ManagedError as error:
            print(str(error))
        default = answer
    raise ManagedError("A valid game location is required before repairing game certificates.")
