"""E2E smoke tests for /api/smart_fetch.

These tests require a running ai-browser HTTP service and real Chrome.
"""
import json
import os
import subprocess
import sys

import pytest
import requests


@pytest.mark.smoke
def test_smart_fetch_example_com(base_url):
    """POST /api/smart_fetch returns page text and metadata."""
    response = requests.post(
        f"{base_url}/api/smart_fetch",
        json={"url": "https://example.com"},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()

    assert data["url"] == "https://example.com"
    assert "final_url" in data
    assert "title" in data
    assert "text" in data
    assert "text_truncated" in data
    assert data["engine_used"] in ("claude", "stealth", "native")
    assert "blocked" in data
    assert "block_confidence" in data
    assert "block_signals" in data
    assert "claude_engine_attempted" in data
    assert "claude_engine_succeeded" in data
    assert "claude_engine_failure_reason" in data
    assert "fallback_triggered" in data
    assert "elapsed_ms" in data

    # The page should not be blocked on example.com.
    assert data["blocked"] is False
    # The fast Claude path sometimes omits the visible title but still returns
    # body text, so only assert that we got reasonable non-empty content.
    assert isinstance(data["text"], str)
    assert len(data["text"].strip()) >= 20


@pytest.mark.smoke
def test_smart_fetch_force_disable_not_in_openapi(base_url):
    """The internal force_disable_claude switch must not be in the public schema."""
    response = requests.get(f"{base_url}/openapi.json", timeout=30)
    response.raise_for_status()
    spec = response.json()

    schemas = spec.get("components", {}).get("schemas", {})
    smart_fetch_schema = schemas.get("SmartFetchRequest", {})
    properties = smart_fetch_schema.get("properties", {})

    assert "force_disable_claude" not in properties, (
        "force_disable_claude must not appear in the public SmartFetchRequest schema"
    )


@pytest.mark.smoke
def test_smart_fetch_cli(base_url):
    """CLI smart-fetch prints page text to stdout and metadata to stderr."""
    python = sys.executable
    result = subprocess.run(
        [python, "-m", "src.cli", "smart-fetch", "https://example.com"],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "AI_BROWSER_BASE_URL": base_url},
    )
    assert result.returncode == 0, result.stderr
    # The fast path may omit the title; just confirm we got non-empty text.
    assert len(result.stdout.strip()) >= 20
    assert result.stderr
    metadata = json.loads(result.stderr)
    assert metadata["url"] == "https://example.com"
    assert metadata["engine_used"] in ("claude", "stealth", "native")


@pytest.mark.smoke
def test_smart_fetch_cli_json(base_url):
    """CLI smart-fetch --json prints full JSON to stdout."""
    python = sys.executable
    result = subprocess.run(
        [python, "-m", "src.cli", "smart-fetch", "--json", "https://example.com"],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "AI_BROWSER_BASE_URL": base_url},
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["url"] == "https://example.com"
    assert isinstance(data["text"], str)
    assert len(data["text"].strip()) >= 20
    assert data["engine_used"] in ("claude", "stealth", "native")
