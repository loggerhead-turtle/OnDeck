"""Bluetooth speaker management for the OnDeck Audio Pi.

Wraps ``bluetoothctl`` (BlueZ) to pair, connect, and remember an A2DP speaker
(e.g. a Bose S1 Pro+), and tells the audio layer which PipeWire/PulseAudio sink
to play through. State (the remembered "preferred" speaker + an auto-connect
flag) lives in ``$ONDECK_HOME/bluetooth.json`` — entirely local to the Audio Pi,
so the field workflow needs no cloud round-trip:

  1. Pair the speaker once (locally, via the portal's Bluetooth page).
  2. Mark it the preferred speaker with auto-connect on.
  3. On game day, power the speaker on near the Pi — a background loop
     reconnects within a few seconds, fully offline.

Every shell-out is defensive: timeouts, no exceptions escape, so a flaky radio
can never take down the audio server.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from config_manager import ONDECK_HOME

log = logging.getLogger("bluetooth")

_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
# "Device AA:BB:CC:DD:EE:FF Some Speaker Name"
_DEVICE_LINE = re.compile(r"Device\s+(" + _MAC_RE.pattern[1:-1] + r")\s+(.*)")

STATE_PATH = ONDECK_HOME / "bluetooth.json"

_DEFAULT_STATE = {
    "preferred_mac": None,
    "preferred_name": "",
    "auto_connect": True,
}


class BluetoothManager:
    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._state = self._load_state()
        # The resolved Pulse/PipeWire sink for the connected speaker, refreshed
        # by the auto-connect loop and on connect/disconnect so playback never
        # has to shell out to pactl on the hot path.
        self._cached_sink: str | None = None

    # -- persisted preferred-speaker state --------------------------------

    def _load_state(self) -> dict:
        state = dict(_DEFAULT_STATE)
        try:
            if self.state_path.exists():
                state.update(json.loads(self.state_path.read_text()))
        except Exception as exc:
            log.warning("Could not read %s: %s", self.state_path, exc)
        return state

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self._state, indent=2))
        except OSError as exc:
            log.warning("Could not write %s: %s", self.state_path, exc)

    def get_preferred(self) -> dict:
        with self._lock:
            return dict(self._state)

    def set_preferred(self, mac: str | None, name: str = "",
                      auto_connect: bool = True) -> None:
        with self._lock:
            self._state["preferred_mac"] = (mac or "").upper() or None
            self._state["preferred_name"] = name or ""
            self._state["auto_connect"] = bool(auto_connect)
            self._save_state()

    # -- bluetoothctl plumbing -------------------------------------------

    def _ctl(self, *args: str, timeout: int = 20) -> tuple[int, str]:
        try:
            r = subprocess.run(
                ["bluetoothctl", *args],
                capture_output=True, text=True, check=False, timeout=timeout,
            )
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except FileNotFoundError:
            log.error("bluetoothctl not found — is bluez installed?")
            return 127, "bluetoothctl not installed"
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        except Exception as exc:  # never let the radio crash the caller
            return 1, str(exc)

    @staticmethod
    def _ok(rc: int, out: str) -> bool:
        low = out.lower()
        return rc == 0 or "successful" in low or "changed" in low or "already" in low

    @staticmethod
    def _parse_devices(out: str) -> list[dict]:
        devices = []
        for line in out.splitlines():
            m = _DEVICE_LINE.search(line.strip())
            if m:
                devices.append({"mac": m.group(1).upper(),
                                "name": m.group(2).strip() or m.group(1)})
        return devices

    def _info(self, mac: str) -> dict:
        _, out = self._ctl("info", mac, timeout=8)
        low = out.lower()
        return {
            "connected": "connected: yes" in low,
            "paired": "paired: yes" in low,
            "trusted": "trusted: yes" in low,
        }

    # -- operations -------------------------------------------------------

    def _unblock_radio(self) -> None:
        """Clear an rfkill soft-block on the Bluetooth radio.

        A soft-block is common on a Raspberry Pi — and the OnDeck setup hotspot
        toggles the radios — which makes ``bluetoothctl power on`` fail silently,
        leaving the speaker page stuck on "radio off". ``rfkill`` needs root, so
        we try a passwordless sudo (installed by install.sh) first, then a plain
        call, and ignore any failure so a locked-down box never crashes here.
        """
        for cmd in (["sudo", "-n", "rfkill", "unblock", "bluetooth"],
                    ["rfkill", "unblock", "bluetooth"]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   check=False, timeout=8)
                if r.returncode == 0:
                    return
            except Exception:
                continue

    def power_on(self) -> None:
        self._unblock_radio()
        self._ctl("power", "on", timeout=8)

    def powered(self) -> bool:
        _, out = self._ctl("show", timeout=8)
        return "powered: yes" in out.lower()

    def scan(self, seconds: int = 8) -> list[dict]:
        """Discover nearby devices. Returns [{mac, name}]."""
        self.power_on()
        # `--timeout N scan on` scans then returns; list devices afterwards.
        self._ctl("--timeout", str(seconds), "scan", "on", timeout=seconds + 5)
        _, out = self._ctl("devices", timeout=8)
        return self._parse_devices(out)

    def known_devices(self) -> list[dict]:
        """Remembered (paired) devices, enriched with live flags."""
        _, paired_out = self._ctl("paired-devices", timeout=8)
        devices = self._parse_devices(paired_out)
        if not devices:
            # Newer bluez prefers `devices Paired`.
            _, out = self._ctl("devices", "Paired", timeout=8)
            devices = self._parse_devices(out)
        for d in devices:
            d.update(self._info(d["mac"]))
        return devices

    def pair(self, mac: str) -> bool:
        mac = mac.upper()
        self.power_on()
        self._ctl("pairable", "on", timeout=8)
        ok = self._ok(*self._ctl("pair", mac, timeout=25))
        # Trust so BlueZ allows the speaker to reconnect on its own later.
        self._ctl("trust", mac, timeout=10)
        return ok

    def connect(self, mac: str) -> bool:
        mac = mac.upper()
        self.power_on()
        self._ctl("trust", mac, timeout=10)
        ok = self._ok(*self._ctl("connect", mac, timeout=25))
        if ok:
            self.refresh_sink()
        return ok

    def disconnect(self, mac: str) -> bool:
        ok = self._ok(*self._ctl("disconnect", mac.upper(), timeout=15))
        self._cached_sink = None
        return ok

    def forget(self, mac: str) -> bool:
        mac = mac.upper()
        ok = self._ok(*self._ctl("remove", mac, timeout=15))
        self._cached_sink = None
        with self._lock:
            if self._state.get("preferred_mac") == mac:
                self._state.update(preferred_mac=None, preferred_name="")
                self._save_state()
        return ok

    def connected(self) -> dict | None:
        """The currently connected speaker as {mac, name}, or None."""
        for d in self.known_devices():
            if d.get("connected"):
                return {"mac": d["mac"], "name": d["name"]}
        return None

    def status(self) -> dict:
        conn = self.connected()
        pref = self.get_preferred()
        return {
            "powered": self.powered(),
            "connected_mac": conn["mac"] if conn else None,
            "connected_name": conn["name"] if conn else "",
            "preferred_mac": pref.get("preferred_mac"),
            "preferred_name": pref.get("preferred_name", ""),
            "auto_connect": pref.get("auto_connect", True),
            "devices": self.known_devices(),
        }

    # -- audio routing ----------------------------------------------------

    def active_sink(self) -> str | None:
        """The PipeWire/Pulse sink name for the connected speaker, or None.

        BlueZ A2DP shows up via pipewire-pulse as e.g.
        ``bluez_output.AA_BB_CC_DD_EE_FF.1``. We resolve the live name from
        ``pactl`` rather than guessing the suffix (it differs across stacks).
        """
        conn = self.connected()
        if not conn:
            return None
        needle = conn["mac"].replace(":", "_").upper()
        try:
            r = subprocess.run(["pactl", "list", "short", "sinks"],
                               capture_output=True, text=True,
                               check=False, timeout=6)
        except Exception:
            return None
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and "bluez" in parts[1].lower() \
                    and needle in parts[1].upper():
                return parts[1]
        return None

    def refresh_sink(self) -> str | None:
        """Re-resolve and cache the connected speaker's sink."""
        self._cached_sink = self.active_sink()
        return self._cached_sink

    def current_sink(self) -> str | None:
        """The cached sink for the connected speaker (no shell-out)."""
        return self._cached_sink

    # -- auto-connect loop ------------------------------------------------

    def reconcile_once(self) -> None:
        """Ensure the preferred speaker is connected when auto-connect is on."""
        with self._lock:
            mac = self._state.get("preferred_mac")
            auto = self._state.get("auto_connect", True)
        if not (auto and mac):
            return
        self.power_on()
        if self._info(mac).get("connected"):
            if not self._cached_sink:
                self.refresh_sink()
            return
        log.info("Auto-connecting preferred speaker %s", mac)
        if self.connect(mac):
            log.info("Connected to %s", mac)

    def run_autoconnect(self, interval: int = 20) -> None:
        log.info("Bluetooth auto-connect loop started (every %ss)", interval)
        while not self._stop.wait(2):
            try:
                self.reconcile_once()
            except Exception as exc:
                log.warning("Auto-connect reconcile failed: %s", exc)
            self._stop.wait(interval)

    def start_autoconnect(self, interval: int = 20) -> None:
        threading.Thread(target=self.run_autoconnect, args=(interval,),
                         daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
