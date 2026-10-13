"""Unit tests for StealthEngine control helpers without a real browser."""
import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from nodriver import ProtocolException

from src.core.stealth_cdp import StealthEngine


def _started_engine():
    engine = StealthEngine()
    engine.browser = object()
    engine.tab = AsyncMock()
    return engine


@dataclass
class FakeRemoteObject:
    """Fake nodriver RemoteObject dataclass wrapper."""

    type_: str
    value: object


@pytest.mark.asyncio
async def test_go_back_calls_history_back():
    engine = _started_engine()

    with patch.object(engine, "get_url", AsyncMock(return_value="https://example.com/previous")):
        with patch.object(engine, "get_title", AsyncMock(return_value="Previous")):
            result = await engine.go_back()

    engine.tab.evaluate.assert_awaited_once_with("history.back()")
    assert result == {"url": "https://example.com/previous", "title": "Previous"}


@pytest.mark.asyncio
async def test_go_forward_calls_history_forward():
    engine = _started_engine()

    with patch.object(engine, "get_url", AsyncMock(return_value="https://example.com/next")):
        with patch.object(engine, "get_title", AsyncMock(return_value="Next")):
            result = await engine.go_forward()

    engine.tab.evaluate.assert_awaited_once_with("history.forward()")
    assert result == {"url": "https://example.com/next", "title": "Next"}


@pytest.mark.asyncio
async def test_reload_waits_for_ready_state():
    engine = _started_engine()
    engine.tab.evaluate.side_effect = ["loading", "complete"]

    with patch.object(engine, "get_url", AsyncMock(return_value="https://example.com")):
        with patch.object(engine, "get_title", AsyncMock(return_value="Example")):
            result = await engine.reload()

    engine.tab.reload.assert_awaited_once_with()
    assert engine.tab.evaluate.await_args_list[0].args == ("document.readyState",)
    assert engine.tab.evaluate.await_args_list[0].kwargs == {"return_by_value": True}
    assert result == {"url": "https://example.com", "title": "Example"}


@pytest.mark.asyncio
async def test_navigate_recovers_from_dead_session():
    engine = _started_engine()
    dead_tab = engine.tab
    engine._tabs["tab-0"] = dead_tab
    dead_tab.get.side_effect = ProtocolException({"message": "Session with given id not found"})

    fresh_tab = AsyncMock()
    fresh_tab.evaluate.return_value = "complete"
    engine.browser = AsyncMock()
    engine.browser.get = AsyncMock(return_value=fresh_tab)

    with patch.object(engine, "get_title", AsyncMock(return_value="Recovered")):
        with patch.object(engine, "get_url", AsyncMock(return_value="https://example.com")):
            result = await engine.navigate("https://example.com")

    assert engine.tab is fresh_tab
    assert engine._tabs["tab-0"] is fresh_tab
    fresh_tab.get.assert_awaited_once_with("https://example.com")
    assert result["url"] == "https://example.com"


@pytest.mark.asyncio
async def test_navigate_times_out_instead_of_hanging_forever():
    """Real case (2026-07-26): kmart.com.au never fires the navigation-complete
    CDP event, so nodriver's tab.get() hangs forever. navigate() must bound that
    wait and raise a clear, catchable error instead of blocking the tab forever."""
    engine = _started_engine()

    async def never_returns(url):
        await asyncio.sleep(10)

    engine.tab.get = never_returns

    with patch("src.core.stealth_cdp.STEALTH_NAVIGATE_TIMEOUT_SECONDS", 0.05):
        with pytest.raises(TimeoutError, match="timed out"):
            await engine.navigate("https://kmart.com.au")


@pytest.mark.asyncio
async def test_open_tab_times_out_instead_of_hanging_forever():
    """Real case (2026-07-26): opening a new tab to amazon.com.au took long
    enough to blow past a 30s client timeout. Unlike navigate()'s tab.get(),
    browser.get(new_tab=True) had no timeout at all — bound it the same way."""
    engine = _started_engine()

    async def never_returns(url, new_tab=True):
        await asyncio.sleep(10)

    engine.browser = AsyncMock()
    engine.browser.get = never_returns

    with patch("src.core.stealth_cdp.STEALTH_NAVIGATE_TIMEOUT_SECONDS", 0.05):
        with pytest.raises(TimeoutError, match="timed out"):
            await engine.open_tab("https://www.amazon.com.au/")


@pytest.mark.asyncio
async def test_select_option_by_value():
    engine = _started_engine()
    engine.tab.evaluate.return_value = {"value": "au", "text": "Australia"}

    result = await engine.select("#country", "au")

    js = engine.tab.evaluate.await_args.args[0]
    assert 'document.querySelector("#country")' in js
    assert 'o.value === "au" || o.text === "au"' in js
    assert "dispatchEvent(new Event('change'" in js
    assert engine.tab.evaluate.await_args.kwargs == {"return_by_value": True}
    assert result == {
        "selector": "#country",
        "value": "au",
        "result": {"value": "au", "text": "Australia"},
    }


@pytest.mark.asyncio
async def test_select_element_not_found():
    engine = _started_engine()
    engine.tab.evaluate.return_value = {
        "exception_id": 1,
        "exception": {"description": "Error: Element not found: #missing"},
    }

    result = await engine.select("#missing", "au")

    assert result["selector"] == "#missing"
    assert result["value"] == "au"
    assert result["error"] == "Error: Element not found: #missing"


@pytest.mark.asyncio
async def test_check_unchecked_checkbox():
    engine = _started_engine()
    engine.tab.evaluate.return_value = {"checked": True, "value": "yes"}

    result = await engine.check("#agree")

    js = engine.tab.evaluate.await_args.args[0]
    assert 'document.querySelector("#agree")' in js
    assert "if (!el.checked) { el.click(); }" in js
    assert result == {
        "selector": "#agree",
        "result": {"checked": True, "value": "yes"},
    }


@pytest.mark.asyncio
async def test_check_already_checked_is_idempotent():
    engine = _started_engine()
    engine.tab.evaluate.return_value = {"checked": True, "value": "yes"}

    result = await engine.check("#agree")

    js = engine.tab.evaluate.await_args.args[0]
    assert "if (!el.checked) { el.click(); }" in js
    assert "el.click();" in js
    assert result["result"]["checked"] is True


@pytest.mark.asyncio
async def test_uncheck_checkbox():
    engine = _started_engine()
    engine.tab.evaluate.return_value = {"checked": False, "value": "yes"}

    result = await engine.uncheck("#agree")

    js = engine.tab.evaluate.await_args.args[0]
    assert 'document.querySelector("#agree")' in js
    assert "if (el.checked) { el.click(); }" in js
    assert result == {
        "selector": "#agree",
        "result": {"checked": False, "value": "yes"},
    }


# --- _eval_string / Bug 1 regression tests ---


@pytest.mark.asyncio
async def test_eval_string_returns_string():
    engine = _started_engine()
    engine.tab.evaluate.return_value = "hello"
    assert await engine._eval_string("document.title") == "hello"


@pytest.mark.asyncio
async def test_eval_string_unwraps_remoteobject_string():
    engine = _started_engine()
    engine.tab.evaluate.return_value = FakeRemoteObject("string", "wrapped")
    assert await engine._eval_string("document.title") == "wrapped"


@pytest.mark.asyncio
async def test_eval_string_retries_on_remoteobject_then_succeeds():
    engine = _started_engine()
    engine.tab.evaluate.side_effect = [
        FakeRemoteObject("object", {"foo": 1}),
        "real text",
    ]
    assert await engine._eval_string("document.body.innerText") == "real text"
    assert engine.tab.evaluate.await_count == 2


@pytest.mark.asyncio
async def test_eval_string_exhausts_retries_returns_none():
    engine = _started_engine()
    engine.tab.evaluate.return_value = FakeRemoteObject("object", {"foo": 1})
    assert await engine._eval_string("document.body.innerText") is None
    assert engine.tab.evaluate.await_count == 3


@pytest.mark.asyncio
async def test_eval_string_retries_on_navigated_exception():
    engine = _started_engine()
    engine.tab.evaluate.side_effect = [
        ProtocolException({"message": "Session navigated or closed"}),
        "ok",
    ]
    assert await engine._eval_string("document.title") == "ok"
    assert engine.tab.evaluate.await_count == 2


@pytest.mark.asyncio
async def test_eval_string_raises_non_navigated_protocol_exception():
    engine = _started_engine()
    engine.tab.evaluate.side_effect = ProtocolException({"message": "something else"})
    with pytest.raises(ProtocolException):
        await engine._eval_string("document.title")


@pytest.mark.asyncio
async def test_get_text_no_garbage_on_remoteobject():
    engine = _started_engine()
    engine.tab.evaluate.return_value = FakeRemoteObject("object", {"foo": 1})
    result = await engine.get_text()
    assert result == {"text": ""}


@pytest.mark.asyncio
async def test_get_text_default_uses_body_innertext():
    """Default (main_content_only=False) must be unchanged: plain body.innerText,
    not the nav-stripping extraction — existing callers rely on this."""
    engine = _started_engine()
    engine.tab.evaluate.return_value = "hello world"
    result = await engine.get_text()
    assert result == {"text": "hello world"}
    js_used = engine.tab.evaluate.await_args.args[0]
    assert js_used == "document.body.innerText"


@pytest.mark.asyncio
async def test_get_text_main_content_only_strips_boilerplate():
    """Real pain point (2026-07-26): Amazon/Kmart search results bury actual
    product text under thousands of characters of nav/department lists.
    main_content_only=True must use the nav-stripping JS instead."""
    engine = _started_engine()
    engine.tab.evaluate.return_value = "just the product listings"
    result = await engine.get_text(main_content_only=True)
    assert result == {"text": "just the product listings"}
    js_used = engine.tab.evaluate.await_args.args[0]
    assert js_used != "document.body.innerText"
    assert "hideSelectors" in js_used
    assert "mainSelectors" in js_used


@pytest.mark.asyncio
async def test_get_title_no_garbage_on_remoteobject():
    engine = _started_engine()
    engine.tab.evaluate.return_value = FakeRemoteObject("object", {"foo": 1})
    assert await engine.get_title() == ""


@pytest.mark.asyncio
async def test_get_url_no_garbage_on_remoteobject():
    engine = _started_engine()
    engine.tab.evaluate.return_value = FakeRemoteObject("object", {"foo": 1})
    assert await engine.get_url() == ""


@pytest.mark.asyncio
async def test_get_title_for_tab_no_garbage_on_remoteobject():
    engine = _started_engine()
    tab = AsyncMock()
    tab.evaluate.return_value = FakeRemoteObject("object", {"foo": 1})
    assert await engine._get_title_for_tab(tab) == ""


@pytest.mark.asyncio
async def test_get_url_for_tab_no_garbage_on_remoteobject():
    engine = _started_engine()
    tab = AsyncMock()
    tab.evaluate.return_value = FakeRemoteObject("object", {"foo": 1})
    assert await engine._get_url_for_tab(tab) == ""
