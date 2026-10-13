"""E2E tests for tab registry consistency across daemon restarts."""
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import pytest
import requests


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON = os.path.join(PROJECT_ROOT, "venv", "bin", "python")


def _free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url}/api/health", timeout=2)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _start_server(port: int, profile_base_dir: str):
    """Start the ai-browser server as a subprocess with an isolated profile dir."""
    env = os.environ.copy()
    env["AI_BROWSER_PORT"] = str(port)
    env["AI_BROWSER_HOST"] = "127.0.0.1"
    env["AI_BROWSER_PROFILE_BASE_DIR"] = profile_base_dir
    # Use project venv if available, otherwise rely on the active interpreter.
    python = PYTHON if os.path.exists(PYTHON) else sys.executable
    proc = subprocess.Popen(
        [python, "-m", "src.server"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def _stop_server(proc: subprocess.Popen, graceful: bool = False):
    """Stop the server subprocess."""
    if proc.poll() is not None:
        return
    if graceful:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    else:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


@pytest.mark.smoke
def test_tab_registry_survives_abrupt_daemon_restart():
    """After an abrupt daemon restart, /api/tabs stays in sync with reality.

    This is the regression test for the bug where the daemon's in-memory tab
    registry drifted completely out of sync with the real Chrome window: after
    restart, /api/tabs returned [] while Chrome still had 3 real tabs open.
    The fix kills orphan Chrome processes for the default profile on startup,
    so the new daemon begins with a clean, accurate tab registry.
    """
    profile_base = tempfile.mkdtemp(prefix="ai-browser-tab-sync-")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc1 = _start_server(port, profile_base)
    try:
        assert _wait_for_health(base_url), "first server did not start"

        # Open a couple of real tabs so the browser has more than just the
        # default initial tab.
        for url in ("https://example.com", "https://iana.org"):
            resp = requests.post(
                f"{base_url}/api/tabs",
                json={"url": url, "return_look": False},
                timeout=30,
            )
            resp.raise_for_status()

        tabs = requests.get(f"{base_url}/api/tabs", timeout=10).json()["tabs"]
        assert len(tabs) == 3, f"expected 3 tabs before restart, got {len(tabs)}: {tabs}"
        tab_ids_before = {t["tab_id"] for t in tabs}

        # Abruptly kill the daemon (simulates crash/OOM/SIGKILL) WITHOUT giving
        # it a chance to stop the browser process.
        _stop_server(proc1, graceful=False)
    except Exception:
        _stop_server(proc1, graceful=False)
        raise

    # Start a brand-new daemon on a different port, sharing the same profile
    # base so it sees the orphaned Chrome from the first daemon.
    port2 = _free_port()
    base_url2 = f"http://127.0.0.1:{port2}"
    proc2 = _start_server(port2, profile_base)
    try:
        assert _wait_for_health(base_url2), "second server did not start"

        tabs_after = requests.get(f"{base_url2}/api/tabs", timeout=10).json()["tabs"]
        # The new daemon must report a consistent state: exactly one fresh
        # default tab.  The ID can be reused (it always starts at tab-0), so
        # verify the tab count and that the previously opened tabs are gone.
        assert len(tabs_after) == 1, f"expected 1 fresh tab after restart, got {len(tabs_after)}: {tabs_after}"
        assert tabs_after[0]["tab_id"] == "tab-0"
        stale_ids = tab_ids_before - {"tab-0"}
        assert not stale_ids.intersection({t["tab_id"] for t in tabs_after}), "stale tab ids survived restart"
    finally:
        _stop_server(proc2, graceful=True)
        shutil.rmtree(profile_base, ignore_errors=True)
