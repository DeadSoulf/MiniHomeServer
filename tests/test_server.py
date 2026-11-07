import base64
import json
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        server.DATA = Path(cls.tempdir.name)
        server.DB = server.DATA / "test.db"
        server.PASSWORD = "unit-test-strong-password-never-use"
        server.init_db()
        cls.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.worker = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = "http://127.0.0.1:" + str(cls.http.server_port)
        cls.client = build_opener(HTTPCookieProcessor(CookieJar()))

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.worker.join(timeout=4)
        cls.tempdir.cleanup()

    def call(self, path, method="GET", data=None, csrf=None):
        payload = None if data is None else json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        request = Request(self.base + path, data=payload, headers=headers, method=method)
        with self.client.open(request, timeout=4) as response:
            return json.loads(response.read()), response.status

    def test_01_public_health_and_protected_state(self):
        health, status = self.call("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        with self.assertRaises(HTTPError) as ctx:
            self.call("/api/state")
        self.assertEqual(ctx.exception.code, 401)

    def test_02_full_lifecycle(self):
        with self.assertRaises(HTTPError) as ctx:
            self.call("/api/login", "POST", {"password": "wrong"})
        self.assertEqual(ctx.exception.code, 401)

        login, status = self.call("/api/login", "POST", {"password": server.PASSWORD})
        self.assertEqual(status, 200)
        csrf = login["csrf"]
        state, _ = self.call("/api/state")
        self.assertEqual(len(state["devices"]), 0)

        with self.assertRaises(HTTPError) as ctx:
            self.call("/api/devices", "POST", {"name": "Котельная", "kind": "boiler"})
        self.assertEqual(ctx.exception.code, 403)

        created, status = self.call(
            "/api/devices", "POST",
            {"name": "Котельная", "kind": "boiler", "zone": "Дом", "x": 0.2, "y": 0.75},
            csrf,
        )
        self.assertEqual(status, 201)
        did = created["id"]

        state, _ = self.call("/api/state")
        self.assertEqual(state["devices"][0]["name"], "Котельная")
        self.assertEqual(state["devices"][0]["state"], "unknown")

        self.call("/api/devices/" + did, "PATCH", {"x": 0.34, "y": 0.55}, csrf)
        state, _ = self.call("/api/state")
        self.assertEqual(state["devices"][0]["x"], 0.34)
        self.assertEqual(state["devices"][0]["name"], "Котельная")

        tiny_png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6YQAAAABJRU5ErkJggg=="
        )
        self.call("/api/map", "POST", {"data_url": "data:image/png;base64," + tiny_png}, csrf)
        state, _ = self.call("/api/state")
        self.assertTrue(state["map_url"].startswith("/media/territory"))
        self.assertTrue((server.DATA / "territory").exists())
        self.assertGreater(len(state["events"]), 0)

        with self.assertRaises(HTTPError) as ctx:
            self.call("/api/devices", "POST", {"name": "Bad", "kind": "gate", "x": 1.5, "y": 0.1}, csrf)
        self.assertEqual(ctx.exception.code, 400)

        self.call("/api/devices/" + did, "DELETE", csrf=csrf)
        state, _ = self.call("/api/state")
        self.assertEqual(state["devices"], [])

        self.call("/api/logout", "POST", {}, csrf)
        with self.assertRaises(HTTPError) as ctx:
            self.call("/api/state")
        self.assertEqual(ctx.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
