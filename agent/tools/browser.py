"""Browser tools (Playwright): drive a real Chromium instead of pixels.

One shared browser lives in a dedicated thread (Playwright's sync API is
thread-affine, and tool handlers run on arbitrary executor threads — every
browser operation is marshalled to the browser thread). The browser starts
lazily on first use; importing this module never imports Playwright.

Safety:
  - http/https only. The host is resolved BEFORE navigating and any
    loopback / private / link-local / reserved address is refused (SSRF
    guard); the final URL is re-checked after navigation, so redirects
    cannot smuggle the browser onto the LAN either.
  - downloads are disabled at the context level.
  - browser_click / browser_type are RISKY (confirmation via the shared
    safety pipeline — see agent/security.py).

Install note: `pip install playwright` provides the library; the Chromium
binary itself is a separate one-time setup step, `playwright install
chromium` (Phase 5 packaging bundles it).
"""

import contextlib
import ipaddress
import queue as queue_mod
import socket
import threading
from urllib.parse import urlparse

from . import tool
from ..config import CONFIG

SNAPSHOT_LIMIT = 8000
NAV_TIMEOUT_MS = 30_000
CALL_TIMEOUT_S = 120


# ------------------------------------------------------------- URL guard ---

def _ip_is_blocked(ip_text):
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True           # unparseable resolution result: fail closed
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_url(url):
    """Return an error string when the URL must not be opened, else None."""
    text = str(url or "").strip()
    if not text:
        return "No URL was given."
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        return (
            f"Only http/https pages can be opened "
            f"(this URL's scheme is '{parsed.scheme or 'none'}')."
        )
    host = parsed.hostname
    if not host:
        return "That URL has no host."
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return "Localhost addresses are blocked."
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        return f"Could not resolve host '{host}': {exc}"
    ips = {info[4][0] for info in infos}
    if not ips:
        return f"Could not resolve host '{host}'."
    for ip_text in sorted(ips):
        if _ip_is_blocked(ip_text):
            return (
                f"'{host}' resolves to {ip_text}, a private/local address — "
                "blocked so the browser can't be used to reach the LAN or this machine."
            )
    return None


# --------------------------------------------------------- browser service -

class _BrowserService:
    """Owns the Playwright objects on one dedicated thread."""

    def __init__(self, page_factory=None, headless=None):
        self._page_factory = page_factory      # tests inject a fake page here
        self._headless = headless
        self._jobs = queue_mod.Queue()
        self._thread = None
        self._start_lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # -- marshalling ----------------------------------------------------

    def _ensure_thread(self):
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="browser", daemon=True
                )
                self._thread.start()

    def _loop(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, done, box = job
            try:
                box["result"] = fn()
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                box["error"] = exc
            finally:
                done.set()

    def call(self, fn, timeout=CALL_TIMEOUT_S):
        self._ensure_thread()
        done = threading.Event()
        box = {}
        self._jobs.put((fn, done, box))
        if not done.wait(timeout):
            raise TimeoutError(f"browser operation timed out after {timeout}s")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # -- page lifecycle (browser thread only) ----------------------------

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        if self._page_factory is not None:
            self._page = self._page_factory()
            return self._page
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        headless = CONFIG.browser_headless if self._headless is None else self._headless
        self._browser = self._pw.chromium.launch(headless=headless)
        self._context = self._browser.new_context(accept_downloads=False)
        self._page = self._context.new_page()
        return self._page

    def _close_all(self):
        for attr in ("_context", "_browser"):
            obj = getattr(self, attr)
            if obj is not None:
                with contextlib.suppress(Exception):
                    obj.close()
                setattr(self, attr, None)
        if self._pw is not None:
            with contextlib.suppress(Exception):
                self._pw.stop()
            self._pw = None
        if self._page is not None and self._page_factory is not None:
            with contextlib.suppress(Exception):
                self._page.close()
        self._page = None

    # -- operations (each runs on the browser thread) ---------------------

    def open(self, url):
        def _open():
            page = self._ensure_page()
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            final_url = page.url
            problem = validate_url(final_url)   # redirects re-checked
            if problem is not None:
                with contextlib.suppress(Exception):
                    page.goto("about:blank")
                raise ValueError(f"The page redirected somewhere blocked: {problem}")
            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                title = ""
            return final_url, title

        return self.call(_open)

    def snapshot(self):
        def _snapshot():
            if self._page is None:
                return None
            page = self._page
            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                title = ""
            body = page.inner_text("body") or ""
            return page.url, title, body

        result = self.call(_snapshot)
        if result is None:
            return None
        url, title, body = result
        text = body.strip()
        if len(text) > SNAPSHOT_LIMIT:
            text = text[:SNAPSHOT_LIMIT] + f"\n... [snapshot truncated, {len(body)} chars total]"
        return f"Page: {url}\nTitle: {title}\n\n{text}"

    def click(self, target):
        def _click():
            page = self._ensure_page()
            if _looks_like_selector(target):
                page.click(target, timeout=NAV_TIMEOUT_MS)
            else:
                page.get_by_text(target).first.click(timeout=NAV_TIMEOUT_MS)
            return page.url

        return self.call(_click)

    def type(self, selector, text):
        def _type():
            page = self._ensure_page()
            page.fill(selector, text, timeout=NAV_TIMEOUT_MS)
            return page.url

        return self.call(_type)

    def close(self):
        def _close():
            was_open = self._page is not None
            self._close_all()
            return was_open

        return self.call(_close)


def _looks_like_selector(target):
    text = str(target or "").strip()
    if text.startswith(("#", ".", "[", "//")):
        return True
    return text.lower().startswith(("text=", "css=", "xpath="))


# ------------------------------------------------------------- the tools ---

_service = None
_service_lock = threading.Lock()


def _get_service():
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = _BrowserService()
    return _service


def set_service(service):
    """Replace the shared browser service (tests inject a fake-backed one)."""
    global _service
    with _service_lock:
        _service = service
    return service


@tool(
    "browser_open",
    "Open a web page in the built-in browser (Chromium). Only public "
    "http/https sites — local/LAN addresses are refused. Prefer this over "
    "mouse-and-keyboard for web work: reading pages, filling forms, "
    "extracting data. Returns the final URL and page title; follow up with "
    "browser_snapshot to read the page.",
    {
        "url": {
            "type": "string",
            "description": "The full URL to open, e.g. 'https://example.com/login'.",
        },
    },
    required=["url"],
)
def browser_open(url):
    problem = validate_url(url)
    if problem is not None:
        return {"text": f"Refused: {problem}"}
    try:
        final_url, title = _get_service().open(str(url).strip())
    except Exception as exc:  # noqa: BLE001 - surface, never crash the turn
        return {"text": f"browser_open failed: {type(exc).__name__}: {exc}"}
    return {"text": f"Opened {final_url}\nTitle: {title or '(no title)'}"}


@tool(
    "browser_snapshot",
    "Read the currently open page as text (URL, title and the readable page "
    "text). Use it to see what a page says after browser_open or a click, "
    "instead of taking a screenshot.",
    {},
)
def browser_snapshot():
    try:
        text = _get_service().snapshot()
    except Exception as exc:  # noqa: BLE001
        return {"text": f"browser_snapshot failed: {type(exc).__name__}: {exc}"}
    if text is None:
        return {"text": "No page is open yet — use browser_open first."}
    return {"text": text}


@tool(
    "browser_click",
    "Click something on the open page: pass a CSS selector (e.g. '#submit', "
    "'.menu a') or the visible text of a link/button (e.g. 'Sign in'). "
    "This changes the page, so it asks for confirmation when confirmations "
    "are on.",
    {
        "target": {
            "type": "string",
            "description": "A CSS selector, or the visible text to click.",
        },
    },
    required=["target"],
)
def browser_click(target):
    try:
        url = _get_service().click(str(target))
    except Exception as exc:  # noqa: BLE001
        return {"text": f"browser_click failed: {type(exc).__name__}: {exc}"}
    return {"text": f"Clicked '{target}'. Now at {url} — use browser_snapshot to see the result."}


@tool(
    "browser_type",
    "Type text into a field on the open page (fills the field, replacing "
    "its current content). Pass a CSS selector for the field and the text. "
    "Never type passwords, card numbers or API keys. Asks for confirmation "
    "when confirmations are on.",
    {
        "selector": {
            "type": "string",
            "description": "CSS selector of the input/textarea, e.g. '#email' or 'input[name=q]'.",
        },
        "text": {
            "type": "string",
            "description": "The text to put into the field.",
        },
    },
    required=["selector", "text"],
)
def browser_type(selector, text):
    try:
        _get_service().type(str(selector), str(text))
    except Exception as exc:  # noqa: BLE001
        return {"text": f"browser_type failed: {type(exc).__name__}: {exc}"}
    return {"text": f"Typed into '{selector}'."}


@tool(
    "browser_close",
    "Close the built-in browser and free its memory. The next browser_open "
    "starts a fresh browser.",
    {},
)
def browser_close():
    try:
        was_open = _get_service().close()
    except Exception as exc:  # noqa: BLE001
        return {"text": f"browser_close failed: {type(exc).__name__}: {exc}"}
    return {"text": "Browser closed." if was_open else "The browser wasn't open."}
