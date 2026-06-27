#!/usr/bin/env bash
#
# OnDeck installer.
#
# Runs on any Raspberry Pi OS (or Debian/Ubuntu) install. It never assumes the
# "pi" account — everything is set up for whatever user runs this script.
#
# Usage:
#   ./install.sh            # install for the current user, both roles
#   ROLE=audio ./install.sh # install only the Audio Pi service (music)
#   ROLE=deck  ./install.sh # install only the Stream Deck Pi service
#   ROLE=coach ./install.sh # alias of deck (back-compat)
#
# Re-running is safe (idempotent).

set -euo pipefail

# --- who and where -------------------------------------------------------
RUN_USER="${ONDECK_USER:-${SUDO_USER:-$USER}}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLE="${ROLE:-both}"          # audio | deck | coach | both  (coach == deck)
PYTHON="${PYTHON:-python3}"

# Resolve the role into which services to install. "deck" and the legacy
# "coach" both install the Stream Deck unit (Stream Deck + local web portal).
case "$ROLE" in
  audio)      INSTALL_AUDIO=1; INSTALL_DECK=0 ;;
  deck|coach) INSTALL_AUDIO=0; INSTALL_DECK=1 ;;
  both)       INSTALL_AUDIO=1; INSTALL_DECK=1 ;;
  *) echo "Unknown ROLE '$ROLE' (use audio|deck|coach|both)"; exit 1 ;;
esac

echo "OnDeck installer"
echo "  user:   $RUN_USER"
echo "  home:   $RUN_HOME"
echo "  repo:   $REPO_DIR"
echo "  role:   $ROLE  (audio=$INSTALL_AUDIO deck=$INSTALL_DECK)"

# --- system dependencies -------------------------------------------------
echo "==> Installing system packages (sudo may prompt)..."
sudo apt-get update -qq
PKGS=(python3 python3-venv python3-pip ffmpeg)
if [[ "$INSTALL_AUDIO" == 1 ]]; then
  # Audio playback + Bluetooth A2DP speaker output. PipeWire (the Bookworm
  # default) provides the bluez audio sink ffmpeg plays into; pulseaudio-utils
  # gives us `pactl` for sink discovery; bluez gives `bluetoothctl`.
  PKGS+=(alsa-utils bluez pipewire pipewire-pulse wireplumber
         libspa-0.2-bluetooth pulseaudio-utils)
fi
if [[ "$INSTALL_DECK" == 1 ]]; then
  # HID backend the Python `streamdeck` library dlopen's to talk to the deck.
  PKGS+=(libhidapi-libusb0)
fi
# Headless Wi-Fi onboarding (captive-portal hotspot + network tooling) runs on
# EVERY role — both the Audio Pi and the Stream Deck Pi can be set up with no
# pre-configured Wi-Fi.
PKGS+=(hostapd dnsmasq iw wireless-tools wpasupplicant iptables)
sudo apt-get install -y --no-install-recommends "${PKGS[@]}"

# The setup portal manages the AP itself; keep these services from grabbing
# wlan0 at boot (the boot gate starts/stops them on demand).
sudo systemctl unmask hostapd 2>/dev/null || true
sudo systemctl disable hostapd dnsmasq 2>/dev/null || true

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
  local name="$1" exec_line="$2" desc="$3" extra_env="${4:-}"
  local unit="/etc/systemd/system/${name}.service"
  echo "==> Installing service: $name"
  sudo tee "$unit" >/dev/null <<EOF
[Unit]
Description=$desc
After=network-online.target sound.target bluetooth.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_DIR
Environment=ONDECK_HOME=$ONDECK_HOME
${extra_env}
ExecStart=$VENV/bin/python $exec_line
# Always restart — covers a hung/blocked process, not just a non-zero exit.
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable "$name"
  sudo systemctl restart "$name"
}

if [[ "$INSTALL_AUDIO" == 1 ]]; then
  # --- Bluetooth A2DP speaker output -------------------------------------
  # The audio server plays into the connected speaker's PipeWire sink. For a
  # service (no login session) to reach PipeWire we (a) let the user's session
  # manager linger so PipeWire/WirePlumber run at boot, (b) add the user to the
  # audio/bluetooth groups, and (c) point the service at the user's runtime dir.
  echo "==> Configuring Bluetooth audio for $RUN_USER"
  sudo apt-get install -y --no-install-recommends rfkill 2>/dev/null || true
  sudo usermod -aG bluetooth,audio "$RUN_USER" 2>/dev/null || true
  sudo loginctl enable-linger "$RUN_USER" 2>/dev/null || true
  # Clear any rfkill soft-block and let BlueZ power the controller on at boot,
  # so the speaker page never gets stuck on "radio off".
  sudo rfkill unblock bluetooth 2>/dev/null || true
  if [[ -f /etc/bluetooth/main.conf ]] && ! grep -q '^\s*AutoEnable=true' /etc/bluetooth/main.conf; then
    if grep -q '^\[Policy\]' /etc/bluetooth/main.conf; then
      sudo sed -i '/^\[Policy\]/a AutoEnable=true' /etc/bluetooth/main.conf
    else
      printf '\n[Policy]\nAutoEnable=true\n' | sudo tee -a /etc/bluetooth/main.conf >/dev/null
    fi
  fi
  # Let the service user clear a soft-block at runtime (no password) — the
  # captive-portal hotspot can re-block the radio after onboarding.
  sudo tee /etc/sudoers.d/ondeck-rfkill >/dev/null <<EOF
$RUN_USER ALL=(root) NOPASSWD: /usr/sbin/rfkill unblock bluetooth, /sbin/rfkill unblock bluetooth, /usr/bin/rfkill unblock bluetooth
EOF
  sudo chmod 0440 /etc/sudoers.d/ondeck-rfkill
  sudo systemctl enable bluetooth 2>/dev/null || true
  sudo systemctl start bluetooth 2>/dev/null || true
  # systemd-rfkill restores the *saved* (possibly blocked) state at every boot,
  # so a one-time unblock above won't survive a reboot. This oneshot re-unblocks
  # and powers the radio on each boot — and `enable --now` runs it immediately,
  # which is what actually clears "Soft blocked: yes / Powered: no" during a
  # re-install (no reboot needed).
  sudo tee /etc/systemd/system/ondeck-bt-radio.service >/dev/null <<EOF
[Unit]
Description=OnDeck — unblock and power on the Bluetooth radio
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'rfkill unblock bluetooth || true; sleep 2; for i in 1 2 3 4 5; do bluetoothctl power on && break; sleep 2; done'

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now ondeck-bt-radio.service 2>/dev/null || true
  # WirePlumber gates its Bluetooth monitor on an active logind *seat* by
  # default. A headless Pi has lingering (above) but no seat, so the monitor
  # never registers an A2DP endpoint and `connect` fails with
  # org.bluez.Error.Failed br-connection-profile-unavailable. Disable seat
  # monitoring so the speaker can connect headless.
  sudo mkdir -p /etc/wireplumber/wireplumber.conf.d
  sudo tee /etc/wireplumber/wireplumber.conf.d/50-ondeck-bluez-no-seat.conf >/dev/null <<'EOF'
# OnDeck Audio Pi runs headless with no active logind seat, so the default
# seat-gated Bluetooth monitor never activates. Disable seat monitoring so
# A2DP endpoints register and speakers can connect.
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
EOF
  # Enable the user PipeWire stack so the bluez sink exists headless.
  sudo -u "$RUN_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$RUN_USER")" \
    systemctl --user enable --now pipewire pipewire-pulse wireplumber 2>/dev/null || true

  RUN_UID="$(id -u "$RUN_USER")"
  install_service "ondeck-audio" "$REPO_DIR/music_server.py" \
    "OnDeck Audio Pi server" \
    "Environment=XDG_RUNTIME_DIR=/run/user/$RUN_UID"
fi
if [[ "$INSTALL_DECK" == 1 ]]; then
  # --- Stream Deck USB access --------------------------------------------
  # The Python `streamdeck` library (libusb backend) must be able to open the
  # Elgato device as the service user. A udev rule + plugdev membership grants
  # that headlessly (the `uaccess` tag only covers an interactive seat, which a
  # systemd service doesn't have). Done before install_service so the service
  # starts with access.
  echo "==> Installing Stream Deck udev rule"
  sudo tee /etc/udev/rules.d/99-ondeck-streamdeck.rules >/dev/null <<'EOF'
# Elgato Stream Deck (idVendor 0fd9) — let the OnDeck service user (plugdev) open it.
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", GROUP="plugdev", MODE="0660"
KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", GROUP="plugdev", MODE="0660"
EOF
  sudo groupadd -f plugdev 2>/dev/null || true
  sudo usermod -aG plugdev "$RUN_USER" 2>/dev/null || true
  sudo udevadm control --reload-rules 2>/dev/null || true
  sudo udevadm trigger 2>/dev/null || true

  install_service "ondeck-coach" "$REPO_DIR/main.py" "OnDeck Stream Deck Pi (Stream Deck + web portal)"
fi

# --- keep Wi-Fi awake (both roles) ---------------------------------------
# The Pi's onboard Wi-Fi defaults to power-save, which after an idle stretch
# parks the radio and often fails to re-associate until a reboot — the most
# common cause of "both Pis were unreachable overnight, fine after reboot".
# Disable it persistently.
echo "==> Disabling Wi-Fi power saving on wlan0"
# Turn it off right now for the running connection…
sudo iw dev wlan0 set power_save off 2>/dev/null || true
# …and at the source. On Bookworm, NetworkManager owns the radio and re-enables
# power-save on every (re)connect, so an `iw` command alone doesn't stick — a
# conf drop-in (2 = disable) makes it persistent. `reload` (not restart) avoids
# dropping the SSH/Wi-Fi link you're installing over.
if command -v nmcli >/dev/null 2>&1; then
  sudo mkdir -p /etc/NetworkManager/conf.d
  printf '[connection]\nwifi.powersave = 2\n' \
    | sudo tee /etc/NetworkManager/conf.d/99-ondeck-no-powersave.conf >/dev/null
  active="$(nmcli -t -f NAME connection show --active 2>/dev/null | head -1)"
  [[ -n "$active" ]] && sudo nmcli connection modify "$active" 802-11-wireless.powersave 2 2>/dev/null || true
  sudo systemctl reload NetworkManager 2>/dev/null || true
fi
# Belt-and-suspenders for non-NetworkManager (dhcpcd/wpa_supplicant) images.
sudo tee /etc/systemd/system/ondeck-wifi-awake.service >/dev/null <<EOF
[Unit]
Description=OnDeck — disable wlan0 Wi-Fi power saving
After=network.target
Wants=network.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c '/sbin/iw dev wlan0 set power_save off || /usr/sbin/iw dev wlan0 set power_save off || true'

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ondeck-wifi-awake.service 2>/dev/null || true

# Let the service user manage Wi-Fi via the helper without a password prompt —
# used by the /wifi page on BOTH the deck portal and the Audio Pi's :5100 server.
echo "==> Installing sudoers rule for Wi-Fi management"
sudo tee /etc/sudoers.d/ondeck-wifi >/dev/null <<EOF
$RUN_USER ALL=(root) NOPASSWD: $VENV/bin/python $REPO_DIR/pi/add_wifi.py, /sbin/wpa_cli -i wlan0 reconfigure, /usr/sbin/wpa_cli -i wlan0 reconfigure
EOF
sudo chmod 0440 /etc/sudoers.d/ondeck-wifi

# --- headless network onboarding (boot gate + captive portal) ------------
# A oneshot that runs BEFORE the main service on EVERY role: applies
# boot-partition Wi-Fi, links via a dropped ondeck.json / pending pairing code,
# or opens the OnDeck-Setup hotspot when the Pi has no working Wi-Fi / cloud
# link. Runs as root (needs hostapd/dnsmasq). The Before= lists both units; a
# missing one is simply ignored.
echo "==> Installing service: ondeck-setup (boot gate)"
sudo tee /etc/systemd/system/ondeck-setup.service >/dev/null <<EOF
[Unit]
Description=OnDeck boot gate (Wi-Fi setup / cloud link)
Before=ondeck-audio.service ondeck-coach.service
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
[[ "$INSTALL_AUDIO" == 1 ]] && \
  echo "  Audio server:  http://$(hostname -I | awk '{print $1}'):5100/health"
[[ "$INSTALL_DECK" == 1 ]] && \
  echo "  Web portal:    http://$(hostname -I | awk '{print $1}'):5000"
echo "  Sync logs:     journalctl -u ondeck-sync -f"
echo "  Sync env:      $SYNC_ENV  (add cloud URL + token here)"
