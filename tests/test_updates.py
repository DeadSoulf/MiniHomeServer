"""Update authorization and integrity tests. No network or privileged commands."""
import importlib.util
import json
from types import SimpleNamespace
import shutil
import sqlite3
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import sys
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import server


class UpdateAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        server.DATA = Path(cls.tempdir.name)
        server.DB = server.DATA / "db.sqlite"
        server.UPDATE_STATUS = server.DATA / "update-status.json"
        server.UPDATE_REQUEST = server.DATA / "update-request.json"
        server.UPDATE_UNIT = server.DATA / "minihome-update.path"
        server.UPDATE_UNIT.touch()
        server.REVISION_FILE = server.DATA / ".release-commit"
        server.PASSWORD = "test-only-password-over-twelve"
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
        cls.thread.join(timeout=5)
        cls.tempdir.cleanup()

    def request(self, path, method="GET", data=None, csrf=None):
        body = json.dumps(data).encode() if data is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        req = Request(self.base + path, method=method, data=body, headers=headers)
        with self.client.open(req, timeout=5) as response:
            return json.load(response), response.status

    def test_recovers_stale_queue_only_when_service_stopped(self):
        login, _ = self.request("/api/login", "POST", {"password": server.PASSWORD})
        csrf = login["csrf"]
        sha = "a" * 40
        server.atomic_json(server.UPDATE_STATUS, {
            "state": "queued",
            "step": "queued",
            "message": "old request",
            "target_sha": sha,
            "updated_at": "2000-01-01T00:00:00+00:00",
        })
        server.atomic_json(server.UPDATE_REQUEST, {"sha": sha})
        with patch.object(server.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            with self.assertRaises(HTTPError) as ctx:
                self.request("/api/update/recover", "POST", {}, csrf)
            self.assertEqual(ctx.exception.code, 409)
        self.assertTrue(server.UPDATE_REQUEST.exists())
        with patch.object(server.subprocess, "run", return_value=SimpleNamespace(returncode=3)):
            response, code = self.request("/api/update/recover", "POST", {}, csrf)
            self.assertTrue(response["ok"])
            self.assertEqual(code, 200)
        self.assertFalse(server.UPDATE_REQUEST.exists())
        status, _ = self.request("/api/update/status")
        self.assertEqual(status["state"], "failed")

    def test_pinned_github_update_requires_session_and_csrf(self):
        sha = "a" * 40
        with self.assertRaises(HTTPError) as ctx:
            self.request("/api/update/status")
        self.assertEqual(ctx.exception.code, 401)

        login, status = self.request("/api/login", "POST", {"password": server.PASSWORD})
        self.assertEqual(status, 200)
        csrf = login["csrf"]

        current, _ = self.request("/api/update/status")
        self.assertTrue(current["enabled"])

        with patch.object(server, "github_latest_sha", return_value=sha):
            checked, _ = self.request("/api/update/check")
            self.assertEqual(checked["latest_sha"], sha)
            self.assertTrue(checked["update_available"])

            with self.assertRaises(HTTPError) as ctx:
                self.request("/api/update/install", "POST", {"sha": sha})
            self.assertEqual(ctx.exception.code, 403)

            with self.assertRaises(HTTPError) as ctx:
                self.request("/api/update/install", "POST", {"sha": "../../etc/passwd"}, csrf)
            self.assertEqual(ctx.exception.code, 400)

            accepted, status = self.request("/api/update/install", "POST", {"sha": sha}, csrf)
            self.assertEqual(status, 202)
            self.assertEqual(accepted["sha"], sha)
            self.assertEqual(json.loads(server.UPDATE_REQUEST.read_text())["sha"], sha)
            pending, _ = self.request("/api/update/status")
            self.assertEqual(pending["state"], "queued")

            with self.assertRaises(HTTPError) as ctx:
                self.request("/api/update/install", "POST", {"sha": sha}, csrf)
            self.assertEqual(ctx.exception.code, 409)

        with server.database() as db:
            row = db.execute("SELECT COUNT(*) FROM sessions").fetchone()
            self.assertGreaterEqual(row[0], 1)
            self.assertIsNone(db.execute("SELECT token_hash FROM sessions WHERE token_hash=?",
                                         (csrf,)).fetchone())

        self.request("/api/logout", "POST", {}, csrf)
        with self.assertRaises(HTTPError) as ctx:
            self.request("/api/update/status")
        self.assertEqual(ctx.exception.code, 401)


class WorkerIntegrityTests(unittest.TestCase):
    def test_manifest_detects_local_changes(self):
        spec = importlib.util.spec_from_file_location("minihome_update_worker",
                                                       REPO / "scripts" / "update_worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker.TARGET = root
            worker.MANIFEST = root / ".release-manifest.json"
            for name in worker.FILES:
                src = REPO / name
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
            worker.write_manifest()
            worker.verify_existing_files()
            (root / "static" / "app.js").write_text("modified", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Локальные изменения"):
                worker.verify_existing_files()

    def test_sidecar_helper_rejects_symlinks_without_changes(self):
        spec = importlib.util.spec_from_file_location("minihome_sidecars",
                                                       REPO / "scripts" / "update_worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        with tempfile.TemporaryDirectory() as tmp:
            original_data = worker.DATA
            try:
                worker.DATA = Path(tmp)
                target = Path(tmp) / "unrelated"
                target.write_text("keep", encoding="utf-8")
                (Path(tmp) / "minihome.db-shm").symlink_to(target)
                with self.assertRaisesRegex(RuntimeError, "ссылка SQLite"):
                    worker.restore_sqlite_sidecar_ownership()
                self.assertEqual(target.read_text(encoding="utf-8"), "keep")
            finally:
                worker.DATA = original_data

    def test_sidecar_helper_does_not_touch_other_files(self):
        spec = importlib.util.spec_from_file_location("minihome_sidecars_files",
                                                       REPO / "scripts" / "update_worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        with tempfile.TemporaryDirectory() as tmp:
            original_data = worker.DATA
            try:
                worker.DATA = Path(tmp)
                protected = Path(tmp) / "territory"
                protected.write_bytes(b"original map")
                worker.restore_sqlite_sidecar_ownership()
                self.assertEqual(protected.read_bytes(), b"original map")
            finally:
                worker.DATA = original_data

    def test_updater_source_python_compiles(self):
        for name in ("server.py", "scripts/update_worker.py"):
            path = REPO / name
            compile(path.read_bytes(), str(path), "exec")

    def test_rollback_restores_code_without_overwriting_data(self):
        spec = importlib.util.spec_from_file_location("update_worker_rollback",
                                                       REPO / "scripts" / "update_worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker.TARGET = root / "app"
            worker.TARGET.mkdir()
            worker.DATA = root / "data"
            worker.DATA.mkdir()
            worker.BACKUPS = root / "backups"
            worker.REVISION = worker.TARGET / ".release-commit"
            worker.MANIFEST = worker.TARGET / ".release-manifest.json"
            worker.WORKER = root / "worker.py"
            worker.REVISION.write_text("b" * 40)
            for name in worker.FILES:
                dest = worker.TARGET / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO / name, dest)
            worker.write_manifest()
            conn = sqlite3.connect(worker.DATA / "minihome.db")
            conn.execute("CREATE TABLE example (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO example(value) VALUES('saved')")
            conn.commit()
            conn.close()
            backup = worker.store_backup()
            (worker.TARGET / "server.py").write_text("broken code")
            worker.REVISION.write_text("a" * 40)
            with patch.object(worker, "run_command", return_value=""):
                worker.rollback(backup)
            self.assertEqual((worker.TARGET / "server.py").read_bytes(),
                             (REPO / "server.py").read_bytes())
            self.assertEqual(worker.REVISION.read_text().strip(), "b" * 40)
            conn = sqlite3.connect(worker.DATA / "minihome.db")
            self.assertEqual(conn.execute("SELECT value FROM example").fetchone()[0], "saved")
            conn.close()



if __name__ == "__main__":
    unittest.main()
