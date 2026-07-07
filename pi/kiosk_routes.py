"""No-login field pages: Stream Deck settings + Raspberry Pi settings.

Registered onto the deck Pi's portal by ``main.py`` (and the Pi-settings page
onto the Audio Pi's music server) — never on the cloud deployment. They are
deliberately session-free, like the Wi-Fi / cloud-link pages: anyone standing
at the field with LAN access can fix the volume, jump the deck to a page, or
reboot a Pi without hunting for the portal password mid-game.

  /deck-settings  volume + transport (play/stop/fade/replay), lineup reset,
                  deck page jump, brightness, deck model, Audio Pi target
  /pi-settings    network info, Wi-Fi / cloud-link shortcuts, sync now,
                  service restart, reboot, shutdown

System actions (restart/reboot/shutdown) go through sudo — install.sh drops
the matching NOPASSWD sudoers rule (ondeck-power).
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_routes import _PAGE  # noqa: E402 — shared minimal page shell

log = logging.getLogger("pi.kiosk")

_REPO_DIR = Path(__file__).resolve().parent.parent

# Whitelisted /pi-action commands. Everything runs detached so the HTTP
# response returns before a reboot/restart kills the server.
_ACTIONS: dict[str, list[str]] = {
    "restart_deck":  ["sudo", "-n", "systemctl", "restart", "ondeck-coach"],
    "restart_audio": ["sudo", "-n", "systemctl", "restart", "ondeck-audio"],
    "reboot":        ["sudo", "-n", "systemctl", "reboot"],
    "shutdown":      ["sudo", "-n", "systemctl", "poweroff"],
}


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


def _run_sync_now() -> None:
    """Run one sync cycle in the background with the stored cloud creds."""
    try:
        from netconfig import read_sync_env
        env = dict(os.environ)
        env.update(read_sync_env())
        subprocess.Popen(
            [sys.executable, str(_REPO_DIR / "sync_agent.py")],
            env=env, cwd=str(_REPO_DIR),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log.warning("Sync-now failed to start: %s", exc)


def _btn(label: str, action: str, color: str = "#3aa0ff", confirm: str = "") -> str:
    onsubmit = f" onsubmit=\"return confirm('{confirm}')\"" if confirm else ""
    return (f"<form method='post' style='margin:.35rem 0'{onsubmit}>"
            f"<input type='hidden' name='action' value='{action}'>"
            f"<button class='btn' style='background:{color}'>{label}</button></form>")


def register_pi_settings(app) -> None:
    """The /pi-settings page — works on both the deck Pi and the Audio Pi."""
    from flask import redirect, render_template_string, request, url_for
    from markupsafe import Markup

    def _shell(title, body):
        return render_template_string(_PAGE, title=title, body=Markup(body))

    @app.get("/pi-settings")
    def kiosk_pi_settings():
        msg = request.args.get("ok", "")
        hostname = socket.gethostname()
        body = (
            (f"<div class='card'><div class='ok'>{msg}</div></div>" if msg else "")
            + "<div class='card'><h2>This Raspberry Pi</h2><ul>"
            + f"<li><span>Hostname</span><span>{hostname}</span></li>"
            + f"<li><span>IP address</span><span>{_local_ip()}</span></li>"
            + "</ul></div>"
            + "<div class='card'><h2>Network &amp; cloud</h2>"
            + "<a href='/wifi' style='margin-top:.2rem'>Wi-Fi networks &rarr;</a>"
            + "<a href='/cloud-settings'>Cloud link &rarr;</a>"
            + "</div>"
            + "<div class='card'><h2>Maintenance</h2>"
            + _btn("Sync with cloud now", "sync_now")
            + _btn("Restart OnDeck (Stream Deck)", "restart_deck", "#7a5cff")
            + _btn("Restart OnDeck (Audio)", "restart_audio", "#7a5cff")
            + _btn("Reboot this Pi", "reboot", "#c98a1a", "Reboot this Pi?")
            + _btn("Shut down this Pi", "shutdown", "#c62828",
                   "Shut down this Pi? You will need to unplug/replug power to start it again.")
            + "</div>"
            + "<a href='/deck-settings'>&#8592; Deck settings</a>"
        )
        return _shell("Pi Settings", body)

    @app.post("/pi-settings")
    def kiosk_pi_action():
        action = (request.form.get("action") or "").strip()
        if action == "sync_now":
            threading.Thread(target=_run_sync_now, daemon=True).start()
            return redirect(url_for("kiosk_pi_settings", ok="Sync started — give it a minute."))
        cmd = _ACTIONS.get(action)
        if not cmd:
            return redirect(url_for("kiosk_pi_settings", ok="Unknown action."))
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            log.error("Pi action %s failed: %s", action, exc)
        return redirect(url_for("kiosk_pi_settings", ok=f"'{action}' requested."))


def register(app) -> None:
    """Deck-settings + Pi-settings pages (deck Pi portal)."""
    from flask import redirect, render_template_string, request, url_for
    from markupsafe import Markup

    register_pi_settings(app)

    def _shell(title, body):
        return render_template_string(_PAGE, title=title, body=Markup(body))

    def _rt():
        # Live deck objects, registered by main.py; config comes from the
        # portal's own ConfigManager so edits show everywhere at once.
        from web.app import cfg, runtime
        return cfg, runtime

    @app.get("/deck-settings")
    def kiosk_deck_settings():
        cfg, runtime = _rt()
        msg = request.args.get("ok", "")
        music = runtime.get("music")
        status = music.status() if music else None
        state = (status or {}).get("state", "Audio Pi unreachable")
        volume = (status or {}).get("volume", cfg.system.get("volume", 80))
        from config_manager import DECK_MODELS
        model = cfg.deck_model()
        model_opts = "".join(
            f"<option value='{k}'{' selected' if k == model else ''}>{v['label']}</option>"
            for k, v in DECK_MODELS.items())
        pages = "".join(
            f"<form method='post' style='display:inline-block;margin:.2rem'>"
            f"<input type='hidden' name='action' value='goto'>"
            f"<input type='hidden' name='page' value='{pid}'>"
            f"<button class='btn' style='width:auto;padding:.45rem .8rem;background:#24506e;color:#dff'>"
            f"{cfg.pages.get(pid, {}).get('name', pid)}</button></form>"
            for pid in cfg.get_page_order())
        ip, port = cfg.audio_pi_endpoint()
        body = (
            (f"<div class='card'><div class='ok'>{msg}</div></div>" if msg else "")
            + f"<div class='card'><h2>Playback — {state}</h2>"
            + "<form method='post'><input type='hidden' name='action' value='volume'>"
            + f"<label>Volume ({volume})</label>"
            + f"<input type='range' name='level' min='0' max='100' value='{volume}' "
              "onchange='this.form.submit()'></form>"
            + "<div style='display:flex;gap:.5rem'>"
            + "<form method='post' style='flex:1'><input type='hidden' name='action' value='play'>"
              "<button class='btn' style='background:#2e7d4f'>&#9654; Play</button></form>"
            + "<form method='post' style='flex:1'><input type='hidden' name='action' value='stop'>"
              "<button class='btn' style='background:#c62828'>&#9632; Stop</button></form>"
            + "<form method='post' style='flex:1'><input type='hidden' name='action' value='fade'>"
              "<button class='btn' style='background:#24506e'>&#8600; Fade</button></form>"
            + "</div>"
            + _btn("&#8635; Replay last song", "replay", "#7a5cff")
            + "</div>"
            + "<div class='card'><h2>Lineup</h2>"
            + _btn("&#10226; Reset lineup to the top", "reset_lineup", "#c98a1a",
                   "Send the batting order back to the leadoff hitter?")
            + "</div>"
            + f"<div class='card'><h2>Deck pages</h2>{pages or '<div class=small>No pages.</div>'}</div>"
            + "<div class='card'><h2>Stream Deck</h2>"
            + "<form method='post'><input type='hidden' name='action' value='brightness'>"
            + "<label>Brightness</label>"
            + "<input type='range' name='level' min='10' max='100' value='80' "
              "onchange='this.form.submit()'></form>"
            + "<form method='post'><input type='hidden' name='action' value='model'>"
            + f"<label>Deck model (editor grid + key roles)</label><select name='model'>{model_opts}</select>"
            + "<button class='btn'>Save model</button></form>"
            + "<div class='small'>Note: the cloud portal is the source of truth — a model "
              "changed only here is overwritten by the next cloud sync.</div>"
            + "</div>"
            + "<div class='card'><h2>Audio Pi target</h2>"
            + "<form method='post'><input type='hidden' name='action' value='audio_target'>"
            + f"<label>IP (blank = auto-discover)</label><input name='ip' value='{cfg.system.get('audio_pi_ip', '')}'>"
            + f"<label>Port</label><input name='port' value='{port}'>"
            + f"<div class='small'>Currently talking to {ip}:{port}</div>"
            + "<button class='btn'>Save target</button></form></div>"
            + "<a href='/pi-settings'>Raspberry Pi settings &rarr;</a>"
            + "<a href='/'>&#8592; Portal</a>"
        )
        return _shell("Deck Settings", body)

    @app.post("/deck-settings")
    def kiosk_deck_settings_post():
        cfg, runtime = _rt()
        music, lineup, deck = (runtime.get("music"), runtime.get("lineup"),
                               runtime.get("deck"))
        action = (request.form.get("action") or "").strip()
        ok = "Done."
        if action == "volume" and music:
            try:
                level = max(0, min(100, int(request.form.get("level", 80))))
                music.set_volume(level)
                ok = f"Volume set to {level}."
            except ValueError:
                ok = "Bad volume."
        elif action == "play" and lineup:
            lineup.play()
            ok = "Play sent."
        elif action == "stop" and music:
            music.stop()
            ok = "Stopped."
        elif action == "fade" and music:
            music.fade()
            ok = "Fading out."
        elif action == "replay" and music:
            if lineup:
                lineup.note_external_playback()
            ok = "Replaying the last song." if music.replay() else "Nothing to replay yet."
        elif action == "reset_lineup" and lineup:
            lineup.reset()
            ok = "Lineup reset to the top of the order."
        elif action == "goto" and deck:
            page = request.form.get("page", "")
            ok = "Deck moved." if deck.go_to_page(page) else "Unknown page."
        elif action == "brightness" and deck and getattr(deck, "deck", None):
            try:
                level = max(10, min(100, int(request.form.get("level", 80))))
                deck.deck.set_brightness(level)
                ok = f"Brightness set to {level}."
            except Exception as exc:
                ok = f"Brightness failed: {exc}"
        elif action == "model":
            cfg.set_deck_model(request.form.get("model", ""))
            if deck:
                deck.refresh()
            ok = "Deck model saved (locally — set it in the cloud portal too)."
        elif action == "audio_target":
            with cfg._lock:
                cfg.system["audio_pi_ip"] = (request.form.get("ip") or "").strip()
                try:
                    cfg.system["audio_pi_port"] = int(request.form.get("port", 5100))
                except ValueError:
                    pass
                cfg.save(mark_dirty=False)
            ok = "Audio Pi target saved."
        else:
            ok = "Nothing to do (deck runtime not available)."
        return redirect(url_for("kiosk_deck_settings", ok=ok))
