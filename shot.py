#!/usr/bin/env python3
"""Headless screenshot helper for visually verifying the web UI.

Usage:
    venv/bin/python shot.py <path> [out.png] [--width N] [--height N] [--full]
    venv/bin/python shot.py /            /tmp/index.png
    venv/bin/python shot.py /journal     /tmp/journal.png --width 390   # phone
    venv/bin/python shot.py /            /tmp/full.png --full           # full page

Loads the server URL + <path>, waits for network idle + the live-search /
overflow JS to settle, then writes a PNG. Default viewport is desktop (1280)
unless --width is given. This is the "eyes" — run it after a change to SEE the
result instead of guessing.

The target host/port match the server's convention: override with the
CCDK_HOST / CCDK_PORT env vars (defaults 127.0.0.1:5005).
"""
from __future__ import annotations

import os
import sys
from playwright.sync_api import sync_playwright

_HOST = os.environ.get("CCDK_HOST", "127.0.0.1")
_PORT = os.environ.get("CCDK_PORT", "5005")
BASE = f"http://{_HOST}:{_PORT}"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}

    path = args[0] if args else "/"
    out = args[1] if len(args) > 1 else "/tmp/shot.png"

    def opt(name: str, default: int) -> int:
        for a in sys.argv[1:]:
            if a.startswith(f"--{name}="):
                return int(a.split("=", 1)[1])
        return default

    width = opt("width", 1280)
    height = opt("height", 900)
    full = "--full" in flags

    url = BASE + path
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=2,
            user_agent="Mozilla/5.0 (shotbot)",
        ).new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        # Let DOMContentLoaded handlers (tag collapse, tier dots) settle.
        page.wait_for_timeout(400)
        page.screenshot(path=out, full_page=full)
        browser.close()
    print(f"wrote {out}  ({path} @ {width}x{height}{' full' if full else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
