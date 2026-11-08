"""Regression tests for SQLite schema upgrades and updater access checks.

Tests work on temporary data only; no actual Debian service is changed.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


class SqliteUpgradeTests(unittest.TestCase):
    def test_old_database_migrates_without_losing_devices_or_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            db_path = location / "minihome.db"
            with sqlite3.connect(db_path) as db:
                db.executescript("""
                    CREATE TABLE devices (
                      id TEXT PRIMARY KEY,
                      name TEXT NOT NULL,
                      kind TEXT NOT NULL,
                      zone TEXT NOT NULL,
                      x REAL NOT NULL, y REAL NOT NULL,
                      state TEXT NOT NULL DEFAULT 'unknown',
                      updated_at TEXT
                    );
                    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE events (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      at TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL
                    );
                    CREATE TABLE sessions (
                      token_hash TEXT PRIMARY KEY,
                      csrf TEXT NOT NULL,
                      expires REAL NOT NULL
                    );
                """)
                db.execute("INSERT INTO devices(id,name,kind,zone,x,y) VALUES(?,?,?,?,?,?)",
                           ("existing", "Existing boiler", "boiler", "room", 0.4, 0.7))
                db.execute("INSERT INTO settings(key,value) VALUES(?,?)", ("map_updated", "original"))
                db.execute("INSERT INTO sessions(token_hash,csrf,expires) VALUES(?,?,?)",
                           ("session-hash", "csrf-token", 9999999999.0))
                db.commit()
            saved_data, saved_db = server.DATA, server.DB
            try:
                server.DATA, server.DB = location, db_path
                server.init_db()
                server.init_db()  # Restarting must not repeat any ALTER operation.
                with sqlite3.connect(db_path) as db:
                    tables = {r[0] for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )}
                    self.assertIn("lan_nodes", tables)
                    self.assertIn("zigbee_nodes", tables)
                    columns = {row[1] for row in db.execute("PRAGMA table_info(devices)")}
                    self.assertTrue({"camera_fov", "camera_direction", "camera_reach"} <= columns)
                    self.assertEqual(db.execute(
                        "SELECT camera_fov,camera_direction,camera_reach FROM devices"
                    ).fetchone(), (90, 0, 25))
                    self.assertEqual(db.execute("SELECT name FROM devices").fetchone()[0],
                                     "Existing boiler")
                    self.assertEqual(db.execute("SELECT value FROM settings").fetchone()[0],
                                     "original")
                    self.assertEqual(db.execute("SELECT csrf FROM sessions").fetchone()[0],
                                     "csrf-token")
            finally:
                server.DATA, server.DB = saved_data, saved_db


class SqlitePermissionChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "scripts" / "update_worker.py"
        spec = importlib.util.spec_from_file_location("sqlite_update_worker", path)
        cls.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.worker)

    def test_preflight_rejects_nonwritable_database_without_mutating(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir(mode=0o700)
            db = data / "minihome.db"
            db.write_bytes(b"sqlite-fixture-not-an-open-database")
            os.chmod(db, 0o600)
            original_data = self.worker.DATA
            try:
                self.worker.DATA = data
                with patch("pwd.getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid())):
                    self.worker.verify_sqlite_permissions()
                    os.chmod(db, 0o400)
                    with self.assertRaisesRegex(RuntimeError, "SQLite недоступна"):
                        self.worker.verify_sqlite_permissions()
                    self.assertEqual(db.read_bytes(), b"sqlite-fixture-not-an-open-database")
            finally:
                self.worker.DATA = original_data

    def test_bootstrap_contains_backup_and_systemd_state_directory(self):
        bootstrap = (ROOT / "scripts" / "enable-web-updates.sh").read_text(encoding="utf-8")
        installer = (ROOT / "scripts" / "install-systemd.sh").read_text(encoding="utf-8")
        self.assertIn("minihome-pre-migration.db", bootstrap)
        self.assertIn('connection.execute("BEGIN IMMEDIATE")', bootstrap)
        self.assertIn("StateDirectory=minihome", bootstrap)
        self.assertIn("StateDirectoryMode=0700", bootstrap)
        self.assertIn("StateDirectory=minihome", installer)


if __name__ == "__main__":
    unittest.main()
