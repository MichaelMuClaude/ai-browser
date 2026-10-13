"""Unit tests for browser scratchpad functionality."""
from unittest import mock

import httpx
import pytest

from src.client import BrowserClient
from src.core.session import BrowserSession


class TestBrowserSessionScratchpad:
    """Tests for BrowserSession's in-memory scratchpad."""

    def test_add_scratchpad_entry_returns_tagged_entry(self):
        session = BrowserSession(
            session_id="test",
            engine_manager=mock.MagicMock(),
            profile_dir="/tmp/test",
        )
        entry = session.add_scratchpad_entry(
            content="Price is $49",
            url="https://store-a.example.com/product",
            label="price",
        )
        assert entry["id"] == 1
        assert entry["content"] == "Price is $49"
        assert entry["url"] == "https://store-a.example.com/product"
        assert entry["label"] == "price"
        assert "timestamp" in entry

    def test_get_scratchpad_entries_returns_insertion_order(self):
        session = BrowserSession(
            session_id="test",
            engine_manager=mock.MagicMock(),
            profile_dir="/tmp/test",
        )
        session.add_scratchpad_entry("first", url="https://a.com", label="a")
        session.add_scratchpad_entry("second", url="https://b.com", label="b")

        entries = session.get_scratchpad_entries()

        assert len(entries) == 2
        assert entries[0]["id"] == 1
        assert entries[0]["content"] == "first"
        assert entries[1]["id"] == 2
        assert entries[1]["content"] == "second"

    def test_clear_scratchpad_empties_list_and_returns_count(self):
        session = BrowserSession(
            session_id="test",
            engine_manager=mock.MagicMock(),
            profile_dir="/tmp/test",
        )
        session.add_scratchpad_entry("one", url="https://a.com")
        session.add_scratchpad_entry("two", url="https://b.com")

        count = session.clear_scratchpad()

        assert count == 2
        assert session.get_scratchpad_entries() == []

    def test_clear_empty_scratchpad_returns_zero(self):
        session = BrowserSession(
            session_id="test",
            engine_manager=mock.MagicMock(),
            profile_dir="/tmp/test",
        )
        assert session.clear_scratchpad() == 0

    def test_default_label_is_empty_string(self):
        session = BrowserSession(
            session_id="test",
            engine_manager=mock.MagicMock(),
            profile_dir="/tmp/test",
        )
        entry = session.add_scratchpad_entry("note", url="https://a.com")
        assert entry["label"] == ""


class TestBrowserClientScratchpad:
    """Tests for BrowserClient scratchpad methods using mocked HTTP."""

    @pytest.mark.asyncio
    async def test_scratchpad_add_posts_content_and_label(self):
        client = BrowserClient()
        mock_response = httpx.Response(
            200,
            json={
                "id": 1,
                "content": "note text",
                "url": "https://example.com",
                "timestamp": "2026-07-13T10:00:00",
                "label": "claim",
            },
            request=httpx.Request("POST", "http://127.0.0.1:8888/api/scratchpad"),
        )
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.return_value = mock_response
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            result = await client.scratchpad_add(
                "note text", label="claim", session_id="research"
            )

            assert result["content"] == "note text"
            assert result["label"] == "claim"
            mock_ac.request.assert_awaited_once()
            call_args = mock_ac.request.call_args
            assert call_args.kwargs["json"] == {"content": "note text", "label": "claim"}
            assert call_args.kwargs["params"] == {"session_id": "research"}

    @pytest.mark.asyncio
    async def test_scratchpad_add_without_label_omits_label_field(self):
        client = BrowserClient()
        mock_response = httpx.Response(
            200,
            json={"id": 1, "content": "note text", "url": "", "timestamp": "2026-07-13T10:00:00", "label": ""},
            request=httpx.Request("POST", "http://127.0.0.1:8888/api/scratchpad"),
        )
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.return_value = mock_response
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            await client.scratchpad_add("note text")

            call_args = mock_ac.request.call_args
            assert call_args.kwargs["json"] == {"content": "note text"}

    @pytest.mark.asyncio
    async def test_scratchpad_get_returns_entries(self):
        client = BrowserClient()
        mock_response = httpx.Response(
            200,
            json={"entries": [{"id": 1, "content": "note"}]},
            request=httpx.Request("GET", "http://127.0.0.1:8888/api/scratchpad"),
        )
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.return_value = mock_response
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            result = await client.scratchpad_get(session_id="research")

            assert result["entries"] == [{"id": 1, "content": "note"}]
            call_args = mock_ac.request.call_args
            assert call_args.args[0] == "GET"
            assert call_args.args[1] == "/api/scratchpad"
            assert call_args.kwargs["params"] == {"session_id": "research"}

    @pytest.mark.asyncio
    async def test_scratchpad_clear_returns_count(self):
        client = BrowserClient()
        mock_response = httpx.Response(
            200,
            json={"cleared": True, "count": 3},
            request=httpx.Request("DELETE", "http://127.0.0.1:8888/api/scratchpad"),
        )
        with mock.patch.object(client, "_get_client") as mock_get:
            mock_ac = mock.AsyncMock()
            mock_ac.request.return_value = mock_response
            mock_ac.is_closed = False
            mock_get.return_value = mock_ac

            result = await client.scratchpad_clear(session_id="research")

            assert result == {"cleared": True, "count": 3}
            call_args = mock_ac.request.call_args
            assert call_args.args[0] == "DELETE"
            assert call_args.args[1] == "/api/scratchpad"
