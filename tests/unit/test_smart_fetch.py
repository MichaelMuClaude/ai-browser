"""Unit tests for SmartFetcher."""
import asyncio
import os
from unittest import mock

import pytest

from src.smart_fetch import SmartFetcher, ClaudeEngineResult, _serialize_block_signals
from src.config import CLAUDE_ENGINE_MIN_OUTPUT_CHARS


class FakeEngineManager:
    """Minimal fake EngineManager for direct-session tests."""

    def __init__(self, nav_result, text_result, screenshot_data=None):
        self._nav_result = nav_result
        self._text_result = text_result
        self._screenshot_data = screenshot_data
        self._engine = mock.AsyncMock()
        self._engine.get_text = mock.AsyncMock(return_value=text_result)
        self._engine.screenshot = mock.AsyncMock(return_value=screenshot_data)

    async def navigate_with_fallback(self, url):
        return self._nav_result

    def get_engine(self):
        return self._engine


class FakeSession:
    def __init__(self, engine_manager):
        self.engine_manager = engine_manager


class FakeSessionManager:
    def __init__(self, engine_manager):
        self._engine_manager = engine_manager

    async def get_or_create_session(self, session_id):
        return FakeSession(self._engine_manager)


@pytest.fixture
def logger():
    return mock.Mock(
        info=mock.Mock(),
        warning=mock.Mock(),
        error=mock.Mock(),
    )


@pytest.fixture
def fetcher(logger):
    return SmartFetcher(
        browser_client_factory=lambda: mock.AsyncMock(),
        logger=logger,
    )


def test_serialize_block_signals_from_dicts():
    raw = [
        {"name": "body_too_short", "value": "129", "confidence": 0.3},
    ]
    result = _serialize_block_signals(raw)
    assert result == raw


# ── 1. Claude binary resolution ─────────────────────────────────────────────


def test_resolve_claude_bin_caches_valid_path(logger, tmp_path):
    fake_bin = tmp_path / "claude"
    fake_bin.write_text("#!/bin/sh\necho ok")
    fake_bin.chmod(0o755)

    with mock.patch("src.smart_fetch.shutil.which", return_value=str(fake_bin)):
        fetcher = SmartFetcher(
            browser_client_factory=lambda: mock.AsyncMock(),
            logger=logger,
        )
    assert fetcher._claude_bin == str(fake_bin)


def test_resolve_claude_bin_missing(logger):
    with mock.patch("src.smart_fetch.shutil.which", return_value=None):
        with mock.patch("src.smart_fetch.os.path.isfile", return_value=False):
            fetcher = SmartFetcher(
                browser_client_factory=lambda: mock.AsyncMock(),
                logger=logger,
            )
    assert fetcher._claude_bin == ""


def test_resolve_claude_bin_env_override_missing(logger):
    with mock.patch.dict(os.environ, {"AI_BROWSER_CLAUDE_BIN": "/nonexistent/claude"}):
        fetcher = SmartFetcher(
            browser_client_factory=lambda: mock.AsyncMock(),
            logger=logger,
        )
    assert fetcher._claude_bin == ""


# ── 2. Output parsing ───────────────────────────────────────────────────────


def test_parse_blocked(fetcher):
    result = fetcher._parse_claude_output("BLOCKED", "https://example.com")
    assert result.ok is False
    assert result.reason == "claude_reported_blocked"


def test_parse_refusal(fetcher):
    text = "I can't access that URL due to restrictions. " + "x" * CLAUDE_ENGINE_MIN_OUTPUT_CHARS
    result = fetcher._parse_claude_output(text, "https://example.com")
    assert result.ok is False
    assert result.reason == "claude_refused"


def test_parse_too_short(fetcher):
    result = fetcher._parse_claude_output("abc", "https://example.com")
    assert result.ok is False
    assert result.reason == "output_too_short"
    assert result.raw == "abc"


def test_parse_content_blocked(fetcher):
    text = "Just a moment... Checking your browser before accessing example.com"
    text += "x" * CLAUDE_ENGINE_MIN_OUTPUT_CHARS
    result = fetcher._parse_claude_output(text, "https://example.com")
    assert result.ok is False
    assert result.reason == "content_blocked"


def test_parse_normal(fetcher):
    text = "Example Domain\n\nThis domain is for use in illustrative examples.\n" + "x" * CLAUDE_ENGINE_MIN_OUTPUT_CHARS
    result = fetcher._parse_claude_output(text, "https://example.com")
    assert result.ok is True
    assert result.text == text
    assert result.title == "Example Domain"


# ── 3. Orchestration logic ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_default_does_not_call_claude(logger):
    """Default stealth-first: fetch() does not call _call_claude."""
    client = mock.AsyncMock()
    client.navigate = mock.AsyncMock(
        return_value={
            "url": "https://example.com",
            "title": "Example",
            "blocked": False,
            "block_confidence": 0.0,
            "block_signals": [],
            "engine_used": "stealth",
            "fallback_triggered": False,
        }
    )
    client.get_text = mock.AsyncMock(return_value={"text": "content"})

    fetcher = SmartFetcher(
        browser_client_factory=lambda: client,
        logger=logger,
    )
    fetcher._claude_bin = "/fake/claude"
    fetcher._call_claude = mock.AsyncMock()

    result = await fetcher.fetch("https://example.com")

    fetcher._call_claude.assert_not_awaited()
    client.navigate.assert_awaited_once_with("https://example.com", session_id=None)
    assert result["engine_used"] == "stealth"
    assert result["claude_engine_attempted"] is False
    assert result["text_unavailable"] is False


@pytest.mark.asyncio
async def test_fetch_claude_first_opt_in_calls_claude(logger, monkeypatch):
    """AI_BROWSER_SMART_FETCH_CLAUDE_FIRST=true triggers the claude path."""
    monkeypatch.setenv("AI_BROWSER_SMART_FETCH_CLAUDE_FIRST", "true")
    # Force reimport of config to pick up env change.
    import src.config as config
    monkeypatch.setattr(config, "SMART_FETCH_CLAUDE_FIRST", True)

    client = mock.AsyncMock()
    fetcher = SmartFetcher(
        browser_client_factory=lambda: client,
        logger=logger,
    )
    fetcher._claude_bin = "/fake/claude"

    long_text = "Hello World\n" + "x" * CLAUDE_ENGINE_MIN_OUTPUT_CHARS
    fetcher._call_claude = mock.AsyncMock(
        return_value=ClaudeEngineResult(
            ok=True, text=long_text, title="Hello World"
        )
    )

    result = await fetcher.fetch("https://example.com")

    fetcher._call_claude.assert_awaited_once()
    assert result["engine_used"] == "claude"
    assert result["claude_engine_attempted"] is True
    assert result["claude_engine_succeeded"] is True
    client.navigate.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_claude_success_skips_navigate(logger, monkeypatch):
    monkeypatch.setenv("AI_BROWSER_SMART_FETCH_CLAUDE_FIRST", "true")
    import src.config as config
    monkeypatch.setattr(config, "SMART_FETCH_CLAUDE_FIRST", True)

    client = mock.AsyncMock()
    fetcher = SmartFetcher(
        browser_client_factory=lambda: client,
        logger=logger,
    )
    fetcher._claude_bin = "/fake/claude"

    long_text = "Hello World\n" + "x" * CLAUDE_ENGINE_MIN_OUTPUT_CHARS
    fetcher._call_claude = mock.AsyncMock(
        return_value=ClaudeEngineResult(
            ok=True, text=long_text, title="Hello World"
        )
    )

    result = await fetcher.fetch("https://example.com")

    assert result["engine_used"] == "claude"
    assert result["claude_engine_attempted"] is True
    assert result["claude_engine_succeeded"] is True
    assert result["blocked"] is False
    assert "Hello World" in result["text"]
    client.navigate.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_env_force_disable_claude_skips_claude(logger, monkeypatch):
    """Force-disable must bypass claude even when claude-first is explicitly enabled."""
    monkeypatch.setenv("AI_BROWSER_SMART_FETCH_CLAUDE_FIRST", "true")
    # Force reimport of config to pick up env change.
    import src.config as config
    monkeypatch.setattr(config, "SMART_FETCH_CLAUDE_FIRST", True)
    monkeypatch.setenv("AI_BROWSER_TEST_FORCE_DISABLE_CLAUDE", "1")

    client = mock.AsyncMock()
    client.navigate = mock.AsyncMock(
        return_value={
            "url": "https://example.com",
            "title": "Example",
            "blocked": False,
            "block_confidence": 0.0,
            "block_signals": [],
            "engine_used": "stealth",
            "fallback_triggered": False,
        }
    )
    client.get_text = mock.AsyncMock(return_value={"text": "fallback text"})

    fetcher = SmartFetcher(
        browser_client_factory=lambda: client,
        logger=logger,
    )
    fetcher._claude_bin = "/fake/claude"
    fetcher._call_claude = mock.AsyncMock()

    result = await fetcher.fetch("https://example.com")

    assert result["claude_engine_attempted"] is False
    assert result["claude_engine_succeeded"] is False
    assert result["engine_used"] == "stealth"
    assert result["text"] == "fallback text"
    fetcher._call_claude.assert_not_awaited()
    client.navigate.assert_awaited_once_with("https://example.com", session_id=None)


@pytest.mark.asyncio
async def test_fetch_claude_failure_falls_back(logger, monkeypatch):
    monkeypatch.setenv("AI_BROWSER_SMART_FETCH_CLAUDE_FIRST", "true")
    import src.config as config
    monkeypatch.setattr(config, "SMART_FETCH_CLAUDE_FIRST", True)

    client = mock.AsyncMock()
    client.navigate = mock.AsyncMock(
        return_value={
            "url": "https://example.com",
            "title": "Example",
            "blocked": False,
            "block_confidence": 0.0,
            "block_signals": [],
            "engine_used": "stealth",
            "fallback_triggered": False,
        }
    )
    client.get_text = mock.AsyncMock(return_value={"text": "Example Domain text"})

    fetcher = SmartFetcher(
        browser_client_factory=lambda: client,
        logger=logger,
    )
    fetcher._claude_bin = "/fake/claude"
    fetcher._call_claude = mock.AsyncMock(
        return_value=ClaudeEngineResult(ok=False, reason="claude_refused")
    )

    result = await fetcher.fetch("https://example.com")

    assert result["engine_used"] == "stealth"
    assert result["claude_engine_attempted"] is True
    assert result["claude_engine_succeeded"] is False
    assert result["claude_engine_failure_reason"] == "claude_refused"
    assert result["text"] == "Example Domain text"
    client.navigate.assert_awaited_once_with(
        "https://example.com", session_id=None
    )


@pytest.mark.asyncio
async def test_fetch_claude_unavailable_navigates_directly(logger):
    client = mock.AsyncMock()
    client.navigate = mock.AsyncMock(
        return_value={
            "url": "https://example.com",
            "title": "Example",
            "blocked": False,
            "block_confidence": 0.0,
            "block_signals": [],
            "engine_used": "stealth",
            "fallback_triggered": False,
        }
    )
    client.get_text = mock.AsyncMock(return_value={"text": "fallback text"})

    fetcher = SmartFetcher(
        browser_client_factory=lambda: client,
        logger=logger,
    )
    fetcher._claude_bin = ""

    result = await fetcher.fetch("https://example.com")

    assert result["claude_engine_attempted"] is False
    assert result["claude_engine_succeeded"] is False
    assert result["engine_used"] == "stealth"
    assert result["text"] == "fallback text"


@pytest.mark.asyncio
async def test_fetch_blocked_fallback_returns_empty_text(logger):
    client = mock.AsyncMock()
    client.navigate = mock.AsyncMock(
        return_value={
            "url": "https://example.com",
            "title": "Blocked",
            "blocked": True,
            "block_confidence": 1.0,
            "block_signals": [],
            "engine_used": "native",
            "fallback_triggered": True,
        }
    )
    client.get_text = mock.AsyncMock(return_value={"text": "should be ignored"})

    fetcher = SmartFetcher(
        browser_client_factory=lambda: client,
        logger=logger,
    )
    fetcher._claude_bin = ""

    result = await fetcher.fetch("https://example.com")

    assert result["blocked"] is True
    assert result["text"] == ""
    assert result["text_unavailable"] is False
    assert result["text_unavailable_reason"] is None
    assert result["fallback_triggered"] is True


@pytest.mark.asyncio
async def test_fetch_native_engine_text_unavailable(logger):
    client = mock.AsyncMock()
    client.navigate = mock.AsyncMock(
        return_value={
            "url": "https://example.com",
            "title": "Example",
            "blocked": False,
            "block_confidence": 0.0,
            "block_signals": [],
            "engine_used": "native",
            "fallback_triggered": True,
        }
    )
    client.get_text = mock.AsyncMock(return_value={"text": ""})

    fetcher = SmartFetcher(
        browser_client_factory=lambda: client,
        logger=logger,
    )
    fetcher._claude_bin = ""

    result = await fetcher.fetch("https://example.com")

    assert result["blocked"] is False
    assert result["text"] == ""
    assert result["text_unavailable"] is True
    assert result["text_unavailable_reason"] == "native_engine_cannot_extract_text"


@pytest.mark.asyncio
async def test_fetch_native_engine_blocked_not_text_unavailable(logger):
    client = mock.AsyncMock()
    client.navigate = mock.AsyncMock(
        return_value={
            "url": "https://example.com",
            "title": "Blocked",
            "blocked": True,
            "block_confidence": 1.0,
            "block_signals": [],
            "engine_used": "native",
            "fallback_triggered": True,
        }
    )
    client.get_text = mock.AsyncMock(return_value={"text": ""})

    fetcher = SmartFetcher(
        browser_client_factory=lambda: client,
        logger=logger,
    )
    fetcher._claude_bin = ""

    result = await fetcher.fetch("https://example.com")

    assert result["blocked"] is True
    assert result["text_unavailable"] is False
    assert result["text_unavailable_reason"] is None


@pytest.mark.asyncio
async def test_fetch_stealth_empty_text_not_text_unavailable(logger):
    client = mock.AsyncMock()
    client.navigate = mock.AsyncMock(
        return_value={
            "url": "https://example.com",
            "title": "Example",
            "blocked": False,
            "block_confidence": 0.0,
            "block_signals": [],
            "engine_used": "stealth",
            "fallback_triggered": False,
        }
    )
    client.get_text = mock.AsyncMock(return_value={"text": ""})

    fetcher = SmartFetcher(
        browser_client_factory=lambda: client,
        logger=logger,
    )
    fetcher._claude_bin = ""

    result = await fetcher.fetch("https://example.com")

    assert result["blocked"] is False
    assert result["text"] == ""
    assert result["text_unavailable"] is False
    assert result["text_unavailable_reason"] is None


@pytest.mark.asyncio
async def test_fetch_fallback_screenshot_native(logger):
    screenshot_bytes = b"fake_png_data"
    client = mock.AsyncMock()
    client.navigate = mock.AsyncMock(
        return_value={
            "url": "https://example.com",
            "title": "Example",
            "blocked": False,
            "block_confidence": 0.0,
            "block_signals": [],
            "engine_used": "native",
            "fallback_triggered": True,
        }
    )
    client.get_text = mock.AsyncMock(return_value={"text": ""})
    client.screenshot = mock.AsyncMock(return_value=screenshot_bytes)

    fetcher = SmartFetcher(
        browser_client_factory=lambda: client,
        logger=logger,
    )
    fetcher._claude_bin = ""

    result = await fetcher.fetch(
        "https://example.com", fallback_screenshot=True
    )

    assert result["text_unavailable"] is True
    assert result["screenshot_base64"] == "ZmFrZV9wbmdfZGF0YQ=="
    assert result["screenshot_media_type"] == "image/png"
    client.screenshot.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_uses_session_manager_directly(logger):
    nav_result = {
        "url": "https://example.com",
        "title": "Example",
        "blocked": False,
        "block_confidence": 0.0,
        "block_signals": [],
        "engine_used": "stealth",
        "fallback_triggered": False,
    }
    text_result = {"text": "direct session text"}
    engine_mgr = FakeEngineManager(nav_result, text_result)
    session_manager = FakeSessionManager(engine_mgr)

    fetcher = SmartFetcher(
        browser_client_factory=lambda: mock.AsyncMock(),
        logger=logger,
        session_manager=session_manager,
    )
    fetcher._claude_bin = ""

    result = await fetcher.fetch("https://example.com", session_id="my-session")

    assert result["text"] == "direct session text"
    assert result["engine_used"] == "stealth"


@pytest.mark.asyncio
async def test_fetch_fallback_screenshot_session_manager(logger):
    """fallback_screenshot triggers native engine screenshot() via session manager."""
    screenshot_bytes = b"native_session_png"
    nav_result = {
        "url": "https://nowsecure.nl",
        "title": "Nowsecure",
        "blocked": False,
        "block_confidence": 0.0,
        "block_signals": [],
        "engine_used": "native",
        "fallback_triggered": True,
    }
    text_result = {"text": ""}
    engine_mgr = FakeEngineManager(nav_result, text_result, screenshot_data=screenshot_bytes)
    session_manager = FakeSessionManager(engine_mgr)

    fetcher = SmartFetcher(
        browser_client_factory=lambda: mock.AsyncMock(),
        logger=logger,
        session_manager=session_manager,
    )
    fetcher._claude_bin = ""

    result = await fetcher.fetch(
        "https://nowsecure.nl", session_id="default", fallback_screenshot=True
    )

    assert result["text_unavailable"] is True
    assert result["screenshot_base64"] == "bmF0aXZlX3Nlc3Npb25fcG5n"
    assert result["screenshot_media_type"] == "image/png"
    engine_mgr._engine.screenshot.assert_awaited_once()


# ── 4. Timeout / subprocess handling ────────────────────────────────────────


class NeverEndingProcess:
    """Fake asyncio.Process that never returns from communicate()."""

    def __init__(self):
        self.returncode = None
        self.stdout = mock.Mock(close=mock.Mock())
        self.stderr = mock.Mock(close=mock.Mock())
        self._killed = False
        self._wait_event = asyncio.Event()

    async def communicate(self):
        await self._wait_event.wait()
        return b"", b""

    def kill(self):
        self._killed = True
        self.returncode = -9
        self._wait_event.set()

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_call_claude_timeout_kills_and_waits(logger, monkeypatch):
    fake_bin = "/fake/claude"
    fake_process = NeverEndingProcess()

    async def fake_create_subprocess(*args, **kwargs):
        return fake_process

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess
    )

    fetcher = SmartFetcher(
        browser_client_factory=lambda: mock.AsyncMock(),
        logger=logger,
    )
    fetcher._claude_bin = fake_bin

    result = await fetcher._call_claude("https://example.com", timeout=0.01)

    assert result.ok is False
    assert result.reason == "timeout"
    assert fake_process._killed is True
    fake_process.stdout.close.assert_called_once()
    fake_process.stderr.close.assert_called_once()


@pytest.mark.asyncio
async def test_call_claude_nonzero_exit(logger, monkeypatch):
    fake_bin = "/fake/claude"

    class FailingProcess:
        def __init__(self):
            self.returncode = 1
            self.stdout = mock.Mock(close=mock.Mock())
            self.stderr = mock.Mock(close=mock.Mock())

        async def communicate(self):
            return b"", b"permission denied"

        def kill(self):
            pass

        async def wait(self):
            return self.returncode

    async def fake_create_subprocess(*args, **kwargs):
        return FailingProcess()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", fake_create_subprocess
    )

    fetcher = SmartFetcher(
        browser_client_factory=lambda: mock.AsyncMock(),
        logger=logger,
    )
    fetcher._claude_bin = fake_bin

    result = await fetcher._call_claude("https://example.com")

    assert result.ok is False
    assert result.reason == "nonzero_exit"


# ── 5. max_length truncation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_truncates_claude_text_to_max_length(logger, monkeypatch):
    monkeypatch.setenv("AI_BROWSER_SMART_FETCH_CLAUDE_FIRST", "true")
    import src.config as config
    monkeypatch.setattr(config, "SMART_FETCH_CLAUDE_FIRST", True)

    fetcher = SmartFetcher(
        browser_client_factory=lambda: mock.AsyncMock(),
        logger=logger,
    )
    fetcher._claude_bin = "/fake/claude"

    long_text = "A" * 5000
    fetcher._call_claude = mock.AsyncMock(
        return_value=ClaudeEngineResult(ok=True, text=long_text, title="A")
    )

    result = await fetcher.fetch("https://example.com", max_length=100)

    assert result["text_truncated"] is True
    assert len(result["text"]) == 100
    assert result["text"] == "A" * 100
