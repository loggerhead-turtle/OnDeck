#!/usr/bin/env bash
#
# OnDeck installer.
#
# Runs on any Raspberry Pi OS (or Debian/Ubuntu) install. It never assumes the
# "pi" account — everything is set up for whatever user runs this script.
#
# Usage:
#   ./install.sh            # install for the current user, both roles
#   ROLE=audio ./install.sh # install only the Audio Pi service
#   ROLE=coach ./install.sh # install only the Coach Pi service
#
# Re-running is safe (idempotent).

set -euo pipefail

# --- who and where -------------------------------------------------------
RUN_USER="${SUDO_USER:-$USER}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLE="${ROLE:-both}"          # audio | coach | both
PYTHON="${PYTHON:-python3}"

echo "OnDeck installer"
echo "  user:   $RUN_USER"
echo "  home:   $RUN_HOME"
echo "  repo:   $REPO_DIR"
echo "  role:   $ROLE"

# --- system dependencies -------------------------------------------------
echo "==> Installing system packages (sudo may prompt)..."
sudo apt-get update -qq
PKGS=(python3 python3-venv python3-pip ffmpeg)
if [[ "$ROLE" == "audio" || "$ROLE" == "both" ]]; then
  # Audio playback + YouTube import + Bluetooth.
  PKGS+=(alsa-utils bluez)
fi
sudo apt-get install -y --no-install-recommends "${PKGS[@]}"

# --- python environment --------------------------------------------------
echo "==> Creating virtual environment..."
VENV="$REPO_DIR/.venv"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

# --- runtime data dir ----------------------------------------------------
ONDECK_HOME="$RUN_HOME/ondeck"
mkdir -p "$ONDECK_HOME/music"
echo "==> Runtime data dir: $ONDECK_HOME"

# --- systemd services ----------------------------------------------------
install_service() {
  local name="$1" exec_line="$2" desc="$3"
  local unit="/etc/systemd/system/${name}.service"
  echo "==> Installing service: $name"
  sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=$desc
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR
Environment=ONDECK_HOME=$ONDECK_HOME
ExecStart=$VENV/bin/python $exec_line
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable "$name"
  sudo systemctl restart "$name"
}

if [[ "$ROLE" == "audio" || "$ROLE" == "both" ]]; then
  install_service "ondeck-audio" "$REPO_DIR/music_server.py" "OnDeck Audio Pi server"
fi
if [[ "$ROLE" == "coach" || "$ROLE" == "both" ]]; then
  install_service "ondeck-coach" "$REPO_DIR/main.py" "OnDeck Coach Pi (Stream Deck + web portal)"
fi

# --- cloud sync timer (all roles) ----------------------------------------
# Runs sync_agent.py every 5 minutes when internet is available.
# Secrets live in ~/ondeck/sync.env (not in the repo).
echo "==> Installing cloud sync timer..."

SYNC_ENV="$ONDECK_HOME/sync.env"
if [[ ! -f "$SYNC_ENV" ]]; then
  cat > "$SYNC_ENV" <<'ENVEOF'
# OnDeck cloud sync credentials.
# Set these values after deploying to Render.
ONDECK_CLOUD_URL=
ONDECK_SYNC_TOKEN=
ENVEOF
  echo "  Created $SYNC_ENV — fill in ONDECK_CLOUD_URL and ONDECK_SYNC_TOKEN."
fi

sudo tee /etc/systemd/system/ondeck-sync.service >/dev/null <<EOF
[Unit]
Description=OnDeck cloud sync (one-shot)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$REPO_DIR
Environment=ONDECK_HOME=$ONDECK_HOME
EnvironmentFile=-$SYNC_ENV
ExecStart=$VENV/bin/python $REPO_DIR/sync_agent.py
StandardOutput=journal
StandardError=journal
EOF

sudo tee /etc/systemd/system/ondeck-sync.timer >/dev/null <<EOF
[Unit]
Description=OnDeck cloud sync every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Unit=ondeck-sync.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ondeck-sync.timer
sudo systemctl start ondeck-sync.timer

echo
echo "OnDeck installed."
[[ "$ROLE" == "audio" || "$ROLE" == "both" ]] && \
  echo "  Audio server:  http://$(hostname -I | awk '{print $1}'):5100/health"
[[ "$ROLE" == "coach" || "$ROLE" == "both" ]] && \
  echo "  Web portal:    http://$(hostname -I | awk '{print $1}'):5000"
echo "  Sync logs:     journalctl -u ondeck-sync -f"
echo "  Sync env:      $SYNC_ENV  (add cloud URL + token here)"
