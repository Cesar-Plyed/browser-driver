"""Standalone browser automation via Playwright (Linux / Fedora friendly).

Two modes, chosen with the "browser_mode" setting:

- "attach" (default): connect over CDP to a Chromium-based browser (Brave,
  Chrome, Chromium) running with --remote-debugging-port and a dedicated
  --user-data-dir. If that browser is not running yet, it is started
  automatically with the right flags, so nothing has to be launched by hand.
  The agent works in its own new tab, so your other tabs are never touched.
- "launch": Playwright starts its own Chromium with a persistent profile
  stored in ./job_search_profile (log in once, session is kept).

Settings (all optional, defaults in parentheses):
- enable_browser_automation   (True)   master on/off switch, re-read on every call
- browser_mode                ("attach")
- browser_autostart           (True)   start the debug browser if it isn't running
- browser_debug_port          (9222)
- browser_debug_profile       (~/.browser-driver-profile)
- browser_executable          (auto)   e.g. "brave-browser" or a full path
- require_submit_confirmation (True)   blocks clicks on apply/submit-looking
                                       buttons unless the call passes confirmed=true

Where settings come from, highest priority first:
1. Environment variables prefixed `BA_` (e.g. BA_BROWSER_DEBUG_PORT=9333,
   BA_BROWSER_MODE=launch, BA_ENABLE_BROWSER_AUTOMATION=false)
2. `settings.json` next to this file (see the shipped example)
3. The defaults listed above

Thread safety: Playwright's sync API must always run on the same thread, and
callers may run tools from worker threads. All Playwright work is therefore
funneled through a single dedicated worker thread (`_executor`).
"""

import json
import os
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(SKILL_DIR, "settings.json")

DEFAULT_DEBUG_PORT = 9222
DEFAULT_DEBUG_PROFILE = os.path.join(os.path.expanduser("~"), ".browser-driver-profile")
LAUNCH_PROFILE_DIR = os.path.join(SKILL_DIR, "job_search_profile")
BROWSER_CANDIDATES = (
    "brave-browser", "brave", "google-chrome", "google-chrome-stable",
    "chromium", "chromium-browser", "microsoft-edge",
)
SUBMIT_WORDS = (
    "submit", "apply", "send application", "easy apply",
    "enviar", "postular", "aplicar", "solicitar",
)

DEFAULT_SETTINGS: dict = {
    "enable_browser_automation": True,
    "browser_mode": "attach",
    "browser_autostart": True,
    "browser_debug_port": DEFAULT_DEBUG_PORT,
    "browser_debug_profile": DEFAULT_DEBUG_PROFILE,
    "browser_executable": "",
    "require_submit_confirmation": True,
}

# "BA_BROWSER_DEBUG_PORT" -> "browser_debug_port"
_ENV_MAP = {f"BA_{k.upper()}": k for k in DEFAULT_SETTINGS}


def _coerce(default, raw):
    """Env vars are strings; cast them to the type of the default value."""
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    return raw


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    path = os.environ.get("BA_SETTINGS_FILE", SETTINGS_PATH)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                settings.update({k: v for k, v in json.load(f).items() if k in DEFAULT_SETTINGS})
        except Exception:
            pass
    for env, key in _ENV_MAP.items():
        if env in os.environ:
            settings[key] = _coerce(DEFAULT_SETTINGS[key], os.environ[env])
    return settings


SETTINGS: dict = load_settings()


def reload_settings() -> dict:
    global SETTINGS
    SETTINGS = load_settings()
    return SETTINGS


# --------------------------------------------------------------------------
# Debug-browser helpers (also used by browser_tools.open_browser)
# --------------------------------------------------------------------------

def debug_port() -> int:
    try:
        return int(SETTINGS.get("browser_debug_port", DEFAULT_DEBUG_PORT))
    except (TypeError, ValueError):
        return DEFAULT_DEBUG_PORT


def debug_profile_dir() -> str:
    return os.path.expanduser(SETTINGS.get("browser_debug_profile") or DEFAULT_DEBUG_PROFILE)


def debug_flags() -> list[str]:
    """Flags that make a Chromium-based browser attachable by the agent."""
    return [f"--remote-debugging-port={debug_port()}", f"--user-data-dir={debug_profile_dir()}"]


def is_debug_browser_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", debug_port()), timeout=0.5):
            return True
    except OSError:
        return False


def find_browser_executable() -> str | None:
    custom = SETTINGS.get("browser_executable")
    if custom:
        return shutil.which(custom) or (custom if os.path.exists(custom) else None)
    for name in BROWSER_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def ensure_debug_browser() -> None:
    """Make sure a browser is listening on the debug port, starting one if allowed."""
    if is_debug_browser_running():
        return

    port = debug_port()
    if not SETTINGS.get("browser_autostart", True):
        raise RuntimeError(
            f"No browser is listening on 127.0.0.1:{port}. Start Brave/Chromium with "
            f"--remote-debugging-port={port} and a dedicated --user-data-dir, or set "
            "browser_autostart to true in the skill's settings.json."
        )

    exe = find_browser_executable()
    if not exe:
        raise RuntimeError(
            "No Chromium-based browser was found (looked for brave, chrome, chromium, edge). "
            "Set 'browser_executable' in the skill's settings.json to the browser command or path."
        )

    os.makedirs(debug_profile_dir(), exist_ok=True)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # keep the browser alive if the caller exits
    subprocess.Popen(
        [exe, *debug_flags()],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **kwargs,
    )

    deadline = time.time() + 20
    while time.time() < deadline:
        if is_debug_browser_running():
            return
        time.sleep(0.4)
    raise RuntimeError(
        f"Started '{exe}' but nothing is listening on 127.0.0.1:{port} after 20 s. "
        "If Brave was already open with the same profile, close it and try again."
    )


# --------------------------------------------------------------------------
# Playwright plumbing
# --------------------------------------------------------------------------

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")
_playwright = None
_context = None
_page = None
_browser = None
_attached = False


def _drop_stale_session() -> None:
    """Forget a session whose browser went away (window closed, crash, update...)."""
    global _playwright, _context, _page, _browser, _attached
    _page = None
    _context = None
    _browser = None
    _attached = False
    if _playwright is not None:
        try:
            _playwright.stop()
        except Exception:
            pass
        _playwright = None


def _is_dead_connection(exc: BaseException) -> bool:
    """True when the failure means 'the browser went away mid-call'."""
    return "TargetClosed" in type(exc).__name__ or "has been closed" in str(exc)


def call(func, *args, **kwargs):
    """Run a tool/helper, reconnecting once automatically if the browser died.

    Playwright's `browser.is_connected()` can lag a moment behind the real
    death of the process, so a call may still be routed to the stale page and
    fail with TargetClosedError. When that happens, drop everything, start /
    re-attach to the browser and retry the call exactly once.
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        if not _is_dead_connection(e):
            raise
        _drop_stale_session()
        return func(*args, **kwargs)


def _tool(func):
    """Run the tool on the Playwright thread and turn exceptions into error strings."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        def run():
            # Re-read the flag on every call so toggling it off takes effect
            # immediately, even mid-task.
            if func.__name__ != "browser_close" and not SETTINGS.get("enable_browser_automation", True):
                return (
                    "Browser automation is disabled. Set enable_browser_automation to true in "
                    "the skill's settings.json (or export BA_ENABLE_BROWSER_AUTOMATION=true) "
                    "if you want the assistant to click, type and read pages in the browser."
                )
            try:
                return call(func, *args, **kwargs)
            except Exception as e:
                return f"Error in {func.__name__}: {e}"
        return _executor.submit(run).result()
    return wrapper


def _get_page():
    """Lazily connect/launch the browser and return the agent's page."""
    global _playwright, _context, _page, _browser, _attached

    # The browser can vanish under us at any moment (the user closes the
    # window, Brave updates, the session crashes). Detect it and reconnect
    # from scratch — otherwise every later call reuses a dead context and
    # fails with TargetClosedError forever.
    if _browser is not None and not _browser.is_connected():
        _drop_stale_session()

    if _page is not None and not _page.is_closed():
        return _page

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright is not installed. Run: pip install playwright")

    if _playwright is None:
        _playwright = sync_playwright().start()

    mode = SETTINGS.get("browser_mode", "attach")

    if _context is None:
        if mode == "attach":
            ensure_debug_browser()
            cdp_url = f"http://127.0.0.1:{debug_port()}"
            browser, last_error = None, None
            for _ in range(3):  # the port can open a moment before CDP is ready
                try:
                    browser = _playwright.chromium.connect_over_cdp(cdp_url)
                    break
                except Exception as e:
                    last_error = e
                    time.sleep(1)
            if browser is None:
                raise RuntimeError(f"Could not connect to the browser at {cdp_url}: {last_error}")
            _context = browser.contexts[0] if browser.contexts else browser.new_context()
            _browser = browser
            _attached = True
        else:
            os.makedirs(LAUNCH_PROFILE_DIR, exist_ok=True)
            _context = _playwright.chromium.launch_persistent_context(
                LAUNCH_PROFILE_DIR, headless=False, viewport={"width": 1280, "height": 900}
            )
            _browser = _context.browser()
            _attached = False

    if _attached:
        # Work in our own tab so the user's open tabs are never hijacked.
        _page = _context.new_page()
    else:
        _page = _context.pages[0] if _context.pages else _context.new_page()
    return _page


def _resolve_locator(page, selector: str):
    """Accept a CSS/text=/xpath= selector, or plain label / button / visible text."""
    if selector.startswith(("text=", "css=", "xpath=", "#", ".", "[")) or any(c in selector for c in "<>"):
        return page.locator(selector)

    candidates = (
        page.get_by_label(selector, exact=False),
        page.get_by_placeholder(selector, exact=False),
        page.get_by_role("button", name=selector),
        page.get_by_role("link", name=selector),
        page.get_by_text(selector, exact=False),
    )
    for loc in candidates:
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return page.locator(selector)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@_tool
def browser_goto(url: str) -> str:
    """Navigate the agent's tab to a URL."""
    page = _get_page()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    return f"Navigated to {page.url} (title: {page.title()})"


@_tool
def browser_read_page(max_chars: int = 6000) -> str:
    """Return the visible text of the current page."""
    page = _get_page()
    text = " ".join(page.inner_text("body").split())
    truncated = len(text) > max_chars
    result = f"URL: {page.url}\nTitle: {page.title()}\n\nText:\n{text[:max_chars]}"
    if truncated:
        result += "\n[... truncated ...]"
    return result


@_tool
def browser_list_interactive_elements(limit: int = 40) -> str:
    """List visible buttons, links, inputs, checkboxes and selects on the page."""
    page = _get_page()
    elements = page.eval_on_selector_all(
        "button, a, input, select, textarea, [role=button], [role=checkbox]",
        """(els, limit) => els
            .filter(el => el.offsetParent !== null)
            .slice(0, limit)
            .map((el, i) => {
                const tag = el.tagName.toLowerCase();
                const type = el.getAttribute('type') || '';
                const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 60);
                return `${i}: <${tag}${type ? ' type=' + type : ''}> "${text}" id="${el.id || ''}" name="${el.getAttribute('name') || ''}"`;
            })""",
        limit,
    )
    return "\n".join(elements) if elements else "No visible interactive elements found."


@_tool
def browser_click(selector: str, confirmed: bool = False) -> str:
    """Click an element by CSS selector or visible text."""
    if (
        SETTINGS.get("require_submit_confirmation", True)
        and not confirmed
        and any(word in selector.lower() for word in SUBMIT_WORDS)
    ):
        return (
            f"BLOCKED: '{selector}' looks like a submit/apply action. Ask the user for explicit "
            "confirmation first, then call browser_click again with confirmed=true."
        )
    page = _get_page()
    _resolve_locator(page, selector).first.click(timeout=8000)
    return f"Clicked element matching '{selector}'."


@_tool
def browser_fill(selector: str, text: str) -> str:
    """Type text into an input or textarea."""
    page = _get_page()
    _resolve_locator(page, selector).first.fill(text, timeout=8000)
    return f"Filled '{selector}'."


@_tool
def browser_set_checkbox(selector: str, checked: bool = True) -> str:
    """Check or uncheck a checkbox or radio button."""
    page = _get_page()
    loc = _resolve_locator(page, selector).first
    if checked:
        loc.check(timeout=8000)
    else:
        loc.uncheck(timeout=8000)
    return f"{'Checked' if checked else 'Unchecked'} '{selector}'."


@_tool
def browser_select_option(selector: str, value: str) -> str:
    """Select an option in a <select> dropdown by label or value."""
    page = _get_page()
    loc = _resolve_locator(page, selector).first
    try:
        loc.select_option(label=value, timeout=8000)
    except Exception:
        loc.select_option(value=value, timeout=8000)
    return f"Selected '{value}' in '{selector}'."


@_tool
def browser_press_key(key: str) -> str:
    """Press a key on the focused element (Enter, Tab, Escape...)."""
    _get_page().keyboard.press(key)
    return f"Pressed '{key}'."


@_tool
def browser_close() -> str:
    """Close the agent's tab / session. In attach mode your browser stays open."""
    global _playwright, _context, _page, _browser, _attached
    try:
        if _attached:
            if _page is not None and not _page.is_closed():
                _page.close()
        elif _context is not None:
            _context.close()
    finally:
        if _playwright is not None:
            _playwright.stop()
        _playwright = _context = _page = _browser = None
        _attached = False
    return "Browser session closed."


# --------------------------------------------------------------------------
# Registration (optional; only needed when embedded in an app with a registry)
# --------------------------------------------------------------------------

def register(registry) -> None:
    registry.register(
        name="browser_goto",
        description="Navigate the automation browser tab to a URL.",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to open."}},
            "required": ["url"],
        },
        func=browser_goto,
    )
    registry.register(
        name="browser_read_page",
        description="Read the visible text of the current page.",
        parameters={
            "type": "object",
            "properties": {"max_chars": {"type": "integer", "description": "Max characters to return."}},
        },
        func=browser_read_page,
    )
    registry.register(
        name="browser_list_interactive_elements",
        description="List visible buttons, links, inputs, checkboxes and selects on the current page.",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max elements to list."}},
        },
        func=browser_list_interactive_elements,
    )
    registry.register(
        name="browser_click",
        description=(
            "Click an element by CSS selector or visible text. Submit/apply-type buttons are "
            "blocked until the user confirms; then pass confirmed=true."
        ),
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector or visible text."},
                "confirmed": {"type": "boolean", "description": "Set true only after the user explicitly approved this click."},
            },
            "required": ["selector"],
        },
        func=browser_click,
    )
    registry.register(
        name="browser_fill",
        description="Type text into an input/textarea identified by CSS selector, label or placeholder.",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector, label or placeholder text."},
                "text": {"type": "string", "description": "Text to type."},
            },
            "required": ["selector", "text"],
        },
        func=browser_fill,
    )
    registry.register(
        name="browser_set_checkbox",
        description="Check or uncheck a checkbox/radio button identified by selector or label text.",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector or label text."},
                "checked": {"type": "boolean", "description": "True to check, false to uncheck."},
            },
            "required": ["selector"],
        },
        func=browser_set_checkbox,
    )
    registry.register(
        name="browser_select_option",
        description="Select an option in a dropdown by visible label or value.",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector or label of the <select>."},
                "value": {"type": "string", "description": "Option label or value."},
            },
            "required": ["selector", "value"],
        },
        func=browser_select_option,
    )
    registry.register(
        name="browser_press_key",
        description="Press a keyboard key on the focused element (Enter, Tab, Escape).",
        parameters={
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Key name, e.g. 'Enter'."}},
            "required": ["key"],
        },
        func=browser_press_key,
    )
    registry.register(
        name="browser_close",
        description="Close the agent's browser tab/session. The browser profile and logins persist.",
        parameters={"type": "object", "properties": {}},
        func=browser_close,
    )
