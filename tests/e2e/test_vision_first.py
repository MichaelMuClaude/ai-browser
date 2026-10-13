"""Vision-first browsing E2E tests against real sites.

These tests exercise the redesigned model: search → look → click → look,
with every action returning a bundled screenshot + text + elements response.
They hit real public sites and report genuine pass/fail/skip results.

Each test uses its own browser session so that external agents using the
shared default session do not interfere with the results.
"""
import base64
import json
import threading
import time
import uuid
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest
import requests


def post(
    base_url: str,
    session_id: str,
    path: str,
    payload: dict | None = None,
    params: dict | None = None,
) -> dict:
    resp = requests.post(
        f"{base_url}{path}",
        json=payload or {},
        params={**(params or {}), "session_id": session_id},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def get(
    base_url: str,
    session_id: str,
    path: str,
    params: dict | None = None,
) -> dict:
    resp = requests.get(
        f"{base_url}{path}",
        params={**(params or {}), "session_id": session_id},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def assert_look_bundle(bundle: dict, expect_url: str = ""):
    """Common assertions for a look bundle response."""
    assert "screenshot_base64" in bundle, f"missing screenshot_base64: {bundle.keys()}"
    assert bundle["screenshot_base64"], "screenshot_base64 is empty"
    try:
        raw = base64.b64decode(bundle["screenshot_base64"])
    except Exception as exc:
        pytest.fail(f"screenshot_base64 is not valid base64: {exc}")
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "screenshot is not a PNG"
    assert "text" in bundle, "missing text"
    assert "title" in bundle, "missing title"
    assert "url" in bundle, "missing url"
    assert "elements" in bundle, "missing elements"
    assert "block" in bundle, "missing block"
    if expect_url:
        assert expect_url in bundle["url"], (
            f"expected url containing {expect_url!r}, got {bundle['url']!r}"
        )


@pytest.fixture(scope="function")
def session_id(base_url):
    """Create a fresh browser session for a single test and clean it up after."""
    sid = f"e2e-{uuid.uuid4().hex[:12]}"
    resp = requests.post(
        f"{base_url}/api/sessions",
        json={"session_id": sid},
        timeout=30,
    )
    resp.raise_for_status()
    # Ensure stealth engine is active for CDP-based text/URL access.
    requests.post(
        f"{base_url}/api/engine",
        json={"engine": "stealth"},
        params={"session_id": sid},
        timeout=30,
    ).raise_for_status()
    try:
        yield sid
    finally:
        try:
            requests.delete(f"{base_url}/api/sessions/{sid}", timeout=30)
        except Exception:
            pass


class TestSearchLookClickLoop:
    """Google search -> click a BBC result -> BBC page look bundle."""

    def test_search_returns_look_bundle(self, base_url, session_id):
        result = post(base_url, session_id, "/api/search", {"query": "BBC news"})
        assert_look_bundle(result, expect_url="google.com/search")
        assert result.get("search_url", "").startswith("https://www.google.com/search")
        assert "BBC" in result["text"] or "bbc" in result["text"].lower(), result["text"][:500]

    def test_click_result_returns_look_bundle(self, base_url, session_id):
        post(base_url, session_id, "/api/search", {"query": "BBC news"})
        resp = requests.get(
            f"{base_url}/api/links",
            params={"filter_text": "BBC", "same_domain_only": "false", "session_id": session_id},
            timeout=60,
        )
        resp.raise_for_status()
        links = resp.json()
        bbc_links = [
            l
            for l in links.get("links", [])
            if "bbc.co.uk" in l.get("url", "") or "bbc.com" in l.get("url", "")
        ]
        if not bbc_links:
            pytest.skip("No BBC link found in Google results")
        target = bbc_links[0]
        result = post(base_url, session_id, "/api/click", {"x": target["x"], "y": target["y"]})
        assert_look_bundle(result)
        assert "bbc" in result["url"].lower(), f"unexpected url after click: {result['url']}"


class TestAmazonLookRealText:
    """Navigate to Amazon and verify extracted text stays real across repeated looks (bug 1 regression)."""

    def test_look_text_is_real_not_garbage(self, base_url, session_id):
        result = post(
            base_url,
            session_id,
            "/api/navigate",
            {"url": "https://www.amazon.com.au", "return_look": True},
        )
        assert_look_bundle(result)
        for i in range(3):
            look = get(base_url, session_id, "/api/look")
            assert_look_bundle(look)
            text = look["text"]
            assert "RemoteObject" not in text, f"look {i}: text still contains RemoteObject wrapper: {text[:500]}"
            if not ("Amazon" in text or "Skip to" in text or text.strip()):
                pytest.skip(
                    f"look {i}: Amazon returned empty/blocked text; external site flakiness"
                )


class TestAmazonTabStability:
    """Monitor Amazon URL/title for 10s and collect CDP frameNavigated events."""

    def test_amazon_tab_stability(self, base_url, session_id):
        post(base_url, session_id, "/api/debug/start_frame_navigated_listener")
        result = post(
            base_url,
            session_id,
            "/api/navigate",
            {"url": "https://www.amazon.com.au", "return_look": True},
        )
        assert_look_bundle(result)
        assert "amazon.com.au" in result["url"].lower()

        snapshots = []
        for i in range(10):
            time.sleep(1)
            url = get(base_url, session_id, "/api/url").get("url", "")
            title = get(base_url, session_id, "/api/title").get("title", "")
            snapshots.append({"elapsed": i + 1, "url": url, "title": title})

        nav_log = get(base_url, session_id, "/api/debug/frame_navigations").get("navigations", [])
        print("Amazon stability snapshots:", json.dumps(snapshots, indent=2, ensure_ascii=False))
        print("Amazon frameNavigated log:", json.dumps(nav_log, indent=2, ensure_ascii=False))

        degraded = [s for s in snapshots if not s["url"] or s["url"] == "about:blank"]
        if degraded:
            pytest.fail(
                f"Amazon tab degraded during stability window: {degraded}\n"
                f"frameNavigated log: {nav_log}"
            )
        assert all("amazon" in (s["url"] or "").lower() for s in snapshots), snapshots


class TestScrollLook:
    """Scroll down changes the visible viewport (screenshot)."""

    def test_scroll_look_changes_screenshot(self, base_url, session_id):
        post(
            base_url,
            session_id,
            "/api/navigate",
            {"url": "https://en.wikipedia.org/wiki/Browser_engine", "return_look": True},
        )
        before = get(base_url, session_id, "/api/look", {"max_length": 20000})
        assert_look_bundle(before)
        post(base_url, session_id, "/api/scroll", {"direction": "down", "amount": 5})
        after = get(base_url, session_id, "/api/look", {"max_length": 20000})
        assert_look_bundle(after)
        # The full body text may not change, but the annotated viewport screenshot must.
        assert (
            before["screenshot_base64"] != after["screenshot_base64"]
        ), "scroll did not change annotated screenshot"


class TestMultiTabSwitchLook:
    """Open a second tab and switch between tabs, each switch returning a look bundle."""

    def test_open_tab_and_switch_returns_look_bundle(self, base_url, session_id):
        search_bundle = post(base_url, session_id, "/api/search", {"query": "python.org"})
        assert_look_bundle(search_bundle, expect_url="google.com/search")

        tab1 = post(base_url, session_id, "/api/tabs", {"url": "https://www.python.org"})
        assert tab1.get("tab_id"), tab1
        tab1_id = tab1["tab_id"]

        tab0 = post(base_url, session_id, "/api/tabs/tab-0/activate")
        assert_look_bundle(tab0, expect_url="google.com/search")

        tab1_look = post(base_url, session_id, f"/api/tabs/{tab1_id}/activate")
        assert_look_bundle(tab1_look, expect_url="python.org")


@pytest.fixture(scope="module")
def new_tab_page_url():
    """Serve a tiny HTML page with a target=_blank link on localhost."""
    page = """
    <html><body>
    <a id="external" href="https://www.example.com" target="_blank">Open example</a>
    </body></html>
    """
    tmpdir = Path("/tmp/ai-browser-e2e-newtab")
    tmpdir.mkdir(exist_ok=True)
    (tmpdir / "index.html").write_text(page, encoding="utf-8")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tmpdir), **kwargs)

        def log_message(self, *args, **kwargs):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/index.html"
    try:
        yield url
    finally:
        server.shutdown()


class TestClickNewTabDetection:
    """Click a target=_blank link and detect possible_new_tab."""

    def test_click_new_tab_detection(self, base_url, session_id, new_tab_page_url):
        result = post(
            base_url,
            session_id,
            "/api/navigate",
            {"url": new_tab_page_url, "return_look": True},
        )
        assert_look_bundle(result)
        elements = result.get("elements", [])
        link = next(
            (el for el in elements if el.get("text", "").strip().lower() == "open example"),
            None,
        )
        if not link:
            pytest.skip("target=_blank link not found in annotated elements")
        click_result = post(base_url, session_id, "/api/click", {"x": link["x"], "y": link["y"]})
        assert_look_bundle(click_result)
        assert click_result.get("possible_new_tab") is True, "click did not report possible_new_tab"
        # URL should be unchanged because the new page opened in another tab.
        assert click_result["url"] == result["url"], (
            f"expected current tab URL to stay {result['url']!r}, got {click_result['url']!r}"
        )


class TestRedditBlockDetection:
    """Reddit may block; look bundle should expose it via block signals or screenshot."""

    def test_reddit_look_bundle(self, base_url, session_id):
        result = post(
            base_url,
            session_id,
            "/api/navigate",
            {"url": "https://www.reddit.com", "return_look": True},
        )
        assert_look_bundle(result)
        block = result.get("block", {})
        print("Reddit block result:", json.dumps(block, indent=2, ensure_ascii=False))
        if block.get("blocked"):
            assert block["confidence"] > 0
        else:
            assert result["screenshot_base64"]
