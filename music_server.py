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
  POST /queue    {file, start_ms, end_ms, fade_ms?, announcement?, cue_ms?}
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
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request

from config_manager import MUSIC_DIR, ConfigManager

log = logging.getLogger("ondeck-audio")

try:
    from bluetooth_manager import BluetoothManager
except Exception:  # pragma: no cover - missing optional deps must not stop audio
    BluetoothManager = None  # type: ignore

# On-demand cloud sync (pi/sync_now.py). The deck's Sync key POSTs
# /api/sync-now here so ONE press updates BOTH boxes — without this, the
# deck synced its labels while the disk the speaker plays from stayed
# stale, and every deck showed 'aud ✗' because the endpoint 404'd.
try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent / "pi"))
    import sync_now as _sync_now
except Exception:  # pragma: no cover - sync tooling absent must not stop audio
    _sync_now = None  # type: ignore


app = Flask(__name__)

DEFAULT_FADE_MS = 1000

# The trim editor's fade-out is deliberately short: a walk-up that trails off
# for three seconds is three seconds the batter is standing there waiting.
CLIP_FADE_MAX_MS = 1500

# ffmpeg's pulse output disconnects WITHOUT draining: whatever the sound
# server still buffers of our stream when the process exits is dropped on
# the floor — which is how every clip lost its last couple of seconds (the
# train horn cut off a beat early). Asking ffmpeg for a smaller buffer
# helped on a stock PulseAudio server but not on the Pi's pipewire-pulse,
# which sizes the buffer its own way. So on the pulse route the clip is
# now DECODED by ffmpeg but PLAYED by pacat, which drains: it exits only
# once the server confirms the last sample left the speaker, whatever the
# buffer was. The values below only serve the routes that still talk
# straight to a device: the buffer cap for a direct -f pulse command (the
# fade fallback), the silence tail so what a non-draining exit drops is
# padding, not music.
PULSE_BUFFER_MS = 300
# Generous on purpose: the fallback routes are the ones we know least
# about — on a Pi, ALSA "default" is often PipeWire's ALSA plugin wearing
# a disguise, with a couple of seconds of buffer nobody drains.
TAIL_PAD_S = 2.0
# The pacat pipeline needs less: its latency cap keeps at most
# ~PULSE_BUFFER_MS queued in the server, so the tail only has to outlast
# that plus the hardware FIFO under it. It is also what the state shows
# as "playing" after the music ends, so shorter is better here.
PIPE_PAD_S = 1.0

# Everything is decoded to one raw format for pacat; 44.1 kHz stereo is
# what nearly every MP3 in the library already is.
RAW_RATE = 44100
RAW_ARGS = ["-f", "s16le", "-ar", str(RAW_RATE), "-ac", "2", "-"]

_PACAT = shutil.which("pacat")

_duration_cache: dict[str, float] = {}


def _audio_duration_s(path: str) -> float | None:
    """Length of an audio file in seconds, or None if ffprobe can't say.

    Only needed when a clip has no end trim: the fade has to know where the
    end is, and "the end of the file" is not a number ffmpeg will hand us.
    Cached because the same walk-up gets queued every time the kid bats.
    """
    if path in _duration_cache:
        return _duration_cache[path]
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=10)
        dur = float(out.stdout.strip())
    except Exception:
        log.warning("ffprobe could not read the duration of %s", path)
        return None
    if dur <= 0:
        return None
    _duration_cache[path] = dur
    return dur


def _fade_start(clip: dict, song: str, start_s: float) -> tuple[float, float]:
    """(when the fade starts, how long it runs) in seconds — (0, 0) for none.

    The fade is measured back from the end of the *played span*, which is the
    end trim if there is one and otherwise the end of the file.
    """
    try:
        fade_ms = int(clip.get("fade_ms") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0
    fade_ms = max(0, min(CLIP_FADE_MAX_MS, fade_ms))
    if not fade_ms:
        return 0.0, 0.0
    end_ms = clip.get("end_ms")
    if end_ms is not None:
        span = (end_ms / 1000.0) - start_s
    else:
        total = _audio_duration_s(song)
        if total is None:
            return 0.0, 0.0        # rather no fade than a fade in the wrong place
        span = total - start_s
    fade_s = fade_ms / 1000.0
    if span <= 0:
        return 0.0, 0.0
    fade_s = min(fade_s, span)     # a clip shorter than the fade just fades throughout
    return span - fade_s, fade_s

# A2DP speaker control + audio routing. Disable on laptops/CI with
# ONDECK_NO_BLUETOOTH=1 (then playback uses the ALSA default / override).
bt = BluetoothManager() if (BluetoothManager and
                            not os.environ.get("ONDECK_NO_BLUETOOTH")) else None

# -- sound-server routing -------------------------------------------------
# Everything the Audio Pi plays goes through PipeWire/pipewire-pulse when
# there is one, Bluetooth speaker or wired jack alike, because the live
# fade ramps a sink-input volume and a stream ALSA owns has no sink-input.
# ONDECK_AUDIO_OUT=alsa forces the old behaviour if a box ever needs it.
_PULSE_TTL = 20.0                       # re-resolve the default sink now and then
_pulse_state: dict = {"ok": None, "sink": "", "at": float("-inf")}
_pulse_lock = threading.Lock()


_PULSE_PROBE_RETRY_S = 15.0


def _pulse_usable() -> bool:
    """True when a pulse server answers AND ffmpeg can write to it.

    A success is remembered for good — that can't stop being true without
    the service restarting, and asking on every play would put two
    subprocesses in front of a walk-up. A FAILURE is retried after a short
    while: this service can win the boot race against pipewire-pulse, and
    remembering that first "no" forever glued the box to the ALSA fallback
    (no live fade, and a buffer nobody drains) until someone restarted it.
    """
    now = time.monotonic()
    if _pulse_state["ok"] is None or (
            not _pulse_state["ok"] and
            now - _pulse_state.get("probed_at", float("-inf"))
            > _PULSE_PROBE_RETRY_S):
        was = _pulse_state["ok"]
        ok = False
        try:
            r = subprocess.run(["pactl", "info"], capture_output=True,
                               text=True, timeout=5)
            if r.returncode == 0:
                m = subprocess.run(["ffmpeg", "-hide_banner", "-muxers"],
                                   capture_output=True, text=True, timeout=10)
                ok = " pulse" in (m.stdout or "")
        except Exception as exc:
            if was is None:
                log.info("no pulse audio route (%s) — playing straight "
                         "to ALSA", exc)
        _pulse_state["ok"] = ok
        _pulse_state["probed_at"] = now
        if ok != was:
            log.info("Audio route: %s", "pulse" if ok else "alsa")
    return bool(_pulse_state["ok"])


def _pulse_default_sink() -> str:
    """Name of the server's default sink, or '' when there is no server.

    ffmpeg's pulse muxer takes a sink NAME, and the literal string
    "default" is not one — a sink that does not exist means silence — so
    the real name is resolved and cached.
    """
    mode = (os.environ.get("ONDECK_AUDIO_OUT") or "auto").strip().lower()
    if mode == "alsa":
        return ""
    # A Pi with an HDMI cable in it has more than one sink, and the server's
    # idea of "default" is not always the one the PA is plugged into.
    # ONDECK_AUDIO_SINK pins it; /status prints what is actually in use so
    # the answer is readable from the field instead of guessed at.
    pinned = (os.environ.get("ONDECK_AUDIO_SINK") or "").strip()
    if pinned:
        return pinned
    with _pulse_lock:
        if (time.monotonic() - _pulse_state["at"]) < _PULSE_TTL:
            return _pulse_state["sink"]
        if mode != "pulse" and not _pulse_usable():
            return ""
        name = ""
        try:
            r = subprocess.run(["pactl", "get-default-sink"],
                               capture_output=True, text=True, timeout=5)
            out = (r.stdout or "").strip()
            if r.returncode == 0 and out:
                name = out.splitlines()[0].strip()
        except Exception:
            name = ""
        if not name:
            # pactl older than 15 has no get-default-sink.
            try:
                r = subprocess.run(["pactl", "info"], capture_output=True,
                                   text=True, timeout=5)
                for line in (r.stdout or "").splitlines():
                    if line.startswith("Default Sink:"):
                        name = line.split(":", 1)[1].strip()
                        break
            except Exception:
                name = ""
        if name.lower() in ("", "@default_sink@", "auto_null"):
            # No default set, or the dummy sink PipeWire parks on when no
            # card is ready yet — either way ALSA is the better guess.
            name = ""
        _pulse_state["sink"] = name
        _pulse_state["at"] = time.monotonic()
        return name


class MissingAudio(Exception):
    """A clip references a file that is not on this Pi's disk."""


class Player:
    """Owns the single active ffmpeg playback process.

    State machine: stopped -> queued -> playing -> (stopped). A clip is loaded
    by ``queue`` but stays silent until ``play``. When playback finishes
    naturally an ``on_finish`` callback (if set) fires so the Coach Pi can
    auto-advance the lineup.

    A cue that arrives WHILE a clip plays does not stop it. The new clip is
    parked as *pending*: Play swaps to it (the running song is cut, the
    pending one starts), and a song that ends on its own leaves the box in
    ``queued`` with the pending clip loaded, ready for Play. This is how a
    coach lines up the next batter during the current walk-up without
    silencing the PA the moment the key is pressed — which is what cueing
    used to do.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        # On the pulse route _proc is pacat (its pid owns the sink-input,
        # and it outliving ffmpeg is the drain) and _feeder is the ffmpeg
        # decoding into it; elsewhere _feeder is None.
        self._feeder: subprocess.Popen | None = None
        self._queued: dict | None = None
        # A clip cued while another plays waits here; see ``queue``.
        self._pending: dict | None = None
        self._state = "stopped"          # stopped | queued | playing
        self._started_at: float | None = None
        self._volume = 80                # 0..100, applied as ffmpeg gain
        # Bumped on every spawn/stop. A fade ramp runs on its own thread and
        # checks this, so a walk-up cued mid-fade is never faded down.
        self._gen = 0
        # A fade parks the stream at 0% and the sound server remembers
        # that per application, so the next stream has to be reset. Starts
        # True: a 0% left behind by a crash or a previous run outlives the
        # process that set it, and the symptom is a silent walk-up.
        self._pulse_volume_dirty = True
        self.on_finish = None            # optional callable()

    # -- queue / play -----------------------------------------------------

    def _clip_file_missing(self, clip: dict) -> str | None:
        """The missing filename, or None when everything is on disk.

        Checked at queue AND play time: a file can vanish between the two
        (a sync pruning, an SD hiccup), and spawning ffmpeg on a missing
        input used to report success while playing nothing — the deck
        flashed as if the walk-up ran, and the operator heard silence with
        no clue why."""
        for key in ("file", "announcement"):
            name = clip.get(key)
            if name and not (MUSIC_DIR / name).exists():
                return str(name)
        return None

    def queue(self, clip: dict) -> None:
        missing = self._clip_file_missing(clip)
        if missing:
            raise MissingAudio(missing)
        with self._lock:
            if self._state == "playing" and self._proc is not None:
                # Something is on the PA: park this one instead of cutting
                # the song. Play brings it in; a natural end promotes it.
                self._pending = clip
                return
            self._stop_locked()
            self._queued = clip
            self._state = "queued"

    def play(self) -> bool:
        with self._lock:
            if self._pending is not None:
                self._queued, self._pending = self._pending, None
            if not self._queued:
                return False
            missing = self._clip_file_missing(self._queued)
            if missing:
                self._stop_locked()
                raise MissingAudio(missing)
            sink = self._pulse_sink()
            if sink and _PACAT:
                # Decode with ffmpeg, play with pacat: pacat drains, so
                # the clip is heard to its last sample no matter how far
                # ahead the server buffered. The short silence tail stays
                # even so — the Pi's firmware audio device has a FIFO of
                # its own below the sound server, and Bookworm's PipeWire
                # (0.3.65) has known early-drain bugs, so the last thing
                # any layer can drop at teardown must be silence, not
                # music. pacat's latency cap bounds how much that can be.
                cmd = self._build_command(self._queued, out_args=RAW_ARGS,
                                          pad_s=PIPE_PAD_S)
                self._spawn_pipeline(cmd, sink)
            else:
                cmd = self._build_command(self._queued)
                self._spawn(cmd)
            self._state = "playing"
            self._started_at = time.monotonic()
            return True

    def library_report(self, expected) -> dict:
        """How much of the library is actually on this Pi's disk —
        {'expected', 'present', 'missing': [first few names]}. This is what
        turns 'nothing plays and I don't know why' into '7 files missing,
        press Sync'."""
        missing = [n for n in expected if not (MUSIC_DIR / n).exists()]
        return {'expected': len(expected),
                'present': len(expected) - len(missing),
                'missing': missing[:8]}

    def _build_command(self, clip: dict, out_args: list[str] | None = None,
                       pad_s: float = TAIL_PAD_S) -> list[str]:
        """Compose the ffmpeg invocation for a clip.

        A clip is either a plain trimmed song, or a song mixed under an
        announcement that fades in at ``cue_ms``. ``out_args`` overrides
        the output target (raw-to-stdout for the pacat pipeline);
        ``pad_s`` is the silence tail for outputs that drop their buffer
        on exit — zero when pacat's drain makes it pointless.
        """
        gain = self._volume / 100.0
        song = str(MUSIC_DIR / clip["file"])
        start_s = (clip.get("start_ms") or 0) / 1000.0
        end_ms = clip.get("end_ms")
        fade_at, fade_s = _fade_start(clip, song, start_s)

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
            # The song sits on the delayed timeline, so its fade-out does too.
            fade_out = (f",afade=t=out:st={(cue_s + fade_at):.3f}:d={fade_s:.3f}"
                        if fade_s else "")
            pad = f",apad=pad_dur={pad_s}" if pad_s else ""
            filt = (
                f"[1:a]adelay={delay_ms}|{delay_ms},afade=t=in:st={cue_s:.3f}:d=0.75"
                f"{fade_out}[mus];"
                f"[0:a][mus]amix=inputs=2:duration=longest:dropout_transition=0,"
                f"volume={gain:.3f}{pad}[out]"
            )
            cmd += ["-filter_complex", filt, "-map", "[out]"]
        else:
            # Plain song: the clip's own fade-out (if the editor set one) plus
            # master volume. The live operator fade is the /fade endpoint.
            af = f"volume={gain:.3f}"
            if pad_s:
                af += f",apad=pad_dur={pad_s}"
            if fade_s:
                af = f"afade=t=out:st={fade_at:.3f}:d={fade_s:.3f},{af}"
            cmd += ["-af", af]

        cmd += out_args if out_args is not None else self._output_args()
        return cmd

    def _output_args(self) -> list[str]:
        """ffmpeg output target.

        Priority: explicit ONDECK_FFMPEG_OUT override (laptops/testing) →
        the connected Bluetooth speaker's PipeWire/Pulse sink → the default
        PipeWire/Pulse sink → ALSA default.

        The wired jack goes through pulse too, and that is the whole point:
        a fade can only be ramped on a stream the sound server can see, so
        an ffmpeg talking straight to ALSA had to be killed and relaunched
        to fade — which is what made Fade stutter on a PA plugged into the
        jack. Routing it through the same server the Bluetooth speaker uses
        makes ONE fade implementation cover both. ALSA stays as the
        fallback for a box with no sound server at all.
        """
        override = os.environ.get("ONDECK_FFMPEG_OUT")
        if override:
            return shlex.split(override)
        sink = self._pulse_sink()
        if sink:
            # Small buffer so the apad tail is guaranteed to cover what a
            # non-draining exit throws away; ffmpeg refills a 300 ms buffer
            # from a local file far faster than realtime, so no underruns.
            # (Playback itself goes through pacat when it exists — this
            # direct route remains for the fade fallback and status.)
            return ["-f", "pulse",
                    "-buffer_duration", str(PULSE_BUFFER_MS), sink]
        return ["-f", "alsa", "default"]

    def _pulse_sink(self) -> str | None:
        """The sink playback should target, or None off the pulse route."""
        if os.environ.get("ONDECK_FFMPEG_OUT"):
            return None
        sink = bt.current_sink() if bt else None
        return sink or _pulse_default_sink() or None

    # -- fade / stop ------------------------------------------------------

    def _pulse_sink_input(self, pid: int, tries: int = 1) -> str | None:
        """PulseAudio sink-input index for our ffmpeg process, or None.

        Matched on application.process.id — our own PID — so it can never
        grab another stream, and playback needs no extra ffmpeg flags.

        ``tries`` re-asks a few times, 60 ms apart: ffmpeg registers its
        stream a moment after the process exists, and a Fade pressed on
        the first beat of a walk-up used to miss that window and fall
        through to the stuttering relaunch path.
        """
        for attempt in range(max(1, tries)):
            if attempt:
                time.sleep(0.06)
            try:
                r = subprocess.run(["pactl", "list", "sink-inputs"],
                                   capture_output=True, text=True, timeout=5)
            except Exception:
                return None
            idx = None
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if line.startswith("Sink Input #"):
                    idx = line.split("#", 1)[1].strip()
                elif line.startswith("application.process.id") and idx:
                    if line.split("=", 1)[-1].strip().strip('"') == str(pid):
                        return idx
        return None

    def _restore_stream_volume(self, proc: subprocess.Popen) -> None:
        """Put a freshly started pulse stream back at 100%.

        A fade leaves its sink-input at 0% and the sound server REMEMBERS
        stream volumes per application: without this the walk-up after a
        faded one comes out of the PA silent, which is a far worse bug
        than the stutter this fade replaced. Runs off the play path, and
        only after a fade (or a restart, where the remembered 0% may have
        outlived the process that set it).
        """
        for _ in range(12):
            if proc.poll() is not None:
                return
            idx = self._pulse_sink_input(proc.pid)
            if idx:
                try:
                    subprocess.run(["pactl", "set-sink-input-volume", idx,
                                    "100%"], capture_output=True, timeout=3)
                except Exception:
                    pass
                return
            time.sleep(0.05)

    def _fade_via_pulse(self, idx: str, ms: int, gen: int) -> bool:
        """Ramp the LIVE stream down, then stop.

        Relaunching ffmpeg with an afade filter (what this used to do) is
        inaudible over Bluetooth: killing the process tears down the A2DP
        stream, and the sink re-opens with hundreds of milliseconds of
        latency — so the song stopped dead and the fade clip arrived late or
        not at all. Stepping the sink-input volume touches the stream that
        is already playing, so nothing restarts.
        """
        steps = max(4, min(40, int(ms / 50)))
        began = time.monotonic()
        # From here the sound server has a remembered volume for our
        # streams that is not 100%; the next spawn has to undo it.
        self._pulse_volume_dirty = True
        # The ramp starts at full: entering at (steps-1)/steps squared put an
        # instant step down before the first sample of the fade, which on a
        # short fade is most of the volume and reads as a blip.
        for i in range(steps, -1, -1):
            # Wait for this step's moment, then set it. Sleeping to a
            # deadline rather than for a fixed slice keeps the fade the
            # length that was asked for: each pactl call costs real
            # milliseconds on a Pi, and a 1s fade that ran 1.6s is half a
            # second of music nobody asked for.
            due = began + (ms / 1000.0) * (steps - i) / steps
            time.sleep(max(0.0, due - time.monotonic()))
            with self._lock:
                if gen != self._gen:
                    return True          # a new clip started — leave it alone
            # Perceptual-ish taper: linear volume sounds like it drops late.
            pct = int(round(100 * (i / steps) ** 2))
            try:
                subprocess.run(["pactl", "set-sink-input-volume", idx,
                                f"{pct}%"], capture_output=True, timeout=3)
            except Exception:
                # Half-faded and unable to finish: silence beats a song
                # stuck at 30% for the rest of the inning.
                with self._lock:
                    if gen == self._gen:
                        self._stop_locked()
                return False
        with self._lock:
            if gen == self._gen:
                self._finish_locked()
        return True

    def fade(self, ms: int = DEFAULT_FADE_MS) -> bool:
        """Fade the *currently playing* clip out over ``ms`` and stop.

        Preferred path ramps the live PulseAudio stream — which is now
        every route the Audio Pi has, Bluetooth speaker and wired jack
        alike (see ``_output_args``), because the relaunch fallback below
        is audibly wrong: the song stops, restarts a beat later, and only
        then fades. That fallback is kept for a box with no sound server,
        where doing nothing would be worse.
        """
        with self._lock:
            if self._state != "playing" or not self._queued:
                self._stop_locked()
                return False
            proc, gen = self._proc, self._gen
        if proc and proc.poll() is None:
            idx = self._pulse_sink_input(proc.pid, tries=3)
            if idx:
                threading.Thread(target=self._fade_via_pulse,
                                 args=(idx, ms, gen), daemon=True).start()
                return True
        with self._lock:
            if self._state != "playing" or not self._queued:
                self._stop_locked()
                return False
            pos_s = (time.monotonic() - (self._started_at or 0))
            clip = self._queued

            song = str(MUSIC_DIR / clip["file"])
            start_s = (clip.get("start_ms") or 0) / 1000.0
            dur_s = ms / 1000.0
            gain = self._volume / 100.0
            seek = start_s + pos_s

            def _cmd(sk):
                return [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-nostdin",
                    "-ss", f"{sk:.3f}", "-t", f"{dur_s:.3f}", "-i", song,
                    "-af", f"afade=t=out:st=0:d={dur_s:.3f},"
                           f"volume={gain:.3f},apad=pad_dur={TAIL_PAD_S}",
                ] + self._output_args()

            # Kill-then-spawn put a beat of dead air between the live song
            # and its fade clip, and the clip re-entered at the wall-clock
            # position — which the ear hears as "the music stops, starts
            # again, THEN fades". Start the fade rendition FIRST (seeked a
            # breath ahead to cover its own startup) and only then kill the
            # live process: on a shared/dmix device the two overlap for
            # ~120 ms and the song leans straight into the fade. On an
            # exclusive device the new process exits immediately and we
            # fall back to the historical kill-first order.
            pre = None
            try:
                pre = subprocess.Popen(
                    _cmd(seek + 0.18),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, start_new_session=True)
                time.sleep(0.12)
            except Exception:
                pre = None
            if pre is not None and pre.poll() is None:
                self._kill_proc()
                self._gen += 1
                self._proc = pre
                threading.Thread(target=self._watch,
                                 args=(pre, "stopped"),
                                 daemon=True).start()
            else:
                self._kill_proc()
                self._spawn(_cmd(seek), finish_state="stopped")
            # After the fade clip ends the watcher sets state to stopped; the
            # queue is intentionally cleared so nothing is left cued.
            self._queued = None
            return True

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        self._kill_proc()
        self._gen += 1                   # cancels any in-flight fade ramp
        self._state = "stopped"
        self._queued = None
        self._pending = None
        self._started_at = None

    def _finish_locked(self) -> None:
        """Playback is over of its own accord (or a fade reached silence).

        Unlike Stop this keeps what the coach cued in the meantime: a
        pending clip becomes the loaded one and the box rests in ``queued``
        so the next Play runs it. With nothing pending it is a plain stop.
        """
        if self._pending is None:
            self._stop_locked()
            return
        self._kill_proc()
        self._gen += 1
        self._queued, self._pending = self._pending, None
        self._state = "queued"
        self._started_at = None

    def set_volume(self, level: int) -> None:
        with self._lock:
            self._volume = max(0, min(100, int(level)))
            # Volume change applies to the next play; we don't restart a
            # running clip to avoid an audible glitch mid-walk-up.

    # -- process management ----------------------------------------------

    def _spawn(self, cmd: list[str], finish_state: str = "stopped") -> None:
        self._kill_proc()
        self._gen += 1
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._watch_and_restore(self._proc, finish_state,
                                is_pulse="pulse" in cmd)

    def _spawn_pipeline(self, ff_cmd: list[str], sink: str) -> None:
        """ffmpeg decodes to raw on stdout; pacat plays it and DRAINS.

        pacat is the tracked process: its pid is what the sink-input
        belongs to (so the live fade ramp and the volume restore keep
        working), and it exits only when the last sample has actually
        been played — the watcher's natural end is the audible end.
        """
        self._kill_proc()
        self._gen += 1
        feeder = subprocess.Popen(
            ff_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._proc = subprocess.Popen(
            [_PACAT, "--raw", "--format=s16le", f"--rate={RAW_RATE}",
             "--channels=2", "-d", sink, "--client-name=ondeck-audio",
             # Cap what the server may hold of the stream: everything
             # queued beyond the hardware is at risk when the stream
             # tears down, and this bounds "everything" to less than the
             # silence tail. ffmpeg refills 300 ms faster than realtime.
             f"--latency-msec={PULSE_BUFFER_MS}"],
            stdin=feeder.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # The parent's copy of the pipe must close so pacat sees EOF the
        # moment ffmpeg finishes.
        if feeder.stdout is not None:
            feeder.stdout.close()
        self._feeder = feeder
        self._watch_and_restore(self._proc, "stopped", is_pulse=True)

    def _watch_and_restore(self, proc: subprocess.Popen, finish_state: str,
                           is_pulse: bool) -> None:
        threading.Thread(target=self._watch, args=(proc, finish_state),
                         daemon=True).start()
        if self._pulse_volume_dirty and is_pulse:
            self._pulse_volume_dirty = False
            threading.Thread(target=self._restore_stream_volume,
                             args=(proc,), daemon=True).start()

    def _watch(self, proc: subprocess.Popen, finish_state: str) -> None:
        proc.wait()
        with self._lock:
            # Only react if this is still the active process (not superseded).
            if self._proc is not proc:
                return
            natural_end = self._state == "playing"
            self._started_at = None
            if self._pending is not None:
                # The coach cued the next clip during this one: load it
                # and rest, ready for Play. Not a stop — nothing the
                # lineup poller should advance past.
                self._queued, self._pending = self._pending, None
                self._state = "queued"
            else:
                self._state = finish_state
                if finish_state == "stopped":
                    self._queued = None
            cb = self.on_finish
        if natural_end and self._state == "stopped" and cb:
            # Lineup auto-advance hook. Fire outside the lock.
            try:
                cb()
            except Exception:
                pass

    def _kill_proc(self) -> None:
        for proc in (self._proc, self._feeder):
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self._proc = None
        self._feeder = None

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
                # What the deck lights: the clip the coach cued last. While
                # a clip plays with another parked behind it, that is the
                # parked one — the key just pressed must stay lit, and the
                # poller un-lights whatever "queued" does not name.
                "queued": self._pending or self._queued,
                "playing": self._queued if self._state == "playing" else None,
                "pending": self._pending,
            }


player = Player()


# -- HTTP routes ----------------------------------------------------------

@app.post("/queue")
def http_queue():
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("file"):
        return jsonify(error="file required"), 400
    try:
        player.queue(body)
    except MissingAudio as exc:
        return jsonify(ok=False, error=f"missing file: {exc}",
                       missing=str(exc), status=player.status())
    return jsonify(ok=True, status=player.status())


@app.post("/play")
def http_play():
    try:
        ok = player.play()
    except MissingAudio as exc:
        return jsonify(ok=False, error=f"missing file: {exc}",
                       missing=str(exc), status=player.status())
    if not ok:
        # The deck paints this string on the pressed key — a bare ok=false
        # showed as "Audio Pi said no", which explains nothing.
        return jsonify(ok=False, error="nothing cued — press a song first",
                       status=player.status())
    return jsonify(ok=True, status=player.status())


@app.post("/stop")
def http_stop():
    player.stop()
    return jsonify(ok=True, status=player.status())


@app.post("/fade")
def http_fade():
    body = request.get_json(force=True, silent=True) or {}
    ms = int(body.get("ms", DEFAULT_FADE_MS))
    ok = player.fade(ms)
    if not ok:
        return jsonify(ok=False, error="nothing playing",
                       status=player.status())
    return jsonify(ok=True, status=player.status())


@app.post("/volume")
def http_volume():
    body = request.get_json(force=True, silent=True) or {}
    if "level" not in body:
        return jsonify(error="level required"), 400
    player.set_volume(int(body["level"]))
    return jsonify(ok=True, status=player.status())


@app.get("/status")
def http_status():
    out = dict(player.status())
    # Which output route is live decides whether Fade can ramp the playing
    # stream or has to relaunch — the difference a coach hears — so a
    # support call can read it off /status instead of guessing.
    try:
        args = player._output_args()
        out['route'] = args[args.index('-f') + 1] if '-f' in args else 'custom'
        out['sink'] = args[-1] if len(args) > 2 else ''
        out['fade'] = 'live' if out['route'] == 'pulse' else 'relaunch'
        # Which process actually feeds the speaker — 'pacat' is the
        # draining pipeline, 'ffmpeg' the direct output whose exit can
        # eat a clip tail. The difference a cut-off horn turns on.
        out['player'] = ('pacat' if out['route'] == 'pulse' and _PACAT
                         else 'ffmpeg')
    except Exception:
        pass
    try:
        cfg = ConfigManager()
        expected = {s.get('filename') for s in cfg.songs.values()
                    if s.get('filename')}
        out['library'] = player.library_report(sorted(expected))
    except Exception:                    # a status probe must never 500
        pass
    return jsonify(out)


@app.post("/api/sync-now")
def http_sync_now():
    """Run this Pi's cloud sync immediately (called by the deck's Sync key)."""
    if _sync_now is None:
        return jsonify(ok=False, error="sync tooling unavailable")
    _sync_now.start()          # False = already running; that's still handled
    return jsonify(ok=True, running=True)


@app.get("/api/sync-status")
def http_sync_status():
    """{'running','ok','detail'} of the last /api/sync-now run."""
    if _sync_now is None:
        return jsonify(running=False, ok=False,
                       detail="sync tooling unavailable")
    s = _sync_now.status()
    return jsonify(running=s["running"], ok=s["ok"], detail=s["detail"])


@app.post("/upload")
def http_upload():
    if "file" not in request.files:
        return jsonify(error="file required"), 400
    f = request.files["file"]
    name = Path(f.filename or "").name
    if not name:
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


def _ensure_wired_volume() -> None:
    """Field-appliance rule: the built-in 3.5mm jack must never boot quiet.

    Desktop volume restore is best-effort and headless boots have no human
    to notice a 40% jack until the first walk-up whispers. Pin the wired
    sink to 100%/unmuted once the audio session is up (it appears a few
    seconds after boot, hence the patient retry loop) — gain staging
    belongs on the speaker's knob and OnDeck's own volume setting, not a
    forgotten desktop mixer. Bluetooth sinks are untouched."""
    for _ in range(30):
        try:
            r = subprocess.run(["pactl", "list", "short", "sinks"],
                               capture_output=True, text=True, timeout=5)
            wired = [line.split("\t")[1] for line in r.stdout.splitlines()
                     if "\t" in line and "alsa_output" in line.split("\t")[1]]
            if wired:
                for name in wired:
                    subprocess.run(["pactl", "set-sink-volume", name, "100%"],
                                   capture_output=True, timeout=5)
                    subprocess.run(["pactl", "set-sink-mute", name, "0"],
                                   capture_output=True, timeout=5)
                log.info("Wired sink(s) pinned to 100%%: %s", ", ".join(wired))
                return
        except Exception as exc:
            log.debug("wired-volume check: %s", exc)
        time.sleep(2)
    log.warning("No wired ALSA sink appeared — jack volume not pinned")


def _audio_status_rows():
    """Audio-Pi rows for the shared /status page.

    "Which speaker is this coming out of" and "can Fade ramp it" are the
    two questions a silent or stuttering PA raises, and both are answered
    by the output route. ONDECK_AUDIO_SINK pins the sink when the server's
    default is not the socket the PA is in.
    """
    args = player._output_args()
    route = args[args.index("-f") + 1] if "-f" in args else "custom"
    sink = args[-1] if len(args) > 2 else ""
    return [
        ("Audio out", f"{route} — {sink}" if sink else route),
        ("Fade", "eases down (live)" if route == "pulse"
                 else "relaunch — no sound server"),
        ("Player", "pacat (drains the tail)"
                   if route == "pulse" and _PACAT else "ffmpeg direct"),
    ]


def main() -> None:
    port = int(os.environ.get("ONDECK_AUDIO_PORT", "5100"))
    threading.Thread(target=_ensure_wired_volume, daemon=True).start()
    # Cloud-link + Wi-Fi pages (same ones the deck portal uses) so the Audio Pi
    # can be linked/managed from a browser on the field Wi-Fi without SSH.
    try:
        from pi.web_routes import register as register_pi_routes
        register_pi_routes(app, extra_rows=_audio_status_rows)
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
