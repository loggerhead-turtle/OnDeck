#!/usr/bin/env python3
"""OnDeck boot gatekeeper — ExecStart for ondeck-setup.service.

A oneshot that must complete before the main service (ondeck-coach) starts.

Decision tree:
  0. Apply any Wi-Fi credentials dropped on the SD boot partition.
  0b. If a force-setup marker is on the boot partition → always run the portal.
  1. Already linked (sync.env has a token) → wait for internet, then boot.
       no internet → open the Wi-Fi management portal so a new network can be
       added at the field / school / away game.
  2. Zero-touch: an ondeck.json on the boot partition → write the cloud link
     and boot (Wi-Fi is assumed baked in via Raspberry Pi Imager).
  3. Not linked → run the captive-portal setup (Wi-Fi + cloud link), then reboot.

Must run as root (needs wpa_supplicant, and the portal needs hostapd/dnsmasq).
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
import time
from pathlib import Path

# Importable whether launched as `pi/boot_mode.py` or from within pi/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from netconfig import (
    clear_pending_link,
    is_configured,
    read_pending_link,
    redeem_pairing_code,
    write_sync_env,
    write_wifi,
    AP_IFACE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  boot_mode  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("boot_mode")

# Bookworm mounts the FAT boot partition at /boot/firmware; older OS at /boot.
_BOOT_DIRS = [Path("/boot/firmware"), Path("/boot")]


def _boot_files(*names: str) -> list[Path]:
    return [d / n for d in _BOOT_DIRS for n in names]


# Zero-touch cloud link dropped on the boot partition (see ondeck.boot.example.json).
PROVISION_FILES = _boot_files("ondeck.json")
# Force the setup portal even on a configured Pi (the guaranteed escape hatch).
FORCE_SETUP_FILES = _boot_files("ondeck-setup", "ondeck-setup.txt")
# Wi-Fi credentials applied then deleted (holds the password in plaintext).
WIFI_FILES = _boot_files("ondeck-wifi.json", "ondeck-wifi.txt")


def _wait_for_internet(timeout: int = 60) -> bool:
    log.info("Waiting for internet…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.setdefaulttimeout(3)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
            log.info("Internet up")
            return True
        except OSError:
            time.sleep(2)
    log.warning("Timed out waiting for internet")
    return False


def _wifi_associated() -> bool:
    """On a Wi-Fi network at all (an SSID association / carrier), which is
    NOT the same thing as having internet. The field router being alive
    matters to the deck (it is how the tablet and the Audio Pi are
    reached); the WAN behind it does not."""
    try:
        r = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True,
                           timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return True
    except Exception:
        pass
    try:
        state = Path(f"/sys/class/net/{AP_IFACE}/operstate").read_text().strip()
        return state == "up"
    except Exception:
        return False


def _wait_for_wifi(timeout: int = 30) -> bool:
    log.info("Waiting for Wi-Fi association…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _wifi_associated():
            log.info("Wi-Fi associated")
            return True
        time.sleep(2)
    log.warning("Timed out waiting for Wi-Fi")
    return False


def _spawn_setup_portal_background(wifi_only: bool = True) -> None:
    """Open the setup portal WITHOUT blocking this gate — so the deck
    service still starts behind it. Used only on a configured Pi with no
    Wi-Fi: the coach gets the running deck (its keys and status page say
    what is wrong) AND the OnDeck-Setup hotspot to fix the network from
    a phone. The portal reboots the Pi when setup completes."""
    portal = Path(__file__).resolve().parent / "setup_server.py"
    args = [sys.executable, str(portal)]
    if wifi_only:
        args.append("--wifi-only")
    try:
        subprocess.Popen(args, start_new_session=True)
        log.info("Setup portal launched in the background")
    except Exception as exc:
        log.error("Could not launch background setup portal: %s", exc)


def _apply_boot_wifi() -> bool:
    """Apply a Wi-Fi file from the boot partition, then delete it."""
    for p in WIFI_FILES:
        try:
            if not p.exists():
                continue
            raw = p.read_text().strip()
            ssid = password = ""
            if p.suffix == ".json" or raw.startswith("{"):
                data = json.loads(raw)
                ssid = (data.get("ssid") or "").strip()
                password = data.get("password") or data.get("psk") or ""
            else:
                for line in raw.splitlines():
                    k, _, v = line.partition("=")
                    if k.strip().lower() == "ssid":
                        ssid = v.strip()
                    elif k.strip().lower() in ("password", "psk"):
                        password = v.strip()
            if not ssid:
                log.warning("%s had no ssid — ignoring", p)
                p.unlink()
                continue
            write_wifi(ssid, password)
            log.info("Applied boot-partition Wi-Fi for '%s'", ssid)
            p.unlink()
            subprocess.run(["wpa_cli", "-i", AP_IFACE, "reconfigure"],
                           check=False, capture_output=True)
            return True
        except Exception as exc:
            log.error("Bad boot Wi-Fi file %s: %s", p, exc)
    return False


def _force_setup_requested() -> bool:
    """True if a force-setup marker is present; consume it so it runs once."""
    found = False
    for p in FORCE_SETUP_FILES:
        try:
            if p.exists():
                found = True
                p.unlink()
                log.info("Force-setup marker %s found — opening portal", p)
        except Exception as exc:
            log.warning("Could not consume marker %s: %s", p, exc)
            found = True
    return found


def _read_provision() -> dict | None:
    """Return a boot ondeck.json link, or None.

    Supports either a ready ``sync_token`` or a ``pairing_code`` to be redeemed
    once the (baked-in) Wi-Fi is online.
    """
    for p in PROVISION_FILES:
        try:
            if not p.exists():
                continue
            data = json.loads(p.read_text())
            if data.get("cloud_url") and (data.get("sync_token") or data.get("pairing_code")):
                return {
                    "cloud_url": data["cloud_url"],
                    "sync_token": data.get("sync_token"),
                    "pairing_code": data.get("pairing_code"),
                    "device_name": data.get("device_name", "OnDeck Pi"),
                }
            log.warning("%s missing cloud_url/sync_token/pairing_code — ignoring", p)
        except Exception as exc:
            log.error("Bad boot provision file %s: %s", p, exc)
    return None


def _consume_provision() -> None:
    for p in PROVISION_FILES:
        try:
            if p.exists():
                p.unlink()
                log.info("Removed %s after linking", p)
        except Exception as exc:
            log.warning("Could not remove %s: %s", p, exc)


def _run_setup_portal(wifi_only: bool = False) -> None:
    """Launch the captive-portal setup server (blocks until the Pi reboots)."""
    mode = "Wi-Fi management" if wifi_only else "Wi-Fi + cloud link"
    log.info("Starting %s portal…", mode)
    portal = Path(__file__).resolve().parent / "setup_server.py"
    args = [sys.executable, str(portal)]
    if wifi_only:
        args.append("--wifi-only")
    result = subprocess.run(args, check=False)
    sys.exit(result.returncode or 0)


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "OnDeck Pi"


def main() -> None:
    log.info("── OnDeck boot check ──")

    # 0. Wi-Fi credentials edited onto the SD card take effect first.
    _apply_boot_wifi()

    # 0b. Guaranteed escape hatch: force the portal regardless of state.
    if _force_setup_requested():
        _run_setup_portal(wifi_only=is_configured())
        return

    # 1. Already linked — the deck starts NO MATTER WHAT. Game day happens
    #    at fields with no internet, and everything the deck needs on game
    #    day — config, lineups, music files, the Audio Pi — is local.
    #    FIELD REPORT: "the internet supplying my streamdeck and audiopi
    #    didn't work and so the streamdeck pi wouldn't load. I want it to
    #    work at the field even without internet… at least it will load
    #    and the streamdeck will give me insights as to what is wrong."
    #    So a linked Pi is never parked in the setup portal again:
    #      Wi-Fi up, internet down → boot now (sync catches up later);
    #      no Wi-Fi at all        → boot the deck anyway (its Status page
    #                               names the problem) and open the
    #                               OnDeck-Setup hotspot in the BACKGROUND
    #                               so a phone can fix the network.
    if is_configured():
        if _wifi_associated() or _wait_for_wifi(timeout=30):
            up = _wait_for_internet(timeout=10)
            log.info("Device linked, Wi-Fi up, internet %s — starting main "
                     "service", "up" if up else
                     "DOWN (offline mode; sync resumes when it returns)")
            sys.exit(0)
        log.warning("Linked but NO Wi-Fi — starting the deck anyway and "
                    "opening the Wi-Fi hotspot portal in the background")
        _spawn_setup_portal_background(wifi_only=True)
        sys.exit(0)

    # 1b. The captive portal left a pending pairing code — the field Wi-Fi
    #     should now be up, so redeem it for this device's sync token.
    pending = read_pending_link()
    if pending:
        log.info("Pending cloud link found — redeeming pairing code")
        if _wait_for_internet(timeout=60):
            try:
                token = redeem_pairing_code(
                    pending["cloud_url"], pending["code"], _hostname())
                write_sync_env(pending["cloud_url"], token)
                clear_pending_link()
                log.info("Pairing complete — starting main service")
                sys.exit(0)
            except Exception as exc:
                log.error("Pairing failed: %s — reopening setup portal", exc)
                clear_pending_link()
        else:
            log.warning("No internet for pairing — reopening setup portal")
            clear_pending_link()
        _run_setup_portal()
        return

    # 2. Zero-touch cloud link from the boot partition.
    provision = _read_provision()
    if provision:
        log.info("Found boot-partition cloud link — linking automatically")
        _wait_for_internet(timeout=60)  # let the baked-in Wi-Fi associate
        token = provision.get("sync_token")
        if not token and provision.get("pairing_code"):
            try:
                token = redeem_pairing_code(
                    provision["cloud_url"], provision["pairing_code"], _hostname())
            except Exception as exc:
                log.error("Zero-touch pairing failed: %s", exc)
                token = None
        if token:
            write_sync_env(provision["cloud_url"], token)
            _consume_provision()
            log.info("Zero-touch link complete — starting main service")
            sys.exit(0)
        log.warning("Zero-touch link could not complete — opening setup portal")
        _consume_provision()
        _run_setup_portal()
        return

    # 3. Not linked — run the full setup portal.
    _run_setup_portal()


if __name__ == "__main__":
    main()
