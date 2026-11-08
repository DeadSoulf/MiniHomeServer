#!/usr/bin/env bash
# One-time migration for existing MiniHomeServer systemd installations.
# Preserves password, SQLite database, territory image and device settings.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root: su - ; bash scripts/enable-web-updates.sh" >&2
  exit 1
fi
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
source_url="https://github.com/DeadSoulf/MiniHomeServer.git"
if [ ! -f "$repo_root/server.py" ] || [ ! -f "$repo_root/scripts/update_worker.py" ]; then
  echo "Исходники MiniHomeServer не найдены." >&2
  exit 1
fi
if [ ! -f /opt/minihome/server.py ] || [ ! -f /etc/minihome/admin-password ] ||
   ! systemctl cat minihome.service >/dev/null 2>&1; then
  echo "Служба MiniHomeServer не установлена. Сначала: bash scripts/install-systemd.sh" >&2
  exit 1
fi
if systemctl is-active --quiet minihome-update.service; then
  echo "Обновление уже выполняется — не прерывайте его." >&2
  exit 1
fi
if ! systemctl cat minihome.service | grep -Fq '/opt/minihome/server.py'; then
  echo "Служба minihome использует другую структуру установки — автоматическая миграция остановлена." >&2
  exit 1
fi
if [ "$(git -C "$repo_root" remote get-url origin)" != "$source_url" ]; then
  echo "Неожиданный GitHub-источник. Обновление отменено." >&2
  exit 1
fi
if [ "$(git -C "$repo_root" branch --show-current)" != "main" ]; then
  echo "Для подключения обновлений требуется ветка main." >&2
  exit 1
fi
if ! git -C "$repo_root" diff --quiet HEAD -- server.py static scripts/update_worker.py scripts/systemd scripts/enable-web-updates.sh; then
  echo "В репозитории есть локальные изменения. Сохраните их перед обновлением." >&2
  exit 1
fi
commit="$(git -C "$repo_root" rev-parse HEAD)"
remote_commit="$(git ls-remote "$source_url" refs/heads/main | head -1 | cut -f1)"
if [ "$commit" != "$remote_commit" ]; then
  echo "Локальная версия не совпадает с GitHub main. Сначала выполните git pull --ff-only." >&2
  exit 1
fi
if ! [[ "$commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Не удалось определить точную версию GitHub." >&2
  exit 1
fi

python3 - "$repo_root/server.py" "$repo_root/scripts/update_worker.py" <<'PY'
import sys
from pathlib import Path
for filename in sys.argv[1:]:
    path = Path(filename)
    compile(path.read_bytes(), str(path), "exec")
print("Код Python проверен")
PY
if command -v node >/dev/null 2>&1; then
  node --check "$repo_root/static/app.js"
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)-bootstrap-$$"
backup="/opt/minihome-backups/$stamp"
install -d -m 0700 /opt/minihome-backups
install -d -m 0700 "$backup"
cp -a /opt/minihome "$backup/code"
if [ -f /usr/local/libexec/minihome-update-worker.py ]; then
  cp -a /usr/local/libexec/minihome-update-worker.py "$backup/worker.py"
fi
echo "Резервная копия кода: $backup"

# Important: older installations sometimes have a root-owned SQLite file or
# SQLite WAL sidecars. Old schemas could start because CREATE TABLE IF NOT EXISTS
# required no writes; new database tables fail with "readonly database".
# Back up the live SQLite database before any ownership repair.
state_dir="/var/lib/minihome"
if [ ! -d "$state_dir" ] || [ -L "$state_dir" ]; then
  echo "Каталог данных отсутствует или является символической ссылкой; установка остановлена." >&2
  exit 1
fi
service_user="$(systemctl show -P User minihome.service)"
if [ "$service_user" != "minihome" ] || ! id minihome >/dev/null 2>&1; then
  echo "Непредвиденный пользователь minihome.service. Права базы менять нельзя." >&2
  exit 1
fi
for name in minihome.db minihome.db-wal minihome.db-shm minihome.db-journal; do
  target="$state_dir/$name"
  if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
    echo "Нестандартный файл SQLite $name. Автоматическое исправление остановлено." >&2
    exit 1
  fi
done
if [ -f "$state_dir/minihome.db" ]; then
  python3 - "$state_dir/minihome.db" "$backup/minihome-pre-migration.db" <<'PY'
import sqlite3, sys
source = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True, timeout=15)
destination = sqlite3.connect(sys.argv[2], timeout=15)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
print("Создана резервная копия SQLite перед миграцией")
PY
  chmod 0600 "$backup/minihome-pre-migration.db"
fi

# Only repair this application's own state folder and SQLite files. Do not
# chmod -R, touch credentials, or alter any unrelated system services.
chown minihome:minihome "$state_dir"
chmod 0700 "$state_dir"
for name in minihome.db minihome.db-wal minihome.db-shm minihome.db-journal; do
  target="$state_dir/$name"
  if [ -f "$target" ]; then
    chown minihome:minihome "$target"
    chmod 0600 "$target"
  fi
done

# Check real SQLite write access with minihome privileges WITHOUT changing
# any table or application data.
if [ -f "$state_dir/minihome.db" ]; then
  python3 - <<'PY'
import os, pwd, sqlite3
account = pwd.getpwnam("minihome")
os.setgroups([])
os.setgid(account.pw_gid)
os.setuid(account.pw_uid)
connection = sqlite3.connect(
    "file:/var/lib/minihome/minihome.db?mode=rw", uri=True, timeout=10
)
try:
    connection.execute("BEGIN IMMEDIATE")
    connection.rollback()
finally:
    connection.close()
print("SQLite: запись от пользователя minihome разрешена")
PY
fi

# Keep the permission fix across future restarts; the systemd manager owns
# this directory's lifecycle and adjusts ownership before ExecStart.
install -d -m 0755 /etc/systemd/system/minihome.service.d
cat > /etc/systemd/system/minihome.service.d/30-state-directory.conf <<'EOF'
[Service]
StateDirectory=minihome
StateDirectoryMode=0700
EOF
# iproute2 queries the kernel over AF_NETLINK. Keep an ioctl/ARP fallback
# in the application for servers that have not yet applied this drop-in.
cat > /etc/systemd/system/minihome.service.d/40-network-discovery.conf <<'EOF'
[Service]
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
EOF
systemctl daemon-reload

restore_code() {
  echo "Ошибка запуска, возвращаю предыдущий код" >&2
  cp -a "$backup/code/." /opt/minihome/
  if [ -f "$backup/worker.py" ]; then
    install -m 0755 "$backup/worker.py" /usr/local/libexec/minihome-update-worker.py
  fi
  systemctl restart minihome.service || true
}

install -d -m 0755 /opt/minihome/static /usr/local/libexec
install -m 0644 "$repo_root/server.py" /opt/minihome/server.py
install -m 0644 "$repo_root/static/index.html" /opt/minihome/static/index.html
install -m 0644 "$repo_root/static/app.js" /opt/minihome/static/app.js
install -m 0644 "$repo_root/static/style.css" /opt/minihome/static/style.css
install -m 0755 "$repo_root/scripts/update_worker.py" /usr/local/libexec/minihome-update-worker.py

diagnose_service() {
  echo "=== Ошибка запуска MiniHomeServer: последние записи systemd ===" >&2
  systemctl status minihome.service --no-pager --full -l >&2 || true
  journalctl -u minihome.service -n 35 --no-pager -l >&2 || true
  echo "=== Текущий сокет веб-панели ===" >&2
  ss -lnt '( sport = :8080 )' >&2 || true
}

check_health() {
  python3 - <<'PY'
import json, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=2) as response:
        ok = json.load(response).get("ok") is True
except (OSError, ValueError, TimeoutError):
    ok = False
raise SystemExit(0 if ok else 1)
PY
}

if ! systemctl restart minihome.service; then
  diagnose_service
  restore_code
  exit 1
fi
ok=0
for attempt in $(seq 1 14); do
  if check_health; then
    ok=1
    break
  fi
  sleep 1
done
if [ "$ok" != "1" ]; then
  diagnose_service
  restore_code
  if check_health; then
    echo "Прежняя панель работает; новая версия отклонена." >&2
  else
    echo "ВНИМАНИЕ: даже прежняя версия после отката не отвечает." >&2
    diagnose_service
  fi
  exit 1
fi

printf '%s\n' "$commit" > /opt/minihome/.release-commit
chmod 0644 /opt/minihome/.release-commit

python3 - <<'PY'
import hashlib, json, os
from pathlib import Path
root = Path("/opt/minihome")
names = ("server.py", "static/index.html", "static/app.js", "static/style.css")
digests = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}
path = root / ".release-manifest.json"
path.write_text(json.dumps(digests, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o644)
PY

install -m 0644 "$repo_root/scripts/systemd/minihome-update.service" \
  /etc/systemd/system/minihome-update.service
install -m 0644 "$repo_root/scripts/systemd/minihome-update.path" \
  /etc/systemd/system/minihome-update.path
systemctl daemon-reload
systemctl enable --now minihome-update.path

if [ ! -f /var/lib/minihome/update-status.json ]; then
  python3 - "$commit" <<'PY'
import datetime, json, os, sys
from pathlib import Path
path=Path("/var/lib/minihome/update-status.json")
status={"state":"idle", "step":"ready", "message":"Обновления через веб-панель включены",
        "current_sha":sys.argv[1], "target_sha":"",
        "updated_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}
with path.open("w", encoding="utf-8") as out:
    json.dump(status, out, ensure_ascii=False)
    out.write("\n")
os.chmod(path, 0o644)
PY
fi
echo "Обновления включены; версия GitHub:" "$(printf '%.12s' "$commit")"
echo "Откройте веб-панель, Настройки -> Обновления."
echo "Карта, пароль и база данных не изменялись."
