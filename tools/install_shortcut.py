#!/usr/bin/env python3
"""Install an application-menu entry; no root privileges or shell invocation."""
import os
import subprocess
import sys
from pathlib import Path


def quoted(value):
    value = str(value)
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return '"' + value + '"'


def main():
    root = Path(sys.argv[1]).resolve()
    applications = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    file = applications / "epinelps-managed.desktop"
    file.write_text("\n".join([
        "[Desktop Entry]", "Type=Application", "Name=EpinelPS Managed",
        "Comment=Update, repair certificates and start EpinelPS",
        "Exec=" + quoted(root / "Start-EpinelPS.sh"), "Icon=applications-games",
        "Terminal=false", "Categories=Game;", "Actions=Setup;Repair;",
        "", "[Desktop Action Setup]", "Name=Choose game and launcher folders",
        "Exec=" + quoted(root / "Start-EpinelPS.sh") + " --setup",
        "", "[Desktop Action Repair]", "Name=Repair certificates",
        "Exec=" + quoted(root / "Start-EpinelPS.sh") + " --repair", "",
    ]))
    file.chmod(0o755)
    message = "EpinelPS Managed is now available in your application menu."
    print(message)
    for command in (["kdialog", "--msgbox", message], ["zenity", "--info", "--text=" + message]):
        try:
            subprocess.run(command, check=False)
            break
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
