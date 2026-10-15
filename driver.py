#!/usr/bin/env python3
"""Persistent browser-automation driver for the browser-driver skill.

A tiny TCP server that keeps ONE Playwright session alive across commands, so
an agent can drive a real browser (Brave/Chrome/Chromium) step by step.

    python3 driver.py [--port 8765] [--host 127.0.0.1] [--log /path/driver.log]

Protocol: open a TCP connection, send one JSON command per line, read one JSON
result per line. Every connection is a fresh REPL over the SAME browser session.

    {"fn": "goto_and_wait", "kwargs": {"url": "https://www.canva.com/", "settle": 8}}
    {"fn": "screenshot", "kwargs": {"path": "/tmp/opencode/shot.png"}}
    {"fn": "mouse", "kwargs": {"x": 513, "y": 637, "clicks": 2}}
    {"fn": "type", "kwargs": {"text": "Sofía"}}

Anything exported by this skill's browser_automation.py also works verbatim
(browser_goto, browser_read_page, browser_click, browser_fill,
browser_list_interactive_elements, browser_press_key, browser_close, ...).

Threading contract (why this file looks the way it does): Playwright's sync API
must run on exactly one thread, and the bundled module owns that thread in its
its single-worker `_executor`. Tool functions hop onto it internally; the raw
helpers below must be submitted to it explicitly, which is why this server is
threaded for I/O but funnels every Playwright call through `ba._executor`.
"""

import argparse
import json
import logging
import os
import socketserver
import sys

# Self-contained: this skill ships its own browser_automation.py, so no other
# project on the machine is required. Put the skill dir on sys.path and import
# the local module, so settings come from settings.json next to this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_automation as ba  # noqa: E402

log = logging.getLogger("driver")
HELPERS = {}


def helper(name):
    def deco(fn):
        HELPERS[name] = fn
        return fn
    return deco


# ---------------------------------------------------------------------------
# Helpers (raw Playwright access — always dispatched via ba._executor)
# ---------------------------------------------------------------------------

@helper("screenshot")
def _screenshot(path="/tmp/opencode/shot.png", full_page=False):
    page = ba._get_page()
    page.screenshot(path=path, full_page=full_page, timeout=20000, animations="disabled")
    return f"saved {path} (url={page.url})"


@helper("state")
def _state():
    page = ba._get_page()
    return json.dumps({"url": page.url, "title": page.title()})


@helper("eval")
def _eval(expression):
    """Run JS in the page. `expression` must be a function expression, e.g. "() => document.title"."""
    page = ba._get_page()
    return json.dumps(page.evaluate(expression))


@helper("goto_and_wait")
def _goto_and_wait(url, settle=3):
    page = ba._get_page()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(int(settle) * 1000)
    return f"navigated to {page.url} (title: {page.title()})"


@helper("wait_for")
def _wait_for(selector, timeout=15):
    page = ba._get_page()
    page.wait_for_selector(selector, timeout=int(timeout) * 1000)
    return f"visible: {selector}"


@helper("mouse")
def _mouse(x, y, clicks=1, pause=400):
    """Left-click at viewport coordinates. clicks=2 double-clicks (edit text in Canva)."""
    page = ba._get_page()
    page.mouse.click(float(x), float(y), click_count=int(clicks), delay=120)
    page.wait_for_timeout(int(pause))
    return f"clicked ({x},{y}) x{clicks}"


@helper("type")
def _type(text, delay=25):
    """Type into the focused element. "\n" presses Enter (new line in a text box)."""
    page = ba._get_page()
    page.keyboard.type(text, delay=int(delay))
    page.wait_for_timeout(300)
    return f"typed {text!r}"


@helper("key")
def _key(key):
    """Press one key: Enter, Escape, Tab, Control+a, Control+z, Backspace..."""
    page = ba._get_page()
    page.keyboard.press(key)
    page.wait_for_timeout(300)
    return f"pressed {key}"


@helper("drag")
def _drag(x1, y1, x2, y2, steps=25):
    """Drag from (x1,y1) to (x2,y2) — move elements on a canvas editor."""
    page = ba._get_page()
    page.mouse.move(float(x1), float(y1))
    page.mouse.down()
    page.mouse.move(float(x2), float(y2), steps=int(steps))
    page.mouse.up()
    page.wait_for_timeout(400)
    return f"dragged ({x1},{y1}) -> ({x2},{y2})"


@helper("wheel")
def _wheel(x, y, delta):
    """Scroll with the mouse wheel at (x,y). delta>0 scrolls down."""
    page = ba._get_page()
    page.mouse.move(float(x), float(y))
    page.mouse.wheel(0, float(delta))
    page.wait_for_timeout(400)
    return f"scrolled {delta} at ({x},{y})"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        log.info("client connected")
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            log.info("recv %s", line[:200])
            try:
                call = json.loads(line)
                name = call["fn"]
                kwargs = call.get("kwargs") or {}
                if name in HELPERS:
                    # One thread owns Playwright (the module's _executor); ba.call
                    # adds the automatic reconnect if the browser died mid-task.
                    out = ba._executor.submit(ba.call, HELPERS[name], **kwargs).result()
                else:
                    # Tools already dispatch to _executor internally (and reconnect).
                    out = getattr(ba, name)(**kwargs)
            except AttributeError:
                out = f"EXC: unknown function {name!r}. Helpers: {sorted(HELPERS)}"
            except Exception as e:  # noqa: BLE001
                out = f"EXC in {line[:80]}: {type(e).__name__}: {e}"
            log.info("send %s", str(out)[:200])
            self.wfile.write((json.dumps(out) + "\n").encode("utf-8"))
            self.wfile.flush()
        log.info("client disconnected")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--log", default="/tmp/opencode/driver.log")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(args.log), logging.StreamHandler(sys.stderr)],
    )
    with Server((args.host, args.port), Handler) as srv:
        log.info("listening on %s:%s (helpers: %s)", args.host, args.port, ", ".join(sorted(HELPERS)))
        srv.serve_forever()


if __name__ == "__main__":
    main()
