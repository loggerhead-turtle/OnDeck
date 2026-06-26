#!/usr/bin/env python3
"""Add or remove a Wi-Fi network in wpa_supplicant.conf.

Called via sudo from the web portal (service user → root). Reads JSON from stdin:
    {"ssid": "NetworkName", "password": "secret", "action": "add"}
    {"ssid": "NetworkName", "action": "remove"}

action defaults to "add". Adding an SSID that already exists REPLACES it (so
"change the password" just works), and networks are never duplicated.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

WPA_CONF = Path("/etc/wpa_supplicant/wpa_supplicant.conf")
_HEADER = ("ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
           "update_config=1\ncountry=US\n")


def _split_blocks(text: str):
    """Return (header, [(ssid, block_text), ...])."""
    pattern = re.compile(r"network\s*=\s*\{.*?\}", re.DOTALL)
    header = pattern.sub("", text).strip() + "\n"
    blocks = []
    for m in pattern.finditer(text):
        block = m.group(0)
        sm = re.search(r'ssid\s*=\s*"([^"]*)"', block)
        blocks.append((sm.group(1) if sm else "", block))
    return header, blocks


def _block(ssid: str, password: str) -> str:
    esc_ssid = ssid.replace('"', '\\"')
    if password:
        esc_pass = password.replace('"', '\\"')
        return ('network={\n'
                f'    ssid="{esc_ssid}"\n'
                f'    psk="{esc_pass}"\n'
                '    key_mgmt=WPA-PSK\n'
                '}\n')
    return ('network={\n'
            f'    ssid="{esc_ssid}"\n'
            '    key_mgmt=NONE\n'
            '}\n')


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
        ssid = (data.get("ssid") or "").strip()
        password = data.get("password", "")
        action = (data.get("action") or "add").strip().lower()
    except Exception as exc:
        print(f"Error reading input: {exc}", file=sys.stderr)
        sys.exit(1)

    if not ssid:
        print("ssid is required", file=sys.stderr)
        sys.exit(1)

    if WPA_CONF.exists():
        header, blocks = _split_blocks(WPA_CONF.read_text())
        if not header.strip():
            header = _HEADER
    else:
        WPA_CONF.parent.mkdir(parents=True, exist_ok=True)
        header, blocks = _HEADER, []

    # Drop any existing block for this SSID (dedupe / edit / remove).
    blocks = [(s, b) for (s, b) in blocks if s != ssid]

    text = header.rstrip() + "\n"
    for _s, b in blocks:
        text += "\n" + b.strip() + "\n"
    if action != "remove":
        text += "\n" + _block(ssid, password)

    WPA_CONF.write_text(text)
    WPA_CONF.chmod(0o640)
    print(f"{'Removed' if action == 'remove' else 'Saved'} network: {ssid}")


if __name__ == "__main__":
    main()
