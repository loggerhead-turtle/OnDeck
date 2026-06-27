#!/usr/bin/env python3
"""OnDeck Pi sync agent.

Pulls config and audio files down from the cloud instance, then reports
this Pi's local IP so the cloud dashboard can show it.

Run manually:
    python sync_agent.py

Run as a systemd timer every 5 minutes (see install.sh).

Required environment variables (put in ~/ondeck/sync.env):
    ONDECK_CLOUD_URL    https://your-app.onrender.com
    ONDECK_SYNC_TOKEN   shared secret — must match cloud ONDECK_SYNC_TOKEN

Optional:
    ONDECK_HOME         override data dir (default ~/ondeck)
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from config_manager import CONFIG_PATH, MUSIC_DIR, ONDECK_HOME

CLOUD_URL   = os.environ.get("ONDECK_CLOUD_URL", "").rstrip("/")
SYNC_TOKEN  = os.environ.get("ONDECK_SYNC_TOKEN", "")
REQUEST_TIMEOUT = 30  # seconds


def _headers() -> dict:
    return {"Authorization": f"Bearer {SYNC_TOKEN}"} if SYNC_TOKEN else {}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "unknown"


def sync() -> bool:
    if not CLOUD_URL:
        # Not linked yet — a no-op, not a failure (otherwise the 5-minute timer
        # reports a failed job on every tick until the device is linked).
        print("Not linked yet (no ONDECK_CLOUD_URL) — nothing to sync.", flush=True)
        return True

    print(f"Syncing from {CLOUD_URL} …", flush=True)

    # ------------------------------------------------------------------ #
    # 1. Pull config JSON and replace the local copy atomically.          #
    # ------------------------------------------------------------------ #
    r = requests.get(f"{CLOUD_URL}/sync/config",
                     headers=_headers(), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    cloud_data = r.json()

    ONDECK_HOME.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ONDECK_HOME), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(cloud_data, fh, indent=2)
        os.replace(tmp, CONFIG_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("  Config synced.", flush=True)

    # ------------------------------------------------------------------ #
    # 1b. Pull staff accounts so the same login works on the field Pi.    #
    #     (Player accounts already ride along inside config.json.)        #
    # ------------------------------------------------------------------ #
    try:
        ra = requests.get(f"{CLOUD_URL}/sync/auth",
                          headers=_headers(), timeout=REQUEST_TIMEOUT)
        if ra.status_code == 200:
            users = ra.json().get("users")
            # Only overwrite when the cloud actually has staff users, so a
            # transient empty response can't lock the Pi out.
            if isinstance(users, list) and users:
                auth_path = ONDECK_HOME / "auth.json"
                fd, tmp = tempfile.mkstemp(dir=str(ONDECK_HOME), suffix=".tmp")
                try:
                    with os.fdopen(fd, "w") as fh:
                        json.dump({"users": users}, fh, indent=2)
                    os.replace(tmp, auth_path)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                print(f"  Auth synced ({len(users)} account(s)).", flush=True)
    except requests.exceptions.RequestException as exc:
        print(f"  Auth sync skipped: {exc}", flush=True)

    # ------------------------------------------------------------------ #
    # 2. Sync audio files — download anything new or changed.             #
    # ------------------------------------------------------------------ #
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)

    r2 = requests.get(f"{CLOUD_URL}/sync/files",
                      headers=_headers(), timeout=REQUEST_TIMEOUT)
    r2.raise_for_status()
    cloud_files: list[dict] = r2.json().get("files", [])

    downloaded = 0
    for meta in cloud_files:
        name  = Path(meta["filename"]).name  # safety: strip any path
        local = MUSIC_DIR / name
        if local.exists() and _md5(local) == meta["md5"]:
            continue
        size_kb = meta["size"] // 1024
        print(f"  Downloading {name} ({size_kb} KB) …", flush=True)
        r3 = requests.get(f"{CLOUD_URL}/sync/files/{name}",
                          headers=_headers(), stream=True, timeout=120)
        r3.raise_for_status()
        tmp_path = local.with_suffix(".tmp")
        with open(str(tmp_path), "wb") as fh:
            for chunk in r3.iter_content(chunk_size=65536):
                fh.write(chunk)
        os.replace(str(tmp_path), str(local))
        downloaded += 1

    print(f"  Files: {len(cloud_files)} total, {downloaded} downloaded.", flush=True)

    # ------------------------------------------------------------------ #
    # 3. Ping — report our IP to the cloud dashboard.                     #
    # ------------------------------------------------------------------ #
    hostname = socket.gethostname()
    requests.post(
        f"{CLOUD_URL}/sync/ping",
        headers=_headers(),
        json={
            "pi_id":     hostname,
            "hostname":  hostname,
            "ip":        _local_ip(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        timeout=10,
    )
    print(f"  Pinged as {hostname}.", flush=True)
    return True


if __name__ == "__main__":
    try:
        ok = sync()
        sys.exit(0 if ok else 1)
    except requests.exceptions.ConnectionError as e:
        # No network — happens at the field. Not an error.
        print(f"No network ({e}) — sync skipped.", flush=True)
        sys.exit(0)
    except requests.exceptions.HTTPError as e:
        print(f"HTTP error: {e}", flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"Sync failed: {e}", flush=True)
        sys.exit(1)
