#!/bin/sh
set -eu
location=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$location/tools/install_shortcut.py" "$location"
