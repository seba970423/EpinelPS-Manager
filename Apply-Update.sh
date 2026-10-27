#!/bin/sh
set -eu
location=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$location/tools/apply_update.py"
