"""Building footprints: authenticated CRUD, compatibility, and validated geometry."""
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


class BuildingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.previous = (server.DATA, server.DB, server.PASSWORD)
        server.DATA = Path(cls.temp.name)
        server.DB = server.DATA / "building-fixture.db"
        server.PASSWORD = "building-test-password-only"
        server.init_db()
        cls.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.http.server_port)
        cls.client = build_opener(HTTPCookieProcessor(CookieJar()))
        req = Request(cls.url + "/api/login", method="POST",
                      data=json.dumps({"password":server.PASSWORD}).encode(),
                      headers={"Content-Type":"application/json"})
        with cls.client.open(req, timeout=5) as response:
            cls.csrf = json.load(response)["csrf"]

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(timeout=5)
        server.DATA, server.DB, server.PASSWORD = cls.previous
        cls.temp.cleanup()

    def call(self, path, method="GET", body=None, csrf=True):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if data else {}
        if csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        request = Request(self.url + path, method=method, data=data, headers=headers)
        with self.client.open(request, timeout=5) as response:
            return json.load(response), response.status

    def test_create_move_resize_rotate_and_delete_building(self):
        payload = {"name":"Гараж","kind":"building","zone":"Двор",
                   "x":0.42,"y":0.67,"building_shape":True,
                   "building_width":22,"building_height":18,"building_rotation":135}
        with self.assertRaises(HTTPError) as denied:
            self.call("/api/devices", "POST", payload, csrf=False)
        self.assertEqual(denied.exception.code, 403)
        created,code = self.call("/api/devices","POST",payload)
        self.assertEqual(code,201)
        did=created["id"]
        state,_=self.call("/api/state")
        row=next(d for d in state["devices"] if d["id"] == did)
        self.assertEqual(row["kind"],"building")
        self.assertEqual([row[x] for x in ("building_shape","building_width",
                                            "building_height","building_rotation")],
                         [1,22,18,135])
        self.call("/api/devices/"+did,"PATCH",{"x":0.75,"y":0.23})
        state,_=self.call("/api/state")
        row=next(d for d in state["devices"] if d["id"] == did)
        self.assertEqual((row["x"],row["y"],row["building_width"],row["building_rotation"]),
                         (0.75,0.23,22,135))
        self.call("/api/devices/"+did,"PATCH",
                  {"building_width":35,"building_height":25,"building_rotation":359})
        state,_=self.call("/api/state")
        row=next(d for d in state["devices"] if d["id"]==did)
        self.assertEqual((row["building_width"],row["building_height"],row["building_rotation"]),
                         (35,25,359))
        self.call("/api/devices/"+did,"DELETE")
        state,_=self.call("/api/state")
        self.assertFalse(any(d["id"]==did for d in state["devices"]))

    def test_old_house_remains_marker_until_user_opts_in(self):
        result,_=self.call("/api/devices","POST",{"name":"Дом","kind":"house"})
        did=result["id"]
        state,_=self.call("/api/state")
        row=next(d for d in state["devices"] if d["id"]==did)
        self.assertEqual(row["building_shape"],0)
        self.call("/api/devices/"+did,"PATCH",
                  {"building_shape":True,"building_width":20,"building_rotation":90})
        state,_=self.call("/api/state")
        row=next(d for d in state["devices"] if d["id"]==did)
        self.assertEqual((row["building_shape"],row["building_width"],row["building_rotation"]),
                         (1,20,90))
        self.call("/api/devices/"+did,"PATCH",{"kind":"camera"})
        state,_=self.call("/api/state")
        row=next(d for d in state["devices"] if d["id"]==did)
        self.assertEqual(row["building_shape"],0)
        self.call("/api/devices/"+did,"DELETE")

    def test_reject_invalid_dimensions_and_shape_without_creating_records(self):
        invalid=[
            ("building_shape","yes"),("building_shape",2),
            ("building_width",2),("building_width",66),("building_width",True),
            ("building_height",0),("building_height",66),("building_height",10.5),
            ("building_rotation",-1),("building_rotation",360),
            ("building_rotation","0"),
        ]
        before,_=self.call("/api/state")
        for field,bad in invalid:
            with self.subTest(field=field,bad=bad):
                with self.assertRaises(HTTPError) as denied:
                    self.call("/api/devices","POST",
                              {"name":"Invalid","kind":"building",field:bad})
                self.assertEqual(denied.exception.code,400)
        after,_=self.call("/api/state")
        self.assertEqual(len(before["devices"]),len(after["devices"]))

    def test_non_building_shapes_are_reset(self):
        created,_=self.call("/api/devices","POST",
            {"name":"Камера","kind":"camera","building_shape":True,"building_width":30})
        state,_=self.call("/api/state")
        row=next(d for d in state["devices"] if d["id"]==created["id"])
        self.assertEqual((row["building_shape"],row["building_width"]), (0,15))
        self.call("/api/devices/"+created["id"],"DELETE")


if __name__ == "__main__":
    unittest.main()
