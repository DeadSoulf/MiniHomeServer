"""Camera overlay metadata must survive CRUD and reject invalid JSON."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server

class CameraTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.previous = (server.DATA, server.DB, server.PASSWORD)
        server.DATA = Path(cls.tmp.name)
        server.DB = server.DATA / "camera-test.db"
        server.PASSWORD = "camera-test-local-password"
        server.init_db()
        cls.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.http.server_port)
        cls.client = build_opener(HTTPCookieProcessor(CookieJar()))
        login = Request(cls.base+"/api/login", method="POST",
                        data=json.dumps({"password":server.PASSWORD}).encode(),
                        headers={"Content-Type":"application/json"})
        with cls.client.open(login, timeout=5) as res:
            cls.csrf = json.load(res)["csrf"]

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(timeout=5)
        server.DATA, server.DB, server.PASSWORD = cls.previous
        cls.tmp.cleanup()

    def call(self, url, method="GET", body=None, auth=True):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type":"application/json"} if data else {}
        if method != "GET" and auth:
            headers["X-CSRF-Token"] = self.csrf
        req = Request(self.base+url, data=data, headers=headers, method=method)
        with self.client.open(req, timeout=5) as res:
            return json.load(res), res.status

    def test_create_move_edit_delete_camera(self):
        new = {"name":"Камера двора", "kind":"camera", "x":0.25,"y":0.4,
               "camera_fov":120, "camera_direction":225, "camera_reach":45}
        with self.assertRaises(HTTPError) as denied:
            self.call("/api/devices","POST",new,auth=False)
        self.assertEqual(denied.exception.code,403)
        created,status = self.call("/api/devices","POST",new)
        self.assertEqual(status,201)
        did=created["id"]
        state,_=self.call("/api/state")
        row=next(r for r in state["devices"] if r["id"]==did)
        self.assertEqual((row["camera_fov"],row["camera_direction"],row["camera_reach"]),
                         (120,225,45))
        self.call("/api/devices/"+did,"PATCH",{"x":0.65,"y":0.15})
        state,_=self.call("/api/state")
        row=next(r for r in state["devices"] if r["id"]==did)
        self.assertEqual((row["x"],row["camera_fov"],row["camera_direction"]),
                         (0.65,120,225))
        self.call("/api/devices/"+did,"PATCH",
                  {"camera_fov":360,"camera_direction":0,"camera_reach":20})
        state,_=self.call("/api/state")
        row=next(r for r in state["devices"] if r["id"]==did)
        self.assertEqual((row["camera_fov"],row["camera_direction"],row["camera_reach"]),
                         (360,0,20))
        self.call("/api/devices/"+did,"DELETE")
        state,_=self.call("/api/state")
        self.assertFalse(any(r["id"]==did for r in state["devices"]))

    def test_reject_bad_camera_parameters(self):
        cases=[("camera_fov",0),("camera_fov",361),("camera_fov","90"),
               ("camera_fov",True),("camera_direction",-1),("camera_direction",360),
               ("camera_reach",4),("camera_reach",66),("camera_reach",25.5)]
        for name,value in cases:
            with self.subTest(name=name,value=value):
                with self.assertRaises(HTTPError) as denied:
                    self.call("/api/devices","POST",
                              {"name":"Invalid","kind":"camera",name:value})
                self.assertEqual(denied.exception.code,400)

    def test_other_object_defaults(self):
        created,_=self.call("/api/devices","POST",{"name":"Фонарь","kind":"light"})
        state,_=self.call("/api/state")
        row=next(r for r in state["devices"] if r["id"]==created["id"])
        self.assertEqual((row["camera_fov"],row["camera_direction"],row["camera_reach"]),
                         (90,0,25))
        self.call("/api/devices/"+created["id"],"DELETE")
