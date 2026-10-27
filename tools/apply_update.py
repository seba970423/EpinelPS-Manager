#!/usr/bin/env python3
"""Update application files without replacing or reimporting managed saves."""
import fcntl
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE))
from epinel_manager.common import ManagedError, atomic_write, read_json, write_json
from epinel_manager import __version__
from epinel_manager.manager import pick_directory
from epinel_manager.network import sha256

TOP_LEVEL = {"EpinelPS", "Start-EpinelPS.sh", "Install-Desktop-Shortcut.sh", "Apply-Update.sh",
             "README.md", "VALIDATION.md", "LICENSE", ".gitignore"}
FOLDERS = {"epinel_manager", "tools", "startup-hook", "tests"}


def apply_update(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not (target / ".epinel/state.json").is_file():
        raise ManagedError("Choose the existing EpinelPS-Managed folder containing your managed installation.")
    for name in ("EpinelPS", "Start-EpinelPS.sh", "epinel_manager/manager.py",
                 "epinel_manager/bin/EpinelPS.Managed.StartupHook.dll", "epinel_manager/bin/startup-hook.json"):
        if not (source / name).is_file():
            raise ManagedError("The update is incomplete. Extract the entire archive before applying it.")
    helper = source / "epinel_manager/bin/EpinelPS.Managed.StartupHook.dll"
    if read_json(helper.parent / "startup-hook.json").get("sha256") != sha256(helper):
        raise ManagedError("The update's database helper failed its integrity check.")
    with (target / ".epinel/manager.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ManagedError("Close the running EpinelPS Managed server, then open Apply-Update.sh again.") from exc
        if source == target:
            return target
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
        backup = target / ".epinel/application-updates" / stamp
        backup.mkdir(parents=True)
        plan = []
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if not path.is_file() or any(part in {"__pycache__", "obj", ".git", ".epinel"} for part in relative.parts):
                continue
            if not (str(relative) in TOP_LEVEL or relative.parts[0] in FOLDERS):
                continue
            destination = target / relative
            if not destination.parent.resolve().is_relative_to(target):
                raise ManagedError("An application directory points outside the chosen installation.")
            existed = destination.exists()
            mode = destination.stat().st_mode & 0o777 if existed else (0o755 if path.name in {"EpinelPS", "Start-EpinelPS.sh", "Install-Desktop-Shortcut.sh", "Apply-Update.sh"} else 0o644)
            if existed:
                atomic_write(backup / relative, destination.read_bytes(), mode)
            plan.append({"path": str(relative), "existed": existed, "mode": mode})
        write_json(backup / "manifest.json", plan)
        applied = []
        try:
            for record in plan:
                relative = Path(record["path"])
                applied.append(record)
                mode = 0o755 if relative.name in {"EpinelPS", "Start-EpinelPS.sh", "Install-Desktop-Shortcut.sh", "Apply-Update.sh"} else record["mode"]
                atomic_write(target / relative, (source / relative).read_bytes(), mode)
        except BaseException:
            for record in reversed(applied):
                relative = Path(record["path"])
                if record["existed"]:
                    atomic_write(target / relative, (backup / relative).read_bytes(), record["mode"])
                else:
                    (target / relative).unlink(missing_ok=True)
            raise
    return target


def main():
    candidates = {path.resolve() for path in (SOURCE, Path.home() / "EpinelPS-Managed", SOURCE.parent / "EpinelPS-Managed")
                  if (path / ".epinel/state.json").is_file()}
    target = next(iter(candidates)) if len(candidates) == 1 else pick_directory("Choose your existing EpinelPS-Managed folder", Path.home())
    if not target:
        raise ManagedError("Update cancelled. The existing installation is unchanged.")
    target = apply_update(SOURCE, target)
    print(f"EpinelPS Managed {__version__} installed. Starting the existing managed installation…", flush=True)
    executable = str(target / "Start-EpinelPS.sh")
    os.execv(executable, [executable])


if __name__ == "__main__":
    try:
        main()
    except (ManagedError, OSError) as error:
        message = "Could not apply update: " + str(error)
        print(message, file=sys.stderr)
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            for arguments in (["kdialog", "--error", message], ["zenity", "--error", "--text=" + message]):
                if shutil.which(arguments[0]):
                    subprocess.run(arguments, check=False)
                    break
        raise SystemExit(1)
