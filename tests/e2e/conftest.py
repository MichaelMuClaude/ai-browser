"""E2E test fixtures and helpers."""
import os

import pytest
import requests

BASE_URL = os.environ.get("AI_BROWSER_BASE_URL", "http://127.0.0.1:8888")


@pytest.fixture(scope="session")
def base_url() -> str:
    """Return the base URL of the ai-browser HTTP service."""
    return BASE_URL


@pytest.fixture(scope="session", autouse=True)
def require_running_server(base_url):
    """Skip E2E tests if the ai-browser HTTP service is not reachable.

    Also reset the engine to stealth so smoke tests have CDP-based text/URL access.
    """
    try:
        response = requests.get(f"{base_url}/api/health", timeout=5)
        response.raise_for_status()
    except Exception as e:
        pytest.skip(f"ai-browser HTTP service not reachable at {base_url}: {e}")

    # Smoke tests assume stealth engine capabilities (get_text, get_url, etc.)
    try:
        requests.post(f"{base_url}/api/engine", json={"engine": "stealth"}, timeout=10)
    except Exception as e:
        pytest.skip(f"Could not reset ai-browser engine to stealth: {e}")
