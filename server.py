#!/usr/bin/env python3
"""MiniHomeServer v0.1 — local, dependency-free territory dashboard API.

No simulated telemetry and no remote device-control endpoints. Devices only
become controllable once a validated integration has been installed.
"""
from __future__ import annotations

import base64
import errno
import ipaddress
import shutil
import socket
import struct
import subprocess
import concurrent.futures
import binascii
import hmac
import hashlib
import json
import math
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
KINDS = {"house", "building", "boiler", "camera", "light", "gate", "sensor", "switch", "other"}
FAILURES: dict[str, list[float]] = {}
LOCK = threading.RLock()
VERSION = "0.3.9"
GITHUB_API = "https://api.github.com/repos/DeadSoulf/MiniHomeServer/commits/main"
REVISION_FILE = BASE / ".release-commit"
UPDATE_STATUS = DATA / "update-status.json"
UPDATE_REQUEST = DATA / "update-request.json"
UPDATE_UNIT = Path("/etc/systemd/system/minihome-update.path")
UPDATE_STALE_SECONDS = 600
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SCAN_LOCK = threading.RLock()
NETWORK_SCAN = {"running": False, "last_error": "", "started_at": None,
                "finished_at": None, "subnets": [], "found": 0}
ZIGBEE_SCAN = {"running": False, "last_error": "", "finished_at": None,
               "found": 0, "connected": False}
GATEWAY_CONFIG = DATA / "zigbee-gateway.json"



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
        CREATE TABLE IF NOT EXISTS lan_nodes (
            id TEXT PRIMARY KEY,
            ip TEXT NOT NULL,
            mac TEXT NOT NULL DEFAULT '',
            hostname TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'other',
            vendor TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            online INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS zigbee_nodes (
            ieee TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            vendor TEXT NOT NULL DEFAULT '',
            availability TEXT NOT NULL DEFAULT 'unknown',
            bridge_ip TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        """)
        # Preserve old objects, their coordinates, and all existing settings.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(devices)")}
        for name, definition in (
            ("camera_fov", "INTEGER NOT NULL DEFAULT 90"),
            ("camera_direction", "INTEGER NOT NULL DEFAULT 0"),
            ("camera_reach", "INTEGER NOT NULL DEFAULT 25"),
            ("building_shape", "INTEGER NOT NULL DEFAULT 0"),
            ("building_width", "INTEGER NOT NULL DEFAULT 15"),
            ("building_height", "INTEGER NOT NULL DEFAULT 12"),
            ("building_rotation", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE devices ADD COLUMN {name} {definition}")
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
    state = data.get("state", "idle")
    message = data.get("message", "")
    if state in ("queued", "running"):
        try:
            updated = datetime.fromisoformat(data.get("updated_at", "")).timestamp()
        except (ValueError, TypeError, OverflowError):
            try:
                updated = UPDATE_STATUS.stat().st_mtime
            except OSError:
                updated = time.time()
        if time.time() - updated > UPDATE_STALE_SECONDS:
            state = "stalled"
            message = ("Обновление не завершилось вовремя. "
                       "Можно восстановить очередь и повторить попытку.")
    return {
        "enabled": UPDATE_UNIT.is_file(),
        "state": state,
        "step": data.get("step", ""),
        "message": message,
        "target_sha": data.get("target_sha", ""),
        "current_sha": installed_sha(),
        "current_version": VERSION,
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


def github_version_for_sha(sha):
    """Read the application version from server.py at the pinned GitHub SHA."""
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ValueError("Некорректная ревизия GitHub")
    req = Request(
        "https://api.github.com/repos/DeadSoulf/MiniHomeServer/contents/server.py?ref=" + sha,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "MiniHomeServer/0.3"},
    )
    try:
        with urlopen(req, timeout=8) as response:
            item = json.loads(response.read(200000))
        if not isinstance(item, dict) or item.get("encoding") != "base64":
            raise ValueError("GitHub не предоставил версию")
        source = base64.b64decode(item["content"]).decode("utf-8")
        match = re.search(r'^VERSION\s*=\s*"([0-9]+(?:\.[0-9]+){2})"\s*$', source, re.MULTILINE)
        if not match:
            raise ValueError("Нет версии приложения в указанном коммите")
        return match.group(1)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError,
            TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Не удалось получить номер версии GitHub") from exc


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


def map_scale(db):
    """Stored image extents in metres; unset until the owner calibrates them."""
    try:
        width = float(setting(db, "map_width_m"))
        height = float(setting(db, "map_height_m"))
    except ValueError:
        return None
    if not (math.isfinite(width) and math.isfinite(height)
            and 1 <= width <= 5000 and 1 <= height <= 5000):
        return None
    return {"width_m": width, "height_m": height}


def map_dimension(data, key):
    value = data.get(key)
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError(key + ": укажите число метров")
    if not 1 <= value <= 5000:
        raise ValueError(key + ": допустимое значение от 1 до 5000 м")
    return round(float(value), 2)


def percent_field(data, key, default):
    """Decimal percentages allow metre-level building sizes without rounding."""
    value = data.get(key, default)
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError(key + ": укажите процент числом")
    if not 0.1 <= value <= 65:
        raise ValueError(key + ": допустимый размер 0.1–65% плана")
    return round(float(value), 4)


def snapshot() -> dict:
    with database() as db:
        devices = [dict(row) for row in db.execute(
            "SELECT id,name,kind,zone,x,y,state,updated_at,"
            "camera_fov,camera_direction,camera_reach,"
            "building_shape,building_width,building_height,building_rotation "
            "FROM devices ORDER BY name COLLATE NOCASE"
        )]
        events = [dict(row) for row in db.execute(
            "SELECT id,at,action,detail FROM events ORDER BY id DESC LIMIT 60"
        )]
        stamp = setting(db, "map_updated")
        scale = map_scale(db)
    with SCAN_LOCK:
        zigbee_connected = bool(ZIGBEE_SCAN["connected"])
    return {
        "version": VERSION,
        "devices": devices,
        "events": events,
        "map_url": f"/media/territory?v={stamp}" if stamp else None,
        "map_scale": scale,
        "integrations": ([{"name": "Zigbee2MQTT", "connected": True}]
                         if zigbee_connected else []),
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


def integer_field(data, key, default, lower, upper):
    value = data.get(key, default)
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{key}: требуется целое число от {lower} до {upper}")
    return value


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
    if kind == "camera":
        fov = integer_field(data, "camera_fov", base.get("camera_fov", 90), 15, 360)
        direction = integer_field(data, "camera_direction",
                                  base.get("camera_direction", 0), 0, 359)
        reach = integer_field(data, "camera_reach", base.get("camera_reach", 25), 5, 65)
    else:
        fov, direction, reach = 90, 0, 25
    if kind in ("building", "house", "boiler"):
        shape = data.get("building_shape", base.get("building_shape", 0))
        if type(shape) is bool:
            shape = int(shape)
        elif type(shape) is not int or shape not in (0, 1):
            raise ValueError("building_shape: ожидается да/нет")
        width = percent_field(data, "building_width", base.get("building_width", 15))
        height = percent_field(data, "building_height", base.get("building_height", 12))
        rotation = integer_field(data, "building_rotation",
                                 base.get("building_rotation", 0), 0, 359)
    else:
        shape, width, height, rotation = 0, 15, 12, 0
    return name, kind, zone, x, y, fov, direction, reach, shape, width, height, rotation


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



# LAN discovery is scoped to the server's directly connected private IPv4 /24
# networks. Never scan routed/public networks or probe arbitrary user-supplied
# targets. Only a manual button or opening the Network page starts a scan.
def _ioctl_ipv4_interfaces():
    """Read interface IPv4 settings without AF_NETLINK (systemd sandbox fallback)."""
    import fcntl

    try:
        names = [entry.name for entry in Path("/sys/class/net").iterdir()]
    except OSError:
        try:
            names = [line.split(":", 1)[0].strip()
                     for line in Path("/proc/net/dev").read_text(encoding="ascii").splitlines()[2:]
                     if ":" in line]
        except OSError as exc:
            raise RuntimeError("Не удалось перечислить сетевые интерфейсы Debian") from exc

    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for name in names:
                raw_name = name.encode("utf-8")
                if not raw_name or len(raw_name) > 15:
                    continue
                request = struct.pack("256s", raw_name)
                try:
                    address = fcntl.ioctl(sock.fileno(), 0x8915, request)  # SIOCGIFADDR
                    netmask = fcntl.ioctl(sock.fileno(), 0x891b, request)  # SIOCGIFNETMASK
                    ipv4 = socket.inet_ntoa(address[20:24])
                    mask = socket.inet_ntoa(netmask[20:24])
                    prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
                except (OSError, ValueError):
                    continue
                found.append({"ifname": name, "addr_info": [{
                    "family": "inet", "scope": "global",
                    "local": ipv4, "prefixlen": prefix,
                }]})
    except OSError as exc:
        raise RuntimeError("Служба не может прочитать IPv4 интерфейсы Debian") from exc
    return found


def _proc_arp_entries(path=Path("/proc/net/arp")):
    """Read the kernel ARP cache without AF_NETLINK or additional privileges."""
    try:
        lines = path.read_text(encoding="ascii").splitlines()[1:]
    except OSError as exc:
        raise RuntimeError("Недоступна таблица ARP в /proc/net/arp") from exc
    found = []
    for line in lines:
        columns = line.split()
        if len(columns) < 6:
            continue
        ip, _hardware, flags, mac, _mask, interface = columns[:6]
        try:
            if not int(flags, 0) & 0x2:
                continue
        except ValueError:
            continue
        if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac.lower()):
            continue
        found.append({"dst": ip, "lladdr": mac.lower(),
                      "state": "REACHABLE", "dev": interface})
    return found


def _ip_json(arguments):
    """Use iproute2 when allowed; otherwise use unprivileged ioctl and /proc."""
    binary = shutil.which("ip") or next(
        (path for path in ("/usr/sbin/ip", "/usr/bin/ip", "/sbin/ip")
         if os.path.isfile(path)), None
    )
    if binary:
        try:
            result = subprocess.run([binary, "-j", *arguments], capture_output=True,
                                    text=True, timeout=5, check=False)
            if result.returncode == 0:
                return json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass

    # Linux iproute2 needs AF_NETLINK, which older MiniHomeServer systemd units
    # prohibited. These read-only alternatives need only permitted AF_INET.
    if arguments == ["-4", "addr", "show"]:
        return _ioctl_ipv4_interfaces()
    if arguments == ["-4", "neigh", "show"]:
        return _proc_arp_entries()
    raise RuntimeError("Невозможно выполнить неизвестную сетевую операцию")


def local_networks():
    subnets = []
    for item in _ip_json(["-4", "addr", "show"]):
        ifname = item.get("ifname", "")
        if ifname.startswith(("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "tailscale")):
            continue
        for address in item.get("addr_info", []):
            if address.get("family") != "inet" or address.get("scope") != "global":
                continue
            try:
                local = ipaddress.IPv4Address(address["local"])
                prefix = int(address["prefixlen"])
                net = ipaddress.IPv4Network(f"{local}/{prefix}", strict=False)
            except (KeyError, ValueError):
                continue
            if (not local.is_private or local.is_loopback or local.is_link_local
                or not 24 <= prefix <= 30 or net.num_addresses > 256):
                continue
            subnets.append({"interface": ifname, "local_ip": str(local), "cidr": str(net)})
    # Never scan more than two interfaces at a time, e.g. LAN and guest LAN.
    return subnets[:2]


def _scope_lookup(scopes):
    return [ipaddress.IPv4Network(item["cidr"], strict=False) for item in scopes]


def _in_scope(ip, nets):
    try:
        address = ipaddress.IPv4Address(ip)
        return any(address in net for net in nets)
    except ValueError:
        return False


def neighbor_cache(scopes):
    allowed_interfaces = {s["interface"] for s in scopes}
    result = {}
    try:
        items = _ip_json(["-4", "neigh", "show"])
    except RuntimeError:
        return result
    nets = _scope_lookup(scopes)
    for item in items:
        ip = item.get("dst", "")
        mac = str(item.get("lladdr", "")).lower()
        # iproute2 JSON normally uses an array; accept older string output too.
        status = item.get("state", [])
        if isinstance(status, str):
            status = status.split()
        if not isinstance(status, list) or not all(isinstance(s, str) for s in status):
            continue
        states = {s.upper() for s in status}
        if (item.get("dev") not in allowed_interfaces or
                not _in_scope(ip, nets) or
                states & {"FAILED", "INCOMPLETE", "NONE"} or
                not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac)):
            continue
        result[ip] = mac
    return result


def _tcp_arp_probe(ip):
    # A single TCP connection attempt induces local ARP resolution. No
    # payload, login attempt, camera request or device command is sent.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
            conn.settimeout(0.3)
            result = conn.connect_ex((ip, 80))
            return result in (0, errno.ECONNREFUSED)
    except OSError:
        return False


def _ssdp_devices(nets):
    found = {}
    probe = (b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
             b'MAN: "ssdp:discover"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n')
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.settimeout(0.35)
            sock.sendto(probe, ("239.255.255.250", 1900))
            stop = time.monotonic() + 1.5
            while time.monotonic() < stop:
                try:
                    data, endpoint = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                ip = endpoint[0]
                if not _in_scope(ip, nets):
                    continue
                decoded = data.decode("utf-8", "replace")
                if not decoded.startswith("HTTP/1.1 200"):
                    continue
                fields = {}
                for line in decoded.splitlines()[1:]:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        fields[key.strip().lower()] = value.strip()
                name = fields.get("usn", "")[:100]
                server = fields.get("server", "")[:100]
                category = "other"
                labels = (server + " " + name + " " + fields.get("st", "")).lower()
                if any(key in labels for key in ("zigbee", "hue bridge", "deconz", "zigate")):
                    category = "zigbee-gateway"
                elif "camera" in labels or "onvif" in labels:
                    category = "camera"
                elif "router" in labels or "internetgatewaydevice" in labels:
                    category = "router"
                found[ip] = {"hostname": "", "vendor": server, "kind": category}
    except OSError:
        pass
    return found


def _dns_name(data, start, visited=None):
    # DNS name decompression for mDNS A/SRV records; malformed responses skipped.
    if visited is None:
        visited = set()
    labels = []
    offset = start
    consumed = None
    for _ in range(25):
        if offset >= len(data) or offset in visited:
            raise ValueError("Invalid DNS name")
        visited.add(offset)
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("Invalid DNS pointer")
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            if consumed is None:
                consumed = offset + 2
            offset = ptr
            continue
        if length & 0xC0:
            raise ValueError("Invalid DNS label")
        offset += 1
        if length == 0:
            return ".".join(labels).rstrip("."), (consumed if consumed is not None else offset)
        if offset + length > len(data):
            raise ValueError("Truncated DNS name")
        labels.append(data[offset:offset + length].decode("utf-8", "replace"))
        offset += length
    raise ValueError("Too many DNS labels")


def _mdns_records(data):
    """Return (host A records, SRV service labels) from one DNS response."""
    if len(data) < 12:
        return {}, {}
    _, _, queries, answers, authority, additionals = struct.unpack("!6H", data[:12])
    off = 12
    hosts = {}
    srv = {}
    try:
        for _ in range(min(queries, 20)):
            _, off = _dns_name(data, off)
            off += 4
            if off > len(data):
                return {}, {}
        for _ in range(min(answers + authority + additionals, 140)):
            name, off = _dns_name(data, off)
            if off + 10 > len(data):
                break
            kind, _cls, _ttl, length = struct.unpack("!HHIH", data[off:off + 10])
            start = off + 10
            end = start + length
            if end > len(data):
                break
            if kind == 1 and length == 4:
                hosts[name.lower()] = socket.inet_ntoa(data[start:end])
            if kind == 33 and length >= 7:
                target, _ = _dns_name(data, start + 6)
                srv[name] = target.lower()
            off = end
    except (ValueError, struct.error, OSError):
        pass
    return hosts, srv


def _mdns_devices(nets):
    discovered = {}
    hosts, services = {}, {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.settimeout(0.35)
            # Request unicast replies to our ephemeral UDP port (QU bit).
            for qname in ("_http._tcp.local", "_hap._tcp.local",
                          "_googlecast._tcp.local", "_services._dns-sd._udp.local"):
                wire = b"".join(bytes([len(x)]) + x.encode("ascii") for x in qname.split(".")) + b"\x00"
                query = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00" + wire + b"\x00\x0c\x80\x01"
                sock.sendto(query, ("224.0.0.251", 5353))
            deadline = time.monotonic() + 1.3
            while time.monotonic() < deadline:
                try:
                    payload, _ = sock.recvfrom(16384)
                except socket.timeout:
                    continue
                seen_hosts, seen_services = _mdns_records(payload)
                hosts.update(seen_hosts)
                services.update(seen_services)
    except OSError:
        return discovered
    for hostname, ip in hosts.items():
        if _in_scope(ip, nets):
            discovered[ip] = {"hostname": hostname.rstrip(".local"), "vendor": "", "kind": "other"}
    for service, target in services.items():
        ip = hosts.get(target, "")
        if not _in_scope(ip, nets):
            continue
        label = service.split("._", 1)[0]
        if label and (ip not in discovered or not discovered[ip]["hostname"]):
            discovered[ip] = {"hostname": label[:90], "vendor": "", "kind": "other"}
    return discovered


def _scan_lan():
    scopes = local_networks()
    if not scopes:
        raise RuntimeError("Нет доступной частной IPv4 сети /24–/30. Проверьте подключение Debian")
    nets = _scope_lookup(scopes)
    prior = neighbor_cache(scopes)
    targets = sorted({
        str(addr)
        for net in nets
        for addr in net.hosts()
        if str(addr) not in {scope["local_ip"] for scope in scopes}
    }, key=lambda value: int(ipaddress.IPv4Address(value)))
    active = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        for address, succeeded in zip(targets, pool.map(_tcp_arp_probe, targets)):
            if succeeded:
                active.add(address)
    neighbors = neighbor_cache(scopes)
    ssdp = _ssdp_devices(nets)
    mdns = _mdns_devices(nets)
    seen = set(prior) | set(neighbors) | active | set(ssdp) | set(mdns)
    now = timestamp()
    with database() as db:
        for ip in sorted(seen, key=lambda value: int(ipaddress.IPv4Address(value))):
            mac = neighbors.get(ip) or prior.get(ip) or ""
            identifier = "mac:" + mac if mac else "ip:" + ip
            db.execute("DELETE FROM lan_nodes WHERE ip=? AND id<>?", (ip, identifier))
            info = mdns.get(ip) or ssdp.get(ip) or {}
            extra = ssdp.get(ip, {})
            hostname = str(info.get("hostname", ""))[:100]
            vendor = str(extra.get("vendor", ""))[:100]
            kind = extra.get("kind", "other")
            if kind not in {"other", "zigbee-gateway", "camera", "router"}:
                kind = "other"
            online = int(ip in active or ip in neighbors or ip in ssdp or ip in mdns)
            db.execute("""
                INSERT INTO lan_nodes(id,ip,mac,hostname,kind,vendor,source,online,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET ip=excluded.ip,mac=excluded.mac,
                    hostname=CASE WHEN excluded.hostname<>'' THEN excluded.hostname ELSE lan_nodes.hostname END,
                    kind=CASE WHEN excluded.kind<>'other' THEN excluded.kind ELSE lan_nodes.kind END,
                    vendor=CASE WHEN excluded.vendor<>'' THEN excluded.vendor ELSE lan_nodes.vendor END,
                    source=excluded.source,online=excluded.online,last_seen=excluded.last_seen
            """, (identifier, ip, mac, hostname, kind, vendor, "ARP/mDNS/SSDP",
                  online, now, now))
        for row in db.execute("SELECT id,ip FROM lan_nodes").fetchall():
            if row["ip"] not in seen:
                db.execute("UPDATE lan_nodes SET online=0 WHERE id=?", (row["id"],))
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('lan_last_scan',?)", (now,))
        add_event(db, "network.scan", f"Поиск в локальной сети: найдено {len(seen)} устройств")
    return {"found": len(seen), "subnets": [s["cidr"] for s in scopes]}


def _network_scan_worker():
    try:
        result = _scan_lan()
        with SCAN_LOCK:
            NETWORK_SCAN.update({"last_error": "", "found": result["found"],
                                 "subnets": result["subnets"], "finished_at": timestamp()})
    except Exception as exc:
        with SCAN_LOCK:
            NETWORK_SCAN.update({"last_error": str(exc)[:180], "finished_at": timestamp()})
    finally:
        with SCAN_LOCK:
            NETWORK_SCAN["running"] = False


def launch_network_scan():
    with SCAN_LOCK:
        if NETWORK_SCAN["running"]:
            return False
        if NETWORK_SCAN["finished_at"]:
            try:
                elapsed = time.time() - datetime.fromisoformat(NETWORK_SCAN["finished_at"]).timestamp()
                if elapsed < 30:
                    return False
            except ValueError:
                pass
        NETWORK_SCAN.update({"running": True, "last_error": "",
                             "started_at": timestamp()})
    threading.Thread(target=_network_scan_worker, name="lan-discovery", daemon=True).start()
    return True


def network_snapshot(automatic=False):
    with database() as db:
        devices = [dict(r) for r in db.execute(
            "SELECT id,ip,mac,hostname,kind,vendor,source,online,first_seen,last_seen "
            "FROM lan_nodes ORDER BY online DESC,ip")]
        zigbee = [dict(r) for r in db.execute(
            "SELECT ieee,name,type,model,vendor,availability,bridge_ip,last_seen "
            "FROM zigbee_nodes ORDER BY name COLLATE NOCASE")]
        last = setting(db, "lan_last_scan")
    with SCAN_LOCK:
        scanning = dict(NETWORK_SCAN)
        z_status = dict(ZIGBEE_SCAN)
    # Stored availability is historical until this process confirms the gateway.
    # Keep inventory and last_seen intact when a poll fails or the server restarts.
    if not z_status["connected"]:
        for item in zigbee:
            item["availability"] = "unknown"
    gateway = public_gateway_settings()
    if automatic and not scanning["running"]:
        try:
            if not last or time.time() - datetime.fromisoformat(last).timestamp() > 600:
                launch_network_scan()
        except ValueError:
            launch_network_scan()
    if automatic and gateway["configured"] and not z_status["running"]:
        try:
            if not z_status["finished_at"] or (
                time.time() - datetime.fromisoformat(z_status["finished_at"]).timestamp() > 600
            ):
                launch_zigbee_scan()
        except ValueError:
            launch_zigbee_scan()
    return {"lan": devices, "zigbee": zigbee, "scan": scanning,
            "zigbee_status": z_status, "last_scan": last, "gateway": gateway}


def _safe_gateway_ip(value):
    if not isinstance(value, str):
        raise ValueError("Введите IP-адрес MQTT-шлюза")
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise ValueError("Укажите IPv4-адрес, например 10.10.10.20") from exc
    if not addr.is_private or addr.is_link_local or addr.is_multicast:
        raise ValueError("Шлюз MQTT должен быть доступен по частному IPv4-адресу")
    if not addr.is_loopback:
        nets = _scope_lookup(local_networks())
        if not any(addr in net for net in nets):
            raise ValueError("Адрес шлюза должен находиться в одной из локальных подсетей Debian")
    return str(addr)


def gateway_settings():
    try:
        raw = GATEWAY_CONFIG.read_bytes()
        if len(raw) > 8192:
            raise ValueError("Bad gateway settings")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def public_gateway_settings():
    config = gateway_settings()
    return {"configured": bool(config.get("host")),
            "mode": "zigbee2mqtt",
            "host": config.get("host", ""),
            "port": config.get("port", 1883),
            "base_topic": config.get("base_topic", "zigbee2mqtt"),
            "username": config.get("username", ""),
            "password_configured": bool(config.get("password"))}


def set_gateway_settings(data):
    config = gateway_settings()
    host = _safe_gateway_ip(data.get("host"))
    port = data.get("port", 1883)
    if type(port) is not int or port < 1 or port > 65535:
        raise ValueError("Неверный порт MQTT")
    base = data.get("base_topic", "zigbee2mqtt")
    if not isinstance(base, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", base):
        raise ValueError("Имя базового MQTT-топика должно состоять из латинских букв, цифр, _ или -")
    username = data.get("username", "")
    if not isinstance(username, str) or len(username) > 128:
        raise ValueError("Некорректное имя пользователя MQTT")
    new_pass = data.get("password", "")
    if not isinstance(new_pass, str) or len(new_pass) > 256:
        raise ValueError("Недопустимый MQTT-пароль")
    if new_pass:
        config["password"] = new_pass
    elif data.get("clear_password") is True:
        config["password"] = ""
    config.update({"host": host, "port": port, "base_topic": base, "username": username})
    tmp = DATA / (".zigbee-config-" + secrets.token_hex(8))
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(config, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, GATEWAY_CONFIG)
    finally:
        tmp.unlink(missing_ok=True)
    return public_gateway_settings()


def _mqtt_remaining_length(n):
    encoded = bytearray()
    while True:
        digit = n % 128
        n //= 128
        if n:
            digit |= 0x80
        encoded.append(digit)
        if not n:
            return bytes(encoded)


def _mqtt_string(text):
    raw = text.encode("utf-8")
    if len(raw) > 65535:
        raise ValueError("MQTT field too long")
    return struct.pack("!H", len(raw)) + raw


def _mqtt_exact(sock, length):
    chunks = []
    while length:
        data = sock.recv(length)
        if not data:
            raise RuntimeError("MQTT-сервер закрыл соединение")
        length -= len(data)
        chunks.append(data)
    return b"".join(chunks)


def _mqtt_packet(sock):
    header = _mqtt_exact(sock, 1)[0]
    count, multiplier = 0, 1
    for _ in range(4):
        digit = _mqtt_exact(sock, 1)[0]
        count += (digit & 127) * multiplier
        if not digit & 128:
            break
        multiplier *= 128
    else:
        raise ValueError("Invalid MQTT packet size")
    if count > 1024 * 1024:
        raise ValueError("MQTT пакет слишком большой")
    return header, _mqtt_exact(sock, count)


def _mqtt_inventory(config, wait_seconds=6):
    # Read-only MQTT v3.1.1: CONNECT + SUBSCRIBE. Never publish /set commands.
    host, port = config["host"], config["port"]
    base = config["base_topic"]
    username, password = config.get("username", ""), config.get("password", "")
    flags = 0x02
    data = _mqtt_string("minihome-" + secrets.token_hex(4))
    if username:
        flags |= 0x80
        data += _mqtt_string(username)
        if password:
            flags |= 0x40
            data += _mqtt_string(password)
    elif password:
        raise ValueError("Для MQTT-пароля нужно указать имя пользователя")
    variable = _mqtt_string("MQTT") + bytes([4, flags]) + struct.pack("!H", 20)
    payload = variable + data
    with socket.create_connection((host, port), timeout=4) as conn:
        conn.settimeout(3)
        conn.sendall(b"\x10" + _mqtt_remaining_length(len(payload)) + payload)
        kind, reply = _mqtt_packet(conn)
        if kind != 0x20 or len(reply) != 2 or reply[1] != 0:
            raise RuntimeError("MQTT-брокер отклонил подключение (проверьте логин/пароль)")
        topics = [base + "/bridge/devices", base + "/bridge/state",
                  base + "/+/availability"]
        body = b"\x00\x01" + b"".join(_mqtt_string(topic) + b"\x00" for topic in topics)
        conn.sendall(b"\x82" + _mqtt_remaining_length(len(body)) + body)
        devices, availability = None, {}
        until = time.monotonic() + max(0.2, min(float(wait_seconds), 15))
        while time.monotonic() < until:
            conn.settimeout(min(1.0, max(0.1, until - time.monotonic())))
            try:
                header, data = _mqtt_packet(conn)
            except socket.timeout:
                continue
            if header >> 4 != 3:
                continue
            if len(data) < 2:
                continue
            n = struct.unpack("!H", data[:2])[0]
            if n + 2 > len(data):
                continue
            topic = data[2:2 + n].decode("utf-8", "replace")
            position = 2 + n
            if (header >> 1) & 0x03:
                position += 2
            message = data[position:]
            if topic == base + "/bridge/devices":
                try:
                    decoded = json.loads(message)
                    if isinstance(decoded, list) and len(decoded) <= 2000:
                        devices = decoded
                except (UnicodeDecodeError, ValueError):
                    continue
            elif topic.endswith("/availability") and topic.startswith(base + "/"):
                try:
                    payload = json.loads(message)
                    state = payload.get("state") if isinstance(payload, dict) else message.decode("utf-8", "replace")
                    friendly = topic[len(base) + 1:-len("/availability")]
                    if state in ("online", "offline"):
                        availability[friendly] = state
                except ValueError:
                    continue
        try:
            conn.sendall(b"\xe0\x00")
        except OSError:
            pass
    if devices is None:
        raise RuntimeError("Не получен список Zigbee2MQTT bridge/devices. Проверьте базовый топик и retained MQTT")
    return devices, availability


def _store_zigbee_devices(items, availability, gateway_ip):
    now = timestamp()
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ieee = str(item.get("ieee_address") or "").lower()
        if not re.fullmatch(r"0x[0-9a-f]{16}", ieee):
            continue
        definition = item.get("definition") or {}
        if not isinstance(definition, dict):
            definition = {}
        friendly = str(item.get("friendly_name") or ieee)[:100]
        state = availability.get(friendly, "unknown")
        kind = str(item.get("type") or "Unknown")[:40]
        rows.append((ieee, friendly, kind, str(definition.get("model") or item.get("model_id") or "")[:100],
                     str(definition.get("vendor") or "")[:100], state, gateway_ip, now))
    with database() as db:
        for row in rows:
            db.execute("""
                INSERT INTO zigbee_nodes(ieee,name,type,model,vendor,availability,bridge_ip,last_seen)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(ieee) DO UPDATE SET name=excluded.name,type=excluded.type,
                    model=excluded.model,vendor=excluded.vendor,
                    availability=excluded.availability,bridge_ip=excluded.bridge_ip,
                    last_seen=excluded.last_seen
            """, row)
        keep = {row[0] for row in rows}
        for row in db.execute("SELECT ieee FROM zigbee_nodes WHERE bridge_ip=?", (gateway_ip,)).fetchall():
            if row["ieee"] not in keep:
                db.execute("DELETE FROM zigbee_nodes WHERE ieee=?", (row["ieee"],))
        add_event(db, "zigbee.refresh", f"Zigbee2MQTT: получено {len(rows)} устройств")
    return len(rows)


def _zigbee_scan_worker():
    try:
        settings = gateway_settings()
        if not settings.get("host"):
            raise RuntimeError("Настройте IP MQTT-шлюза")
        _safe_gateway_ip(settings["host"])
        items, availability = _mqtt_inventory(settings)
        found = _store_zigbee_devices(items, availability, settings["host"])
        with SCAN_LOCK:
            ZIGBEE_SCAN.update({"found": found, "connected": True,
                                "last_error": "", "finished_at": timestamp()})
    except Exception as exc:
        with SCAN_LOCK:
            ZIGBEE_SCAN.update({"connected": False, "last_error": str(exc)[:180],
                                "finished_at": timestamp()})
    finally:
        with SCAN_LOCK:
            ZIGBEE_SCAN["running"] = False


def launch_zigbee_scan():
    with SCAN_LOCK:
        if ZIGBEE_SCAN["running"]:
            return False
        ZIGBEE_SCAN["running"] = True
        ZIGBEE_SCAN["last_error"] = ""
    threading.Thread(target=_zigbee_scan_worker, name="zigbee-inventory", daemon=True).start()
    return True



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
        if path == "/api/network/devices":
            if not self.require():
                return
            return self.send_bytes(200, network_snapshot(automatic=True))
        if path == "/api/network/status":
            if not self.require():
                return
            with SCAN_LOCK:
                info = {"scan": dict(NETWORK_SCAN), "zigbee": dict(ZIGBEE_SCAN)}
            return self.send_bytes(200, info)
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
            try:
                latest_version = VERSION if latest == current else github_version_for_sha(latest)
            except ValueError:
                latest_version = None  # Pinned SHA remains usable if GitHub contents API fails.
            return self.send_bytes(200, {
                "current_sha": current, "current_version": VERSION,
                "latest_sha": latest, "latest_version": latest_version,
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

        if method == "POST" and path == "/api/network/scan":
            if not launch_network_scan():
                return self.error(409, "Сканирование уже идёт или выполнялось менее 30 секунд назад")
            return self.send_bytes(202, {"ok": True, "message": "Поиск устройств в LAN запущен"})

        if method == "POST" and path == "/api/zigbee/config":
            data = self.body()
            config = set_gateway_settings(data)
            with database() as db:
                add_event(db, "zigbee.configured", "Настроен шлюз Zigbee2MQTT")
            if not launch_zigbee_scan():
                return self.error(409, "Опрос Zigbee уже выполняется")
            return self.send_bytes(202, {"ok": True, "gateway": config})

        if method == "POST" and path == "/api/zigbee/refresh":
            if not gateway_settings().get("host"):
                return self.error(409, "Шлюз Zigbee2MQTT пока не настроен")
            if not launch_zigbee_scan():
                return self.error(409, "Опрос Zigbee уже выполняется")
            return self.send_bytes(202, {"ok": True, "message": "Опрос Zigbee2MQTT запущен"})

        if method == "POST" and path == "/api/update/recover":
            if not UPDATE_UNIT.is_file():
                return self.error(409, "Служба обновлений не установлена")
            self.body()
            with LOCK:
                previous = update_status()
                if previous["state"] != "stalled":
                    return self.error(409, "Обновление не зависло или ещё выполняется")
                systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
                try:
                    result = subprocess.run(
                        [systemctl, "is-active", "--quiet", "minihome-update.service"],
                        timeout=4, capture_output=True, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    return self.error(503, "Не удалось проверить системную службу")
                if result.returncode == 0:
                    return self.error(409, "Обновление продолжает выполняться")
                if result.returncode not in (3, 4):
                    return self.error(503, "Состояние системной службы не определено")
                # TimeoutStartSec=300; recovery only after >600 seconds.
                UPDATE_REQUEST.unlink(missing_ok=True)
                atomic_json(UPDATE_STATUS, {
                    "state": "failed",
                    "step": "recover",
                    "message": "Зависшая заявка сброшена. Проверьте GitHub и повторите установку.",
                    "target_sha": previous.get("target_sha", ""),
                    "current_sha": installed_sha(),
                    "updated_at": timestamp(),
                })
                with database() as db:
                    add_event(db, "update.recovered", "Зависшая очередь обновлений сброшена")
            return self.send_bytes(200, {"ok": True})

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

        if method == "POST" and path == "/api/map/scale":
            data = self.body()
            if not isinstance(data, dict):
                raise ValueError("Ожидается объект JSON")
            if data == {"clear": True}:
                with database() as db:
                    db.execute("DELETE FROM settings WHERE key IN ('map_width_m','map_height_m')")
                    add_event(db, "map.scale", "Сброшены размеры плана в метрах")
                return self.send_bytes(200, {"ok": True, "map_scale": None})
            if set(data) != {"width_m", "height_m"}:
                raise ValueError("Необходимо указать ширину и высоту плана")
            width_m = map_dimension(data, "width_m")
            height_m = map_dimension(data, "height_m")
            with database() as db:
                db.execute("INSERT OR REPLACE INTO settings(key,value)"
                           " VALUES('map_width_m',?)", (str(width_m),))
                db.execute("INSERT OR REPLACE INTO settings(key,value)"
                           " VALUES('map_height_m',?)", (str(height_m),))
                add_event(db, "map.scale", "Размеры плана заданы в метрах")
            return self.send_bytes(200, {
                "ok": True, "map_scale": {"width_m": width_m, "height_m": height_m}
            })

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
            name, kind, zone, x, y, fov, direction, reach, shape, width, height, rotation = (
                device_values(self.body()))
            did = str(uuid.uuid4())
            with database() as db:
                db.execute("INSERT INTO devices(id,name,kind,zone,x,y,"
                           "camera_fov,camera_direction,camera_reach,"
                           "building_shape,building_width,building_height,building_rotation)"
                           " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (did, name, kind, zone, x, y, fov, direction, reach,
                            shape, width, height, rotation))
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
                    (name, kind, zone, x, y, fov, direction, reach,
                     shape, width, height, rotation) = device_values(
                        self.body(), dict(row))
                    db.execute("UPDATE devices SET name=?,kind=?,zone=?,x=?,y=?,"
                               "camera_fov=?,camera_direction=?,camera_reach=?,"
                               "building_shape=?,building_width=?,building_height=?,"
                               "building_rotation=? WHERE id=?",
                               (name, kind, zone, x, y, fov, direction, reach,
                                shape, width, height, rotation, did))
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
