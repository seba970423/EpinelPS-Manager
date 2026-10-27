import errno
import socket
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from epinel_manager.ports import PortConflict, check_ports, listeners


class PortAvailability(unittest.TestCase):
    def test_real_listener_is_detected_before_another_server_starts(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            with self.assertRaisesRegex(PortConflict, str(port)):
                check_ports((port,))

    def test_kernel_listener_scan_checks_ipv4_and_ipv6(self):
        def table(path):
            port = "0050" if path.name == "tcp" else "01BB"
            return "header\n 0: 00000000:" + port + " 00000000:0000 0A 0:0 0:0 0 1000 0 987654\n"
        with patch.object(Path, "read_text", table):
            self.assertEqual(listeners((80, 443)), {80: {"987654"}, 443: {"987654"}})

    def test_permission_denial_on_80_does_not_skip_occupied_443(self):
        probe = MagicMock()
        probe.__enter__.return_value = probe
        def bind(address):
            if address[1] == 80:
                raise PermissionError(errno.EACCES, "low ports restricted")
            raise OSError(errno.EADDRINUSE, "address already in use")
        probe.bind.side_effect = bind
        with patch("epinel_manager.ports.listeners", return_value={}), patch("epinel_manager.ports.socket.socket", return_value=probe), self.assertRaisesRegex(PortConflict, "443"):
            check_ports()
        self.assertIn((("0.0.0.0", 443),), [call.args for call in probe.bind.call_args_list])

    def test_unavailable_ipv6_does_not_block_an_ipv4_server(self):
        probe = MagicMock()
        probe.__enter__.return_value = probe
        def create(family, kind):
            if family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, "no IPv6")
            return probe
        with patch("epinel_manager.ports.listeners", return_value={}), patch("epinel_manager.ports.socket.socket", side_effect=create):
            check_ports()

    def test_all_restricted_ports_are_checked_before_capability_setup(self):
        probe = MagicMock()
        probe.__enter__.return_value = probe
        probe.bind.side_effect = PermissionError(errno.EACCES, "low ports restricted")
        with patch("epinel_manager.ports.listeners", return_value={}), patch("epinel_manager.ports.socket.socket", return_value=probe):
            check_ports()
        self.assertEqual(probe.bind.call_count, 4)
