#!/usr/bin/env bash
# MiniHomeServer: install the local HTTP panel as an isolated systemd service.
# Does not modify SSH, firewall, networking, existing containers or other services.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root: su - ; bash scripts/install-systemd.sh" >&2
  exit 1
fi

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -f "$repo_root/server.py" ] || [ ! -f "$repo_root/static/index.html" ]; then
  echo "Ошибка: запустите скрипт из репозитория MiniHomeServer." >&2
  exit 1
fi
if [ -e /etc/systemd/system/minihome.service ]; then
  echo "Служба minihome уже существует — установка не перезаписывает её автоматически." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Требуется Python 3.11+: apt-get update && apt-get install -y python3" >&2
  exit 1
fi
python_exec="$(command -v python3)"
if ! "$python_exec" -c 'import sys; assert sys.version_info >= (3, 11)' >/dev/null 2>&1; then
  echo "Требуется Python 3.11 или новее." >&2
  exit 1
fi

if ! "$python_exec" - <<'PY'
import socket
s = socket.socket()
try:
    s.bind(('0.0.0.0', 8080))
except OSError:
    raise SystemExit("Порт 127.0.0.1:8080 уже занят. Установка остановлена.")
finally:
    s.close()
PY
then
  exit 1
fi

if [ ! -t 0 ]; then
  echo "Установку нужно запускать в интерактивном терминале для безопасного ввода пароля." >&2
  exit 1
fi
printf 'Новый пароль веб-панели (не менее 12 символов): '
IFS= read -r -s panel_password
printf '\n'
if [ "${#panel_password}" -lt 12 ]; then
  echo "Пароль слишком короткий; ничего не установлено." >&2
  exit 1
fi
printf 'Подтвердите пароль: '
IFS= read -r -s panel_confirm
printf '\n'
if [ "$panel_password" != "$panel_confirm" ]; then
  echo "Пароли не совпадают; ничего не установлено." >&2
  exit 1
fi
unset panel_confirm

if ! id minihome >/dev/null 2>&1; then
  useradd --system --home /var/lib/minihome --no-create-home --shell /usr/sbin/nologin minihome
fi
install -d -m 0755 /opt/minihome /opt/minihome/static
install -d -m 0700 -o minihome -g minihome /var/lib/minihome
install -d -m 0750 -o root -g minihome /etc/minihome
install -m 0644 "$repo_root/server.py" /opt/minihome/server.py
install -m 0644 "$repo_root/static/index.html" /opt/minihome/static/index.html
install -m 0644 "$repo_root/static/app.js" /opt/minihome/static/app.js
install -m 0644 "$repo_root/static/style.css" /opt/minihome/static/style.css

umask 077
printf '%s\n' "$panel_password" > /etc/minihome/admin-password
unset panel_password
chown root:minihome /etc/minihome/admin-password
chmod 0640 /etc/minihome/admin-password

cat > /etc/systemd/system/minihome.service <<EOF
[Unit]
Description=MiniHomeServer local territory control dashboard
After=network.target

[Service]
Type=simple
User=minihome
Group=minihome
WorkingDirectory=/opt/minihome
# systemd creates and maintains writable private SQLite state for minihome.
StateDirectory=minihome
StateDirectoryMode=0700
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=MINIHOME_HOST=0.0.0.0
Environment=MINIHOME_PORT=8080
Environment=MINIHOME_DATA_DIR=/var/lib/minihome
Environment=MINIHOME_ADMIN_PASSWORD_FILE=/etc/minihome/admin-password
ExecStart=$python_exec /opt/minihome/server.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths=/var/lib/minihome
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now minihome.service
sleep 2
if "$python_exec" - <<'PY'
import json, urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3) as r:
        data=json.load(r)
        assert data.get('ok') is True
except Exception as e:
    raise SystemExit('Не удалось проверить панель: ' + str(e))
PY
then
  if [ -f "$repo_root/scripts/enable-web-updates.sh" ]; then
    bash "$repo_root/scripts/enable-web-updates.sh"
  fi
  echo "MiniHomeServer запущен на 0.0.0.0:8080 (на всех сетевых интерфейсах)"
  echo "В браузере: http://IP_СЕРВЕРА:8080 (если есть разрешение в firewall / проброс порта)"
  echo "ВАЖНО: подключение HTTP не шифруется; для выхода в интернет нужен HTTPS."
else
  echo "Служба установлена, но запуск не подтвердился. Проверьте: journalctl -u minihome -n 50 --no-pager" >&2
  exit 1
fi
