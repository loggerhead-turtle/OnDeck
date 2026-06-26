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
    scan_networks,
    write_sync_env,
)

log = logging.getLogger("pi.web")

_ADD_WIFI = Path(__file__).resolve().parent / "add_wifi.py"

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

    @app.get("/cloud-settings")
    def pi_cloud_settings():
        env = read_sync_env()
        msg = request.args.get("ok", "")
        body = (
            (f"<div class='card'><div class='ok'>{msg}</div></div>" if msg else "")
            + "<div class='card'><h2>Cloud link</h2>"
            + "<div class='small'>From the cloud portal's Settings page.</div>"
            + "<form method='post'><label>Cloud URL</label>"
            + f"<input type='url' name='cloud_url' value=\"{env.get('ONDECK_CLOUD_URL','')}\" "
              "placeholder='https://your-app.onrender.com' required>"
            + "<label>Sync Token</label>"
            + f"<input name='sync_token' value=\"{env.get('ONDECK_SYNC_TOKEN','')}\" "
              "placeholder='Sync token' autocomplete='off' required>"
            + "<button class='btn' type='submit'>Save</button></form></div>"
            + "<a href='/'>&#8592; Back</a>"
        )
        return _shell("Cloud Settings", body)

    @app.post("/cloud-settings")
    def pi_cloud_settings_post():
        cloud_url = request.form.get("cloud_url", "").strip()
        sync_token = request.form.get("sync_token", "").strip()
        if not cloud_url or not sync_token:
            return redirect(url_for("pi_cloud_settings", ok="Both fields are required."))
        write_sync_env(cloud_url, sync_token)
        return redirect(url_for("pi_cloud_settings",
                                ok="Saved. The Pi will sync on the next cycle."))
