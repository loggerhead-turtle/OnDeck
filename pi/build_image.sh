#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  OnDeck — flashable SD-card image builder (pi-gen)
#
#  Produces a ready-to-flash Raspberry Pi OS Lite (64-bit) image with OnDeck
#  baked in: all apt packages preinstalled, the repo + Python venv in place,
#  and a first-boot service that finishes the install (systemd units, udev,
#  Bluetooth/WirePlumber config) and then hands off to the normal OnDeck
#  boot gate — so an unconfigured Pi raises the `OnDeck-Setup` hotspot and
#  the coach finishes Wi-Fi + cloud pairing from a phone.
#
#  Usage (on any Linux box with Docker, ~30 GB free disk):
#
#    ROLE=audio ./pi/build_image.sh    # Audio Pi image (default)
#    ROLE=deck  ./pi/build_image.sh    # Stream Deck Pi image
#    ROLE=both  ./pi/build_image.sh    # single-Pi image (deck + audio,
#                                      #   e.g. deck Pi → Bluetooth speaker)
#
#  Optional overrides:
#    ONDECK_REPO_SLUG   github repo to bake in   (default loggerhead-turtle/OnDeck)
#    ONDECK_BRANCH      branch to bake in        (default main)
#    PIGEN_DIR          pi-gen checkout location (default ./.pi-gen)
#
#  Output: deploy/image_*OnDeck-<role>.img.xz — flash with Raspberry Pi
#  Imager ("Use custom") or `xzcat … | dd`. The role can be changed later by
#  editing `ondeck-role` on the boot partition before first boot.
# ════════════════════════════════════════════════════════════
set -euo pipefail

ROLE="${ROLE:-audio}"
case "$ROLE" in audio|deck|coach|both) ;; *)
  echo "ROLE must be audio|deck|both"; exit 1 ;;
esac
[[ "$ROLE" == "coach" ]] && ROLE=deck

REPO_SLUG="${ONDECK_REPO_SLUG:-loggerhead-turtle/OnDeck}"
BRANCH="${ONDECK_BRANCH:-main}"
PIGEN_DIR="${PIGEN_DIR:-$(pwd)/.pi-gen}"
FIRST_USER="${ONDECK_IMG_USER:-ondeck}"

command -v docker >/dev/null 2>&1 || {
  echo "Docker is required (pi-gen builds inside a container)."; exit 1; }

# --- pi-gen checkout (arm64 branch = 64-bit OS) ---------------------------
if [ ! -d "$PIGEN_DIR/.git" ]; then
  git clone --depth 1 --branch arm64 https://github.com/RPi-Distro/pi-gen.git "$PIGEN_DIR"
else
  git -C "$PIGEN_DIR" pull --ff-only || true
fi

STAGE="$PIGEN_DIR/stage-ondeck"
rm -rf "$STAGE"
mkdir -p "$STAGE/01-ondeck/files"

# Stage plumbing: build on top of stage2 (the Lite image).
touch "$STAGE/EXPORT_IMAGE"
cat > "$STAGE/prerun.sh" <<'EOF'
#!/bin/bash -e
if [ ! -d "${ROOTFS_DIR}" ]; then
  copy_previous
fi
EOF
chmod +x "$STAGE/prerun.sh"

# --- packages preinstalled into the image (superset of install.sh) --------
cat > "$STAGE/01-ondeck/00-packages" <<'EOF'
git curl ca-certificates
python3 python3-venv python3-pip ffmpeg
alsa-utils bluez rfkill
pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth pulseaudio-utils
libhidapi-libusb0
hostapd dnsmasq iw wireless-tools wpasupplicant iptables
EOF

# --- bake the repo + venv, install the first-boot finisher -----------------
cat > "$STAGE/01-ondeck/01-run-chroot.sh" <<CHROOT
#!/bin/bash -e
# Runs INSIDE the image chroot during the build.

USER_NAME="${FIRST_USER}"
USER_HOME="/home/\${USER_NAME}"
REPO_DIR="\${USER_HOME}/OnDeck"

# Repo checkout baked into the image (updated later by re-running bootstrap).
rm -rf "\${REPO_DIR}"
git clone --depth 1 -b "${BRANCH}" "https://github.com/${REPO_SLUG}.git" "\${REPO_DIR}"

# Python env prebuilt so first boot doesn't need the network for pip.
python3 -m venv "\${REPO_DIR}/.venv"
"\${REPO_DIR}/.venv/bin/pip" install --no-cache-dir --upgrade pip
"\${REPO_DIR}/.venv/bin/pip" install --no-cache-dir -r "\${REPO_DIR}/requirements.txt"
chown -R "\${USER_NAME}:\${USER_NAME}" "\${REPO_DIR}"

# Role marker on the boot partition — editable before first boot.
echo "${ROLE}" > /boot/firmware/ondeck-role || echo "${ROLE}" > /boot/ondeck-role || true

# First-boot finisher: runs install.sh on the live system (systemd units,
# udev rules, sudoers, Bluetooth config — things a chroot can't do), then
# disables itself. install.sh is idempotent and its apt step is a no-op here
# because every package is already baked in.
cat > /usr/local/sbin/ondeck-firstboot <<'SCRIPT'
#!/bin/bash
set -euo pipefail
ROLE=\$(cat /boot/firmware/ondeck-role 2>/dev/null || cat /boot/ondeck-role 2>/dev/null || echo audio)
USER_NAME=\$(getent passwd 1000 | cut -d: -f1)
sudo -u "\$USER_NAME" ONDECK_USER="\$USER_NAME" ROLE="\$ROLE" bash "/home/\$USER_NAME/OnDeck/install.sh"
touch /var/lib/ondeck-firstboot.done
SCRIPT
chmod +x /usr/local/sbin/ondeck-firstboot

cat > /etc/systemd/system/ondeck-firstboot.service <<'UNIT'
[Unit]
Description=OnDeck first-boot installer
After=network.target
ConditionPathExists=!/var/lib/ondeck-firstboot.done

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=infinity
ExecStart=/usr/local/sbin/ondeck-firstboot

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable ondeck-firstboot.service
CHROOT
chmod +x "$STAGE/01-ondeck/01-run-chroot.sh"

# --- pi-gen config ---------------------------------------------------------
cat > "$PIGEN_DIR/config" <<EOF
IMG_NAME="OnDeck-${ROLE}"
RELEASE="bookworm"
DEPLOY_COMPRESSION="xz"
TARGET_HOSTNAME="ondeck-${ROLE}"
FIRST_USER_NAME="${FIRST_USER}"
FIRST_USER_PASS="ondeck"
DISABLE_FIRST_BOOT_USER_RENAME=1
ENABLE_SSH=1
STAGE_LIST="stage0 stage1 stage2 stage-ondeck"
EOF

echo "==> Building OnDeck-${ROLE} image (this takes 30–60 minutes)…"
cd "$PIGEN_DIR"
./build-docker.sh

echo
echo "Done. Flash the image in ${PIGEN_DIR}/deploy/ with Raspberry Pi Imager"
echo "('Use custom'). Default login: ${FIRST_USER} / ondeck — change it on"
echo "first login. On first boot the Pi finishes installing itself, then"
echo "opens the OnDeck-Setup hotspot if it has no Wi-Fi."
