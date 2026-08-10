#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="$(id -un)"
SERVICE_GROUP="$(id -gn)"
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
MINERU_API="$PROJECT_DIR/.venv/bin/mineru-api"

if [[ ! -x "$MINERU_API" ]]; then
  echo "MinerU API was not found at: $MINERU_API"
  echo "Run ./install_ubuntu.sh first."
  exit 1
fi

echo "Installing MinerU as a persistent local service..."
sudo tee /etc/systemd/system/mineru-api.service >/dev/null <<EOF
[Unit]
Description=MinerU Local PDF Processing API
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$PROJECT_DIR
Environment=HOME=$SERVICE_HOME
Environment=PATH=$PROJECT_DIR/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
Environment=PYTHONUNBUFFERED=1
Environment=MINERU_API_MAX_CONCURRENT_REQUESTS=1
ExecStart="$MINERU_API" --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=15
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now mineru-api

echo
echo "Service installed. It can take several minutes to become ready after boot."
echo "Status: sudo systemctl status mineru-api"
echo "Logs:   sudo journalctl -u mineru-api -f"
echo "Health: curl --fail http://127.0.0.1:8000/health"
