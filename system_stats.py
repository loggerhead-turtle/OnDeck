"""System resource stats for OnDeck diagnostics.

Read by the portal Resources page, the Stream Deck "stats" button, the Audio
Pi's /stats endpoint, and the sync ping (so the cloud dashboard can show field
device health). Everything is best-effort and dependency-free: a metric that
can't be read on this platform is simply reported as None — a broken sensor
must never take down playback.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from config_manager import ONDECK_HOME

# CPU usage is a delta between two /proc/stat samples; remember the last one.
_cpu_prev: dict = {"total": 0, "idle": 0}
_cpu_lock = threading.Lock()

# gather() is called from HTTP handlers and the deck render loop; cache briefly
# so a 1-second poll storm doesn't turn into a subprocess storm.
_cache: dict = {"ts": 0.0, "data": None}
_CACHE_S = 2.0


def _read_first_line(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.readline().strip()
    except OSError:
        return None


def cpu_temp_c() -> float | None:
    """SoC temperature in °C (Raspberry Pi thermal zone, then vcgencmd)."""
    raw = _read_first_line("/sys/class/thermal/thermal_zone0/temp")
    if raw:
        try:
            return round(int(raw) / 1000.0, 1)
        except ValueError:
            pass
    try:
        out = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                             text=True, timeout=3).stdout
        # temp=48.3'C
        return round(float(out.split("=")[1].split("'")[0]), 1)
    except Exception:
        return None


def cpu_percent() -> float | None:
    """CPU busy % since the previous call (first call returns since-boot)."""
    line = _read_first_line("/proc/stat")
    if not line or not line.startswith("cpu "):
        return None
    fields = [int(x) for x in line.split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
    total = sum(fields)
    with _cpu_lock:
        dt = total - _cpu_prev["total"]
        di = idle - _cpu_prev["idle"]
        _cpu_prev["total"], _cpu_prev["idle"] = total, idle
    if dt <= 0:
        return None
    return round(100.0 * (dt - di) / dt, 1)


def memory() -> dict | None:
    """{total_mb, available_mb, used_pct} from /proc/meminfo."""
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0])  # kB
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return {
            "total_mb": round(total / 1024),
            "available_mb": round(avail / 1024),
            "used_pct": round(100.0 * (total - avail) / total, 1) if total else None,
        }
    except (OSError, KeyError, ValueError, IndexError):
        return None


def disk() -> dict | None:
    """{total_gb, free_gb, used_pct} for the volume holding ONDECK_HOME."""
    try:
        target = ONDECK_HOME if ONDECK_HOME.exists() else Path.home()
        du = shutil.disk_usage(target)
        return {
            "total_gb": round(du.total / 1e9, 1),
            "free_gb": round(du.free / 1e9, 1),
            "used_pct": round(100.0 * du.used / du.total, 1) if du.total else None,
        }
    except OSError:
        return None


def throttled() -> dict | None:
    """Decoded Raspberry Pi `vcgencmd get_throttled` flags (power health)."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        bits = int(out.split("=")[1], 16)
    except Exception:
        return None
    return {
        "raw": f"0x{bits:x}",
        "undervoltage_now": bool(bits & 0x1),
        "freq_capped_now": bool(bits & 0x2),
        "throttled_now": bool(bits & 0x4),
        "undervoltage_seen": bool(bits & 0x10000),
        "freq_capped_seen": bool(bits & 0x20000),
        "throttled_seen": bool(bits & 0x40000),
    }


def wifi() -> dict | None:
    """{ssid, signal_dbm} for wlan0, via `iw` (present on the Pi images)."""
    try:
        out = subprocess.run(["iw", "dev", "wlan0", "link"], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:
        return None
    if "Connected to" not in out:
        return None
    ssid = signal = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            ssid = line.split(":", 1)[1].strip()
        elif line.startswith("signal:"):
            try:
                signal = int(line.split(":", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
    return {"ssid": ssid, "signal_dbm": signal}


def uptime_s() -> int | None:
    raw = _read_first_line("/proc/uptime")
    try:
        return int(float(raw.split()[0])) if raw else None
    except (ValueError, IndexError):
        return None


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""


def gather(brief: bool = False) -> dict:
    """One snapshot of everything, cached for a couple of seconds.

    ``brief`` returns just the cheap fields (for the sync ping payload).
    """
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < _CACHE_S:
        data = _cache["data"]
    else:
        try:
            load = os.getloadavg()
        except OSError:
            load = None
        data = {
            "hostname": socket.gethostname(),
            "ip": local_ip(),
            "cpu_temp_c": cpu_temp_c(),
            "cpu_percent": cpu_percent(),
            "load_avg": [round(x, 2) for x in load] if load else None,
            "memory": memory(),
            "disk": disk(),
            "throttled": throttled(),
            "wifi": wifi(),
            "uptime_s": uptime_s(),
        }
        _cache["ts"], _cache["data"] = now, data
    if brief:
        mem = data.get("memory") or {}
        return {
            "cpu_temp_c": data.get("cpu_temp_c"),
            "cpu_percent": data.get("cpu_percent"),
            "mem_used_pct": mem.get("used_pct"),
            "disk_used_pct": (data.get("disk") or {}).get("used_pct"),
            "uptime_s": data.get("uptime_s"),
        }
    return data


if __name__ == "__main__":
    import json
    gather()          # prime the CPU delta
    time.sleep(0.5)
    print(json.dumps(gather(), indent=2))
