"""Pre-ship latency benchmark: smart_fetch vs navigate+text.

Run manually: pytest tests/benchmarks -m benchmark -v
NOT in default suite (network-dependent, inherently variable).
"""
import time

import pytest
import requests


BENCHMARK_URLS = [
    "https://example.com",                    # static page
    "https://en.wikipedia.org/wiki/Perth",    # moderate JavaScript
]

# smart_fetch should not be dramatically slower than navigate+text
RELATIVE_THRESHOLD = 3.0   # smart_fetch < navigate_text * 3
ABSOLUTE_CAP = 20.0        # smart_fetch absolute cap (seconds)
SLACK = 3.0                # fixed slack (seconds), absorbs smart_fetch response assembly overhead


@pytest.mark.benchmark
@pytest.mark.parametrize("url", BENCHMARK_URLS)
def test_smart_fetch_not_slower_than_navigate_text(url, base_url):
    # 1. Measure navigate+text (old path)
    t0 = time.monotonic()
    nav_resp = requests.post(
        f"{base_url}/api/navigate",
        json={"url": url},
        timeout=60,
    )
    nav_resp.raise_for_status()
    text_resp = requests.get(
        f"{base_url}/api/text",
        timeout=60,
    )
    text_resp.raise_for_status()
    navigate_text_time = time.monotonic() - t0

    # 2. Measure smart_fetch (new path)
    t0 = time.monotonic()
    smart_resp = requests.post(
        f"{base_url}/api/smart_fetch",
        json={"url": url},
        timeout=60,
    )
    smart_resp.raise_for_status()
    smart_fetch_time = time.monotonic() - t0

    data = smart_resp.json()

    # 3. Assert: smart_fetch must not be dramatically slower than the old path
    relative_limit = navigate_text_time * RELATIVE_THRESHOLD + SLACK
    assert smart_fetch_time < relative_limit, (
        f"smart_fetch ({smart_fetch_time:.1f}s) is slower than navigate+text "
        f"({navigate_text_time:.1f}s) by too much "
        f"(threshold {relative_limit:.1f}s)"
    )
    assert smart_fetch_time < ABSOLUTE_CAP, (
        f"smart_fetch ({smart_fetch_time:.1f}s) exceeds the absolute cap of {ABSOLUTE_CAP}s"
    )

    # Sanity: smart_fetch should return the same fields as the old path
    assert "text" in data
    assert "engine_used" in data
    assert "elapsed_ms" in data
