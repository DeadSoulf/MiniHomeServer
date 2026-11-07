#!/usr/bin/env python3
"""Privileged, fixed-source MiniHomeServer updater for systemd.

Invoked ONLY by minihome-update.service (a root-owned systemd unit). The web
process can request a pinned SHA in its data directory but cannot specify a
command, a repository, a file path, or an executable to run as root.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SOURCE_URL = "https://github.com/DeadSoulf/MiniHomeServer.git"
DATA = Path("/var/lib/minihome")
TARGET = Path("/opt/minihome")
WORKER = Path("/usr/local/libexec/minihome-update-worker.py")
BACKUPS = Path("/opt/minihome-backups")
REQUEST = DATA / "update-request.json"
STATUS = DATA / "update-status.json"
LOCKFILE = Path("/run/lock/minihome-update.lock")
REVISION = TARGET / ".release-commit"
FILES = (
    "server.py",
    "static/index.html",
    "static/app.js",
    "static/style.css",
)
WORKER_SOURCE = "scripts/update_worker.py"
_SHA = re.compile(r"^[0-9a-f]{40}$")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def write_status(state, step, message, requested="", current=None):
    data = {
        "state": state,
        "step": step,
        "message": str(message)[:400],
        "target_sha": requested,
        "current_sha": current if current is not None else installed_sha(),
        "updated_at": utc_now(),
    }
    DATA.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".update-",
                                     dir=DATA, delete=False) as out:
        tmp = Path(out.name)
        json.dump(data, out, ensure_ascii=False)
        out.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATUS)
    print(f"{data['state']} [{data['step']}]: {data['message']}", flush=True)


def installed_sha():
    try:
        value = REVISION.read_text(encoding="ascii").strip()
        return value if _SHA.fullmatch(value) else None
    except OSError:
        return None


def run_command(args, timeout=120):
    result = subprocess.run(args, check=False, capture_output=True,
                            text=True, timeout=timeout,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode != 0:
        raise RuntimeError(f"Команда завершилась с кодом {result.returncode}: "
                           f"{' '.join(args[:3])}; {result.stderr[-350:]}")
    return result.stdout.strip()


def health():
    for _ in range(14):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=2) as resp:
                info = json.load(resp)
                if info.get("ok") is True:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def safe_source(repo):
    for name in (*FILES, WORKER_SOURCE):
        path = repo / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"В релизе отсутствует обычный файл: {name}")
    for name in ("server.py", WORKER_SOURCE):
        path = repo / name
        compile(path.read_bytes(), str(path), "exec")
    node = shutil.which("node")
    if node:
        run_command([node, "--check", str(repo / "static/app.js")], timeout=15)


def store_backup(code_dir):
    BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(BACKUPS, 0o700)
    name = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name += "-" + str(os.getpid())
    path = BACKUPS / name
    path.mkdir(mode=0o700)
    (path / "code" / "static").mkdir(parents=True, mode=0o700)
    for name in FILES:
        old = TARGET / name
        if not old.is_file() or old.is_symlink():
            raise RuntimeError(f"Установленный файл не найден: {name}")
        dest = path / "code" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old, dest)
    if REVISION.is_file():
        shutil.copy2(REVISION, path / "code" / ".release-commit")
    if WORKER.is_file():
        shutil.copy2(WORKER, path / "update_worker.py")
    dbfile = DATA / "minihome.db"
    if dbfile.exists():
        source = sqlite3.connect(f"file:{dbfile}?mode=ro", uri=True, timeout=15)
        destination = sqlite3.connect(path / "minihome.db", timeout=15)
        try:
            source.backup(destination)
        finally:
            source.close()
            destination.close()
    return path


def deploy_files(repo):
    for name in FILES:
        dest = TARGET / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / name, dest)
        os.chmod(dest, 0o644)


def rollback(backup):
    if not backup:
        raise RuntimeError("Не удалось найти резервную копию")
    for name in FILES:
        shutil.copy2(backup / "code" / name, TARGET / name)
    previous_sha = backup / "code" / ".release-commit"
    if previous_sha.is_file():
        shutil.copy2(previous_sha, REVISION)
    else:
        REVISION.unlink(missing_ok=True)
    previous_worker = backup / "update_worker.py"
    if previous_worker.is_file():
        shutil.copy2(previous_worker, WORKER)
        os.chmod(WORKER, 0o755)
    run_command(["systemctl", "restart", "minihome.service"], timeout=30)


def prune_backups():
    entries = sorted(p for p in BACKUPS.iterdir() if p.is_dir() and not p.is_symlink())
    for path in entries[:-3]:
        shutil.rmtree(path)


def record_event(action, detail):
    try:
        conn = sqlite3.connect(DATA / "minihome.db", timeout=10)
        with conn:
            conn.execute("INSERT INTO events(at,action,detail) VALUES(?,?,?)",
                         (utc_now(), action, str(detail)[:180]))
        conn.close()
    except (OSError, sqlite3.Error):
        pass


def perform(requested):
    backup = None
    modified = False
    try:
        if shutil.disk_usage(TARGET).free < 40 * 1024 * 1024:
            raise RuntimeError("Недостаточно свободного места для обновления")
        write_status("running", "download", "Загрузка обновления из GitHub", requested)
        with tempfile.TemporaryDirectory(prefix="minihome-update-") as folder:
            repo = Path(folder) / "source"
            run_command([
                "git", "-c", "core.hooksPath=/dev/null", "clone", "--quiet",
                "--depth", "1", "--single-branch", "--branch", "main",
                "--", SOURCE_URL, str(repo),
            ], timeout=150)
            actual = run_command(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=10)
            if not _SHA.fullmatch(actual) or requested != actual:
                raise RuntimeError("Ветка main изменилась. Проверьте версию ещё раз")
            if installed_sha() == actual:
                write_status("success", "complete", "Уже установлена последняя версия", actual)
                return
            write_status("running", "verify", "Проверка файлов обновления", requested)
            safe_source(repo)
            write_status("running", "backup", "Сохранение предыдущей версии и копии базы данных", requested)
            backup = store_backup(TARGET)
            write_status("running", "install", "Установка новой версии", requested)
            modified = True
            deploy_files(repo)
            write_status("running", "restart", "Перезапуск MiniHomeServer и проверка", requested)
            run_command(["systemctl", "restart", "minihome.service"], timeout=30)
            if not health():
                raise RuntimeError("Новая версия не ответила на запрос проверки здоровья")
            REVISION.write_text(actual + "\n", encoding="ascii")
            os.chmod(REVISION, 0o644)
            # After the new application passed health checks, upgrade the helper itself.
            shutil.copy2(repo / WORKER_SOURCE, WORKER)
            os.chmod(WORKER, 0o755)
            write_status("success", "complete", "Обновление установлено успешно", requested, actual)
            record_event("update.success", f"Версия обновлена до {actual[:12]}")
            prune_backups()
    except Exception as exc:
        detail = str(exc)
        if modified:
            try:
                write_status("running", "rollback", "Ошибка запуска. Восстановление прежней версии", requested)
                rollback(backup)
                if health():
                    write_status("rolled_back", "complete",
                                 f"Предыдущая версия восстановлена: {detail}", requested)
                    record_event("update.rollback", detail)
                    return
                detail += "; прежняя версия после восстановления не отвечает"
            except Exception as rollback_error:
                detail += "; ошибка отката: " + str(rollback_error)
        write_status("failed", "complete", detail, requested)
        record_event("update.failed", detail)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Обновление запускается только системной службой от root")
    DATA.mkdir(parents=True, exist_ok=True)
    with LOCKFILE.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Обновление уже запущено")
        if not REQUEST.exists():
            return
        try:
            if REQUEST.stat().st_size > 4096:
                raise ValueError("Слишком большой запрос")
            request = json.loads(REQUEST.read_text(encoding="utf-8"))
            requested = request.get("sha")
            if not isinstance(requested, str) or not _SHA.fullmatch(requested):
                raise ValueError("Недопустимый идентификатор версии")
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            REQUEST.unlink(missing_ok=True)
            write_status("failed", "complete", f"Неверный запрос: {exc}")
            return
        REQUEST.unlink(missing_ok=True)
        perform(requested)


if __name__ == "__main__":
    main()
