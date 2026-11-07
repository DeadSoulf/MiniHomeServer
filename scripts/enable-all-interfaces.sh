#!/usr/bin/env bash
# Switch an already installed MiniHomeServer systemd service from localhost to all IPv4 interfaces.
# Preserves the database, authentication secret, SSH, Termalin and firewall settings.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Нужны права root: su - ; bash /root/MiniHomeServer/scripts/enable-all-interfaces.sh" >&2
  exit 1
fi

if ! systemctl cat minihome.service >/dev/null 2>&1; then
  echo "Служба minihome.service не найдена. Сначала установите MiniHomeServer:" >&2
  echo "bash /root/MiniHomeServer/scripts/install-systemd.sh" >&2
  exit 1
fi

install -d -m 0755 /etc/systemd/system/minihome.service.d
cat > /etc/systemd/system/minihome.service.d/20-listen-all.conf <<'EOF'
[Service]
Environment=MINIHOME_HOST=0.0.0.0
EOF

systemctl daemon-reload
systemctl restart minihome.service

if ! systemctl is-active --quiet minihome.service; then
  echo "Ошибка запуска. Проверьте: journalctl -u minihome -n 50 --no-pager" >&2
  exit 1
fi

python3 - <<'PY'
import json
import urllib.request
with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=5) as resp:
    info = json.load(resp)
    if not info.get("ok"):
        raise SystemExit("API ответил, но health-check не пройден")
print("HTTP API:", info)
PY

if ! ss -H -ltn '( sport = :8080 )' | grep -q '0.0.0.0:8080'; then
  echo "Проверка: сокет 0.0.0.0:8080 не обнаружен. Текущее состояние:" >&2
  ss -lnt '( sport = :8080 )' >&2
  exit 1
fi

echo "MiniHomeServer слушает 0.0.0.0:8080"
echo "Локальная сеть: http://<LAN_IP>:8080"
echo "Из интернета: http://<PUBLIC_IP>:8080 — только после разрешения входящего TCP 8080 / проброса порта"
echo "ВНИМАНИЕ: HTTP не шифрует пароль. Для доступа из интернета настройте HTTPS или VPN."
