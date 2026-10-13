"""E2E tests for the Native OS engine (xdotool).

These tests require a working X11 environment and real browser window.
They are marked with `native` and will be skipped if the environment is unavailable.
"""
import io
import os

import pytest
import requests
from PIL import Image


BASE_URL = os.environ.get("AI_BROWSER_BASE_URL", "http://127.0.0.1:8888")


def _switch_engine(engine: str):
    response = requests.post(f"{BASE_URL}/api/engine", json={"engine": engine}, timeout=10)
    response.raise_for_status()
    return response.json()


def _has_x11() -> bool:
    """Check if an X11 display is available for xdotool."""
    return bool(os.environ.get("DISPLAY"))


@pytest.fixture(scope="module", autouse=True)
def require_x11():
    """Skip all native engine tests if X11 is not available."""
    if not _has_x11():
        pytest.skip("X11 display not available; native engine tests require DISPLAY")


@pytest.mark.native
def test_native_engine_starts():
    """Switch to native engine and verify it reports active."""
    _switch_engine("stealth")
    data = _switch_engine("native")
    assert data["engine"] == "native"

    health = requests.get(f"{BASE_URL}/api/health").json()
    assert health["native_active"] is True


@pytest.mark.native
def test_native_navigate():
    """Native engine can navigate to a simple page."""
    _switch_engine("native")
    response = requests.post(
        f"{BASE_URL}/api/navigate",
        json={"url": "https://example.com"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    assert "url" in data
    assert "title" in data
    assert data.get("engine") == "native"
    assert data.get("method") == "native_os"


@pytest.mark.native
def test_native_screenshot():
    """Native engine can take a screenshot."""
    _switch_engine("native")
    requests.post(
        f"{BASE_URL}/api/navigate",
        json={"url": "https://example.com"},
        timeout=30,
    ).raise_for_status()

    response = requests.get(f"{BASE_URL}/api/screenshot", timeout=30)
    response.raise_for_status()
    assert response.headers["Content-Type"] == "image/png"

    img = Image.open(io.BytesIO(response.content))
    assert img.format == "PNG"
    assert img.width > 0
    assert img.height > 0


@pytest.mark.native
def test_native_click():
    """Native engine can click at coordinates."""
    _switch_engine("native")
    requests.post(
        f"{BASE_URL}/api/navigate",
        json={"url": "https://example.com"},
        timeout=30,
    ).raise_for_status()

    response = requests.post(
        f"{BASE_URL}/api/click",
        json={"x": 100, "y": 100},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    assert data["x"] == 100
    assert data["y"] == 100


@pytest.fixture(scope="module", autouse=True)
def reset_to_stealth_after_module():
    """Ensure we leave the service in stealth mode after native tests."""
    yield
    try:
        _switch_engine("stealth")
    except Exception:
        pass
