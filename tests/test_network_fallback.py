"""Linux LAN discovery fallback tests — no real device scans or router access."""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class NetworkFallbackTests(unittest.TestCase):
    def test_iproute2_failure_falls_back_for_both_queries(self):
        interfaces = [{"ifname": "enp3s0", "addr_info": [{
            "family": "inet", "scope": "global",
            "local": "192.168.77.10", "prefixlen": 24,
        }]}]
        neighbors = [{"dst": "192.168.77.23", "lladdr": "02:11:22:33:44:55",
                      "state": "REACHABLE", "dev": "enp3s0"}]
        with (patch.object(server.shutil, "which", return_value="/usr/sbin/ip"),
              patch.object(server.subprocess, "run",
                           return_value=type("Result", (), {"returncode": 1, "stdout": ""})()),
              patch.object(server, "_ioctl_ipv4_interfaces", return_value=interfaces) as fallback_addr,
              patch.object(server, "_proc_arp_entries", return_value=neighbors) as fallback_arp):
            self.assertEqual(server._ip_json(["-4", "addr", "show"]), interfaces)
            self.assertEqual(server._ip_json(["-4", "neigh", "show"]), neighbors)
            fallback_addr.assert_called_once()
            fallback_arp.assert_called_once()

    def test_ioctl_reads_private_address_without_netlink(self):
        import fcntl
        def ioctl_stub(_fd, operation, _request):
            value = "192.168.77.10" if operation == 0x8915 else "255.255.255.0"
            return bytes(20) + socket.inet_aton(value) + bytes(232)
        with (patch.object(server.Path, "iterdir",
                           return_value=[Path("/sys/class/net/enp3s0")]),
              patch.object(fcntl, "ioctl", side_effect=ioctl_stub)):
            result = server._ioctl_ipv4_interfaces()
        self.assertEqual(result[0]["ifname"], "enp3s0")
        self.assertEqual(result[0]["addr_info"][0]["local"], "192.168.77.10")
        self.assertEqual(result[0]["addr_info"][0]["prefixlen"], 24)

    def test_proc_arp_rejects_incomplete_and_invalid_neighbors(self):
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / "arp"
            sample.write_text(
                "IP address HW type Flags HW address Mask Device\n"
                "192.168.77.23 0x1 0x2 02:11:22:33:44:55 * enp3s0\n"
                "192.168.77.24 0x1 0x0 00:00:00:00:00:00 * enp3s0\n"
                "192.168.77.25 0x1 0x2 invalid-mac * enp3s0\n", encoding="ascii")
            neighbors = server._proc_arp_entries(sample)
            self.assertEqual(len(neighbors), 1)
            self.assertEqual(neighbors[0]["dst"], "192.168.77.23")

    def test_network_scan_with_blocked_netlink_keeps_scope(self):
        interfaces = [{"ifname": "enp3s0", "addr_info": [{
            "family": "inet", "scope": "global", "local": "192.168.77.10",
            "prefixlen": 24}]}]
        with patch.object(server, "_ip_json", return_value=interfaces):
            scopes = server.local_networks()
        self.assertEqual(scopes, [{"interface":"enp3s0",
                                   "local_ip":"192.168.77.10",
                                   "cidr":"192.168.77.0/24"}])


if __name__ == "__main__":
    unittest.main()
