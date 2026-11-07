"""MiniHomeServer LAN/Zigbee inventory tests: no external hosts are contacted."""
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class NetworkInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.DATA = Path(self.tmp.name)
        server.DB = server.DATA / "db.sqlite"
        server.GATEWAY_CONFIG = server.DATA / "zigbee-gateway.json"
        server.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_interfaces_accept_only_direct_private_subnets(self):
        rows = [
            {"ifname": "ens18", "addr_info": [
                {"family": "inet", "scope": "global", "local": "10.10.10.61", "prefixlen": 24}]},
            {"ifname": "docker0", "addr_info": [
                {"family": "inet", "scope": "global", "local": "172.17.0.1", "prefixlen": 24}]},
            {"ifname": "ens19", "addr_info": [
                {"family": "inet", "scope": "global", "local": "8.8.8.2", "prefixlen": 24}]},
        ]
        with patch.object(server, "_ip_json", return_value=rows):
            scopes = server.local_networks()
        self.assertEqual(scopes, [{
            "interface": "ens18", "local_ip": "10.10.10.61", "cidr": "10.10.10.0/24"
        }])

    def test_bounded_scan_stores_only_devices_seen_by_server(self):
        scopes = [{"interface": "ens18", "local_ip": "10.10.10.61", "cidr": "10.10.10.60/30"}]
        neighbor = {"10.10.10.62": "aa:bb:cc:dd:ee:ff"}
        with (patch.object(server, "local_networks", return_value=scopes),
              patch.object(server, "neighbor_cache", return_value=neighbor),
              patch.object(server, "_tcp_arp_probe", return_value=True),
              patch.object(server, "_ssdp_devices", return_value={}),
              patch.object(server, "_mdns_devices", return_value={"10.10.10.62": {
                  "hostname": "zigbee-gateway", "vendor": "", "kind": "other"}})):
            result = server._scan_lan()
        self.assertEqual(result["found"], 1)
        summary = server.network_snapshot()
        self.assertEqual(len(summary["lan"]), 1)
        device = summary["lan"][0]
        self.assertEqual(device["ip"], "10.10.10.62")
        self.assertEqual(device["mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(device["hostname"], "zigbee-gateway")
        self.assertEqual(device["online"], 1)
        self.assertEqual(summary["last_scan"] is not None, True)

    def test_gateway_configuration_never_leaks_password(self):
        scopes = [{"interface": "ens18", "local_ip": "10.10.10.61", "cidr": "10.10.10.0/24"}]
        with patch.object(server, "local_networks", return_value=scopes):
            response = server.set_gateway_settings({
                "host": "10.10.10.20", "port": 1883, "base_topic": "zigbee2mqtt",
                "username": "bridgeuser", "password": "example-only-not-real",
            })
        self.assertTrue(response["configured"])
        self.assertTrue(response["password_configured"])
        self.assertNotIn("password", response)
        self.assertEqual(stat.S_IMODE(server.GATEWAY_CONFIG.stat().st_mode), 0o600)
        with patch.object(server, "local_networks", return_value=scopes):
            with self.assertRaisesRegex(ValueError, "подсет"):
                server.set_gateway_settings({"host": "10.10.20.2"})
            with self.assertRaises(ValueError):
                server.set_gateway_settings({"host": "8.8.8.8"})

    def test_zigbee_inventory_metadata_and_unknown_availability(self):
        items = [
            {"ieee_address": "0x00158d00018255df", "friendly_name": "Датчик кухни",
             "type": "EndDevice", "definition": {"model": "WXKG01LM", "vendor": "Example"}},
            {"ieee_address": "0x00124b00120144ae", "friendly_name": "Coordinator",
             "type": "Coordinator", "definition": None},
        ]
        count = server._store_zigbee_devices(items, {"Датчик кухни": "online"}, "10.10.10.20")
        self.assertEqual(count, 2)
        rows = server.network_snapshot()["zigbee"]
        sensor = next(item for item in rows if item["name"] == "Датчик кухни")
        coordinator = next(item for item in rows if item["type"] == "Coordinator")
        self.assertEqual(sensor["availability"], "online")
        self.assertEqual(coordinator["availability"], "unknown")
        self.assertEqual(sensor["bridge_ip"], "10.10.10.20")

    def test_mqtt_read_packet_and_utf8(self):
        left, right = socket.socketpair()
        try:
            payload = b"Hello MQTT"
            left.sendall(b"\x30" + server._mqtt_remaining_length(len(payload)) + payload)
            kind, body = server._mqtt_packet(right)
            self.assertEqual(kind, 0x30)
            self.assertEqual(body, payload)
            self.assertEqual(server._mqtt_string("дом")[2:], "дом".encode())
        finally:
            left.close()
            right.close()


class NetworkAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.DATA = Path(cls.tmp.name)
        server.DB = server.DATA / "api.sqlite"
        server.GATEWAY_CONFIG = server.DATA / "zigbee-gateway.json"
        server.PASSWORD = "only-for-test-never-use"
        server.init_db()
        cls.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = build_opener(HTTPCookieProcessor(CookieJar()))
        cls.base = "http://127.0.0.1:" + str(cls.http.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(timeout=3)
        cls.tmp.cleanup()

    def req(self, path, method="GET", body=None, csrf=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        req = Request(self.base + path, data=data, method=method, headers=headers)
        with self.client.open(req, timeout=4) as response:
            return json.load(response), response.status

    def test_network_discovery_requires_auth_and_csrf(self):
        with self.assertRaises(HTTPError) as unauthorized:
            self.req("/api/network/devices")
        self.assertEqual(unauthorized.exception.code, 401)
        login, _ = self.req("/api/login", "POST", {"password": server.PASSWORD})
        csrf = login["csrf"]
        with patch.object(server, "launch_network_scan", return_value=True):
            snapshot, status = self.req("/api/network/devices")
            self.assertEqual(status, 200)
            self.assertIn("lan", snapshot)
            with self.assertRaises(HTTPError) as wrong_csrf:
                self.req("/api/network/scan", "POST", {})
            self.assertEqual(wrong_csrf.exception.code, 403)
            accepted, status = self.req("/api/network/scan", "POST", {}, csrf)
            self.assertEqual(status, 202)
            self.assertTrue(accepted["ok"])


if __name__ == "__main__":
    unittest.main()
