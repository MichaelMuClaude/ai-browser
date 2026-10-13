"""Unit tests for BrowserClient without requiring a running HTTP service."""
from unittest import mock
import httpx
import pytest

from src.client import BrowserClient, BrowserClientError


class TestBrowserClient:
    """Tests for BrowserClient using mocked httpx.AsyncClient."""

    def test_default_base_url(self):
        client = BrowserClient()
        assert client.base_url == "http://127.0.0.1:8888"

    def test_custom_base_url(self):
        client = BrowserClient(base_url="http://localhost:9999")
        assert client.base_url == "http://localhost:9999"

    @pytest.mark.asyncio
    async def test_health_parses_json(self):
        client = BrowserClient()
        mock_response = httpx.Response(
            200, json={"status": "ok"}, request=httpx.Request("GET", "http://127.0.0.1:8888/api/health")
        )
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.return_value = mock_response
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            result = await client.health()
            assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_navigate_posts_json(self):
        client = BrowserClient()
        mock_response = httpx.Response(
            200,
            json={"url": "https://example.com", "title": "Example"},
            request=httpx.Request("POST", "http://127.0.0.1:8888/api/navigate"),
        )
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.return_value = mock_response
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            result = await client.navigate("https://example.com")
            assert result["title"] == "Example"

    @pytest.mark.asyncio
    async def test_request_error_raises_browser_client_error(self):
        client = BrowserClient()
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.side_effect = httpx.ConnectError("connection refused")
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            with pytest.raises(BrowserClientError):
                await client.health()

    @pytest.mark.asyncio
    async def test_request_error_retries_then_recovers(self):
        client = BrowserClient()
        mock_response = httpx.Response(
            200, json={"status": "ok"}, request=httpx.Request("GET", "http://127.0.0.1:8888/api/health")
        )
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.side_effect = [
                httpx.ConnectError("connection refused"),
                mock_response,
            ]
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            result = await client.health()
            assert result["status"] == "ok"
            assert mock_ac.request.call_count == 2

    @pytest.mark.asyncio
    async def test_request_error_exhausts_retries(self):
        client = BrowserClient()
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.side_effect = [
                httpx.ConnectError("connection refused"),
                httpx.ConnectError("connection refused"),
            ]
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            with pytest.raises(BrowserClientError):
                await client.health()
            assert mock_ac.request.call_count == 2

    @pytest.mark.asyncio
    async def test_http_status_error_not_retried(self):
        client = BrowserClient()
        mock_response = httpx.Response(
            500,
            text="internal error",
            request=httpx.Request("GET", "http://127.0.0.1:8888/api/health"),
        )
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.return_value = mock_response
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            with pytest.raises(BrowserClientError) as exc_info:
                await client.health()
            assert exc_info.value.status_code == 500
            assert mock_ac.request.call_count == 1

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ReadTimeout("read timeout"),
            httpx.WriteError("write error"),
            httpx.PoolTimeout("pool timeout"),
        ],
    )
    @pytest.mark.asyncio
    async def test_non_connect_request_error_not_retried(self, error):
        """Read/write/pool timeouts must not retry: the daemon may have already
        received and acted on the request."""
        client = BrowserClient()
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.side_effect = error
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            with pytest.raises(BrowserClientError):
                await client.health()
            assert mock_ac.request.call_count == 1

    @pytest.mark.asyncio
    async def test_smart_fetch_includes_session_id_in_json_body(self):
        """Regression: non-default session_id must be sent in the JSON body
        because /api/smart_fetch reads session_id from the SmartFetchRequest
        payload, not query params.
        """
        client = BrowserClient()
        mock_response = httpx.Response(
            200,
            json={"text": "hello", "engine_used": "stealth"},
            request=httpx.Request("POST", "http://127.0.0.1:8888/api/smart_fetch"),
        )
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.return_value = mock_response
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            result = await client.smart_fetch(
                "https://example.com",
                session_id="my-session",
                fallback_screenshot=True,
            )

            assert result["text"] == "hello"
            call_kwargs = mock_ac.request.call_args.kwargs
            assert call_kwargs["json"]["session_id"] == "my-session"
            assert call_kwargs["json"]["url"] == "https://example.com"
            assert call_kwargs["json"]["fallback_screenshot"] is True
            assert call_kwargs["params"]["session_id"] == "my-session"

    @pytest.mark.asyncio
    async def test_search_includes_engine_in_json_body(self):
        """search(engine='bing') must put engine into the JSON payload."""
        client = BrowserClient()
        mock_response = httpx.Response(
            200,
            json={"search_url": "https://www.bing.com/search?q=test", "search_engine": "bing"},
            request=httpx.Request("POST", "http://127.0.0.1:8888/api/search"),
        )
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.return_value = mock_response
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            result = await client.search("test", engine="bing")

            assert result["search_engine"] == "bing"
            call_kwargs = mock_ac.request.call_args.kwargs
            assert call_kwargs["json"]["query"] == "test"
            assert call_kwargs["json"]["engine"] == "bing"
