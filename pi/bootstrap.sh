#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  OnDeck — one-line bootstrap installer
#
#  Downloads the repo (no SSH key needed) and runs the installer.
#  On a freshly-imaged Pi (any username), run ONE of:
#
#    curl -fsSL https://raw.githubusercontent.com/loggerhead-turtle/OnDeck/main/pi/bootstrap.sh | sudo bash -s -- deck
#    curl -fsSL https://raw.githubusercontent.com/loggerhead-turtle/OnDeck/main/pi/bootstrap.sh | sudo bash -s -- audio
#    curl -fsSL https://raw.githubusercontent.com/loggerhead-turtle/OnDeck/main/pi/bootstrap.sh | sudo bash -s -- both
#
#  Role can also come from ONDECK_ROLE=deck|audio|both (coach == deck). Other overrides:
#    ONDECK_REPO_SLUG  (default loggerhead-turtle/OnDeck)
#    ONDECK_BRANCH     (default main)
#    ONDECK_GIT_TOKEN  (for a private repo)
#    ONDECK_USER       (service user; defaults to the user who ran sudo)
# ════════════════════════════════════════════════════════════
set -euo pipefail

ROLE="${1:-${ONDECK_ROLE:-both}}"
case "$ROLE" in deck|coach|audio|both) ;; *)
  echo "Usage: bootstrap.sh <deck|audio|both>   (or set ONDECK_ROLE)"; exit 1 ;;
esac

if [[ $EUID -ne 0 ]]; then echo "Run with sudo."; exit 1; fi

REPO_SLUG="${ONDECK_REPO_SLUG:-loggerhead-turtle/OnDeck}"
BRANCH="${ONDECK_BRANCH:-main}"

# Service user / home, so the repo lands in the right place regardless of the
# username chosen in Raspberry Pi Imager (no hardcoded /home/pi).
if [[ -n "${ONDECK_USER:-}" ]]; then USER_NAME="$ONDECK_USER"
elif [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then USER_NAME="$SUDO_USER"
elif id pi &>/dev/null; then USER_NAME="pi"
else USER_NAME="$(whoami)"; fi
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"; USER_HOME="${USER_HOME:-/home/$USER_NAME}"
REPO_DIR="$USER_HOME/OnDeck"

echo "[bootstrap] role=$ROLE user=$USER_NAME repo=$REPO_SLUG@$BRANCH dir=$REPO_DIR"

command -v git  >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y --no-install-recommends git; }
command -v curl >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y --no-install-recommends curl ca-certificates; }

url="https://github.com/${REPO_SLUG}.git"
[[ -n "${ONDECK_GIT_TOKEN:-}" ]] && url="https://${ONDECK_GIT_TOKEN}@github.com/${REPO_SLUG}.git"

if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" remote set-url origin "$url" 2>/dev/null || true
  git -C "$REPO_DIR" fetch origin "$BRANCH" && git -C "$REPO_DIR" reset --hard "origin/$BRANCH" || true
elif git clone -b "$BRANCH" --depth 1 "$url" "$REPO_DIR" 2>/dev/null; then
  :
else
  echo "[bootstrap] git clone failed — using tarball"
  rm -rf "$REPO_DIR.tmp"; mkdir -p "$REPO_DIR.tmp"
  curl -fsSL "https://codeload.github.com/${REPO_SLUG}/tar.gz/refs/heads/${BRANCH}" \
    | tar xz -C "$REPO_DIR.tmp" --strip-components=1
  rm -rf "$REPO_DIR"; mv "$REPO_DIR.tmp" "$REPO_DIR"
fi
chown -R "$USER_NAME:$USER_NAME" "$REPO_DIR"

# Hand off to the repo's installer as the service user.
exec sudo -u "$USER_NAME" ROLE="$ROLE" bash "$REPO_DIR/install.sh"
