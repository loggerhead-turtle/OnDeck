#!/usr/bin/env python3
"""Shared Wi-Fi + cloud-link helpers for the OnDeck Pi onboarding.

Used by both the boot gatekeeper (``boot_mode.py``) and the captive-portal
setup server (``setup_server.py``). All functions are defensive — a missing or
malformed file is logged and ignored rather than raised, so a Pi never bricks
its boot over a bad config.

Account linking model: OnDeck links a Pi to its cloud by writing
``ONDECK_CLOUD_URL`` and ``ONDECK_SYNC_TOKEN`` into ``$ONDECK_HOME/sync.env``,
which ``sync_agent.py`` reads on its timer. The sync token (copied from the
cloud's Render env / Team Settings) *is* the credential — there is no separate
activation handshake.
"""

from __future__ import annotations

import logging
import os
import pwd
import subprocess
from pathlib import Path

log = logging.getLogger("netconfig")

WPA_CONF = Path("/etc/wpa_supplicant/wpa_supplicant.conf")
_WPA_HEADER = (
    "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
    "update_config=1\ncountry=US\n"
)

AP_IFACE = "wlan0"


# ── sync.env (cloud link) ──────────────────────────────────────────────────

def sync_env_path() -> Path:
    """Location of sync.env — under ONDECK_HOME, default ~<user>/ondeck."""
    home = os.environ.get("ONDECK_HOME")
    if home:
        return Path(home) / "sync.env"
    user = os.environ.get("ONDECK_USER")
    if user:
        try:
            return Path(pwd.getpwnam(user).pw_dir) / "ondeck" / "sync.env"
        except KeyError:
            pass
    return Path.home() / "ondeck" / "sync.env"


def read_sync_env() -> dict:
    env: dict[str, str] = {}
    path = sync_env_path()
    if path.exists():
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
        except OSError as exc:
            log.warning("Could not read %s: %s", path, exc)
    return env


def write_sync_env(cloud_url: str, sync_token: str) -> None:
    """Persist the cloud link, owned by the service user so sync_agent can read it."""
    path = sync_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"ONDECK_CLOUD_URL={cloud_url.rstrip('/')}\n"
        f"ONDECK_SYNC_TOKEN={sync_token}\n"
    )
    path.chmod(0o660)
    # boot_mode runs as root; hand the file back to the service user.
    user = os.environ.get("ONDECK_USER")
    if user:
        try:
            pw = pwd.getpwnam(user)
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except (KeyError, PermissionError) as exc:
            log.warning("Could not chown %s to %s: %s", path, user, exc)
    log.info("Wrote cloud link to %s", path)


def is_configured() -> bool:
    """A Pi is linked once it has a sync token."""
    return bool(read_sync_env().get("ONDECK_SYNC_TOKEN"))


def pending_link_path() -> Path:
    """Where the captive portal stashes a cloud_url + pairing code to redeem.

    The portal runs while the Pi is its own access point (no internet), so it
    cannot redeem the code itself. It writes the pending link here and reboots;
    ``boot_mode`` redeems it once the field Wi-Fi is up.
    """
    return sync_env_path().parent / "pending_link.json"


def write_pending_link(cloud_url: str, code: str) -> None:
    import json
    path = pending_link_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cloud_url": cloud_url.rstrip("/"), "code": code}))
    _chown_to_service_user(path)
    log.info("Saved pending cloud link (pairing code) to %s", path)


def read_pending_link() -> dict | None:
    import json
    path = pending_link_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if data.get("cloud_url") and data.get("code"):
            return data
    except Exception as exc:
        log.warning("Bad pending link file %s: %s", path, exc)
    return None


def clear_pending_link() -> None:
    path = pending_link_path()
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        log.warning("Could not remove %s: %s", path, exc)


def _chown_to_service_user(path: Path) -> None:
    """Hand a root-written file back to the service user (boot_mode runs as root)."""
    user = os.environ.get("ONDECK_USER")
    if user:
        try:
            pw = pwd.getpwnam(user)
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except (KeyError, PermissionError) as exc:
            log.warning("Could not chown %s to %s: %s", path, user, exc)


def redeem_pairing_code(cloud_url: str, code: str, hostname: str = "") -> str:
    """Trade a portal pairing code for this Pi's own sync token.

    POSTs to ``{cloud_url}/sync/pair``; returns the minted token. Raises on a
    bad/expired code or a network failure so the caller can show the error.
    Uses only the standard library — no extra Pi dependencies.
    """
    import json
    import urllib.error
    import urllib.request

    url = cloud_url.rstrip("/") + "/sync/pair"
    body = json.dumps({"code": code.strip(), "hostname": hostname}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Pairing code was rejected (invalid or expired).") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not reach the cloud: {exc}") from exc
    token = data.get("sync_token")
    if not data.get("ok") or not token:
        raise RuntimeError("Pairing failed — check the code and try again.")
    return token


# ── Wi-Fi (wpa_supplicant) ─────────────────────────────────────────────────

def _network_block(ssid: str, password: str) -> str:
    esc_ssid = ssid.replace('"', '\\"')
    if password:
        esc_pass = password.replace('"', '\\"')
        return ('network={\n'
                f'    ssid="{esc_ssid}"\n'
                f'    psk="{esc_pass}"\n'
                '    key_mgmt=WPA-PSK\n'
                '}\n')
    return ('network={\n'
            f'    ssid="{esc_ssid}"\n'
            '    key_mgmt=NONE\n'
            '}\n')


def write_wifi(ssid: str, password: str) -> None:
    """Replace wpa_supplicant.conf with a single network (open if no password)."""
    WPA_CONF.parent.mkdir(parents=True, exist_ok=True)
    WPA_CONF.write_text(_WPA_HEADER + "\n" + _network_block(ssid, password))
    WPA_CONF.chmod(0o640)
    log.info("Saved Wi-Fi for SSID '%s'", ssid)


def append_wifi(ssid: str, password: str) -> None:
    """Add a network block, keeping any already-saved networks."""
    if WPA_CONF.exists():
        current = WPA_CONF.read_text().rstrip() + "\n"
    else:
        WPA_CONF.parent.mkdir(parents=True, exist_ok=True)
        current = _WPA_HEADER
    WPA_CONF.write_text(current + "\n" + _network_block(ssid, password))
    WPA_CONF.chmod(0o640)
    log.info("Appended Wi-Fi network '%s'", ssid)


def list_saved_networks() -> list[str]:
    if not WPA_CONF.exists():
        return []
    ssids = []
    for line in WPA_CONF.read_text().splitlines():
        line = line.strip()
        if line.startswith("ssid="):
            ssid = line[5:].strip().strip('"')
            if ssid:
                ssids.append(ssid)
    return ssids


def scan_networks() -> list[str]:
    """Visible SSIDs (best-effort; empty list if the scan fails)."""
    try:
        subprocess.run(["ip", "link", "set", AP_IFACE, "up"],
                       check=False, capture_output=True)
        result = subprocess.run(["iwlist", AP_IFACE, "scan"],
                                capture_output=True, text=True, timeout=15)
        ssids: list[str] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("ESSID:"):
                ssid = line[7:].strip().strip('"')
                if ssid and ssid not in ssids:
                    ssids.append(ssid)
        return ssids
    except Exception:
        return []
