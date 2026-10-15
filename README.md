# browser-driver

A persistent, self-contained [Playwright](https://playwright.dev/) driver that controls a real desktop browser (Brave/Chrome/Chromium) on Linux. It exposes a TCP server that keeps **one browser session alive** across many commands, so you can chain dozens of steps without losing page state, scroll position, or logins.

## When to use it

- Opening web pages, browsing, clicking, typing, or reading page content
- Visual/web-app work (Canva, canvas editors, etc.)
- Signing into sites, filling forms, navigating logged-in pages
- Any task that needs to survive across many steps

## How it works

The skill ships three components:

| File | Purpose |
|---|---|
| `browser_automation.py` | Standalone automation module (browser tools + autostart) |
| `driver.py` | TCP server — keeps one Playwright session alive on a single thread |
| `browser-driver.sh` | Start / stop / status / send wrapper |

The driver listens on `127.0.0.1:8765`. Each `send` opens a fresh connection over the same live session, so state persists between calls.

## Quick start

```bash
D=~/.config/opencode/skills/browser-driver/browser-driver.sh

"$D" start                       # starts server + auto-starts the debug browser
"$D" send '{"fn":"goto_and_wait","kwargs":{"url":"https://example.com","settle":8}}' 120
"$D" send '{"fn":"screenshot","kwargs":{"path":"/tmp/opencode/screenshot.png"}}'
"$D" send '{"fn":"state","kwargs":{}}'
"$D" stop                        # stops the driver; browser window stays open
```

## Settings

Read in this order (later wins): defaults → `settings.json` → `BA_*` env vars.

| Key | Default | Meaning |
|---|---|---|
| `enable_browser_automation` | `true` | Master on/off switch, re-read on every call |
| `browser_mode` | `"attach"` | Attach to debug browser, or `"launch"` Playwright's own |
| `browser_autostart` | `true` | Start the debug browser if not running |
| `browser_debug_port` | `9222` | CDP port |
| `browser_debug_profile` | `~/.browser-driver-profile` | Dedicated profile (logins persist here) |
| `browser_executable` | `""` (auto) | e.g. `"brave-browser"` or a full path |
| `require_submit_confirmation` | `true` | Block submit-looking clicks without `confirmed=true` |

## Commands

Send one JSON object per call: `{"fn": "<name>", "kwargs": {...}}`.

### Helpers (defined in `driver.py`)

| fn | kwargs | Description |
|---|---|---|
| `state` | — | Current URL + title as JSON |
| `screenshot` | `path`, `full_page` (default false) | PNG of the viewport |
| `goto_and_wait` | `url`, `settle` (seconds) | Navigate to a URL |
| `eval` | `expression` (JS function string) | Run JS in the page, returns JSON |
| `wait_for` | `selector`, `timeout` | Block until an element appears |
| `mouse` | `x`, `y`, `clicks` (1 or 2), `pause` | Click / double-click at viewport coords |
| `type` | `text`, `delay` | Type into focused element; `\n` = new line |
| `key` | `key` | Press a key: `Enter`, `Escape`, `Tab`, `Control+a`, etc. |
| `drag` | `x1`, `y1`, `x2`, `y2`, `steps` | Drag across a canvas |
| `wheel` | `x`, `y`, `delta` | Scroll at a point |

### Browser automation tools (from `browser_automation.py`)

| fn | Description |
|---|---|
| `browser_goto` | Navigate to a URL |
| `browser_read_page` | Return visible text of current page |
| `browser_list_interactive_elements` | List buttons, links, inputs, etc. |
| `browser_click` | Click by selector or text (blocks submit unless `confirmed=true`) |
| `browser_fill` | Type into an input/textarea |
| `browser_set_checkbox` | Check/uncheck a checkbox |
| `browser_select_option` | Select from a dropdown |
| `browser_press_key` | Press a key on the focused element |
| `browser_close` | Close the agent's tab/session |

## Requirements

- Python 3 (system Python where Playwright is installed)
- Playwright with Chromium browsers (`pip install playwright && playwright install chromium`)
- A Chromium-based browser (Brave, Chrome, Chromium, or Edge)

## License

MIT License (MIT) — see [LICENSE](LICENSE) for details.