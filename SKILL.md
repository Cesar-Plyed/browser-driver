---
name: browser-driver
description: >
  Persistent, self-contained Playwright driver for controlling a real
  desktop browser (Brave/Chrome/Chromium) on this machine. Use this
  whenever the user asks to open a web page, browse, click, type or read
  pages in the browser, or to do something on a website — especially
  visual/web-app work such as Canva (creating designs, invitations,
  publishing sites, editing text on canvas editors), signing into sites,
  filling forms, or navigating logged-in pages. Also use it when a task
  needs to survive across many steps: the driver keeps ONE browser
  session alive behind a TCP server, unlike one-shot scripts. It has no
  dependency on any other project on this machine.
---

# Browser driver

A TCP server that holds a single Playwright session attached to the user's
real browser, so you can drive it step by step from the shell. The skill
ships its own copy of `browser_automation.py` (attach mode: Brave on
  `--remote-debugging-port=9222`, profile `~/.browser-driver-profile`).


## Files in this skill

- `browser_automation.py` — standalone automation module (tools + browser
  autostart). No imports from any other project; settings come from
  `settings.json` beside it or `BA_*` env vars.
- `driver.py` — the TCP server (helpers + every function exported by `browser_automation`)
- `browser-driver.sh` — start / stop / status / send wrapper
- `settings.json` — skill-owned config (debug port, profile, mode, autostart)

## Settings

Read in this order, later wins: defaults → `settings.json` → `BA_*` env vars
(e.g. `BA_BROWSER_DEBUG_PORT=9333`, `BA_BROWSER_MODE=launch`,
`BA_ENABLE_BROWSER_AUTOMATION=false`, `BA_SETTINGS_FILE=/other.json`).

| key | default | meaning |
| --- | --- | --- |
| `enable_browser_automation` | `true` | master switch, re-read on every call |
| `browser_mode` | `"attach"` | `"attach"` to the debug browser, or `"launch"` Playwright's own |
| `browser_autostart` | `true` | start the debug browser if it isn't running |
| `browser_debug_port` | `9222` | CDP port |
| `browser_debug_profile` | `~/.browser-driver-profile` | dedicated profile (logins persist here) |
| `browser_executable` | `""` (auto) | e.g. `"brave-browser"` or a full path |
| `require_submit_confirmation` | `true` | block submit-looking clicks without `confirmed=true` |

## Quick start

```bash
D=~/.config/opencode/skills/browser-driver/browser-driver.sh
"$D" start                       # starts server on 127.0.0.1:8765; auto-starts the debug browser too
"$D" send '{"fn":"goto_and_wait","kwargs":{"url":"https://www.canva.com/","settle":8}}' 120
"$D" send '{"fn":"screenshot","kwargs":{"path":"/tmp/opencode/s.png"}}'
"$D" send '{"fn":"state","kwargs":{}}'
"$D" stop                        # stops the driver only; the browser window stays open
```

Each `send` opens a fresh connection over the same live session, so you can
chain dozens of steps without losing page state, scroll position or logins.

## Commands

Send one JSON object per call: `{"fn": "<name>", "kwargs": {...}}`.

Helpers (defined in `driver.py`):

| fn | kwargs | what it does |
| --- | --- | --- |
| `state` | — | current URL + title as JSON |
| `screenshot` | `path`, `full_page` (default false) | PNG of the viewport; `animations="disabled"` so canvas apps stop shimmering |
| `goto_and_wait` | `url`, `settle` (seconds to wait after load) | navigate; handles bare domains |
| `eval` | `expression` (a JS function string, e.g. `"() => document.title"`) | run JS in the page, returns JSON |
| `wait_for` | `selector`, `timeout` | block until something appears |
| `mouse` | `x`, `y`, `clicks` (1 or 2), `pause` | click / double-click at viewport coords |
| `type` | `text`, `delay` | type into focused element; `\n` makes a new line |
| `key` | `key` | one key: `Enter`, `Escape`, `Tab`, `Control+a`, `Control+z`, `Backspace` |
| `drag` | `x1`, `y1`, `x2`, `y2`, `steps` | drag an element across a canvas |
| `wheel` | `x`, `y`, `delta` | scroll at a point |

Anything in the skill's own `browser_automation.py` also works directly:
`browser_goto`, `browser_read_page`, `browser_list_interactive_elements`,
`browser_click`, `browser_fill` (selector = CSS / `text=` / label / placeholder),
`browser_set_checkbox`, `browser_select_option`, `browser_press_key`,
`browser_close`. Note `browser_click` blocks submit/apply-looking buttons
unless you pass `"confirmed": true` (set `require_submit_confirmation` to
`false` in `settings.json` to disable that guard).

## Operational rules (learned the hard way)

1. **Never drive it from a one-shot script.** Playwright state lives in the
   server process; a new process opens a new tab and forgets everything.
   The old stdin/FIFO version died the moment the writer closed — that is why
   this is a TCP server.
2. **Never `pkill -f driver.py`.** Your own shell's command line contains that
   string, so you kill your own session. Kill by port instead:
   `kill $(ss -lptnH 'sport = :8765' | grep -oP 'pid=\K[0-9]+' | head -1)`
   (`browser-driver.sh stop` already does this).
3. **All Playwright calls run on one thread** — the module's single-worker
   `_executor`. Helpers must be submitted to it (`ba._executor.submit(...)`),
   otherwise Playwright raises `cannot switch to a different thread`. That
   error means the routing broke.
4. **Screenshot pixels == viewport CSS pixels** (devicePixelRatio 1 here), so
   you can click exactly what you see: read the PNG, then click its
   coordinates. Re-screenshot after every destructive step; layouts shift.
5. **Use `python3` (system Python),** where Playwright is installed
   (`~/.local/lib/python3.14`) with browsers in `~/.cache/ms-playwright`.
   If you manage Playwright in a venv instead, point `browser-driver.sh`'s
   interpreter at it.
6. Canva/Brave live on the desktop (`DISPLAY=:1`, KDE on Wayland). A window
   left open is fine; closing the driver never closes the browser.

## Recipe: editing a Canva design

Canva renders text on a canvas — no DOM to select. Drive it visually:

1. Search templates: fill the box labelled `Busca millones de plantillas`,
   press `Enter`. Results are `a[href*="/templates/"]`; grab hrefs+coords with
   `eval` instead of guessing.
2. **Open the editor in the same tab.** On a template page, find the
   "Personalizar la plantilla" anchor and navigate to its `href`
   (`/design/editor/shell?create&type=...&template=...`) with `goto_and_wait`.
   Clicking it would open a popup tab you cannot reach.
3. Pick a **static** template (video templates carry a play/sound icon and
   cannot be text-edited).
4. Per text box: **single click** to select (the top toolbar then shows font
   + size, which tells you what you actually hit) → `Enter` to edit →
   `Control+a` → `type` → `Escape`. Always verify with a screenshot before
   typing the next box.
5. Dates like `DOM | 26 | OCT` are usually **three separate text elements**
   (plus divider graphics). Edit each one alone; replacing the whole string in
   one box wraps and wrecks the layout. If that happens, `Control+z` a few
   times — but note undo also reverts earlier good edits.
6. Rename: `Archivo` menu (top-left, ~x=105,y=28) → pencil icon (~x=349,y=84)
   → `Control+a` → type → `Enter`.
7. Autosave is confirmed in the same menu: "Guardar — Se guardaron los cambios".

## Troubleshooting

- `No browser is listening on 127.0.0.1:9222` → autostart is on by default,
  so the driver starts Brave itself. If Brave is already open **with that same
  profile**, close it first. Wrong port or profile? Fix it in the skill's
  `settings.json`.
- `cannot switch to a different thread` → a helper bypassed `ba._executor`.
- Browser closed while the driver was connected → no action needed: the next
  call detects the dead connection, auto-starts Brave and retries once
  (`ba.call`). If you still see `TargetClosedError`, run `browser-driver.sh
  restart`.
- `EXC: unknown function` → typo in `fn`; helpers are listed in the error.
- Stale tabs pile up in the debug browser → `{"fn":"browser_close","kwargs":{}}`
  then reconnect; logins persist in `~/.browser-driver-profile`.
