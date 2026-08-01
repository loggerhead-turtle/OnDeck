"""Watch the cloud for lineup changes — so the deck can *say* a
substitution happened, without acting on it.

Why a watcher and not a push:

  * The game runs offline. Everything the deck needs is already on this Pi,
    and the 5-minute timer keeps it that way. Making the batting order
    depend on the dugout uplink staying up is how you get a dead deck in
    the fifth inning.
  * A deck that repaints by itself moves a key out from under the media
    person's finger between deciding and pressing. The result is the wrong
    kid's walk-up music, in front of everyone.

So this only ever sets a flag. The operator presses Sync when they are
ready, between batters.

THREE states, and the third is the whole point:

  fresh    checked recently, order unchanged
  changed  the cloud has a different batting order — press Sync
  unknown  we cannot reach the cloud, so we genuinely do not know

A two-state design would render 'unknown' as 'fresh', which reads to the
operator as "no substitutions have happened". That is a lie, and a worse
failure than not having the feature: it manufactures confidence exactly
when the network is least trustworthy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request

from netconfig import read_sync_env

log = logging.getLogger('ondeck.lineup_watch')

FRESH, CHANGED, UNKNOWN = 'fresh', 'changed', 'unknown'

# Short: this runs on the render thread's timescale, and a captive portal
# that accepts a connection then never answers must not wedge the deck.
TIMEOUT = 3.0

DEFAULT_POLL = 30.0
DEFAULT_JITTER = 10.0
# A flapping field uplink shouldn't turn into a retry storm.
BACKOFF_MAX = 300.0
# How long a successful check stays trustworthy. Past this we admit we are
# out of date rather than showing a stale 'fresh'.
STALE_AFTER = 180.0

_lock = threading.Lock()
_state = {
    'state': UNKNOWN,
    'version': None,      # last version the cloud reported
    'baseline': None,     # what this Pi last synced to
    'checked_at': 0.0,    # last SUCCESSFUL check
    'detail': 'Not checked yet',
}
_stop = threading.Event()


def status() -> dict:
    """{'state', 'version', 'baseline', 'checked_at', 'detail'}.

    Safe from any thread. 'state' is recomputed on read so a watcher that
    dies, or a network that went away, decays to 'unknown' instead of
    leaving a stale 'fresh' on the key forever.
    """
    with _lock:
        s = dict(_state)
    if s['state'] != UNKNOWN and time.time() - s['checked_at'] > STALE_AFTER:
        s['state'] = UNKNOWN
        s['detail'] = 'No answer from the cloud recently'
    return s


def _url(env) -> str | None:
    base = (env.get('ONDECK_CLOUD_URL') or '').rstrip('/')
    return f'{base}/sync/lineup-version' if base else None


def _fetch(env, opener=None):
    """(version, poll, jitter). Raises on any failure — the caller decides
    what a failure means."""
    url = _url(env)
    if not url:
        raise RuntimeError('No ONDECK_CLOUD_URL set')
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + (env.get('ONDECK_SYNC_TOKEN') or ''),
        'Accept': 'application/json'})
    with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT) as r:
        body = json.loads(r.read().decode('utf-8'))
    return (body.get('version'),
            float(body.get('poll') or DEFAULT_POLL),
            float(body.get('jitter') or DEFAULT_JITTER))


def local_fingerprint(lineup) -> str:
    """The same hash the cloud computes, over the lineup this Pi holds.

    Computing it locally rather than remembering the last value we were
    told means a Pi that syncs by the 5-minute timer, or by someone
    pressing Sync in the portal, re-baselines correctly without this
    module being told about it.
    """
    raw = '|'.join('' if p is None else str(p) for p in (lineup or []))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def note_local_lineup(lineup) -> None:
    """Tell the watcher what this Pi is currently playing from."""
    fp = local_fingerprint(lineup)
    with _lock:
        _state['baseline'] = fp
        if _state['version'] is not None:
            _state['state'] = FRESH if _state['version'] == fp else CHANGED


def _record_ok(version) -> None:
    with _lock:
        _state['version'] = version
        _state['checked_at'] = time.time()
        base = _state['baseline']
        if base is None:
            # Nothing to compare against yet — knowing the cloud's answer
            # is not the same as knowing we match it.
            _state['state'] = UNKNOWN
            _state['detail'] = 'Waiting for the local lineup'
        elif version == base:
            _state['state'] = FRESH
            _state['detail'] = 'Up to date'
        else:
            _state['state'] = CHANGED
            _state['detail'] = 'Lineup changed — press Sync'


def _record_fail(detail: str) -> None:
    with _lock:
        _state['state'] = UNKNOWN
        _state['detail'] = detail


def check_once(env=None, opener=None) -> dict:
    """One poll. Never raises — a watcher that can die is a watcher that
    silently stops telling the truth."""
    try:
        version, poll, jitter = _fetch(env or read_sync_env() or {}, opener)
        _record_ok(version)
        return {'ok': True, 'poll': poll, 'jitter': jitter}
    except urllib.error.HTTPError as exc:
        _record_fail(f'Cloud said {exc.code}')
    except Exception as exc:                       # timeout, DNS, no route
        _record_fail(f'Cannot reach the cloud: {type(exc).__name__}')
    return {'ok': False, 'poll': DEFAULT_POLL, 'jitter': DEFAULT_JITTER}


def _sleep_for(poll, jitter, fails, rand=None) -> float:
    """Jittered interval, backing off while the uplink is down.

    The jitter is not cosmetic: a hundred Pis that powered up together on a
    tournament morning would otherwise stay aligned forever and arrive in
    the same instant against a 20-connection pool.
    """
    if fails:
        poll = min(poll * (2 ** min(fails, 6)), BACKOFF_MAX)
    r = (rand or random.random)()
    return max(1.0, poll + (r * 2 - 1) * jitter)


def run(on_change=None, env=None, opener=None, sleeper=None) -> None:
    """Poll loop. Calls on_change() when the state changes, so the deck can
    repaint the key — and only the key."""
    fails = 0
    last = None
    while not _stop.is_set():
        r = check_once(env, opener)
        fails = 0 if r['ok'] else fails + 1
        now = status()['state']
        if now != last:
            last = now
            if on_change:
                try:
                    on_change(now)
                except Exception:
                    log.exception('lineup-watch on_change failed')
        (sleeper or _stop.wait)(_sleep_for(r['poll'], r['jitter'], fails))


def start(on_change=None) -> threading.Thread:
    _stop.clear()
    t = threading.Thread(target=run, args=(on_change,), daemon=True,
                         name='lineup-watch')
    t.start()
    return t


def stop() -> None:
    _stop.set()
