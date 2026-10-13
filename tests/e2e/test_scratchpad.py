"""E2E tests for browser scratchpad (session-scoped research buffer)."""
import asyncio

import pytest
import requests

from src.client import BrowserClient


@pytest.mark.smoke
def test_scratchpad_add_get_clear(base_url):
    """Add entries, verify order/URL/timestamp, then clear."""
    session_id = requests.post(f"{base_url}/api/sessions", json={}).json()["session_id"]
    try:
        requests.post(
            f"{base_url}/api/navigate",
            params={"session_id": session_id},
            json={"url": "https://example.com"},
            timeout=30,
        ).raise_for_status()

        add1 = requests.post(
            f"{base_url}/api/scratchpad",
            params={"session_id": session_id},
            json={"content": "First note from example.com", "label": "note"},
        )
        add1.raise_for_status()
        entry1 = add1.json()

        requests.post(
            f"{base_url}/api/navigate",
            params={"session_id": session_id},
            json={"url": "https://iana.org"},
            timeout=30,
        ).raise_for_status()

        add2 = requests.post(
            f"{base_url}/api/scratchpad",
            params={"session_id": session_id},
            json={"content": "Second note from iana"},
        )
        add2.raise_for_status()
        entry2 = add2.json()

        # Verify individual entries
        assert entry1["id"] == 1
        assert entry1["content"] == "First note from example.com"
        assert entry1["label"] == "note"
        assert "example.com" in entry1["url"]
        assert "timestamp" in entry1

        assert entry2["id"] == 2
        assert entry2["content"] == "Second note from iana"
        assert entry2["label"] == ""
        assert "iana.org" in entry2["url"]
        assert "timestamp" in entry2

        # Verify GET returns both in order
        get_resp = requests.get(
            f"{base_url}/api/scratchpad", params={"session_id": session_id}
        )
        get_resp.raise_for_status()
        entries = get_resp.json()["entries"]
        assert len(entries) == 2
        assert entries[0]["id"] == 1
        assert entries[1]["id"] == 2
        assert entries[0]["content"] == "First note from example.com"
        assert entries[1]["content"] == "Second note from iana"

        # Verify clear
        clear_resp = requests.delete(
            f"{base_url}/api/scratchpad", params={"session_id": session_id}
        )
        clear_resp.raise_for_status()
        assert clear_resp.json() == {"cleared": True, "count": 2}

        get_after = requests.get(
            f"{base_url}/api/scratchpad", params={"session_id": session_id}
        )
        get_after.raise_for_status()
        assert get_after.json()["entries"] == []
    finally:
        requests.delete(f"{base_url}/api/sessions/{session_id}")


@pytest.mark.smoke
def test_scratchpad_isolated_between_sessions(base_url):
    """Two sessions must not share scratchpad entries (assert on content)."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        session_a = (await client.create_session())["session_id"]
        session_b = (await client.create_session())["session_id"]

        try:
            await client.navigate("https://example.com", session_id=session_a, return_look=False)
            await client.navigate("https://iana.org", session_id=session_b, return_look=False)

            await asyncio.sleep(2)

            await client.scratchpad_add(
                "Session A content about example.com",
                label="a",
                session_id=session_a,
            )
            await client.scratchpad_add(
                "Session B content about iana",
                label="b",
                session_id=session_b,
            )

            entries_a = (await client.scratchpad_get(session_id=session_a))["entries"]
            entries_b = (await client.scratchpad_get(session_id=session_b))["entries"]

            assert len(entries_a) == 1
            assert len(entries_b) == 1

            # Assert on actual content difference, not just metadata
            assert "Session A content" in entries_a[0]["content"]
            assert "Session B content" in entries_b[0]["content"]
            assert entries_a[0]["content"] != entries_b[0]["content"]

            assert "example.com" in entries_a[0]["url"]
            assert "iana.org" in entries_b[0]["url"]

            # A must not contain B's content and vice versa
            assert "Session B" not in entries_a[0]["content"]
            assert "Session A" not in entries_b[0]["content"]
        finally:
            await client.delete_session(session_a)
            await client.delete_session(session_b)
            await client.close()

    asyncio.run(_run())


@pytest.mark.smoke
def test_scratchpad_rejects_invalid_session_id(base_url):
    """Scratchpad endpoints must validate session_id and return 400."""
    invalid_ids = ["../x", "a/b", "a" * 65, "session with spaces"]
    for bad_id in invalid_ids:
        post_resp = requests.post(
            f"{base_url}/api/scratchpad",
            params={"session_id": bad_id},
            json={"content": "x"},
        )
        assert post_resp.status_code == 400, (
            f"POST /api/scratchpad expected 400 for {bad_id!r}, got {post_resp.status_code}"
        )

        get_resp = requests.get(
            f"{base_url}/api/scratchpad", params={"session_id": bad_id}
        )
        assert get_resp.status_code == 400, (
            f"GET /api/scratchpad expected 400 for {bad_id!r}, got {get_resp.status_code}"
        )

        delete_resp = requests.delete(
            f"{base_url}/api/scratchpad", params={"session_id": bad_id}
        )
        assert delete_resp.status_code == 400, (
            f"DELETE /api/scratchpad expected 400 for {bad_id!r}, got {delete_resp.status_code}"
        )
