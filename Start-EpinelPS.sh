#!/bin/sh
set -eu
location=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if ! command -v python3 >/dev/null 2>&1 || ! command -v openssl >/dev/null 2>&1; then
    message='EpinelPS Managed needs Python 3 and OpenSSL from your distribution.'
    if command -v kdialog >/dev/null 2>&1; then
        kdialog --error "$message"
    elif command -v zenity >/dev/null 2>&1; then
        zenity --error --text="$message"
    else
        printf '%s\n' "$message" >&2
    fi
    exit 1
fi

cd -- "$location"
if [ -t 0 ] && [ -t 1 ]; then
    exec ./EpinelPS "$@"
fi
if command -v konsole >/dev/null 2>&1; then
    exec konsole --hold -e "$location/EpinelPS" "$@"
elif command -v xfce4-terminal >/dev/null 2>&1; then
    exec xfce4-terminal --hold -x "$location/EpinelPS" "$@"
elif command -v x-terminal-emulator >/dev/null 2>&1; then
    exec x-terminal-emulator -e "$location/EpinelPS" "$@"
else
    exec ./EpinelPS "$@"
fi
