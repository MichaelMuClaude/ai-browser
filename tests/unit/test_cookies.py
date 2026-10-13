"""Unit tests for cookie APIs without launching a real browser."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.core.native_os import NativeEngine
from src.core.stealth_cdp import StealthEngine


def _cookie(name, value, domain, path="/", secure=False, http_only=False, expires=-1):
    return SimpleNamespace(
        name=name,
        value=value,
        domain=domain,
        path=path,
        secure=secure,
        http_only=http_only,
        expires=expires,
    )


def _started_stealth_engine():
    engine = StealthEngine()
    engine.browser = object()
    engine.tab = AsyncMock()
    return engine


@pytest.mark.asyncio
async def test_get_cookies_returns_list():
    engine = _started_stealth_engine()
    engine.tab.send.return_value = [
        _cookie("session", "abc", ".example.com", secure=True, http_only=True),
        _cookie("theme", "dark", "docs.example.com"),
    ]

    with patch("src.core.stealth_cdp.uc.cdp.storage.get_cookies", return_value="get-cookies-command") as get_cookies:
        cookies = await engine.get_cookies()

    get_cookies.assert_called_once_with()
    engine.tab.send.assert_awaited_once_with("get-cookies-command")
    assert cookies == [
        {
            "name": "session",
            "value": "abc",
            "domain": ".example.com",
            "path": "/",
            "secure": True,
            "http_only": True,
            "expires": -1,
        },
        {
            "name": "theme",
            "value": "dark",
            "domain": "docs.example.com",
            "path": "/",
            "secure": False,
            "http_only": False,
            "expires": -1,
        },
    ]


@pytest.mark.asyncio
async def test_get_cookies_domain_filter():
    engine = _started_stealth_engine()
    engine.tab.send.return_value = [
        _cookie("a", "1", ".example.com"),
        _cookie("b", "2", "other.test"),
        _cookie("c", "3", "docs.example.com"),
    ]

    cookies = await engine.get_cookies(domain="example.com")

    assert [cookie["name"] for cookie in cookies] == ["a", "c"]
    assert all("example.com" in cookie["domain"] for cookie in cookies)


@pytest.mark.asyncio
async def test_set_cookie_calls_cdp():
    engine = _started_stealth_engine()

    with patch("src.core.stealth_cdp.uc.cdp.network.set_cookie", return_value="set-cookie-command") as set_cookie:
        result = await engine.set_cookie(
            "token",
            "abc123",
            "example.com",
            path="/app",
            expires=12345,
            secure=True,
            http_only=True,
        )

    set_cookie.assert_called_once_with(
        name="token",
        value="abc123",
        domain="example.com",
        path="/app",
        expires=12345,
        secure=True,
        http_only=True,
    )
    engine.tab.send.assert_awaited_once_with("set-cookie-command")
    assert result == {"name": "token", "domain": "example.com", "path": "/app"}


@pytest.mark.asyncio
async def test_set_cookie_omits_negative_expires():
    engine = _started_stealth_engine()

    with patch("src.core.stealth_cdp.uc.cdp.network.set_cookie", return_value="set-cookie-command") as set_cookie:
        await engine.set_cookie("token", "abc123", "example.com")

    assert set_cookie.call_args.kwargs["expires"] is None


@pytest.mark.asyncio
async def test_clear_all_cookies():
    engine = _started_stealth_engine()

    with patch(
        "src.core.stealth_cdp.uc.cdp.network.clear_browser_cookies",
        return_value="clear-command",
    ) as clear_browser_cookies:
        result = await engine.clear_cookies()

    clear_browser_cookies.assert_called_once_with()
    engine.tab.send.assert_awaited_once_with("clear-command")
    assert result == {"cleared": "all"}


@pytest.mark.asyncio
async def test_clear_cookies_by_domain():
    engine = _started_stealth_engine()

    with patch.object(
        engine,
        "get_cookies",
        AsyncMock(
            return_value=[
                {"name": "session", "domain": ".example.com", "path": "/"},
                {"name": "theme", "domain": "docs.example.com", "path": "/app"},
            ]
        ),
    ) as get_cookies:
        with patch(
            "src.core.stealth_cdp.uc.cdp.network.delete_cookies",
            side_effect=["delete-session", "delete-theme"],
        ) as delete_cookies:
            result = await engine.clear_cookies(domain="example.com")

    get_cookies.assert_awaited_once_with(domain="example.com")
    assert delete_cookies.call_count == 2
    assert engine.tab.send.await_args_list[0].args == ("delete-session",)
    assert engine.tab.send.await_args_list[1].args == ("delete-theme",)
    assert result == {"cleared": 2, "domain": "example.com"}


@pytest.mark.asyncio
async def test_native_engine_cookie_unsupported():
    engine = NativeEngine()

    set_result = await engine.set_cookie("x", "y", "z.com")
    clear_result = await engine.clear_cookies("z.com")
    cookies = await engine.get_cookies("z.com")

    assert set_result.get("error") == "ENGINE_NOT_SUPPORTED"
    assert clear_result.get("error") == "ENGINE_NOT_SUPPORTED"
    assert cookies == []
