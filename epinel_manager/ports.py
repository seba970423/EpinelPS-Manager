"""Detect occupied server ports independently of low-port permissions."""
from __future__ import annotations

import errno
import os
import socket
from pathlib import Path

from .common import ManagedError


class PortConflict(ManagedError):
    """An environmental conflict, not evidence of a broken upstream build."""


def listeners(ports: tuple[int, ...]) -> dict[int, set[str]]:
    result = {}
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = table.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            try:
                port = int(fields[1].rsplit(":", 1)[1], 16)
                if fields[3] == "0A" and port in ports:
                    result.setdefault(port, set()).add(fields[9])
            except (IndexError, ValueError):
                continue
    return result


def owners(inodes: set[str]) -> list[str]:
    result = []
    targets = {"socket:[" + inode + "]" for inode in inodes}
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if any(os.readlink(fd) in targets for fd in (process / "fd").iterdir()):
                name = (process / "comm").read_text().strip()
                result.append(f"{name} (PID {process.name})")
        except OSError:
            continue
    return sorted(result)


def conflict(port: int, inodes: set[str] | None = None) -> PortConflict:
    processes = owners(inodes) if inodes else []
    detail = " by " + ", ".join(processes) if processes else " by another process"
    return PortConflict(f"Port {port} is already in use{detail}. An earlier EpinelPS server may still be running after suspend. "
                        "Close the other server's terminal or stop that service, then start again. This build was not rejected.")


def check_ports(ports=(80, 443)) -> None:
    # On restricted systems, bind(80) can report EACCES before discovering that
    # 443 is occupied. Check kernel listeners first and never skip later ports.
    occupied = listeners(ports)
    if occupied:
        port = next(port for port in ports if port in occupied)
        raise conflict(port, occupied[port])
    for port in ports:
        for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
            try:
                with socket.socket(family, socket.SOCK_STREAM) as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if family == socket.AF_INET6:
                        probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    probe.bind((address, port))
            except PermissionError:
                # Capability setup happens separately, after all ports pass.
                continue
            except OSError as error:
                if error.errno == errno.EADDRINUSE:
                    raise conflict(port) from error
                if family == socket.AF_INET6 and error.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL):
                    continue
                raise ManagedError(f"Could not check server port {port}: {error}") from error
