"""Exercise real iproute2 JSON parsing without contacting a network.

Addresses below are synthetic documentation fixtures, not owner infrastructure.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class NeighborCacheTests(unittest.TestCase):
    scopes = [{"interface": "eth0", "cidr": "192.0.2.0/24"}]
    row = {"dst": "192.0.2.2", "dev": "eth0", "lladdr": "AA:BB:CC:DD:EE:FF"}

    def read_rows(self, rows):
        result = subprocess.CompletedProcess([], 0, json.dumps(rows), "")
        with (patch.object(server.shutil, "which", return_value="/usr/bin/ip"),
              patch.object(server.subprocess, "run", return_value=result) as run):
            found = server.neighbor_cache(self.scopes)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/ip", "-j", "-4", "neigh", "show"])
        return found

    def test_array_and_string_states(self):
        for state in (["REACHABLE"], "REACHABLE", ["STALE"], "DELAY", ["PROBE"],
                      ["PERMANENT"], ["NOARP"], [], ""):
            with self.subTest(state=state):
                self.assertEqual(self.read_rows([{**self.row, "state": state}]),
                                 {"192.0.2.2": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(self.read_rows([self.row]), {"192.0.2.2": "aa:bb:cc:dd:ee:ff"})

    def test_unresolved_and_malformed_states_are_skipped(self):
        for state in (["FAILED"], "FAILED", ["INCOMPLETE"], "INCOMPLETE", ["NONE"],
                      "NONE", ["REACHABLE", "FAILED"], "REACHABLE FAILED",
                      None, 1, {"FAILED": True}, [["REACHABLE"]]):
            with self.subTest(state=state):
                self.assertEqual(self.read_rows([{**self.row, "state": state}]), {})

    def test_scope_interface_and_mac_filters(self):
        for change in ({"dev": "eth1"}, {"dst": "198.51.100.2"}, {"dst": "invalid"},
                       {"lladdr": ""}, {"lladdr": "not-a-mac"}):
            with self.subTest(change=change):
                self.assertEqual(self.read_rows([{**self.row, "state": ["REACHABLE"], **change}]), {})

    def test_invalid_json_and_failed_command_use_arp_fallback(self):
        for result in (subprocess.CompletedProcess([], 0, "invalid", ""),
                       subprocess.CompletedProcess([], 1, "", "failed")):
            with (patch.object(server.shutil, "which", return_value="/usr/bin/ip"),
                  patch.object(server.subprocess, "run", return_value=result),
                  patch.object(server, "_proc_arp_entries", return_value=[]) as fallback):
                self.assertEqual(server.neighbor_cache(self.scopes), {})
                fallback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
