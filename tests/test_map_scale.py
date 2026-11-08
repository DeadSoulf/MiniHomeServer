"""Map calibration API tests on a temporary SQLite database."""
import json
import sys
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class MapScaleAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old = server.DATA, server.DB, server.PASSWORD
        server.DATA = Path(cls.temp.name)
        server.DB = server.DATA / "map-scale.db"
        server.PASSWORD = "test-private-metric-scale-only"
        server.init_db()
        cls.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.http.server_port)
        cls.client = build_opener(HTTPCookieProcessor(CookieJar()))
        req = Request(cls.url + "/api/login", method="POST",
                      data=json.dumps({"password": server.PASSWORD}).encode(),
                      headers={"Content-Type": "application/json"})
        with cls.client.open(req, timeout=5) as response:
            cls.csrf = json.load(response)["csrf"]

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(timeout=5)
        server.DATA, server.DB, server.PASSWORD = cls.old
        cls.temp.cleanup()

    def call(self, route, method="GET", payload=None, csrf=True):
        data = json.dumps(payload, allow_nan=True).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        req = Request(self.url + route, method=method, data=data, headers=headers)
        with self.client.open(req, timeout=5) as reply:
            return json.load(reply), reply.status

    def test_01_calibrate_without_moving_existing_buildings(self):
        created, status = self.call("/api/devices", "POST", {
            "name": "Гараж", "kind": "building", "x": 0.28, "y": 0.41,
            "building_shape": True, "building_width": 25,
            "building_height": 15, "building_rotation": 45
        })
        self.assertEqual(status, 201)
        did = created["id"]
        state, _ = self.call("/api/state")
        self.assertIsNone(state["map_scale"])
        building = next(d for d in state["devices"] if d["id"] == did)
        original = {key: building[key] for key in (
            "x", "y", "building_width", "building_height", "building_rotation")}
        with self.assertRaises(HTTPError) as denied:
            self.call("/api/map/scale", "POST",
                      {"width_m": 30, "height_m": 40}, csrf=False)
        self.assertEqual(denied.exception.code, 403)

        saved, _ = self.call("/api/map/scale", "POST",
                             {"width_m": 30, "height_m": 40})
        self.assertEqual(saved["map_scale"], {"width_m": 30.0, "height_m": 40.0})
        after, _ = self.call("/api/state")
        same = next(d for d in after["devices"] if d["id"] == did)
        self.assertEqual({key: same[key] for key in original}, original)
        self.assertEqual(after["map_scale"], saved["map_scale"])

        changed, _ = self.call("/api/map/scale", "POST",
                               {"width_m": 32.5, "height_m": 41.75})
        self.assertEqual(changed["map_scale"], {"width_m": 32.5, "height_m": 41.75})
        cleared, _ = self.call("/api/map/scale", "POST", {"clear": True})
        self.assertIsNone(cleared["map_scale"])
        state, _ = self.call("/api/state")
        self.assertIsNone(state["map_scale"])
        self.assertEqual(next(d for d in state["devices"] if d["id"] == did)["x"], 0.28)
        self.call("/api/devices/" + did, "DELETE")

    def test_02_fractional_building_percentages_roundtrip(self):
        created, _ = self.call("/api/devices", "POST", {
            "name": "Малое строение", "kind": "building", "building_shape": True,
            "building_width": 3.3333, "building_height": 6.25
        })
        state, _ = self.call("/api/state")
        row = next(d for d in state["devices"] if d["id"] == created["id"])
        self.assertAlmostEqual(row["building_width"], 3.3333)
        self.assertAlmostEqual(row["building_height"], 6.25)
        self.call("/api/devices/" + created["id"], "DELETE")

    def test_03_bad_scale_inputs_are_rejected(self):
        bad = [
            {"width_m": 0, "height_m": 40},
            {"width_m": 30, "height_m": 5001},
            {"width_m": True, "height_m": 40},
            {"width_m": "30", "height_m": 40},
            {"width_m": 30},
            {"width_m": 30, "height_m": 40, "extra": 1},
            {"clear": False},
            {"clear": True, "width_m": 30},
            {"width_m": float("nan"), "height_m": 40},
            {"width_m": float("inf"), "height_m": 40},
            {"width_m": 10 ** 800, "height_m": 40},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(HTTPError) as failure:
                    self.call("/api/map/scale", "POST", payload)
                self.assertEqual(failure.exception.code, 400)
        state, _ = self.call("/api/state")
        self.assertIsNone(state["map_scale"])


if __name__ == "__main__":
    unittest.main()
