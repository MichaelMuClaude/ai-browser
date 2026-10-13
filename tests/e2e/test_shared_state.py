"""E2E test verifying HTTP API and MCP client share the same browser state.

Since the MCP server is now a thin wrapper around BrowserClient, this test
validates the shared-state contract at the client level: changes made through
the HTTP API are visible to BrowserClient, and vice versa.
"""
import asyncio
import pytest
import requests

from src.client import BrowserClient


@pytest.mark.smoke
def test_http_navigate_visible_to_client(base_url):
    """A page navigated via HTTP API is visible to BrowserClient."""
    requests.post(
        f"{base_url}/api/navigate",
        json={"url": "https://example.com", "return_look": False},
        timeout=30,
    ).raise_for_status()

    async def _check():
        client = BrowserClient(base_url=base_url)
        title = await client.get_title()
        await client.close()
        return title

    title = asyncio.run(_check())
    assert "title" in title
    assert title["title"] != ""


@pytest.mark.smoke
def test_client_navigate_visible_to_http(base_url):
    """A page navigated via BrowserClient is visible to HTTP API."""
    async def _navigate():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        client_title = await client.get_title()
        await client.close()
        return client_title

    client_title = asyncio.run(_navigate())

    response = requests.get(f"{base_url}/api/title", timeout=10)
    response.raise_for_status()
    http_title = response.json()

    assert http_title["title"] == client_title["title"]
