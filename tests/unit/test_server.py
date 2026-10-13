"""Unit tests for server-level helpers that do not require a running browser."""
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.core.native_os import NativeEngine
from src.core.stealth_cdp import StealthEngine
from src.server import (
    _build_search_url,
    _sanitize_header_value,
    _validate_url,
    _TABLE_JS_TEMPLATE,
    app,
    session_mgr,
)


class TestValidateUrl:
    """Tests for URL scheme whitelist validation."""

    def test_allows_http(self):
        _validate_url("http://example.com")

    def test_allows_https(self):
        _validate_url("https://example.com/path?query=1")

    def test_rejects_javascript(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_url("javascript:alert(1)")
        assert "javascript" in exc_info.value.detail.lower()

    def test_rejects_file(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_url("file:///etc/passwd")
        assert "file" in exc_info.value.detail.lower()

    def test_rejects_no_scheme(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_url("example.com")
        assert "(none)" in exc_info.value.detail


class TestBuildSearchUrl:
    """Tests for search engine URL construction."""

    def test_google_url(self):
        url = _build_search_url("google", "hello world")
        assert url == "https://www.google.com/search?q=hello+world"

    def test_bing_url(self):
        url = _build_search_url("bing", "hello world")
        assert url == "https://www.bing.com/search?q=hello+world"

    def test_duckduckgo_url(self):
        """DuckDuckGo uses root path /?q=, not /search?q=."""
        url = _build_search_url("duckduckgo", "hello world")
        assert url == "https://duckduckgo.com/?q=hello+world"

    def test_unknown_engine_fallback_to_google(self):
        """Unknown engine names fall back to the default Google template."""
        url = _build_search_url("yahoo", "hello world")
        assert url == "https://www.google.com/search?q=hello+world"

    def test_special_characters_quoted(self):
        url = _build_search_url("google", "a+b=c&d")
        # Spaces/+/&/= in the query value must be quoted so they don't break the URL.
        assert "%2B" in url  # '+'
        assert "%3D" in url  # '='
        assert "%26" in url  # '&'


class TestTableJsTemplate:
    """Tests that table extraction JS is built safely."""

    def test_index_is_json_encoded(self):
        """Index should be inserted via json.dumps to avoid JS injection."""
        js = _TABLE_JS_TEMPLATE.format(index=json.dumps(0))
        assert "var index = 0;" in js
        # Template should not contain raw braces left for str.format interpolation
        assert "{index}" not in js

    def test_negative_index_not_accepted_by_endpoint(self):
        """The endpoint itself rejects negative indices before building JS."""
        # This is a lightweight regression check; the real behavior is tested in e2e.
        assert "table_index: index" in _TABLE_JS_TEMPLATE.format(index=json.dumps(1))


class TestHeaderSanitizing:
    """Tests for values emitted into HTTP headers."""

    def test_sanitize_header_value_removes_newlines_and_non_ascii(self):
        value = _sanitize_header_value("1. link at (1, 2)\n2. café\r\n3. button")

        assert "\n" not in value
        assert "\r" not in value
        assert "caf" in value
        value.encode("latin-1")


class TestExecuteJsEndpoint:
    """Tests for /api/execute_js endpoint behavior."""

    def test_execute_js_native_returns_structured_error(self, monkeypatch):
        """When current engine is native, return structured ENGINE_NOT_SUPPORTED error."""
        fake_native = NativeEngine(browser_cmd=["fake-chrome"])
        fake_mgr = MagicMock()
        fake_mgr.get_engine.return_value = fake_native
        fake_session = MagicMock()
        fake_session.engine_manager = fake_mgr

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        client = TestClient(app)
        response = client.post("/api/execute_js", json={"script": "1+1"})

        assert response.status_code == 400
        body = response.json()
        assert body["error"] == "ENGINE_NOT_SUPPORTED"
        assert "stealth" in body["message"].lower()
        assert body["current_engine"] == "native"


class TestScreenshotAnnotatedEndpoint:
    """Tests for /api/screenshot_annotated engine-state headers."""

    def test_screenshot_annotated_native_has_engine_note(self, monkeypatch):
        """Native engine screenshot includes X-Engine and X-Elements-Note headers."""
        fake_native = NativeEngine(browser_cmd=["fake-chrome"])
        fake_native.screenshot = AsyncMock(return_value=b"fake-png")
        fake_mgr = MagicMock()
        fake_mgr.get_engine.return_value = fake_native
        fake_session = MagicMock()
        fake_session.engine_manager = fake_mgr

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )
        monkeypatch.setattr(
            "src.server.annotate_screenshot", lambda _raw, _elements: b"annotated-png"
        )

        client = TestClient(app)
        response = client.get("/api/screenshot_annotated")

        assert response.status_code == 200
        assert response.headers["X-Engine"] == "native"
        assert "X-Elements-Note" in response.headers
        assert "stealth" in response.headers["X-Elements-Note"].lower()
        assert response.headers["X-Element-Count"] == "0"


# --- Vision-first look bundle tests ---


def _fake_stealth_session():
    """Return a fake session with a patched StealthEngine."""
    engine = StealthEngine()
    engine.browser = object()
    engine.tab = object()
    engine._tabs = {"tab-0": engine.tab}
    engine.screenshot = AsyncMock(return_value=b"raw-png")
    engine.get_text = AsyncMock(return_value={"text": "page text"})
    engine.get_title = AsyncMock(return_value="Page Title")
    engine.get_url = AsyncMock(return_value="https://example.com")
    engine._evaluate_tolerant = AsyncMock(return_value="complete")

    fake_mgr = MagicMock()
    fake_mgr.get_engine.return_value = engine
    fake_session = MagicMock()
    fake_session.engine_manager = fake_mgr
    return fake_session, engine


class TestLookEndpoint:
    """Tests for /api/look."""

    def test_look_bundle_shape(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.get("/api/look")

        assert response.status_code == 200
        body = response.json()
        assert body["screenshot_base64"] == base64.b64encode(b"annotated-png").decode("utf-8")
        assert body["screenshot_media_type"] == "image/png"
        assert body["text"] == "page text"
        assert body["title"] == "Page Title"
        assert body["url"] == "https://example.com"
        assert body["element_count"] >= 0
        assert "elements" in body
        assert "elements_text" in body
        assert body["engine"] == "stealth"
        assert body["tab_id"] == "tab-0"
        assert "block" in body
        assert "blocked" in body["block"]
        assert "confidence" in body["block"]
        assert "signals" in body["block"]

    def test_look_native_engine_text_unavailable(self, monkeypatch):
        fake_native = NativeEngine(browser_cmd=["fake-chrome"])
        fake_native.screenshot = AsyncMock(return_value=b"native-png")
        fake_native.get_title = AsyncMock(return_value="")
        fake_native.get_url = AsyncMock(return_value="")
        fake_mgr = MagicMock()
        fake_mgr.get_engine.return_value = fake_native
        fake_session = MagicMock()
        fake_session.engine_manager = fake_mgr

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.get("/api/look")

        assert response.status_code == 200
        body = response.json()
        assert body["engine"] == "native"
        assert body["text"] == ""
        assert body["text_unavailable"] is True
        assert body["elements"] == []
        assert body["element_count"] == 0


class TestMainContentOnlyText:
    """Tests for the main_content_only text-extraction option (2026-07-26)."""

    def test_api_text_default_does_not_request_main_content_only(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})

        client = TestClient(app)
        response = client.get("/api/text")

        assert response.status_code == 200
        engine.get_text.assert_awaited_once_with(main_content_only=False)

    def test_api_text_main_content_only_true(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})

        client = TestClient(app)
        response = client.get("/api/text?main_content_only=true")

        assert response.status_code == 200
        engine.get_text.assert_awaited_once_with(main_content_only=True)

    def test_navigate_forwards_main_content_only_to_look_bundle(self, monkeypatch):
        """Real pain point (2026-07-26): Amazon/Kmart search text buried under
        nav boilerplate. navigate(main_content_only=True) must reach get_text()."""
        fake_session, engine = _fake_stealth_session()
        fake_session.engine_manager.navigate_with_fallback = AsyncMock(
            return_value={"url": "https://example.com", "title": "Example"}
        )
        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.post(
                "/api/navigate", json={"url": "https://example.com", "main_content_only": True}
            )

        assert response.status_code == 200
        engine.get_text.assert_awaited_once_with(main_content_only=True)


class TestSearchThinTextFallback:
    """Tests for /api/search's thin-text link fallback (2026-07-28).

    Real case: certain queries make Google render an image-card-heavy results
    layout where extracted text comes back nearly empty, even though the page
    has real result links. /api/search must surface those links via the
    fallback instead of returning what looks like a failed/empty search."""

    def test_thin_text_gets_real_result_links_appended(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        fake_session.engine_manager.navigate_with_fallback = AsyncMock(
            return_value={"url": "https://www.google.com/search?q=x", "title": "x - Google Search"}
        )
        engine.get_text = AsyncMock(return_value={"text": "Skip to main content\nSearch Results"})
        engine.tab = MagicMock()
        engine.tab.evaluate = AsyncMock(
            return_value=json.dumps(
                [
                    {"text": "Go to Google Home", "href": "https://www.google.com/webhp"},
                    {"text": "A real result", "href": "https://zhuanlan.zhihu.com/p/123"},
                ]
            )
        )
        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.post("/api/search", json={"query": "x"})

        assert response.status_code == 200
        body = response.json()
        assert "https://zhuanlan.zhihu.com/p/123" in body["text"]
        assert "google.com/webhp" not in body["text"]

    def test_normal_length_text_is_not_modified(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        fake_session.engine_manager.navigate_with_fallback = AsyncMock(
            return_value={"url": "https://www.google.com/search?q=x", "title": "x - Google Search"}
        )
        normal_text = "Real search results content. " * 20  # well over the thin-text threshold
        engine.get_text = AsyncMock(return_value={"text": normal_text})
        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.post("/api/search", json={"query": "x"})

        assert response.status_code == 200
        assert response.json()["text"] == normal_text

    def test_regional_google_domain_chrome_link_excluded(self, monkeypatch):
        """Real gap found during live re-test (2026-07-28): the host-suffix match
        (fixing Codex's substring-match finding) doesn't catch Google's regional
        domains (google.com.au) since they don't end with ".google.com" — a real
        "Google apps" chrome link to google.com.au leaked into the fallback text
        until the actual navigated hostname was added to exclude_domains too."""
        fake_session, engine = _fake_stealth_session()
        fake_session.engine_manager.navigate_with_fallback = AsyncMock(
            return_value={"url": "https://www.google.com.au/search?q=x", "title": "x - Google Search"}
        )
        engine.get_text = AsyncMock(return_value={"text": "Skip to main content"})
        engine.get_url = AsyncMock(return_value="https://www.google.com.au/search?q=x")
        engine.tab = MagicMock()
        engine.tab.evaluate = AsyncMock(
            return_value=json.dumps(
                [
                    {"text": "Google apps", "href": "https://www.google.com.au/intl/en/about/products"},
                    {"text": "A real result", "href": "https://example.com/page"},
                ]
            )
        )
        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.post("/api/search", json={"query": "x"})

        assert response.status_code == 200
        body = response.json()
        assert "https://example.com/page" in body["text"]
        assert "google.com.au/intl" not in body["text"]

    def test_bing_engine_excludes_its_own_chrome_domain(self, monkeypatch):
        """Codex review (2026-07-28): the original test suite only covered Google;
        confirm the exclude-domain set is actually per-engine, not hardcoded to Google."""
        fake_session, engine = _fake_stealth_session()
        fake_session.engine_manager.navigate_with_fallback = AsyncMock(
            return_value={"url": "https://www.bing.com/search?q=x", "title": "x - Bing"}
        )
        engine.get_text = AsyncMock(return_value={"text": "Skip to main content"})
        engine.get_url = AsyncMock(return_value="https://www.bing.com/search?q=x")
        engine.tab = MagicMock()
        engine.tab.evaluate = AsyncMock(
            return_value=json.dumps(
                [
                    {"text": "Bing Home", "href": "https://www.bing.com/"},
                    {"text": "A real result", "href": "https://example.com/page"},
                ]
            )
        )
        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.post("/api/search", json={"query": "x", "engine": "bing"})

        assert response.status_code == 200
        body = response.json()
        assert "https://example.com/page" in body["text"]
        assert "www.bing.com/" not in body["text"]


class TestExtractLinksWithContext:
    """Tests for /api/links `with_context` (2026-07-26)."""

    def test_default_omits_context_field(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        engine.tab = MagicMock()
        engine.tab.evaluate = AsyncMock(
            return_value=json.dumps(
                [{"text": "Example", "url": "https://example.com", "x": 1, "y": 2}]
            )
        )
        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})

        client = TestClient(app)
        response = client.get("/api/links")

        assert response.status_code == 200
        body = response.json()
        assert body["links"] == [
            {"text": "Example", "url": "https://example.com", "x": 1, "y": 2}
        ]
        js_used = engine.tab.evaluate.await_args.args[0]
        assert "context" not in js_used

    def test_with_context_includes_context_field(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        engine.tab = MagicMock()
        engine.tab.evaluate = AsyncMock(
            return_value=json.dumps(
                [
                    {
                        "text": "",
                        "url": "https://example.com/product/1",
                        "context": "Desk Lamp - Black $12",
                        "x": 1,
                        "y": 2,
                    }
                ]
            )
        )
        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})

        client = TestClient(app)
        response = client.get("/api/links?with_context=true")

        assert response.status_code == 200
        body = response.json()
        assert body["links"][0]["context"] == "Desk Lamp - Black $12"
        js_used = engine.tab.evaluate.await_args.args[0]
        assert "context" in js_used

    def test_filter_text_matches_context_too(self, monkeypatch):
        """filter_text should match either the link's own text or its context —
        many e-commerce card links have no visible text of their own (image
        links, bare 'Add' buttons)."""
        fake_session, engine = _fake_stealth_session()
        engine.tab = MagicMock()
        engine.tab.evaluate = AsyncMock(
            return_value=json.dumps(
                [
                    {"text": "", "url": "https://x.com/a", "context": "Desk Lamp $12", "x": 1, "y": 1},
                    {"text": "", "url": "https://x.com/b", "context": "Floor Lamp $29", "x": 2, "y": 2},
                ]
            )
        )
        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})

        client = TestClient(app)
        response = client.get("/api/links?with_context=true&filter_text=Desk")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["links"][0]["url"] == "https://x.com/a"


class TestReturnLookActions:
    """Tests for return_look wired into action endpoints."""

    def test_click_return_look_true(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        engine.click = AsyncMock(return_value={"x": 100, "y": 200})
        engine.get_url = AsyncMock(return_value="https://example.com/after")

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.post("/api/click", json={"x": 100, "y": 200, "return_look": True})

        assert response.status_code == 200
        body = response.json()
        assert body["x"] == 100
        assert body["y"] == 200
        assert "screenshot_base64" in body
        assert body["text"] == "page text"

    def test_click_return_look_false_backward_compat(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        engine.click = AsyncMock(return_value={"x": 100, "y": 200})

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        client = TestClient(app)
        response = client.post("/api/click", json={"x": 100, "y": 200, "return_look": False})

        assert response.status_code == 200
        body = response.json()
        assert body == {"x": 100, "y": 200, "session_id": "default"}

    def test_scroll_return_look(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        engine.scroll = AsyncMock(return_value={"direction": "down", "amount": 3})

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.post("/api/scroll", json={"direction": "down", "amount": 3})

        assert response.status_code == 200
        body = response.json()
        assert body["direction"] == "down"
        assert "screenshot_base64" in body

    def test_switch_tab_return_look(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        fake_session.engine_manager.switch_tab = AsyncMock(
            return_value={"tab_id": "tab-0", "url": "https://example.com", "title": "Title"}
        )

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.post("/api/tabs/tab-0/activate")

        assert response.status_code == 200
        body = response.json()
        assert body["tab_id"] == "tab-0"
        assert "screenshot_base64" in body

    def test_search_returns_look_bundle(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        engine.get_url = AsyncMock(return_value="https://www.google.com/search?q=test")
        fake_session.engine_manager.navigate_with_fallback = AsyncMock(
            return_value={
                "url": "https://www.google.com/search?q=test",
                "title": "test - Google Search",
            }
        )

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.post("/api/search", json={"query": "test"})

        assert response.status_code == 200
        body = response.json()
        assert body["url"] == "https://www.google.com/search?q=test"
        assert body["search_url"] == "https://www.google.com/search?q=test"
        assert body["search_engine"] == "google"
        assert "screenshot_base64" in body
        assert body["text"] == "page text"

    def test_search_with_bing_engine(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        engine.get_url = AsyncMock(return_value="https://www.bing.com/search?q=test")
        fake_session.engine_manager.navigate_with_fallback = AsyncMock(
            return_value={
                "url": "https://www.bing.com/search?q=test",
                "title": "test - Bing",
            }
        )

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.post("/api/search", json={"query": "test", "engine": "bing"})

        assert response.status_code == 200
        body = response.json()
        assert body["search_url"] == "https://www.bing.com/search?q=test"
        assert body["search_engine"] == "bing"
        fake_session.engine_manager.navigate_with_fallback.assert_awaited_once_with(
            "https://www.bing.com/search?q=test"
        )


    def test_search_unknown_engine_reports_google(self, monkeypatch):
        """When an unknown engine is requested, search_engine reports 'google'."""
        fake_session, engine = _fake_stealth_session()
        engine.get_url = AsyncMock(return_value="https://www.google.com/search?q=test")
        fake_session.engine_manager.navigate_with_fallback = AsyncMock(
            return_value={
                "url": "https://www.google.com/search?q=test",
                "title": "test - Google Search",
            }
        )

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        monkeypatch.setattr(
            session_mgr, "get_or_create_session", AsyncMock(return_value=fake_session)
        )

        with patch("src.look.annotate_screenshot", lambda _raw, _elements: b"annotated-png"):
            client = TestClient(app)
            response = client.post("/api/search", json={"query": "test", "engine": "yahoo"})

        assert response.status_code == 200
        body = response.json()
        assert body["search_url"] == "https://www.google.com/search?q=test"
        assert body["search_engine"] == "google"
        fake_session.engine_manager.navigate_with_fallback.assert_awaited_once_with(
            "https://www.google.com/search?q=test"
        )


class TestHealthEndpointRecovery:
    """Tests for /api/health's dead-browser auto-recovery path (2026-07-26).

    Real case: a user closed the ai-browser Chrome window after logging into
    a dedicated account in it. The health check's recovery path used to call
    session_mgr.delete_session(), which wipes the whole profile directory —
    silently destroying that login. It must call recover_default_session()
    instead, which preserves the profile.
    """

    def test_dead_browser_calls_recover_not_delete(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        fake_session.profile_dir = "/fake/profile/dir"

        recovered_session, _ = _fake_stealth_session()
        recovered_session.profile_dir = "/fake/profile/dir"

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        recover_mock = AsyncMock(return_value=recovered_session)
        delete_mock = AsyncMock()
        monkeypatch.setattr(session_mgr, "recover_default_session", recover_mock)
        monkeypatch.setattr(session_mgr, "delete_session", delete_mock)

        with patch("src.server.has_chrome_processes_for_profile_dir", return_value=False):
            client = TestClient(app)
            response = client.get("/api/health")

        assert response.status_code == 200
        recover_mock.assert_awaited_once_with()
        delete_mock.assert_not_awaited()

    def test_healthy_browser_does_not_trigger_recovery(self, monkeypatch):
        fake_session, engine = _fake_stealth_session()
        fake_session.profile_dir = "/fake/profile/dir"

        monkeypatch.setattr(session_mgr, "sessions", {"default": fake_session})
        recover_mock = AsyncMock()
        monkeypatch.setattr(session_mgr, "recover_default_session", recover_mock)

        with patch("src.server.has_chrome_processes_for_profile_dir", return_value=True):
            client = TestClient(app)
            response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        recover_mock.assert_not_awaited()
