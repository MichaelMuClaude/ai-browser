"""Shared pytest fixtures and hooks for ai-browser tests."""
import datetime
import json
import os
from pathlib import Path
from typing import Callable

import pytest
import requests

# Base URL for the running ai-browser HTTP service.
# Override with: AI_BROWSER_BASE_URL=http://host:port pytest
BASE_URL = os.environ.get("AI_BROWSER_BASE_URL", "http://127.0.0.1:8888")


def _artifact_root() -> Path:
    """Return the root directory for test artifacts."""
    return Path(__file__).parent / "artifacts"


def _make_artifact_dir(test_name: str) -> Path:
    """Create and return a unique artifact directory for a test."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = test_name.replace("/", "_").replace("[", "_").replace("]", "_")
    artifact_dir = _artifact_root() / f"{timestamp}_{safe_name}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


@pytest.fixture(scope="session")
def base_url() -> str:
    """Return the base URL of the ai-browser HTTP service."""
    return BASE_URL


@pytest.fixture(scope="function")
def artifact_dir(request) -> Path:
    """Return a unique artifact directory for the current test."""
    return _make_artifact_dir(request.node.name)


@pytest.fixture(scope="function")
def save_artifact(artifact_dir: Path) -> Callable:
    """Return a helper that saves content into the current artifact directory."""
    def _save(name: str, content: bytes | str, mode: str = "wb") -> Path:
        path = artifact_dir / name
        if isinstance(content, str) and "b" not in mode:
            content = content.encode("utf-8")
        with open(path, mode) as f:
            f.write(content)
        return path
    return _save


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """On test failure, capture browser state and screenshots as evidence."""
    outcome = yield
    report = outcome.get_result()

    if report.when != "call" or report.outcome != "failed":
        return

    artifact_dir = _make_artifact_dir(f"{item.name}_FAILED")

    # Record basic test info.
    with open(artifact_dir / "pytest_report.txt", "w", encoding="utf-8") as f:
        f.write(f"Test: {item.nodeid}\n")
        f.write(f"Outcome: {report.outcome}\n")
        f.write(f"Duration: {report.duration:.3f}s\n")
        if report.longrepr:
            f.write(f"\n{report.longreprtext}\n")

    # Try to capture current service / browser state.
    try:
        health = requests.get(f"{BASE_URL}/api/health", timeout=5).json()
        with open(artifact_dir / "health.json", "w", encoding="utf-8") as f:
            json.dump(health, f, indent=2)
    except Exception as e:
        with open(artifact_dir / "health_error.txt", "w", encoding="utf-8") as f:
            f.write(f"Could not reach ai-browser service at {BASE_URL}: {e}\n")

    # Try to capture screenshots.
    for endpoint, filename in [
        ("/api/screenshot", "raw_screenshot.png"),
        ("/api/screenshot_annotated", "annotated_screenshot.png"),
    ]:
        try:
            response = requests.get(f"{BASE_URL}{endpoint}", timeout=15)
            if response.status_code == 200:
                with open(artifact_dir / filename, "wb") as f:
                    f.write(response.content)
        except Exception:
            pass

    # Try to capture title and URL.
    for endpoint, filename in [
        ("/api/title", "title.txt"),
        ("/api/url", "url.txt"),
    ]:
        try:
            response = requests.get(f"{BASE_URL}{endpoint}", timeout=5)
            if response.status_code == 200:
                with open(artifact_dir / filename, "w", encoding="utf-8") as f:
                    f.write(response.text)
        except Exception:
            pass
