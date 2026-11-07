"""MiniHomeServer LAN/Zigbee inventory tests: no external hosts are contacted.

All addresses used below are fictitious private LAN test fixtures and do not
correspond to the owner's actual home network.
"""
import json
import os
import socket
import struct
import time
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
                {"family": "inet", "scope": "global", "local": "192.168.50.61", "prefixlen": 24}]},
            {"ifname": "docker0", "addr_info": [
                {"family": "inet", "scope": "global", "local": "172.17.0.1", "prefixlen": 24}]},
            {"ifname": "ens19", "addr_info": [
                {"family": "inet", "scope": "global", "local": "8.8.8.2", "prefixlen": 24}]},
        ]
        with patch.object(server, "_ip_json", return_value=rows):
            scopes = server.local_networks()
        self.assertEqual(scopes, [{
            "interface": "ens18", "local_ip": "192.168.50.61", "cidr": "192.168.50.0/24"
        }])

    def test_bounded_scan_stores_only_devices_seen_by_server(self):
        scopes = [{"interface": "ens18", "local_ip": "192.168.50.61", "cidr": "192.168.50.60/30"}]
        neighbor = {"192.168.50.62": "aa:bb:cc:dd:ee:ff"}
        with (patch.object(server, "local_networks", return_value=scopes),
              patch.object(server, "neighbor_cache", return_value=neighbor),
              patch.object(server, "_tcp_arp_probe", return_value=True),
              patch.object(server, "_ssdp_devices", return_value={}),
              patch.object(server, "_mdns_devices", return_value={"192.168.50.62": {
                  "hostname": "zigbee-gateway", "vendor": "", "kind": "other"}})):
            result = server._scan_lan()
        self.assertEqual(result["found"], 1)
        summary = server.network_snapshot()
        self.assertEqual(len(summary["lan"]), 1)
        device = summary["lan"][0]
        self.assertEqual(device["ip"], "192.168.50.62")
        self.assertEqual(device["mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(device["hostname"], "zigbee-gateway")
        self.assertEqual(device["online"], 1)
        self.assertEqual(summary["last_scan"] is not None, True)

    def test_gateway_configuration_never_leaks_password(self):
        scopes = [{"interface": "ens18", "local_ip": "192.168.50.61", "cidr": "192.168.50.0/24"}]
        with patch.object(server, "local_networks", return_value=scopes):
            response = server.set_gateway_settings({
                "host": "192.168.50.20", "port": 1883, "base_topic": "zigbee2mqtt",
                "username": "bridgeuser", "password": "example-only-not-real",
            })
        self.assertTrue(response["configured"])
        self.assertTrue(response["password_configured"])
        self.assertNotIn("password", response)
        self.assertEqual(stat.S_IMODE(server.GATEWAY_CONFIG.stat().st_mode), 0o600)
        with patch.object(server, "local_networks", return_value=scopes):
            with self.assertRaisesRegex(ValueError, "подсет"):
                server.set_gateway_settings({"host": "192.168.51.2"})
            with self.assertRaises(ValueError):
                server.set_gateway_settings({"host": "8.8.8.8"})

    def test_zigbee_inventory_metadata_and_unknown_availability(self):
        items = [
            {"ieee_address": "0x00158d00018255df", "friendly_name": "Датчик кухни",
             "type": "EndDevice", "definition": {"model": "WXKG01LM", "vendor": "Example"}},
            {"ieee_address": "0x00124b00120144ae", "friendly_name": "Coordinator",
             "type": "Coordinator", "definition": None},
        ]
        count = server._store_zigbee_devices(items, {"Датчик кухни": "online"}, "192.168.50.20")
        self.assertEqual(count, 2)
        with patch.dict(server.ZIGBEE_SCAN, {"connected": True}):
            rows = server.network_snapshot()["zigbee"]
        sensor = next(item for item in rows if item["name"] == "Датчик кухни")
        coordinator = next(item for item in rows if item["type"] == "Coordinator")
        self.assertEqual(sensor["availability"], "online")
        self.assertEqual(coordinator["availability"], "unknown")
        self.assertEqual(sensor["bridge_ip"], "192.168.50.20")

    def test_zigbee_restart_does_not_present_saved_status_as_live(self):
        server._store_zigbee_devices([
            {"ieee_address": "0x00158d00018255df", "friendly_name": "Saved sensor"}
        ], {"Saved sensor": "online"}, "192.168.50.20")
        with patch.dict(server.ZIGBEE_SCAN, {"connected": False}):
            item = server.network_snapshot()["zigbee"][0]
        self.assertEqual(item["availability"], "unknown")
        self.assertEqual(item["name"], "Saved sensor")
        self.assertTrue(item["last_seen"])

    def test_zigbee_failed_poll_hides_stale_status_until_recovery(self):
        items = [{"ieee_address": "0x00158d00018255df", "friendly_name": "Saved sensor"}]
        server._store_zigbee_devices(items, {"Saved sensor": "online"}, "192.168.50.20")
        with (patch.dict(server.ZIGBEE_SCAN, {"connected": True}),
              patch.object(server, "gateway_settings", return_value={"host": "192.168.50.20"}),
              patch.object(server, "_safe_gateway_ip"),
              patch.object(server, "_mqtt_inventory", side_effect=OSError("Test gateway unavailable"))):
            server._zigbee_scan_worker()
            failed = server.network_snapshot()
            self.assertFalse(failed["zigbee_status"]["connected"])
            self.assertEqual(failed["zigbee"][0]["availability"], "unknown")
            with patch.object(server, "_mqtt_inventory", return_value=(items, {"Saved sensor": "online"})):
                server._zigbee_scan_worker()
            recovered = server.network_snapshot()
            self.assertTrue(recovered["zigbee_status"]["connected"])
            self.assertEqual(recovered["zigbee"][0]["availability"], "online")
            self.assertEqual(len(recovered["zigbee"]), 1)

    def test_zigbee2mqtt_retained_inventory(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        errors = []

        def broker():
            try:
                with listener:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(4)
                        kind, _ = server._mqtt_packet(connection)
                        self.assertEqual(kind, 0x10)
                        connection.sendall(b"\x20\x02\x00\x00")
                        kind, _ = server._mqtt_packet(connection)
                        self.assertEqual(kind, 0x82)
                        connection.sendall(b"\x90\x05\x00\x01\x00\x00\x00")

                        def retained(topic, content):
                            raw_topic = topic.encode("utf-8")
                            message = json.dumps(content, ensure_ascii=False).encode("utf-8")
                            payload = struct.pack("!H", len(raw_topic)) + raw_topic + message
                            connection.sendall(b"\x31" + server._mqtt_remaining_length(len(payload)) + payload)

                        retained("zigbee2mqtt/bridge/devices", [{
                            "ieee_address": "0x00158d00018255df",
                            "type": "EndDevice", "friendly_name": "Датчик",
                            "definition": {"model": "WXKG01LM", "vendor": "Example"}
                        }])
                        retained("zigbee2mqtt/Датчик/availability", {"state": "online"})
                        time.sleep(1)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=broker, daemon=True)
        thread.start()
        try:
            devices, availability = server._mqtt_inventory({
                "host": "127.0.0.1", "port": port, "base_topic": "zigbee2mqtt",
                "username": "", "password": "",
            }, wait_seconds=0.7)
        finally:
            thread.join(timeout=4)
            listener.close()
        self.assertFalse(errors, str(errors))
        self.assertEqual(devices[0]["friendly_name"], "Датчик")
        self.assertEqual(availability["Датчик"], "online")

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


    def test_zigbee_config_requires_csrf_and_hides_password(self):
        login, _ = self.req("/api/login", "POST", {"password": server.PASSWORD})
        csrf = login["csrf"]
        cfg = {"host": "192.168.50.20", "port": 1883, "base_topic": "zigbee2mqtt",
               "username": "bridge", "password": "only-a-test-password"}
        with self.assertRaises(HTTPError) as no_csrf:
            self.req("/api/zigbee/config", "POST", cfg)
        self.assertEqual(no_csrf.exception.code, 403)
        scopes = [{"interface": "ens18", "local_ip": "192.168.50.61",
                   "cidr": "192.168.50.0/24"}]
        with (patch.object(server, "local_networks", return_value=scopes),
              patch.object(server, "launch_zigbee_scan", return_value=True),
              patch.object(server, "launch_network_scan", return_value=False)):
            response, status = self.req("/api/zigbee/config", "POST", cfg, csrf)
            self.assertEqual(status, 202)
            self.assertEqual(response["gateway"]["host"], "192.168.50.20")
            self.assertNotIn("password", response["gateway"])
            snapshot, status = self.req("/api/network/devices")
            self.assertEqual(status, 200)
            self.assertTrue(snapshot["gateway"]["password_configured"])
            self.assertNotIn("password", snapshot["gateway"])


if __name__ == "__main__":
    unittest.main()
