#!/usr/bin/env python3
"""OnDeck Wi-Fi + cloud-link captive portal.

Runs when the Pi is unlinked (or when forced). Brings up a Wi-Fi hotspot named
``OnDeck-Setup``, serves a phone-friendly page on port 80, and guides the coach
through:
  1. Picking their Wi-Fi network + password
  2. Pasting the Cloud URL and Sync Token (from the cloud's Team Settings)

On submit the Pi saves the Wi-Fi credentials and the cloud link, then reboots;
``boot_mode.py`` brings it up linked on the next boot. With ``--wifi-only`` (an
already-linked Pi that lost its network) the page just adds a Wi-Fi network.

Must run as root (needs hostapd, dnsmasq, wpa_supplicant, port 80).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# Importable whether launched as `pi/setup_server.py` or from within pi/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from netconfig import (
    append_wifi,
    list_saved_networks,
    scan_networks,
    write_pending_link,
    write_wifi,
    AP_IFACE,
)

log = logging.getLogger("setup")

AP_IP = "192.168.4.1"
AP_SSID = "OnDeck-Setup"
AP_CHANNEL = 6
DHCP_RANGE = "192.168.4.2,192.168.4.20,255.255.255.0,2h"

HOSTAPD_CONF = f"""\
interface={AP_IFACE}
driver=nl80211
ssid={AP_SSID}
hw_mode=g
channel={AP_CHANNEL}
wmm_enabled=0
auth_algs=1
ignore_broadcast_ssid=0
"""

DNSMASQ_CONF = f"""\
interface={AP_IFACE}
dhcp-range={DHCP_RANGE}
dhcp-option=3,{AP_IP}
dhcp-option=6,{AP_IP}
address=/#/{AP_IP}
no-resolv
"""


# ── Access point ────────────────────────────────────────────────────────────

def _run(cmd: list, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _start_ap() -> None:
    log.info("Starting AP: SSID=%s IP=%s", AP_SSID, AP_IP)
    _run(["wpa_cli", "-i", AP_IFACE, "terminate"])
    for proc in ("wpa_supplicant", "hostapd", "dnsmasq"):
        _run(["killall", proc])
    time.sleep(1)

    _run(["ip", "link", "set", AP_IFACE, "up"])
    _run(["ip", "addr", "flush", "dev", AP_IFACE])
    _run(["ip", "addr", "add", f"{AP_IP}/24", "dev", AP_IFACE])

    hostapd_path = Path("/tmp/ondeck_hostapd.conf")
    dnsmasq_path = Path("/tmp/ondeck_dnsmasq.conf")
    hostapd_path.write_text(HOSTAPD_CONF)
    dnsmasq_path.write_text(DNSMASQ_CONF)

    subprocess.Popen(["hostapd", str(hostapd_path)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    subprocess.Popen(["dnsmasq", "--no-daemon", f"--conf-file={dnsmasq_path}"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

    # Funnel captive-portal probes to our page.
    _run(["iptables", "-t", "nat", "-F", "PREROUTING"])
    _run(["iptables", "-t", "nat", "-A", "PREROUTING", "-i", AP_IFACE,
          "-p", "tcp", "--dport", "80", "-j", "DNAT",
          "--to-destination", f"{AP_IP}:80"])
    log.info("AP running — connect a phone to %s", AP_SSID)


def _stop_ap() -> None:
    log.info("Stopping AP")
    for proc in ("hostapd", "dnsmasq"):
        _run(["killall", proc])
    _run(["iptables", "-t", "nat", "-F", "PREROUTING"])
    _run(["ip", "addr", "flush", "dev", AP_IFACE])
    time.sleep(1)


def _reboot() -> None:
    log.info("Rebooting…")
    time.sleep(2)
    _run(["reboot"])


def _device_name() -> str:
    try:
        return Path("/etc/hostname").read_text().strip() or "OnDeck Pi"
    except Exception:
        return "OnDeck Pi"


# ── Portal pages ────────────────────────────────────────────────────────────

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0b1622;color:#eee;font-family:system-ui,sans-serif;
     min-height:100vh;display:flex;flex-direction:column;align-items:center;
     justify-content:center;padding:1rem}
.logo{font-size:1.6rem;font-weight:700;color:#3aa0ff;margin-bottom:.25rem}
.sub{font-size:.8rem;color:#668;margin-bottom:2rem}
.card{background:#13202e;border:1px solid #24384c;border-radius:12px;
      padding:1.5rem;width:100%;max-width:380px}
h2{font-size:1rem;font-weight:600;margin-bottom:1.25rem;color:#cdd}
label{display:block;font-size:.75rem;color:#8ab;margin-bottom:.3rem}
input,select{display:block;width:100%;padding:.65rem .9rem;background:#0b1622;
  border:1px solid #2c4660;border-radius:6px;color:#eee;font-size:1rem;
  margin-bottom:1rem;-webkit-appearance:none}
input:focus,select:focus{outline:none;border-color:#3aa0ff}
.btn{display:block;width:100%;padding:.8rem;background:#3aa0ff;color:#012;
     border:none;border-radius:8px;font-size:1rem;font-weight:700;cursor:pointer}
.hint{font-size:.75rem;color:#668;margin:-.5rem 0 1rem}
.alert{background:#2a0f0f;border:1px solid #c62828;border-radius:6px;
       padding:.75rem;margin-bottom:1rem;font-size:.85rem;color:#ef9a9a}
.success{background:#0d2b1a;border:1px solid #2e7d4f;border-radius:6px;
         padding:1rem;text-align:center}
.success .big{font-size:2.5rem;margin-bottom:.5rem;color:#4caf72}
.success p{font-size:.9rem;color:#bcd;line-height:1.5}
.saved-list{list-style:none;margin-bottom:1rem}
.saved-list li{font-size:.85rem;color:#8ab;padding:.25rem 0;border-bottom:1px solid #1d2c3c}
"""

_WIFI_SELECT = """
    <label>Wi-Fi Network</label>
    {% if networks %}
    <select name="ssid" id="ssid-sel" onchange="checkOther(this)">
      <option value="">— select —</option>
      {% for n in networks %}<option value="{{ n }}">{{ n }}</option>{% endfor %}
      <option value="__other__">Other (type manually)…</option>
    </select>
    <input type="text" name="ssid_manual" id="ssid-manual"
           placeholder="Network name" style="display:none">
    {% else %}
    <input type="text" name="ssid_manual" placeholder="Network name" required>
    {% endif %}
    <label>Wi-Fi Password</label>
    <input type="password" name="password" placeholder="Password" autocomplete="off">
"""

_SCRIPT = """
<script>
function checkOther(sel){
  var m=document.getElementById('ssid-manual');
  if(sel.value==='__other__'){m.style.display='block';m.required=true;}
  else{m.style.display='none';m.required=false;}
}
</script>
"""

SETUP_PAGE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OnDeck Setup</title><style>""" + _CSS + """</style></head><body>
<div class="logo">OnDeck</div><div class="sub">Device Setup</div>
{% if success %}
<div class="card"><div class="success"><div class="big">&#10003;</div>
<p><strong style="color:#4caf72">Setup complete!</strong><br><br>
Connecting to <strong>{{ wifi_ssid }}</strong> and linking to the cloud.
Ready in about a minute.<br><br>You can close this page.</p></div></div>
{% elif error %}
<div class="card"><div class="alert">{{ error }}</div>
<a href="/"><div class="btn">Try again</div></a></div>
{% else %}
<div class="card"><h2>Connect &amp; link your device</h2>
<form method="post" action="/setup">""" + _WIFI_SELECT + """
    <label>Cloud URL</label>
    <input type="url" name="cloud_url" placeholder="https://your-app.onrender.com" required>
    <label>Pairing Code</label>
    <input type="text" name="pairing_code" placeholder="From the Devices page"
           autocomplete="off" required>
    <div class="hint">Generate a code in the cloud portal under Devices, then enter it here.</div>
    <button class="btn" type="submit">Connect &amp; Link</button>
</form></div>{% endif %}""" + _SCRIPT + """</body></html>"""

WIFI_ONLY_PAGE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OnDeck — Add Wi-Fi</title><style>""" + _CSS + """</style></head><body>
<div class="logo">OnDeck</div><div class="sub">Wi-Fi Management</div>
{% if success %}
<div class="card"><div class="success"><div class="big">&#10003;</div>
<p><strong style="color:#4caf72">Network added!</strong><br><br>
<strong>{{ wifi_ssid }}</strong> saved. Rebooting; it will connect when in range.<br><br>
You can close this page.</p></div></div>
{% elif error %}
<div class="card"><div class="alert">{{ error }}</div>
<a href="/"><div class="btn">Try again</div></a></div>
{% else %}
<div class="card"><h2>Add a Wi-Fi network</h2>
{% if saved %}<label>Already saved</label><ul class="saved-list">
{% for s in saved %}<li>&#10003; {{ s }}</li>{% endfor %}</ul>{% endif %}
<form method="post" action="/setup">""" + _WIFI_SELECT + """
    <button class="btn" type="submit">Add Network &amp; Reboot</button>
</form></div>{% endif %}""" + _SCRIPT + """</body></html>"""


def _run_flask(wifi_only: bool = False) -> None:
    from flask import Flask, request, render_template_string

    app = Flask(__name__)
    done = threading.Event()
    page = WIFI_ONLY_PAGE if wifi_only else SETUP_PAGE

    def _render(**kw):
        kw.setdefault("networks", scan_networks())
        kw.setdefault("saved", list_saved_networks() if wifi_only else [])
        kw.setdefault("success", False)
        kw.setdefault("error", None)
        return render_template_string(page, **kw)

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def index(path):
        return _render()

    @app.route("/setup", methods=["POST"])
    def setup():
        ssid = request.form.get("ssid", "").strip()
        if ssid == "__other__":
            ssid = request.form.get("ssid_manual", "").strip()
        password = request.form.get("password", "")
        if not ssid:
            return _render(error="Please enter a Wi-Fi network name.")

        if wifi_only:
            append_wifi(ssid, password)
        else:
            cloud_url = request.form.get("cloud_url", "").strip()
            pairing_code = request.form.get("pairing_code", "").strip()
            if not cloud_url or not pairing_code:
                return _render(error="Cloud URL and Pairing Code are required.")
            # The portal runs as an access point with no internet, so it can't
            # redeem the code here. Save the Wi-Fi + a pending link and reboot;
            # boot_mode redeems the code once the field network is up.
            write_wifi(ssid, password)
            write_pending_link(cloud_url, pairing_code)
            log.info("Setup saved: SSID=%s device=%s", ssid, _device_name())

        resp = _render(success=True, wifi_ssid=ssid)
        done.set()
        return resp

    # Captive-portal probe endpoints → bounce to our page.
    for path in ["/generate_204", "/hotspot-detect.html", "/connecttest.txt",
                 "/ncsi.txt", "/redirect"]:
        app.add_url_rule(
            path, path.lstrip("/") or "gen204",
            lambda: ("", 302, {"Location": f"http://{AP_IP}/"}))

    def _shutdown():
        done.wait()
        time.sleep(3)   # let the phone render the success page
        _stop_ap()
        _reboot()

    threading.Thread(target=_shutdown, daemon=True).start()
    app.run(host="0.0.0.0", port=80, threaded=True, use_reloader=False)


def run() -> None:
    if os.geteuid() != 0:
        sys.exit("setup_server.py must run as root")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-10s %(levelname)-7s  %(message)s")
    _start_ap()
    _run_flask(wifi_only="--wifi-only" in sys.argv)


if __name__ == "__main__":
    run()
