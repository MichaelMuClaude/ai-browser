"""Process tree cleanup for Chrome processes leaked by nodriver.

nodriver's Browser.stop() sends SIGTERM/SIGKILL to the main Chrome process
but never verifies it actually exited, nor does it handle Chrome's
multi-process child structure (zygote, gpu-process, renderer, etc.).
When the main process is killed before cascading signals to children,
children get reparented to init and become permanent orphans.

This module provides reliable process tree cleanup by scanning /proc for
Chrome processes matching a specific --user-data-dir and killing them with
SIGTERM → wait → SIGKILL escalation.
"""

import os
import signal
import asyncio
import time
from typing import Set

from ..log_config import get_logger

logger = get_logger(__name__)

_TERM_TIMEOUT_SECONDS = 3.0
_POLL_INTERVAL_SECONDS = 0.15


def _is_pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # The PID exists; make sure it is not a zombie (already dead but not reaped).
    try:
        with open(f"/proc/{pid}/status", "rb") as f:
            for line in f:
                if line.startswith(b"State:"):
                    state = line.split()[1].decode("ascii", errors="replace")
                    return state != "Z"
    except (OSError, PermissionError):
        pass
    return True


def _cmdline_tokens(cmdline_bytes: bytes) -> list[bytes]:
    """Split /proc/PID/cmdline into tokens.

    Handles two kernel layouts seen in the wild:
    - Normal NUL-separated argv (most processes).
    - A single argv entry where flags are space-separated (Chrome on some
      systems presents itself this way in /proc/PID/cmdline).
    """
    if cmdline_bytes.count(b"\x00") > 1:
        return cmdline_bytes.split(b"\x00")
    return cmdline_bytes.split()


def find_chrome_pids_by_profile_dir(profile_dir: str) -> Set[int]:
    """Return PIDs of all Chrome processes using the given --user-data-dir.

    Scans /proc/*/cmdline and looks for an exact `--user-data-dir=<profile_dir>`
    token.  This avoids prefix collisions where `/path/session-abc` would
    otherwise match `/path/session-abc-2`.

    This catches the main browser process AND all child processes
    (zygote, gpu-process, renderer, utility, crashpad handler), since
    Chrome propagates --user-data-dir to every child.
    """
    marker = f"--user-data-dir={profile_dir}"
    pids: Set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline_bytes = f.read()
        except (OSError, PermissionError):
            continue
        tokens = _cmdline_tokens(cmdline_bytes)
        if any(token.decode("utf-8", errors="replace") == marker for token in tokens):
            pids.add(pid)
    return pids


def has_chrome_processes_for_profile_dir(profile_dir: str) -> bool:
    """Return True if any live Chrome process uses the given profile dir."""
    for pid in find_chrome_pids_by_profile_dir(profile_dir):
        if _is_pid_alive(pid):
            return True
    return False


async def kill_pids_with_escalation(
    pids: Set[int],
    term_timeout: float = _TERM_TIMEOUT_SECONDS,
) -> int:
    """Kill a set of PIDs: SIGTERM → wait → SIGKILL.

    Returns the number of PIDs that were killed (or were already dead).
    Does NOT use killpg / process-group kill because nodriver launches
    Chrome in the daemon's own process group (no start_new_session), so
    killpg would kill the daemon itself.
    """
    alive_pids = {pid for pid in pids if _is_pid_alive(pid)}
    if not alive_pids:
        return 0

    for pid in alive_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.warning("Permission denied sending SIGTERM to PID %d", pid)

    survivors = set(alive_pids)
    deadline = time.monotonic() + term_timeout
    while survivors and time.monotonic() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        for pid in list(survivors):
            if not _is_pid_alive(pid):
                survivors.discard(pid)

    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            logger.warning("Permission denied sending SIGKILL to PID %d", pid)

    if survivors:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS * 2)

    still_alive = [pid for pid in survivors if _is_pid_alive(pid)]
    if still_alive:
        logger.error("Failed to kill Chrome PIDs: %s", still_alive)

    killed = len(alive_pids) - len(still_alive)
    return killed


async def kill_chrome_by_profile_dir(
    profile_dir: str,
    term_timeout: float = _TERM_TIMEOUT_SECONDS,
) -> int:
    """Find and kill all Chrome processes for the given profile directory.

    Returns the number of processes killed.
    """
    pids = find_chrome_pids_by_profile_dir(profile_dir)
    if not pids:
        return 0
    logger.debug(
        "Found %d Chrome process(es) for profile %s: PIDs=%s",
        len(pids), profile_dir, sorted(pids),
    )
    killed = await kill_pids_with_escalation(pids, term_timeout=term_timeout)
    return killed
