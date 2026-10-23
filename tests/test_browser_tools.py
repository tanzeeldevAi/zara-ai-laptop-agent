"""Phase 3: browser tools — URL guard (SSRF) + behaviour against a fake page.

No real Chromium is launched here: the service is injected with a fake
page factory. A separate optional test exercises a real browser only when
Playwright AND its Chromium binary are actually installed.
"""

import socket

import pytest

from agent.security import classify
from agent.tools import browser as browser_mod
from agent.tools import load_all

_, HANDLERS = load_all()

PUBLIC_IP = "93.184.216.34"          # example.com's classic public address


def fake_dns(mapping):
    """A getaddrinfo stand-in: hostname -> [ip, ...] (or 'fail')."""

    def _getaddrinfo(host, port, *args, **kwargs):
        result = mapping.get(host, "fail")
        if result == "fail":
            raise socket.gaierror(f"Name or service not known: {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in result]

    return _getaddrinfo


@pytest.fixture
def dns(monkeypatch):
    mapping = {
        "example.com": [PUBLIC_IP],
        "internal.lan": ["192.168.1.10"],
        "mixed.example": [PUBLIC_IP, "10.0.0.5"],
    }
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns(mapping))
    return mapping


# -------------------------------------------------------------- URL guard --

def test_public_https_url_allowed(dns):
    assert browser_mod.validate_url("https://example.com/some/page") is None


def test_non_http_schemes_blocked(dns):
    for url in ("file:///etc/passwd", "ftp://example.com/x", "chrome://settings", "example.com"):
        problem = browser_mod.validate_url(url)
        assert problem is not None and "http/https" in problem, url


def test_localhost_blocked(dns):
    assert "Localhost" in browser_mod.validate_url("http://localhost:8080/admin")
    assert browser_mod.validate_url("http://127.0.0.1/admin") is not None
    assert browser_mod.validate_url("http://[::1]/admin") is not None


def test_private_and_mixed_resolution_blocked(dns):
    assert "private/local" in browser_mod.validate_url("http://internal.lan/")
    # One private address among public ones is enough to refuse.
    assert "private/local" in browser_mod.validate_url("https://mixed.example/")


def test_unresolvable_host_blocked(dns):
    assert "Could not resolve" in browser_mod.validate_url("https://nope.invalid/")


def test_literal_private_ip_blocked_without_dns():
    # IP literals resolve to themselves — no DNS fixture needed.
    assert browser_mod.validate_url("http://192.168.0.1/router") is not None
    assert browser_mod.validate_url("http://169.254.169.254/latest/meta-data/") is not None


# ------------------------------------------------------------ fake page ----

class FakeLocator:
    def __init__(self, page, text):
        self.page = page
        self.text = text
        self.first = self

    def click(self, timeout=None):
        self.page.clicks.append(("text", self.text))


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.clicks = []
        self.fills = []
        self.closed = False
        self.redirect_to = None

    def goto(self, url, wait_until=None, timeout=None):
        self.url = self.redirect_to or url
        self.redirect_to = None

    def title(self):
        return "Fake Title"

    def inner_text(self, selector):
        assert selector == "body"
        return "Hello from the fake page. Some readable text."

    def click(self, selector, timeout=None):
        self.clicks.append(("selector", selector))

    def get_by_text(self, text):
        return FakeLocator(self, text)

    def fill(self, selector, text, timeout=None):
        self.fills.append((selector, text))

    def close(self):
        self.closed = True


@pytest.fixture
def fake_page(dns):
    page = FakePage()
    service = browser_mod._BrowserService(page_factory=lambda: page)
    browser_mod.set_service(service)
    yield page
    browser_mod.set_service(None)


def test_open_snapshot_click_type_close(fake_page):
    out = HANDLERS["browser_open"](url="https://example.com/start")
    assert "Opened https://example.com/start" in out["text"]
    assert "Fake Title" in out["text"]

    out = HANDLERS["browser_snapshot"]()
    assert "Hello from the fake page." in out["text"]
    assert "https://example.com/start" in out["text"]

    out = HANDLERS["browser_click"](target="Sign in")          # by visible text
    assert "Clicked 'Sign in'" in out["text"]
    assert fake_page.clicks[-1] == ("text", "Sign in")

    out = HANDLERS["browser_click"](target="#submit")          # by selector
    assert fake_page.clicks[-1] == ("selector", "#submit")

    out = HANDLERS["browser_type"](selector="#q", text="hello world")
    assert fake_page.fills == [("#q", "hello world")]

    out = HANDLERS["browser_close"]()
    assert out["text"] == "Browser closed."
    assert fake_page.closed is True

    out = HANDLERS["browser_snapshot"]()
    assert "No page is open" in out["text"]


def test_open_refused_for_private_url(fake_page):
    out = HANDLERS["browser_open"](url="http://internal.lan/secret")
    assert out["text"].startswith("Refused:")


def test_redirect_to_private_is_blocked(fake_page):
    fake_page.redirect_to = "http://127.0.0.1/admin"
    out = HANDLERS["browser_open"](url="https://example.com/redirector")
    assert "redirected somewhere blocked" in out["text"]
    assert fake_page.url == "about:blank"        # browser was taken off the page


def test_snapshot_without_open_page(dns):
    browser_mod.set_service(browser_mod._BrowserService(page_factory=FakePage))
    try:
        out = HANDLERS["browser_snapshot"]()
        assert "No page is open yet" in out["text"]
    finally:
        browser_mod.set_service(None)


def test_browser_safety_classes():
    assert classify("browser_open", {"url": "https://example.com"})[0] is False
    assert classify("browser_snapshot", {})[0] is False
    assert classify("browser_close", {})[0] is False
    assert classify("browser_click", {"target": "x"})[0] is True
    assert classify("browser_type", {"selector": "#a", "text": "b"})[0] is True


# ------------------------------------------- optional real-browser smoke ---

def test_real_chromium_smoke_optional():
    """Runs ONLY when Playwright + its Chromium binary are really installed."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        executable = pw.chromium.executable_path
    import os

    if not os.path.exists(executable):
        pytest.skip("Chromium binary not installed (playwright install chromium)")

    service = browser_mod._BrowserService(headless=True)
    try:
        url, title = service.open("https://example.com/")
        assert "example.com" in url
        text = service.snapshot()
        assert "Example Domain" in text
    finally:
        service.close()
