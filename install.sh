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
if [[ "$ROLE" == "coach" || "$ROLE" == "both" ]]; then
  # Headless Wi-Fi onboarding: captive-portal hotspot + network tooling.
  PKGS+=(hostapd dnsmasq iw wireless-tools wpasupplicant iptables)
fi
sudo apt-get install -y --no-install-recommends "${PKGS[@]}"

# The setup portal manages the AP itself; keep these services from grabbing
# wlan0 at boot (the boot gate starts/stops them on demand).
if [[ "$ROLE" == "coach" || "$ROLE" == "both" ]]; then
  sudo systemctl unmask hostapd 2>/dev/null || true
  sudo systemctl disable hostapd dnsmasq 2>/dev/null || true
fi

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

  # --- headless network onboarding (boot gate + captive portal) ----------
  # A oneshot that runs BEFORE ondeck-coach: applies boot-partition Wi-Fi,
  # links via a dropped ondeck.json, or opens the OnDeck-Setup hotspot when the
  # Pi has no working Wi-Fi / cloud link. Runs as root (needs hostapd/dnsmasq).
  echo "==> Installing service: ondeck-setup (boot gate)"
  sudo tee /etc/systemd/system/ondeck-setup.service >/dev/null <<EOF
[Unit]
Description=OnDeck boot gate (Wi-Fi setup / cloud link)
Before=ondeck-coach.service
After=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=root
WorkingDirectory=$REPO_DIR
Environment=ONDECK_HOME=$ONDECK_HOME
Environment=ONDECK_USER=$RUN_USER
ExecStart=$VENV/bin/python $REPO_DIR/pi/boot_mode.py
# The portal blocks until the coach finishes setup, which can take minutes.
TimeoutStartSec=infinity
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable ondeck-setup.service

  # Let the web portal (service user) manage Wi-Fi via the helper without a
  # password prompt — used by the /wifi page.
  echo "==> Installing sudoers rule for Wi-Fi management"
  sudo tee /etc/sudoers.d/ondeck-wifi >/dev/null <<EOF
$RUN_USER ALL=(root) NOPASSWD: $VENV/bin/python $REPO_DIR/pi/add_wifi.py, /sbin/wpa_cli -i wlan0 reconfigure, /usr/sbin/wpa_cli -i wlan0 reconfigure
EOF
  sudo chmod 0440 /etc/sudoers.d/ondeck-wifi
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
