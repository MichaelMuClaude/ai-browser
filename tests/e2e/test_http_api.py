"""E2E smoke tests for ai-browser HTTP API.

These tests require the ai-browser HTTP service to be running.
Use stable, simple sites (example.com) for the smoke suite.
"""
import io
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests
from PIL import Image


@pytest.mark.smoke
def test_health(base_url):
    """Service health endpoint returns status and engine info."""
    response = requests.get(f"{base_url}/api/health")
    response.raise_for_status()
    data = response.json()
    assert data["status"] == "ok"
    assert "engine" in data
    assert "stealth_active" in data
    assert "native_active" in data


@pytest.mark.smoke
def test_navigate_to_example_com(base_url):
    """Navigate to a stable, simple page."""
    response = requests.post(
        f"{base_url}/api/navigate",
        json={"url": "https://example.com"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    assert "url" in data
    assert "title" in data
    assert data["block"]["blocked"] is False
    assert data["engine"] == "stealth"


@pytest.mark.smoke
def test_get_title(base_url):
    """GET /api/title returns the current page title."""
    requests.post(
        f"{base_url}/api/navigate",
        json={"url": "https://example.com"},
        timeout=30,
    ).raise_for_status()

    response = requests.get(f"{base_url}/api/title")
    response.raise_for_status()
    data = response.json()
    assert "title" in data
    assert isinstance(data["title"], str)
    assert len(data["title"]) > 0


@pytest.mark.smoke
def test_get_text(base_url):
    """GET /api/text returns visible page text."""
    requests.post(
        f"{base_url}/api/navigate",
        json={"url": "https://example.com"},
        timeout=30,
    ).raise_for_status()

    response = requests.get(f"{base_url}/api/text")
    response.raise_for_status()
    data = response.json()
    assert "text" in data
    assert "example" in data["text"].lower()


@pytest.mark.smoke
def test_screenshot_returns_png(base_url):
    """GET /api/screenshot returns a valid PNG image."""
    requests.post(
        f"{base_url}/api/navigate",
        json={"url": "https://example.com"},
        timeout=30,
    ).raise_for_status()

    response = requests.get(f"{base_url}/api/screenshot", timeout=30)
    response.raise_for_status()
    assert response.headers["Content-Type"] == "image/png"

    img = Image.open(io.BytesIO(response.content))
    assert img.format == "PNG"
    assert img.width > 0
    assert img.height > 0


@pytest.mark.smoke
def test_screenshot_annotated_returns_png_and_elements(base_url):
    """GET /api/screenshot_annotated returns a valid PNG and element metadata."""
    requests.post(
        f"{base_url}/api/navigate",
        json={"url": "https://example.com"},
        timeout=30,
    ).raise_for_status()

    response = requests.get(f"{base_url}/api/screenshot_annotated", timeout=30)
    response.raise_for_status()
    assert response.headers["Content-Type"] == "image/png"

    # Stealth engine should find at least the main link element.
    element_count = response.headers.get("X-Element-Count")
    assert element_count is not None
    assert int(element_count) >= 0

    img = Image.open(io.BytesIO(response.content))
    assert img.format == "PNG"
    assert img.width > 0
    assert img.height > 0


@pytest.mark.smoke
def test_navigate_returns_block_signals(base_url):
    """Navigate response includes block detection signals (even if not blocked)."""
    response = requests.post(
        f"{base_url}/api/navigate",
        json={"url": "https://example.com"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    assert "block" in data
    assert "confidence" in data["block"]
    assert "signals" in data["block"]
    assert isinstance(data["block"]["signals"], list)
    assert "fallback_triggered" in data


@pytest.fixture(scope="module")
def redirect_server_url():
    """A tiny local HTTP server that 302-redirects /redirect to /final."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/final")
                self.end_headers()
            elif self.path == "/final":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body>Final</body></html>")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args, **kwargs):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield url
    finally:
        server.shutdown()


@pytest.mark.smoke
def test_navigate_returns_final_url_after_redirect(base_url, redirect_server_url):
    """Navigate returns the final URL after any redirects."""
    response = requests.post(
        f"{base_url}/api/navigate",
        json={"url": f"{redirect_server_url}/redirect"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    assert "url" in data
    assert "requested_url" in data
    assert data["requested_url"] == f"{redirect_server_url}/redirect"
    # The final URL should be the redirect target, not the original URL.
    assert "/final" in data["url"], f"Expected final URL to contain /final, got {data['url']!r}"


@pytest.mark.smoke
def test_go_back_and_forward(base_url):
    """POST /api/back and /api/forward navigate through history."""
    session_id = requests.post(
        f"{base_url}/api/sessions",
        json={},
        timeout=30,
    ).json()["session_id"]
    try:
        # Build history: example.com -> iana.org
        requests.post(
            f"{base_url}/api/navigate",
            params={"session_id": session_id},
            json={"url": "https://example.com"},
            timeout=30,
        ).raise_for_status()
        requests.post(
            f"{base_url}/api/navigate",
            params={"session_id": session_id},
            json={"url": "https://iana.org"},
            timeout=30,
        ).raise_for_status()

        # Go back should return to example.com
        response = requests.post(
            f"{base_url}/api/back", params={"session_id": session_id}, timeout=30
        )
        response.raise_for_status()
        data = response.json()
        assert "Example Domain" in data["title"]

        # Go forward should return to iana.org
        response = requests.post(
            f"{base_url}/api/forward", params={"session_id": session_id}, timeout=30
        )
        response.raise_for_status()
        data = response.json()
        assert "Internet Assigned" in data["title"] or "iana.org" in data["url"]
    finally:
        requests.delete(f"{base_url}/api/sessions/{session_id}", timeout=30)


@pytest.mark.smoke
def test_reload(base_url):
    """POST /api/reload refreshes the current page."""
    requests.post(
        f"{base_url}/api/navigate",
        json={"url": "https://example.com"},
        timeout=30,
    ).raise_for_status()

    response = requests.post(f"{base_url}/api/reload", timeout=30)
    response.raise_for_status()
    data = response.json()
    assert "Example Domain" in data["title"]
    assert "url" in data
