"""OnDeck Audio Pi server.

Runs on the Raspberry Pi plugged into the field PA. It owns the speakers: the
Coach Pi (or the cloud) sends it simple HTTP commands and this process turns
them into precise ffmpeg playback.

Design goals:
  * One thing plays at a time. Queue a clip, then play it.
  * Trim and fade are sample-accurate via ffmpeg, not guesswork.
  * A fade or a stop always actually stops the sound, immediately.
  * No internet needed to play; internet is only used for YouTube import.

Endpoints (all JSON):
  POST /queue    {file, start_ms, end_ms, announcement?, cue_ms?}
  POST /play
  POST /stop
  POST /fade     {ms?}            -> fade out over ms (default 1000)
  POST /volume   {level}          -> 0..100
  GET  /status
  POST /upload   (multipart file) -> stores in music dir
  POST /import    {url}           -> yt-dlp audio import
  GET  /health
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request
from werkzeug.utils import secure_filename

from config_manager import MUSIC_DIR

log = logging.getLogger("ondeck-audio")

try:
    from bluetooth_manager import BluetoothManager
except Exception:  # pragma: no cover - missing optional deps must not stop audio
    BluetoothManager = None  # type: ignore


app = Flask(__name__)

DEFAULT_FADE_MS = 1000

# A2DP speaker control + audio routing. Disable on laptops/CI with
# ONDECK_NO_BLUETOOTH=1 (then playback uses the ALSA default / override).
bt = BluetoothManager() if (BluetoothManager and
                            not os.environ.get("ONDECK_NO_BLUETOOTH")) else None


class Player:
    """Owns the single active ffmpeg playback process.

    State machine: stopped -> queued -> playing -> (stopped). A clip is loaded
    by ``queue`` but stays silent until ``play``. When playback finishes
    naturally an ``on_finish`` callback (if set) fires so the Coach Pi can
    auto-advance the lineup.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._queued: dict | None = None
        self._state = "stopped"          # stopped | queued | playing
        self._started_at: float | None = None
        self._volume = 80                # 0..100, applied as ffmpeg gain
        self.on_finish = None            # optional callable()

    # -- queue / play -----------------------------------------------------

    def queue(self, clip: dict) -> None:
        with self._lock:
            self._stop_locked()
            self._queued = clip
            self._state = "queued"

    def play(self) -> bool:
        with self._lock:
            if not self._queued:
                return False
            cmd = self._build_command(self._queued)
            self._spawn(cmd)
            self._state = "playing"
            self._started_at = time.monotonic()
            return True

    def _build_command(self, clip: dict) -> list[str]:
        """Compose the ffmpeg invocation for a clip.

        A clip is either a plain trimmed song, or a song mixed under an
        announcement that fades in at ``cue_ms``.
        """
        gain = self._volume / 100.0
        song = str(MUSIC_DIR / clip["file"])
        start_s = (clip.get("start_ms") or 0) / 1000.0
        end_ms = clip.get("end_ms")

        # Build the main (song) input with its trim window.
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        cmd += ["-ss", f"{start_s:.3f}"]
        if end_ms is not None:
            cmd += ["-to", f"{(end_ms / 1000.0):.3f}"]
        cmd += ["-i", song]

        announcement = clip.get("announcement")
        if announcement:
            ann = str(MUSIC_DIR / announcement)
            cue_s = (clip.get("cue_ms") or 0) / 1000.0
            # Two inputs: announcement (input 0 after the song? keep order
            # song=0, announcement=1) — delay the song so it fades in at cue.
            # We rebuild as: announcement first at full volume, song delayed.
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", ann,
                "-ss", f"{start_s:.3f}",
            ]
            if end_ms is not None:
                cmd += ["-to", f"{(end_ms / 1000.0):.3f}"]
            cmd += ["-i", song]
            # Delay the song by cue_s, fade it in over 0.75s, mix under the
            # announcement, then apply master volume.
            delay_ms = int(cue_s * 1000)
            filt = (
                f"[1:a]adelay={delay_ms}|{delay_ms},afade=t=in:st={cue_s:.3f}:d=0.75[mus];"
                f"[0:a][mus]amix=inputs=2:duration=longest:dropout_transition=0,"
                f"volume={gain:.3f}[out]"
            )
            cmd += ["-filter_complex", filt, "-map", "[out]"]
        else:
            # Plain song: master volume only. (End-of-song fade is applied by
            # the editor when it sets end_ms; live fade is the /fade endpoint.)
            cmd += ["-af", f"volume={gain:.3f}"]

        cmd += self._output_args()
        return cmd

    def _output_args(self) -> list[str]:
        """ffmpeg output target.

        Priority: explicit ONDECK_FFMPEG_OUT override (laptops/testing) →
        the connected Bluetooth speaker's PipeWire/Pulse sink → ALSA default.
        """
        override = os.environ.get("ONDECK_FFMPEG_OUT")
        if override:
            return shlex.split(override)
        sink = bt.current_sink() if bt else None
        if sink:
            return ["-f", "pulse", sink]
        return ["-f", "alsa", "default"]

    # -- fade / stop ------------------------------------------------------

    def fade(self, ms: int = DEFAULT_FADE_MS) -> bool:
        """Fade the *currently playing* clip out over ``ms`` and stop.

        We can't retroactively edit a running ffmpeg's filter graph, so we
        capture the current playback position, kill the process, and relaunch
        a short clip that plays from that position with a fade-out, then ends.
        """
        with self._lock:
            if self._state != "playing" or not self._queued:
                self._stop_locked()
                return False
            pos_s = (time.monotonic() - (self._started_at or 0))
            clip = self._queued
            self._kill_proc()

            song = str(MUSIC_DIR / clip["file"])
            start_s = (clip.get("start_ms") or 0) / 1000.0
            dur_s = ms / 1000.0
            gain = self._volume / 100.0
            seek = start_s + pos_s
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", f"{seek:.3f}", "-t", f"{dur_s:.3f}", "-i", song,
                "-af", f"afade=t=out:st=0:d={dur_s:.3f},volume={gain:.3f}",
            ] + self._output_args()
            self._spawn(cmd, finish_state="stopped")
            # After the fade clip ends the watcher sets state to stopped; the
            # queue is intentionally cleared so nothing is left cued.
            self._queued = None
            return True

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        self._kill_proc()
        self._state = "stopped"
        self._queued = None
        self._started_at = None

    def set_volume(self, level: int) -> None:
        with self._lock:
            self._volume = max(0, min(100, int(level)))
            # Volume change applies to the next play; we don't restart a
            # running clip to avoid an audible glitch mid-walk-up.

    # -- process management ----------------------------------------------

    def _spawn(self, cmd: list[str], finish_state: str = "stopped") -> None:
        self._kill_proc()
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        watcher = threading.Thread(
            target=self._watch, args=(self._proc, finish_state), daemon=True
        )
        watcher.start()

    def _watch(self, proc: subprocess.Popen, finish_state: str) -> None:
        proc.wait()
        with self._lock:
            # Only react if this is still the active process (not superseded).
            if self._proc is not proc:
                return
            natural_end = self._state == "playing"
            self._state = finish_state
            self._started_at = None
            if finish_state == "stopped":
                self._queued = None if finish_state == "stopped" else self._queued
            cb = self.on_finish
        if natural_end and finish_state == "stopped" and cb:
            # Lineup auto-advance hook. Fire outside the lock.
            try:
                cb()
            except Exception:
                pass

    def _kill_proc(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self._proc = None

    # -- status -----------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            pos_ms = 0
            if self._state == "playing" and self._started_at is not None:
                pos_ms = int((time.monotonic() - self._started_at) * 1000)
            return {
                "state": self._state,
                "position_ms": pos_ms,
                "volume": self._volume,
                "queued": self._queued,
            }


player = Player()


# -- HTTP routes ----------------------------------------------------------

@app.post("/queue")
def http_queue():
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("file"):
        return jsonify(error="file required"), 400
    player.queue(body)
    return jsonify(ok=True, status=player.status())


@app.post("/play")
def http_play():
    ok = player.play()
    return jsonify(ok=ok, status=player.status())


@app.post("/stop")
def http_stop():
    player.stop()
    return jsonify(ok=True, status=player.status())


@app.post("/fade")
def http_fade():
    body = request.get_json(force=True, silent=True) or {}
    ms = int(body.get("ms", DEFAULT_FADE_MS))
    ok = player.fade(ms)
    return jsonify(ok=ok, status=player.status())


@app.post("/volume")
def http_volume():
    body = request.get_json(force=True, silent=True) or {}
    if "level" not in body:
        return jsonify(error="level required"), 400
    player.set_volume(int(body["level"]))
    return jsonify(ok=True, status=player.status())


@app.get("/status")
def http_status():
    return jsonify(player.status())


AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus",
              ".webm", ".flac", ".mp4", ".wma", ".aiff", ".aif"}


@app.post("/upload")
def http_upload():
    if "file" not in request.files:
        return jsonify(error="file required"), 400
    f = request.files["file"]
    name = secure_filename(Path(f.filename or "").name)
    if not name or Path(name).suffix.lower() not in AUDIO_EXTS:
        return jsonify(error="bad filename"), 400
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    dest = MUSIC_DIR / name
    f.save(str(dest))
    return jsonify(ok=True, filename=name)


@app.post("/import")
def http_import():
    body = request.get_json(force=True, silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify(error="url required"), 400
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    # Audio only, mp3, title-based filename. Runs synchronously; the caller
    # should treat this as a slow request.
    out_tmpl = str(MUSIC_DIR / "%(title)s.%(ext)s")
    try:
        result = subprocess.run(
            ["yt-dlp", "-x", "--audio-format", "mp3", "--no-playlist",
             "--print", "after_move:filepath", "-o", out_tmpl, url],
            capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError:
        return jsonify(error="yt-dlp not installed"), 500
    except subprocess.TimeoutExpired:
        return jsonify(error="import timed out"), 504
    if result.returncode != 0:
        return jsonify(error=result.stderr.strip()[-500:] or "import failed"), 502
    filepath = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    return jsonify(ok=True, filename=Path(filepath).name if filepath else None)


# -- Bluetooth speaker control -------------------------------------------
# These run on the Audio Pi; the portal proxies to them so a coach can manage
# the speaker from a browser on the field Wi-Fi. All are no-ops (503) when
# Bluetooth is disabled (e.g. on a laptop with ONDECK_NO_BLUETOOTH=1).

def _bt_or_503():
    if bt is None:
        return None, (jsonify(ok=False, error="bluetooth unavailable"), 503)
    return bt, None


@app.get("/bluetooth/status")
def http_bt_status():
    mgr, err = _bt_or_503()
    if err:
        return err
    return jsonify(ok=True, **mgr.status())


@app.post("/bluetooth/scan")
def http_bt_scan():
    mgr, err = _bt_or_503()
    if err:
        return err
    secs = int((request.get_json(silent=True) or {}).get("seconds", 8))
    return jsonify(ok=True, devices=mgr.scan(max(3, min(secs, 30))))


def _bt_mac_action(method):
    mgr, err = _bt_or_503()
    if err:
        return err
    mac = (request.get_json(force=True, silent=True) or {}).get("mac", "").strip()
    if not mac:
        return jsonify(ok=False, error="mac required"), 400
    return jsonify(ok=bool(method(mgr, mac)), status=mgr.status())


@app.post("/bluetooth/pair")
def http_bt_pair():
    return _bt_mac_action(lambda m, mac: m.pair(mac))


@app.post("/bluetooth/connect")
def http_bt_connect():
    return _bt_mac_action(lambda m, mac: m.connect(mac))


@app.post("/bluetooth/disconnect")
def http_bt_disconnect():
    return _bt_mac_action(lambda m, mac: m.disconnect(mac))


@app.post("/bluetooth/forget")
def http_bt_forget():
    return _bt_mac_action(lambda m, mac: m.forget(mac))


@app.post("/bluetooth/preferred")
def http_bt_preferred():
    """Set (or clear) the remembered speaker + auto-connect flag."""
    mgr, err = _bt_or_503()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    mac = (body.get("mac") or "").strip() or None
    mgr.set_preferred(mac, body.get("name", ""),
                      bool(body.get("auto_connect", True)))
    # Apply immediately if a speaker was just chosen.
    if mac and body.get("auto_connect", True):
        mgr.connect(mac)
    return jsonify(ok=True, status=mgr.status())


@app.get("/health")
def http_health():
    return jsonify(ok=True, service="ondeck-audio")


@app.get("/stats")
def http_stats():
    """System resource snapshot (temp/CPU/mem/disk/Wi-Fi) for diagnostics."""
    try:
        from system_stats import gather
        stats = gather()
    except Exception as exc:  # a broken sensor must never break audio
        stats = {"error": str(exc)}
    return jsonify(ok=True, service="ondeck-audio", player=player.status(),
                   **stats)


@app.get("/")
def http_landing():
    """A tiny page so a coach can link/manage the Audio Pi from a browser on the
    field Wi-Fi — no SSH, no login (the cloud-link + Wi-Fi pages are served by
    pi.web_routes, registered in main())."""
    try:
        from pi.netconfig import read_sync_env
        env = read_sync_env()
    except Exception:
        env = {}
    linked = bool(env.get("ONDECK_SYNC_TOKEN"))
    status = (f"&#10003; Linked to {env.get('ONDECK_CLOUD_URL', 'the cloud')}"
              if linked else "Not linked yet — add your cloud code below.")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OnDeck Audio Pi</title></head>
<body style="font-family:system-ui,sans-serif;background:#0b1622;color:#eee;
 min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1rem;padding:2rem;text-align:center">
<h1 style="color:#3aa0ff;margin:0">OnDeck Audio Pi</h1>
<p style="color:#8ab">{status}</p>
<a href="/cloud-settings" style="display:block;background:#3aa0ff;color:#012;padding:.7rem 1.4rem;
 border-radius:8px;font-weight:700;text-decoration:none">Link to cloud</a>
<a href="/wifi" style="color:#3aa0ff;text-decoration:none">Wi-Fi networks &rarr;</a>
</body></html>"""


def main() -> None:
    port = int(os.environ.get("ONDECK_AUDIO_PORT", "5100"))
    # Cloud-link + Wi-Fi pages (same ones the deck portal uses) so the Audio Pi
    # can be linked/managed from a browser on the field Wi-Fi without SSH.
    try:
        from pi.web_routes import register as register_pi_routes
        register_pi_routes(app)
    except Exception as exc:  # optional — must not stop audio playback
        log.warning("Pi web routes not registered: %s", exc)
    if bt is not None:
        # Power the radio on at boot (clearing any rfkill soft-block) so the
        # speaker page shows "radio on" and scanning works even before a
        # preferred speaker exists — the auto-connect loop only powers on when
        # one is already set.
        try:
            bt.ensure_powered()
        except Exception as exc:
            log.warning("Bluetooth power-on at startup failed: %s", exc)
        bt.start_autoconnect()
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
