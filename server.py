#!/usr/bin/env python3
"""MiniHomeServer v0.1 — local, dependency-free territory dashboard API.

No simulated telemetry and no remote device-control endpoints. Devices only
become controllable once a validated integration has been installed.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = Path(__file__).resolve().parent
DATA = Path(os.getenv("MINIHOME_DATA_DIR", str(BASE / "data"))).resolve()
DB = DATA / "minihome.db"
PASSWORD = os.getenv("MINIHOME_ADMIN_PASSWORD", "")
PASSWORD_FILE = os.getenv("MINIHOME_ADMIN_PASSWORD_FILE")
if not PASSWORD and PASSWORD_FILE:
    PASSWORD = Path(PASSWORD_FILE).read_text(encoding="utf-8").rstrip("\r\n")
HOST = os.getenv("MINIHOME_HOST", "127.0.0.1")
PORT = int(os.getenv("MINIHOME_PORT", "8080"))
SECURE_COOKIE = os.getenv("MINIHOME_COOKIE_SECURE", "0") == "1"
MAX_BODY = 13 * 1024 * 1024
SESSION_TTL = 12 * 3600
KINDS = {"house", "boiler", "camera", "light", "gate", "sensor", "switch", "other"}
FAILURES: dict[str, list[float]] = {}
LOCK = threading.RLock()
VERSION = "0.2.0"
GITHUB_API = "https://api.github.com/repos/DeadSoulf/MiniHomeServer/commits/main"
REVISION_FILE = BASE / ".release-commit"
UPDATE_STATUS = DATA / "update-status.json"
UPDATE_REQUEST = DATA / "update-request.json"
UPDATE_UNIT = Path("/etc/systemd/system/minihome-update.path")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def database():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    with database() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            zone TEXT NOT NULL,
            x REAL NOT NULL,
            y REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'unknown',
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            csrf TEXT NOT NULL,
            expires REAL NOT NULL
        );
        """)
        db.execute("PRAGMA journal_mode=WAL")


def installed_sha():
    try:
        value = REVISION_FILE.read_text(encoding="ascii").strip()
        return value if SHA_RE.fullmatch(value) else None
    except OSError:
        return None


def update_status():
    try:
        if UPDATE_STATUS.stat().st_size > 8192:
            raise ValueError("Status file too large")
        data = json.loads(UPDATE_STATUS.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid status")
    except (OSError, ValueError):
        data = {"state": "idle", "message": "Проверка обновлений ещё не запускалась"}
    return {
        "enabled": UPDATE_UNIT.is_file(),
        "state": data.get("state", "idle"),
        "step": data.get("step", ""),
        "message": data.get("message", ""),
        "target_sha": data.get("target_sha", ""),
        "current_sha": installed_sha(),
        "updated_at": data.get("updated_at", ""),
        "source": "DeadSoulf/MiniHomeServer",
        "branch": "main",
    }


def github_latest_sha():
    request = Request(GITHUB_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "MiniHomeServer/0.2",
    })
    try:
        with urlopen(request, timeout=8) as response:
            data = json.loads(response.read(32768))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise ValueError("Не удалось проверить GitHub. Повторите попытку позже") from exc
    sha = data.get("sha") if isinstance(data, dict) else None
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ValueError("GitHub вернул некорректную версию")
    return sha


def atomic_json(path, payload):
    tmp = DATA / (".update-tmp-" + secrets.token_hex(8))
    try:
        with tmp.open("x", encoding="utf-8") as out:
            json.dump(payload, out, ensure_ascii=False, separators=(",", ":"))
            out.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def session_hash(sid):
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def add_event(db, action: str, detail: str):
    db.execute("INSERT INTO events(at,action,detail) VALUES(?,?,?)",
               (timestamp(), action, detail[:180]))


def setting(db, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def snapshot() -> dict:
    with database() as db:
        devices = [dict(row) for row in db.execute(
            "SELECT id,name,kind,zone,x,y,state,updated_at "
            "FROM devices ORDER BY name COLLATE NOCASE"
        )]
        events = [dict(row) for row in db.execute(
            "SELECT id,at,action,detail FROM events ORDER BY id DESC LIMIT 60"
        )]
        stamp = setting(db, "map_updated")
        return {
            "version": VERSION,
            "devices": devices,
            "events": events,
            "map_url": f"/media/territory?v={stamp}" if stamp else None,
            "integrations": [],
        }


def text_field(data, key, limit=100, required=False) -> str:
    v = data.get(key, "")
    if not isinstance(v, str):
        raise ValueError(f"{key}: должно быть строкой")
    v = v.strip()
    if len(v) > limit or (required and not v):
        raise ValueError(f"{key}: некорректная длина")
    return v


def coords(data, key, default=None) -> float:
    value = data.get(key, default)
    if type(value) not in (int, float):
        raise ValueError(f"{key}: ожидается число")
    value = float(value)
    if not (0 <= value <= 1):
        raise ValueError(f"{key}: диапазон от 0 до 1")
    return round(value, 5)


def device_values(data, current=None):
    if not isinstance(data, dict):
        raise ValueError("Ожидается объект JSON")
    base = current or {}
    name = text_field(data, "name", 80, required=True) if "name" in data or not current else base["name"]
    kind = text_field(data, "kind", 30) if "kind" in data or not current else base["kind"]
    zone = text_field(data, "zone", 80) if "zone" in data or not current else base["zone"]
    if kind not in KINDS:
        raise ValueError("Неизвестный тип объекта")
    x = coords(data, "x", base.get("x", 0.5))
    y = coords(data, "y", base.get("y", 0.5))
    return name, kind, zone, x, y


def image_bytes(data_url: str):
    if not isinstance(data_url, str):
        raise ValueError("Изображение не передано")
    m = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", data_url)
    if not m:
        raise ValueError("Загрузите PNG, JPEG или WebP")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except binascii.Error:
        raise ValueError("Повреждённый файл")
    if not raw or len(raw) > 8 * 1024 * 1024:
        raise ValueError("Максимальный размер изображения — 8 МБ")
    image_type = m.group(1)
    valid = {
        "png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpeg": raw.startswith(b"\xff\xd8\xff"),
        "webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
    }
    if not valid[image_type]:
        raise ValueError("Содержимое файла не соответствует формату")
    return raw, "image/" + image_type


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniHomeServer"
    sys_version = ""

    def log_message(self, fmt, *args):
        print(f"{timestamp()} {self.client_address[0]} {fmt % args}", flush=True)

    def send_bytes(self, status, payload, mime="application/json", cookie=None, cache=False):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        elif isinstance(payload, str):
            payload = payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") or mime == "application/json" else ""))
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
                         "frame-ancestors 'none'; base-uri 'self'")
        self.send_header("Cache-Control", "public, max-age=3600" if cache else "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def error(self, status, message):
        self.send_bytes(status, {"error": message})

    def body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Неверный Content-Length")
        if n <= 0 or n > MAX_BODY:
            raise ValueError("Неверный размер запроса (макс. 13 МБ)")
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ValueError("Ожидается application/json")
        try:
            data = json.loads(self.rfile.read(n))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Неверный JSON")
        if not isinstance(data, dict):
            raise ValueError("Ожидается объект JSON")
        return data

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            sid = cookie["mh_session"].value
        except Exception:
            return None
        with database() as db:
            row = db.execute(
                "SELECT csrf,expires FROM sessions WHERE token_hash=?",
                (session_hash(sid),)
            ).fetchone()
            if row is None:
                return None
            if row["expires"] < time.time():
                db.execute("DELETE FROM sessions WHERE token_hash=?", (session_hash(sid),))
                return None
            return dict(row)

    def require(self, write=False):
        session = self.session()
        if not session:
            self.error(401, "Требуется вход")
            return False
        if write and not hmac.compare_digest(
            self.headers.get("X-CSRF-Token", ""), session["csrf"]
        ):
            self.error(403, "Проверка CSRF не пройдена")
            return False
        return True

    def do_GET(self):
        try:
            self.get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print("GET error:", repr(exc), flush=True)
            self.error(500, "Ошибка сервера")

    def get(self):
        path = urlsplit(self.path).path
        if path == "/api/health":
            return self.send_bytes(200, {"ok": True, "version": VERSION})
        if path == "/api/session":
            session = self.session()
            return self.send_bytes(200, {
                "authenticated": bool(session),
                "csrf": session["csrf"] if session else None,
                "version": VERSION,
            })
        if path == "/api/state":
            if self.require():
                return self.send_bytes(200, snapshot())
            return
        if path == "/api/update/status":
            if not self.require():
                return
            return self.send_bytes(200, update_status())
        if path == "/api/update/check":
            if not self.require():
                return
            try:
                latest = github_latest_sha()
            except ValueError as exc:
                return self.error(502, str(exc))
            current = installed_sha()
            return self.send_bytes(200, {
                "current_sha": current, "latest_sha": latest,
                "update_available": latest != current,
                "source": "DeadSoulf/MiniHomeServer", "branch": "main",
            })
        if path == "/media/territory":
            if not self.require():
                return
            with database() as db:
                mime = setting(db, "map_mime")
            path_img = DATA / "territory"
            if not mime or not path_img.is_file():
                return self.error(404, "Карта отсутствует")
            return self.send_bytes(200, path_img.read_bytes(), mime)
        static_files = {
            "/": ("index.html", "text/html"),
            "/index.html": ("index.html", "text/html"),
            "/app.js": ("app.js", "text/javascript"),
            "/style.css": ("style.css", "text/css"),
        }
        if path in static_files:
            name, mime = static_files[path]
            return self.send_bytes(200, (BASE / "static" / name).read_bytes(), mime)
        self.error(404, "Не найдено")

    def do_POST(self):
        self.mutate("POST")

    def do_PATCH(self):
        self.mutate("PATCH")

    def do_DELETE(self):
        self.mutate("DELETE")

    def mutate(self, method):
        try:
            self.change(method)
        except ValueError as exc:
            self.error(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print("WRITE error:", repr(exc), flush=True)
            self.error(500, "Ошибка сервера")

    def change(self, method):
        path = urlsplit(self.path).path
        if method == "POST" and path == "/api/login":
            data = self.body()
            supplied = data.get("password")
            if not isinstance(supplied, str):
                return self.error(400, "Введите пароль")
            ip = self.client_address[0]
            now = time.time()
            with LOCK:
                recent = [t for t in FAILURES.get(ip, []) if now - t < 600]
                FAILURES[ip] = recent
                if len(recent) >= 5:
                    return self.error(429, "Слишком много попыток. Повторите через 10 минут")
                if not hmac.compare_digest(supplied.encode(), PASSWORD.encode()):
                    recent.append(now)
                    return self.error(401, "Неверный пароль")
                FAILURES.pop(ip, None)
                sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            with database() as db:
                db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
                db.execute(
                    "INSERT INTO sessions(token_hash,csrf,expires) VALUES(?,?,?)",
                    (session_hash(sid), csrf, now + SESSION_TTL),
                )
            cookie = (f"mh_session={sid}; Path=/; Max-Age={SESSION_TTL}; "
                      f"HttpOnly; SameSite=Strict" + ("; Secure" if SECURE_COOKIE else ""))
            with database() as db:
                add_event(db, "auth.login", "Вход администратора")
            return self.send_bytes(200, {"authenticated": True, "csrf": csrf}, cookie=cookie)

        if not self.require(write=True):
            return

        if method == "POST" and path == "/api/logout":
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            sid = cookie.get("mh_session")
            if sid:
                with database() as db:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (session_hash(sid.value),))
            expired = "mh_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
            if SECURE_COOKIE:
                expired += "; Secure"
            return self.send_bytes(200, {"ok": True}, cookie=expired)

        if method == "POST" and path == "/api/update/install":
            if not UPDATE_UNIT.is_file():
                return self.error(409, "Сначала подключите службу обновлений на Debian")
            data = self.body()
            requested = data.get("sha")
            if not isinstance(requested, str) or not SHA_RE.fullmatch(requested):
                return self.error(400, "Неверный идентификатор версии")
            with LOCK:
                status = update_status()
                if status["state"] in ("queued", "running") or UPDATE_REQUEST.exists():
                    return self.error(409, "Обновление уже выполняется")
                if requested == installed_sha():
                    return self.error(409, "Эта версия уже установлена")
                try:
                    actual = github_latest_sha()
                except ValueError as exc:
                    return self.error(502, str(exc))
                if actual != requested:
                    return self.error(409, "На GitHub появилась другая версия. Проверьте ещё раз")
                atomic_json(UPDATE_STATUS, {
                    "state": "queued",
                    "step": "queued",
                    "message": "Запрос обновления принят; ожидаем системную службу",
                    "current_sha": installed_sha(),
                    "target_sha": requested,
                    "updated_at": timestamp(),
                })
                atomic_json(UPDATE_REQUEST, {"sha": requested, "requested_at": timestamp()})
                with database() as db:
                    add_event(db, "update.queued", f"Заказано обновление {requested[:12]}")
            return self.send_bytes(202, {"ok": True, "sha": requested})

        if method == "POST" and path == "/api/map":
            data = self.body()
            raw, mime = image_bytes(data.get("data_url"))
            tmp = DATA / ("territory.tmp-" + secrets.token_hex(6))
            try:
                tmp.write_bytes(raw)
                os.replace(tmp, DATA / "territory")
            finally:
                tmp.unlink(missing_ok=True)
            with database() as db:
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('map_mime',?)", (mime,))
                db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('map_updated',?)",
                           (str(time.time_ns()),))
                add_event(db, "map.updated", "Обновлена карта территории")
            return self.send_bytes(200, {"ok": True})

        if method == "POST" and path == "/api/devices":
            name, kind, zone, x, y = device_values(self.body())
            did = str(uuid.uuid4())
            with database() as db:
                db.execute("INSERT INTO devices(id,name,kind,zone,x,y) VALUES(?,?,?,?,?,?)",
                           (did, name, kind, zone, x, y))
                add_event(db, "device.added", f"Добавлен объект: {name}")
            return self.send_bytes(201, {"ok": True, "id": did})

        match = re.fullmatch(r"/api/devices/([0-9a-f-]{36})", path)
        if match and method in ("PATCH", "DELETE"):
            did = match.group(1)
            with database() as db:
                row = db.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
                if row is None:
                    return self.error(404, "Объект не найден")
                if method == "DELETE":
                    db.execute("DELETE FROM devices WHERE id=?", (did,))
                    add_event(db, "device.deleted", f"Удалён объект: {row['name']}")
                else:
                    name, kind, zone, x, y = device_values(self.body(), dict(row))
                    db.execute("UPDATE devices SET name=?,kind=?,zone=?,x=?,y=? WHERE id=?",
                               (name, kind, zone, x, y, did))
                    add_event(db, "device.updated", f"Изменён объект: {name}")
            return self.send_bytes(200, {"ok": True})
        self.error(404, "Неизвестная операция")


def main():
    if len(PASSWORD) < 12:
        raise SystemExit("Set MINIHOME_ADMIN_PASSWORD (minimum 12 characters) before starting")
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"MiniHomeServer v{VERSION}: http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
