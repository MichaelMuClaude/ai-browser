"""Unit tests for process_cleanup module."""
import asyncio
import os
import signal
import subprocess
import time

from src.core.process_cleanup import (
    find_chrome_pids_by_profile_dir,
    kill_pids_with_escalation,
    kill_chrome_by_profile_dir,
    _is_pid_alive,
    _cmdline_tokens,
)


class TestFindChromePidsByProfileDir:
    def test_finds_process_by_marker(self, tmp_path):
        """A process whose cmdline contains the marker is found."""
        marker_dir = str(tmp_path / "fake-profile")
        os.makedirs(marker_dir)
        # Launch a long-lived process with --user-data-dir in its cmdline.
        # Use python3 instead of sleep because some environments hide zombies'
        # cmdline, which would make the marker scan unreliable in tests.
        proc = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)", f"--user-data-dir={marker_dir}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            pids = find_chrome_pids_by_profile_dir(marker_dir)
            assert proc.pid in pids
        finally:
            proc.kill()
            proc.wait()

    def test_no_match_for_unrelated_dir(self, tmp_path):
        """Processes without the marker are not found."""
        pids = find_chrome_pids_by_profile_dir(str(tmp_path / "nonexistent"))
        assert pids == set()

    def test_no_prefix_match_for_similar_profile_names(self, tmp_path):
        """A profile dir that is a prefix of another must not match.

        Codex found that `--user-data-dir=/path/session-abc` was matching
        `--user-data-dir=/path/session-abc-2` because of simple substring
        matching.  This test reproduces that exact scenario.
        """
        base = tmp_path / "ai-browser-chrome-profile"
        base.mkdir()
        longer = base / "session-abc-2"
        longer.mkdir()
        shorter = base / "session-abc"
        shorter.mkdir()
        proc = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)", f"--user-data-dir={longer}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            pids = find_chrome_pids_by_profile_dir(str(shorter))
            assert proc.pid not in pids
            # Also confirm the longer dir still matches itself.
            pids_longer = find_chrome_pids_by_profile_dir(str(longer))
            assert proc.pid in pids_longer
        finally:
            proc.kill()
            proc.wait()


class TestCmdlineTokens:
    """Tests for parsing /proc/PID/cmdline byte layouts."""

    def test_nul_separated_argv(self):
        """Normal processes have NUL-separated argv."""
        cmdline = b"python3\x00-c\x00import time\x00--user-data-dir=/tmp/a\x00"
        tokens = _cmdline_tokens(cmdline)
        assert tokens == [b"python3", b"-c", b"import time", b"--user-data-dir=/tmp/a", b""]

    def test_space_separated_single_argv(self):
        """Chrome sometimes presents the whole command as one space-separated argv."""
        cmdline = b"/opt/google/chrome/chrome --foo --user-data-dir=/tmp/a --bar\x00"
        tokens = _cmdline_tokens(cmdline)
        assert b"--user-data-dir=/tmp/a" in tokens
        assert b"--user-data-dir=/tmp/a-2" not in tokens

    def test_no_prefix_collision_in_space_separated(self):
        """Exact token match prevents session-abc from matching session-abc-2."""
        cmdline = b"chrome --user-data-dir=/tmp/session-abc-2 --headless\x00"
        tokens = _cmdline_tokens(cmdline)
        assert b"--user-data-dir=/tmp/session-abc-2" in tokens
        assert b"--user-data-dir=/tmp/session-abc" not in tokens


class TestKillPidsWithEscalation:
    def test_kills_resistant_process(self):
        """A process that ignores SIGTERM is killed by SIGKILL."""
        # Launch a process that ignores SIGTERM
        proc = subprocess.Popen(
            ["python3", "-c",
             "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _is_pid_alive(proc.pid)

            killed = asyncio.run(kill_pids_with_escalation({proc.pid}, term_timeout=1.0))
            assert killed == 1
            assert not _is_pid_alive(proc.pid)
        finally:
            if _is_pid_alive(proc.pid):
                proc.kill()
            proc.wait()

    def test_empty_set_returns_zero(self):
        killed = asyncio.run(kill_pids_with_escalation(set()))
        assert killed == 0

    def test_already_dead_pid_handled_gracefully(self):
        killed = asyncio.run(kill_pids_with_escalation({999999}))
        assert killed == 0


class TestKillChromeByProfileDir:
    def test_end_to_end(self, tmp_path):
        """Launch a fake Chrome process, find it, kill it."""
        profile = str(tmp_path / "session-test")
        os.makedirs(profile)
        proc = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)", f"--user-data-dir={profile}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _is_pid_alive(proc.pid)
            killed = asyncio.run(kill_chrome_by_profile_dir(profile, term_timeout=1.0))
            assert killed >= 1
            assert not _is_pid_alive(proc.pid)
        finally:
            if _is_pid_alive(proc.pid):
                proc.kill()
            proc.wait()
