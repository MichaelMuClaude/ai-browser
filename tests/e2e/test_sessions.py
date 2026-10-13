"""E2E tests for browser session isolation and lifecycle."""
import asyncio
import os
import time

import pytest
import requests

from src.client import BrowserClient


@pytest.mark.smoke
def test_create_session(base_url):
    """A new session can be created and listed."""
    response = requests.post(f"{base_url}/api/sessions", json={})
    response.raise_for_status()
    data = response.json()
    assert "session_id" in data
    session_id = data["session_id"]

    list_resp = requests.get(f"{base_url}/api/sessions")
    list_resp.raise_for_status()
    sessions = list_resp.json()["sessions"]
    assert any(s["session_id"] == session_id for s in sessions)

    # Cleanup
    requests.delete(f"{base_url}/api/sessions/{session_id}").raise_for_status()


@pytest.mark.smoke
def test_session_isolation(base_url):
    """Two sessions can hold different pages at the same time."""
    async def _run():
        client = BrowserClient(base_url=base_url)

        session_a = (await client.create_session())["session_id"]
        session_b = (await client.create_session())["session_id"]

        try:
            await client.navigate("https://example.com", session_id=session_a, return_look=False)
            await client.navigate("https://iana.org", session_id=session_b, return_look=False)

            await asyncio.sleep(2)

            title_a = (await client.get_title(session_id=session_a))["title"]
            title_b = (await client.get_title(session_id=session_b))["title"]
            url_a = (await client.get_url(session_id=session_a))["url"]
            url_b = (await client.get_url(session_id=session_b))["url"]

            assert title_a != title_b, f"Titles should differ: {title_a!r} vs {title_b!r}"
            assert url_a != url_b, f"URLs should differ: {url_a!r} vs {url_b!r}"
            assert "example.com" in url_a
            assert "iana.org" in url_b
        finally:
            await client.delete_session(session_a)
            await client.delete_session(session_b)
            await client.close()

    asyncio.run(_run())


@pytest.mark.smoke
def test_default_session_still_works(base_url):
    """Existing endpoints without session_id still use the default session."""
    requests.post(
        f"{base_url}/api/navigate",
        json={"url": "https://example.com", "return_look": False},
        timeout=30,
    ).raise_for_status()

    response = requests.get(f"{base_url}/api/title")
    response.raise_for_status()
    data = response.json()
    assert data["title"] == "Example Domain"
    assert data["session_id"] == "default"


@pytest.mark.smoke
def test_session_id_validation(base_url):
    """Invalid session IDs are rejected with 400."""
    invalid_ids = ["../x", "a/b", "", "a" * 65, "session with spaces"]
    for bad_id in invalid_ids:
        resp = requests.post(f"{base_url}/api/sessions", json={"session_id": bad_id})
        assert resp.status_code == 400, f"Expected 400 for {bad_id!r}, got {resp.status_code}"

    for good_id in ["test-1", "test_2", "test.3", "abc123"]:
        resp = requests.post(f"{base_url}/api/sessions", json={"session_id": good_id})
        assert resp.status_code == 200, f"Expected 200 for {good_id!r}, got {resp.status_code}"
        requests.delete(f"{base_url}/api/sessions/{good_id}").raise_for_status()


@pytest.mark.smoke
def test_tab_endpoints_reject_invalid_session_id(base_url):
    """Tab endpoints must validate session_id and return 400 for invalid values."""
    invalid_ids = ["../x", "a/b", "a" * 65, "session with spaces"]
    for bad_id in invalid_ids:
        list_resp = requests.get(f"{base_url}/api/tabs", params={"session_id": bad_id})
        assert list_resp.status_code == 400, (
            f"GET /api/tabs expected 400 for {bad_id!r}, got {list_resp.status_code}"
        )

        create_resp = requests.post(
            f"{base_url}/api/tabs", json={}, params={"session_id": bad_id}
        )
        assert create_resp.status_code == 400, (
            f"POST /api/tabs expected 400 for {bad_id!r}, got {create_resp.status_code}"
        )

        activate_resp = requests.post(
            f"{base_url}/api/tabs/foo/activate", params={"session_id": bad_id}
        )
        assert activate_resp.status_code == 400, (
            f"POST /api/tabs/foo/activate expected 400 for {bad_id!r}, "
            f"got {activate_resp.status_code}"
        )


@pytest.mark.smoke
def test_default_session_cannot_be_deleted(base_url):
    """The default session must be protected from deletion."""
    resp = requests.delete(f"{base_url}/api/sessions/default")
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}"
    assert "default" in resp.json()["detail"].lower()

    # Default session should still work.
    health = requests.get(f"{base_url}/api/health")
    health.raise_for_status()


@pytest.mark.smoke
def test_create_tab_auto_generates_ids(base_url):
    """Opening multiple tabs should auto-generate unique tab_ids."""
    session_id = requests.post(f"{base_url}/api/sessions", json={}).json()["session_id"]
    try:
        # Open two tabs
        resp1 = requests.post(
            f"{base_url}/api/tabs", json={"url": "https://example.com"}, params={"session_id": session_id}
        )
        assert resp1.status_code == 200
        tab_id_1 = resp1.json()["tab_id"]

        resp2 = requests.post(
            f"{base_url}/api/tabs", json={"url": "https://example.com"}, params={"session_id": session_id}
        )
        assert resp2.status_code == 200
        tab_id_2 = resp2.json()["tab_id"]

        # IDs should be different
        assert tab_id_1 != tab_id_2, f"Expected unique tab IDs, got {tab_id_1} twice"
    finally:
        requests.delete(f"{base_url}/api/sessions/{session_id}")


@pytest.mark.smoke
def test_activate_nonexistent_tab_returns_404(base_url):
    """Activating a non-existent tab should return 404."""
    session_id = requests.post(f"{base_url}/api/sessions", json={}).json()["session_id"]
    try:
        resp = requests.post(
            f"{base_url}/api/tabs/nonexistent-tab/activate",
            params={"session_id": session_id},
        )
        assert resp.status_code == 404, f"Expected 404, got {resp.status_code}"
    finally:
        requests.delete(f"{base_url}/api/sessions/{session_id}")


@pytest.mark.smoke
def test_lru_cap_e2e(base_url):
    """When the non-default session cap is reached, the LRU session is evicted."""
    session_ids = []
    try:
        # Default cap is 3; creating a 4th session should evict the first.
        for i in range(4):
            sid = f"lru-test-{i}"
            resp = requests.post(f"{base_url}/api/sessions", json={"session_id": sid})
            assert resp.status_code == 200, f"Failed to create {sid}: {resp.text}"
            session_ids.append(sid)
            # Small stagger so LRU ordering is deterministic
            time.sleep(0.2)

        list_resp = requests.get(f"{base_url}/api/sessions")
        list_resp.raise_for_status()
        current_ids = {s["session_id"] for s in list_resp.json()["sessions"]}

        assert "lru-test-0" not in current_ids, "LRU session should have been evicted"
        assert {"lru-test-1", "lru-test-2", "lru-test-3"}.issubset(current_ids)
    finally:
        for sid in session_ids:
            if sid == "lru-test-0":
                continue  # already evicted
            requests.delete(f"{base_url}/api/sessions/{sid}")


@pytest.mark.smoke
def test_default_session_unaffected_by_lru(base_url):
    """LRU eviction must never remove the default session."""
    session_ids = []
    try:
        for i in range(4):
            sid = f"default-safe-{i}"
            resp = requests.post(f"{base_url}/api/sessions", json={"session_id": sid})
            assert resp.status_code == 200, f"Failed to create {sid}: {resp.text}"
            session_ids.append(sid)
            time.sleep(0.2)

        health = requests.get(f"{base_url}/api/health")
        health.raise_for_status()
        assert health.json()["sessions"] >= 3

        # Default session should still be usable
        resp = requests.post(
            f"{base_url}/api/navigate",
            json={"url": "https://example.com", "return_look": False},
            timeout=30,
        )
        resp.raise_for_status()
        assert resp.json()["session_id"] == "default"
    finally:
        for sid in session_ids:
            requests.delete(f"{base_url}/api/sessions/{sid}")


@pytest.mark.smoke
@pytest.mark.skipif(
    int(os.environ.get("AI_BROWSER_E2E_SHORT_TTL", "0")) == 0,
    reason=(
        "Idle expiry e2e requires the server to be started with a short TTL and reaper interval, "
        "e.g. AI_BROWSER_SESSION_IDLE_TTL=2 AI_BROWSER_SESSION_REAPER_INTERVAL=1"
    ),
)
def test_idle_expiry_e2e(base_url):
    """A non-default session is removed after being idle longer than the TTL."""
    ttl = int(os.environ["AI_BROWSER_E2E_SHORT_TTL"])
    sid = "idle-test"
    try:
        resp = requests.post(f"{base_url}/api/sessions", json={"session_id": sid})
        assert resp.status_code == 200

        # Wait slightly longer than the TTL for the reaper to run
        time.sleep(ttl + 2)

        list_resp = requests.get(f"{base_url}/api/sessions")
        list_resp.raise_for_status()
        current_ids = {s["session_id"] for s in list_resp.json()["sessions"]}

        assert sid not in current_ids, f"Session {sid} should have expired after {ttl}s idle"
    finally:
        requests.delete(f"{base_url}/api/sessions/{sid}")
