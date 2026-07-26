"""Coach Pi-only web routes: Wi-Fi management and cloud-link settings.

Registered onto the portal in ``main.py`` (never on the cloud app), these let a
logged-in coach manage networks and the cloud link from the normal portal —
the same things the first-boot captive portal does, but after setup.

Wi-Fi changes go through ``pi/add_wifi.py`` via sudo (the installer adds a
NOPASSWD sudoers rule). The cloud link is written to sync.env via netconfig.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netconfig import (  # noqa: E402
    list_saved_networks,
    read_sync_env,
    redeem_pairing_code,
    scan_networks,
    write_sync_env,
)

log = logging.getLogger("pi.web")

_ADD_WIFI = Path(__file__).resolve().parent / "add_wifi.py"
_REPO_DIR = Path(__file__).resolve().parent.parent


def _read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _status_rows(env):
    """(label, value) pairs for the status page — everything a coach or a
    support call would ask for, in one place."""
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        temp = f"{int(temp) / 1000:.1f} °C"
    except (TypeError, ValueError):
        temp = "—"
    load = _read("/proc/loadavg").split(" ")[:3]
    up = _read("/proc/uptime").split(" ")[0]
    try:
        mins = int(float(up) // 60)
        uptime = f"{mins // 1440}d {mins % 1440 // 60}h {mins % 60}m"
    except ValueError:
        uptime = "—"
    mem = ""
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemAvailable:"):
            mem = f"{int(line.split()[1]) / 1024:.0f} MB free"
    disk = _sh(["df", "-h", "--output=avail", str(Path.home())]).splitlines()
    return [
        ("Hostname", _read("/etc/hostname", "—")),
        ("IP address", _sh(["hostname", "-I"]) or "not on a network"),
        ("Temperature", temp),
        ("Load (1/5/15m)", " ".join(load) or "—"),
        ("Memory", mem or "—"),
        ("Disk free", disk[-1].strip() if len(disk) > 1 else "—"),
        ("Uptime", uptime),
        ("Cloud", env.get("ONDECK_CLOUD_URL") or "not linked"),
    ]


def _software_summary():
    head = _sh(["git", "-C", str(_REPO_DIR), "log", "-1",
                "--format=%h %s (%cr)"])
    return head or "version unknown (not a git checkout)"


def _run_update():
    """git pull + restart the OnDeck services. Deliberately manual — never
    automatic, and never mid-game."""
    if not (_REPO_DIR / ".git").exists():
        return False, "Not a git checkout — update it the way it was installed."
    out = _sh(["git", "-C", str(_REPO_DIR), "pull", "--ff-only"], timeout=120)
    if not out:
        return False, "Update failed — could not reach GitHub."
    for unit in ("ondeck-coach", "ondeck-audio"):
        if _sh(["systemctl", "is-enabled", unit]) in ("enabled", "static"):
            subprocess.run(["sudo", "systemctl", "restart", unit],
                           capture_output=True, timeout=60)
    return True, f"Updated: {out.splitlines()[-1][:120]}"

_PAGE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OnDeck — {{ title }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0b1622;color:#eee;font-family:system-ui,sans-serif;
     min-height:100vh;display:flex;flex-direction:column;align-items:center;
     padding:1.5rem}
.logo{font-size:1.4rem;font-weight:700;color:#3aa0ff;margin-bottom:1.5rem}
.card{background:#13202e;border:1px solid #24384c;border-radius:12px;
      padding:1.25rem;width:100%;max-width:420px;margin-bottom:1rem}
h2{font-size:1rem;margin-bottom:1rem;color:#cdd}
label{display:block;font-size:.75rem;color:#8ab;margin:.3rem 0}
input,select{display:block;width:100%;padding:.6rem .8rem;background:#0b1622;
  border:1px solid #2c4660;border-radius:6px;color:#eee;font-size:1rem;margin-bottom:.8rem}
.btn{display:block;width:100%;padding:.7rem;background:#3aa0ff;color:#012;border:none;
     border-radius:8px;font-size:1rem;font-weight:700;cursor:pointer}
.ok{background:#0d2b1a;border:1px solid #2e7d4f;padding:.6rem;border-radius:6px;
    color:#a5d6b8;font-size:.85rem;margin-bottom:.8rem}
.err{background:#2a0f0f;border:1px solid #c62828;padding:.6rem;border-radius:6px;
     color:#ef9a9a;font-size:.85rem;margin-bottom:.8rem}
ul{list-style:none}li{font-size:.85rem;color:#8ab;padding:.25rem 0;
   display:flex;justify-content:space-between;border-bottom:1px solid #1d2c3c}
.small{font-size:.75rem;color:#668;margin-bottom:.8rem}
a{color:#3aa0ff;font-size:.85rem;text-decoration:none;display:block;margin-top:1rem}
</style></head><body><div class="logo">OnDeck</div>{{ body }}</body></html>"""


def register(app) -> None:
    from flask import request, redirect, render_template_string, url_for

    def _shell(title, body):
        from markupsafe import Markup
        return render_template_string(_PAGE, title=title, body=Markup(body))

    def _wpa_reconfigure():
        for path in ("/usr/sbin/wpa_cli", "/sbin/wpa_cli"):
            try:
                subprocess.run(["sudo", path, "-i", "wlan0", "reconfigure"],
                               check=False, capture_output=True, timeout=10)
                return
            except Exception:
                continue

    @app.get("/wifi")
    def pi_wifi():
        saved = list_saved_networks()
        available = scan_networks()
        msg = request.args.get("ok", "")
        err = request.args.get("err", "")
        rows = "".join(f"<li><span>&#10003; {s}</span>"
                       f"<form method='post' style='margin:0'>"
                       f"<input type='hidden' name='ssid' value='{s}'>"
                       f"<input type='hidden' name='action' value='forget'>"
                       f"<button class='btn' style='width:auto;padding:.2rem .6rem;"
                       f"background:#3a1a1a;color:#ef9a9a'>Forget</button>"
                       f"</form></li>" for s in saved) or "<li>None saved yet.</li>"
        opts = "".join(f"<option>{n}</option>" for n in available)
        body = (
            (f"<div class='card'><div class='ok'>{msg}</div></div>" if msg else "")
            + (f"<div class='card'><div class='err'>{err}</div></div>" if err else "")
            + f"<div class='card'><h2>Saved networks</h2><ul>{rows}</ul></div>"
            + "<div class='card'><h2>Add a network</h2>"
            + "<form method='post'><label>Wi-Fi Network</label>"
            + (f"<input list='nets' name='ssid' placeholder='Network name' required>"
               f"<datalist id='nets'>{opts}</datalist>" if opts
               else "<input name='ssid' placeholder='Network name' required>")
            + "<label>Password</label>"
            + "<input type='password' name='password' placeholder='Password' autocomplete='off'>"
            + "<button class='btn' type='submit'>Save Network</button></form></div>"
            + "<a href='/'>&#8592; Back</a>"
        )
        return _shell("Wi-Fi", body)

    @app.post("/wifi")
    def pi_wifi_post():
        import json
        ssid = request.form.get("ssid", "").strip()
        password = request.form.get("password", "")
        action = request.form.get("action", "add").strip().lower()
        if not ssid:
            return redirect(url_for("pi_wifi", err="Enter a network name."))
        payload = json.dumps({"ssid": ssid, "password": password,
                              "action": "remove" if action == "forget" else "add"})
        try:
            result = subprocess.run(
                ["sudo", sys.executable, str(_ADD_WIFI)],
                input=payload, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "unknown error")
            _wpa_reconfigure()
        except Exception as exc:
            log.error("Wi-Fi %s failed: %s", action, exc)
            return redirect(url_for("pi_wifi", err=f"Could not update network: {exc}"))
        verb = "forgotten" if action == "forget" else "saved"
        return redirect(url_for("pi_wifi", ok=f'"{ssid}" {verb}.'))

    @app.get("/status")
    def pi_status():
        """What the box is doing right now, with the two buttons you'd want
        while standing in front of it. Registered on both Pis."""
        import sync_now
        st = sync_now.status()
        env = read_sync_env()
        rows = "".join(
            f"<div class='row'><span class='k'>{k}</span>"
            f"<span class='v'>{v}</span></div>"
            for k, v in _status_rows(env))
        busy = st.get("running")
        body = (
            (f"<div class='card'><div class='ok'>{request.args.get('ok')}</div>"
             "</div>" if request.args.get("ok") else "")
            + f"<div class='card'><h2>This Pi</h2>{rows}</div>"
            + "<div class='card'><h2>Sync</h2>"
            + f"<div class='small'>{sync_now.summary()}</div>"
            + (f"<div class='small'>{st.get('detail')}</div>"
               if st.get("detail") else "")
            + "<form method='post' action='/sync-now' style='margin-top:.6rem'>"
            + f"<button class='btn' {'disabled' if busy else ''}>"
            + ("Syncing…" if busy else "Sync now") + "</button></form>"
            + "<div class='small' style='margin-top:.4rem'>Pulls the lineup, "
              "songs and settings from the cloud.</div></div>"
            + "<div class='card'><h2>Software</h2>"
            + f"<div class='small'>{_software_summary()}</div>"
            + "<form method='post' action='/update' style='margin-top:.6rem' "
              "onsubmit=\"return confirm('Update this Pi\\'s software and "
              "restart its services? Do not do this during a game.')\">"
              "<button class='btn'>Update software</button></form>"
            + "<div class='small' style='margin-top:.4rem'>Pulls the latest "
              "code and restarts. Takes a minute; the deck goes dark while it "
              "does.</div></div>"
            + "<div class='card'><h2>More</h2>"
              "<a class='btn' href='/wifi'>Wi-Fi</a> "
              "<a class='btn' href='/cloud-settings'>Cloud link</a></div>"
            + "<a href='/'>&#8592; Back</a>"
            + ("<meta http-equiv='refresh' content='5'>" if busy else "")
        )
        return _shell("Status", body)

    @app.post("/update")
    def pi_update():
        import sync_now
        ok, detail = _run_update()
        if ok:
            sync_now.start()
        return redirect(url_for("pi_status", ok=detail))

    @app.get("/sync-now")
    def pi_sync_now():
        """Pull config, lineup and any new audio from the cloud right now,
        instead of waiting out the 5-minute timer. Lives here so BOTH Pis
        get it — this module is registered on the deck portal and on the
        Audio Pi's music server."""
        import sync_now
        st = sync_now.status()
        env = read_sync_env()
        linked = bool(env.get("ONDECK_SYNC_TOKEN"))
        msg = request.args.get("ok", "")
        tone = "ok" if st.get("ok") or st.get("running") else "err"
        detail = st.get("detail") or ""
        body = (
            (f"<div class='card'><div class='ok'>{msg}</div></div>" if msg else "")
            + "<div class='card'><h2>Sync now</h2>"
            + ("<div class='small'>Pulls the latest lineup, songs and settings "
               "from the cloud — use it after a coach changes a walk-up song "
               "or makes a substitution.</div>"
               if linked else
               "<div class='err'>Not linked to the cloud yet — set that up "
               "under <a href='/cloud-settings'>Cloud Settings</a> first.</div>")
            + (f"<div class='{tone}' style='margin-top:.6rem'>"
               f"{sync_now.summary()}</div>")
            + (f"<div class='small'>{detail}</div>" if detail else "")
            + ("<form method='post' style='margin-top:.8rem'>"
               "<button class='btn' type='submit'>Sync now</button></form>"
               if linked else "")
            + "</div><a href='/'>&#8592; Back</a>"
            # While a sync is running, refresh so the result appears without
            # the user wondering whether the tap registered.
            + ("<meta http-equiv='refresh' content='3'>"
               if st.get("running") else "")
        )
        return _shell("Sync", body)

    @app.post("/sync-now")
    def pi_sync_now_post():
        import sync_now
        started = sync_now.start()
        return redirect(url_for(
            "pi_sync_now",
            ok="Sync started." if started else "A sync is already running."))

    @app.get("/cloud-settings")
    def pi_cloud_settings():
        env = read_sync_env()
        msg = request.args.get("ok", "")
        err = request.args.get("err", "")
        linked = bool(env.get("ONDECK_SYNC_TOKEN"))
        status = (f"<div class='ok'>&#10003; Linked to {env.get('ONDECK_CLOUD_URL','the cloud')}</div>"
                  if linked else
                  "<div class='err'>Not linked yet — enter your Cloud URL and pairing code below.</div>")
        body = (
            (f"<div class='card'><div class='ok'>{msg}</div></div>" if msg else "")
            + (f"<div class='card'><div class='err'>{err}</div></div>" if err else "")
            + f"<div class='card'>{status}</div>"
            + "<div class='card'><h2>Cloud link</h2>"
            + "<div class='small'>Generate a pairing code in the cloud portal under "
              "<b>Devices</b>, then enter it here. (Or paste a raw sync token instead.)</div>"
            + "<form method='post'><label>Cloud URL</label>"
            + f"<input type='url' name='cloud_url' value=\"{env.get('ONDECK_CLOUD_URL','')}\" "
              "placeholder='https://playsigns.net/ondeck' required>"
            + "<label>Pairing Code</label>"
            + "<input name='pairing_code' placeholder='From the Devices page' autocomplete='off'>"
            + "<label>Sync Token (optional)</label>"
            + f"<input name='sync_token' value=\"{env.get('ONDECK_SYNC_TOKEN','')}\" "
              "placeholder='Paste a raw token instead' autocomplete='off'>"
            + "<button class='btn' type='submit'>Save</button></form></div>"
            + "<a href='/'>&#8592; Back</a>"
        )
        return _shell("Cloud Settings", body)

    @app.post("/cloud-settings")
    def pi_cloud_settings_post():
        import socket
        cloud_url = request.form.get("cloud_url", "").strip()
        pairing_code = request.form.get("pairing_code", "").strip()
        sync_token = request.form.get("sync_token", "").strip()
        if not cloud_url or (not pairing_code and not sync_token):
            return redirect(url_for("pi_cloud_settings",
                                    err="Enter a Cloud URL and either a pairing code or a token."))
        # Post-boot the Pi is online, so a pairing code can be redeemed inline.
        if pairing_code:
            try:
                sync_token = redeem_pairing_code(cloud_url, pairing_code, socket.gethostname())
            except Exception as exc:
                return redirect(url_for("pi_cloud_settings", err=str(exc)))
        write_sync_env(cloud_url, sync_token)
        return redirect(url_for("pi_cloud_settings",
                                ok="Linked. The Pi will sync on the next cycle."))
