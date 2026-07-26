"""Sync on demand — the "pull it from the cloud NOW" button.

The systemd timer polls every 5 minutes, which is right for battery and
bandwidth but wrong for a person standing at the deck who just changed a
walk-up song, or a coach who just made a substitution. This runs the same
``sync_agent.py`` immediately, from the portal or from a Stream Deck key.

Runs it as a subprocess rather than importing it: sync_agent reads its
cloud URL and token from the environment, which the systemd unit supplies
via sync.env and the portal process does not have. We load the same file
through netconfig, so both callers behave identically to the timer.

One sync at a time (a second press while one is running is a no-op), and
the last result is kept so the UI can show what happened.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from netconfig import read_sync_env

SYNC_AGENT = Path(__file__).resolve().parent.parent / 'sync_agent.py'
TIMEOUT = 180          # generous: a first sync downloads every song

_lock = threading.Lock()
_state = {'running': False, 'ok': None, 'at': 0.0, 'detail': ''}


def status() -> dict:
    """{'running', 'ok', 'at', 'detail'} — safe to call from any thread."""
    with _lock:
        return dict(_state)


def _run(runner):
    env = dict(os.environ)
    env.update({k: v for k, v in (read_sync_env() or {}).items() if v})
    ok, detail = False, ''
    try:
        r = runner([sys.executable, str(SYNC_AGENT)], env)
        ok = r.returncode == 0
        out = ((r.stdout or '') + (r.stderr or '')).strip().splitlines()
        detail = out[-1][:200] if out else ('Synced.' if ok else 'Sync failed.')
    except subprocess.TimeoutExpired:
        detail = f'Timed out after {TIMEOUT}s — still downloading?'
    except Exception as exc:
        detail = f'Could not run the sync: {exc}'
    with _lock:
        _state.update(running=False, ok=ok, at=time.time(), detail=detail)


def _default_runner(cmd, env):
    return subprocess.run(cmd, env=env, capture_output=True, text=True,
                          timeout=TIMEOUT)


def start(runner=None, background=True) -> bool:
    """Kick off a sync. False when one is already running.

    `runner`/`background` exist for tests — the real callers use neither.
    """
    with _lock:
        if _state['running']:
            return False
        _state.update(running=True, detail='Syncing…')
    target = runner or _default_runner
    if background:
        threading.Thread(target=_run, args=(target,), daemon=True).start()
    else:
        _run(target)
    return True


def summary() -> str:
    """One short line for a status page or a deck key."""
    s = status()
    if s['running']:
        return 'Syncing…'
    if s['ok'] is None:
        return 'Not synced yet this boot'
    when = time.strftime('%H:%M', time.localtime(s['at'])) if s['at'] else ''
    return ('Synced ' + when) if s['ok'] else ('Failed ' + when)
