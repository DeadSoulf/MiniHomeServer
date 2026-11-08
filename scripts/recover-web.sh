#!/usr/bin/env bash
# MiniHomeServer rescue for the installed systemd service.
# Does not reinstall the app, touch SSH/firewall, or remove SQLite/map/password.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root: bash scripts/recover-web.sh" >&2
  exit 1
fi
if ! systemctl cat minihome.service >/dev/null 2>&1; then
  echo "Служба minihome.service не найдена. Сценарий предназначен для systemd-установки." >&2
  exit 1
fi
if systemctl is-active --quiet minihome-update.service; then
  echo "Выполняется обновление MiniHomeServer. Не прерываем его." >&2
  exit 1
fi

check_http() {
  python3 - <<'PY'
import json
import urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=3) as response:
        info = json.load(response)
        if info.get("ok") is True:
            print("MiniHomeServer API работает. Версия:", info.get("version", "неизвестна"))
            raise SystemExit(0)
except (OSError, ValueError, TimeoutError) as exc:
    pass
raise SystemExit(1)
PY
}

show_status() {
  echo "--- Состояние службы ---"
  systemctl is-active minihome.service || true
  echo "--- Прослушивание порта 8080 ---"
  ss -ltn '( sport = :8080 )' || true
  echo "--- Последние ошибки запуска ---"
  journalctl -u minihome.service -n 35 --no-pager -l || true
}

if check_http; then
  echo "Локальная панель исправна. Если браузер её не открывает, проверьте адрес сервера, firewall, HTTPS/proxy или кэш браузера."
  exit 0
fi

echo "API локально не отвечает; проверяем службу."
systemctl restart minihome.service || true
sleep 2
if check_http; then
  echo "Доступ восстановлен обычным перезапуском minihome.service."
  exit 0
fi

show_status

# Repair only the previously confirmed SQLite ownership failure.
if ! journalctl -u minihome.service -n 100 --no-pager -l | grep -Fq 'attempt to write a readonly database'; then
  echo "Причина не подтверждена как проблема прав SQLite. Автоматически ничего больше не меняем." >&2
  exit 1
fi

if [ "$(systemctl show -P User minihome.service)" != "minihome" ]; then
  echo "Служба использует другого пользователя. Исправление остановлено." >&2
  exit 1
fi
data="/var/lib/minihome"
if [ ! -d "$data" ] || [ -L "$data" ]; then
  echo "Неверный тип каталога данных. Исправление остановлено." >&2
  exit 1
fi

for name in minihome.db minihome.db-wal minihome.db-shm minihome.db-journal; do
  path="$data/$name"
  if [ -L "$path" ] || { [ -e "$path" ] && [ ! -f "$path" ]; }; then
    echo "Нестандартный файл $name. Исправление остановлено." >&2
    exit 1
  fi
done
if [ ! -f "$data/minihome.db" ]; then
  echo "База данных отсутствует; нельзя менять права без дополнительной диагностики." >&2
  exit 1
fi

backup_root="/opt/minihome-backups"
install -d -m 0700 "$backup_root"
backup="$backup_root/sqlite-rescue-$(date -u +%Y%m%dT%H%M%SZ)-$$.db"

# A consistent SQLite backup covers WAL data; no copying of a live DB file.
python3 - "$data/minihome.db" "$backup" <<'PY'
import sqlite3
import sys
source = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True, timeout=15)
destination = sqlite3.connect(sys.argv[2], timeout=15)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
print("Сохранена согласованная резервная копия SQLite.")
PY
chmod 0600 "$backup"
echo "Резервная копия: $backup"

# The backup above may create shared-memory sidecars; repair them too.
chown minihome:minihome "$data"
chmod 0700 "$data"
for name in minihome.db minihome.db-wal minihome.db-shm minihome.db-journal; do
  path="$data/$name"
  if [ -L "$path" ] || { [ -e "$path" ] && [ ! -f "$path" ]; }; then
    echo "Тип файла $name изменился. Остановлено." >&2
    exit 1
  fi
  if [ -f "$path" ]; then
    chown minihome:minihome "$path"
    chmod 0600 "$path"
  fi
done

systemctl restart minihome.service
sleep 2
if check_http; then
  echo "Доступ восстановлен. База, карта и пароль сохранены."
  exit 0
fi
echo "После исправления прав API не ответил. Никакие файлы не удалены." >&2
show_status
exit 1
