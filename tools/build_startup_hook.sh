#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
dotnet build "$root/startup-hook/EpinelPS.Managed.StartupHook.csproj" -c Release -o "$root/epinel_manager/bin" --nologo
python3 - "$root" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
binary = root / 'epinel_manager/bin/EpinelPS.Managed.StartupHook.dll'
manifest = {'version': '0.1.1', 'target_framework': 'net10.0', 'sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}
(binary.parent / 'startup-hook.json').write_text(json.dumps(manifest, indent=2) + '\n')
print('Database startup helper and integrity manifest built.')
PY
