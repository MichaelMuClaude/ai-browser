"""Browser session management.

A BrowserSession owns an EngineManager and one or more BrowserTabs.
Sessions allow multiple agents/tasks to use the browser without clobbering
each other's state.
"""
import os
import re
import shutil
import asyncio
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .engine_manager import EngineManager
from .tab import BrowserTab
from .process_cleanup import (
    find_chrome_pids_by_profile_dir,
    kill_chrome_by_profile_dir,
    kill_pids_with_escalation,
)
from ..config import (
    BROWSER_PATH,
    SESSION_IDLE_TTL_SECONDS,
    SESSION_REAPER_INTERVAL_SECONDS,
    MAX_NON_DEFAULT_SESSIONS,
    STEALTH_PROFILE_DIR,
)
from ..log_config import get_logger

logger = get_logger(__name__)

# Session IDs must be safe for filesystem paths and REST URLs.
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class InvalidSessionIdError(ValueError):
    """Raised when a session ID is invalid."""
    pass


class InvalidTabIdError(ValueError):
    """Raised when a tab ID is invalid."""
    pass


def validate_session_id(session_id: str) -> None:
    """Validate a session ID for safety.

    Allowed characters: A-Z, a-z, 0-9, underscore, dot, hyphen.
    Length: 1-64 characters.
    """
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.match(session_id):
        raise InvalidSessionIdError(
            "session_id must match ^[A-Za-z0-9_.-]{1,64}$"
        )


def validate_tab_id(tab_id: Optional[str]) -> None:
    """Validate a tab ID.

    Uses the same rules as session_id. Passing None is valid (server will
    generate a tab_id automatically).
    """
    if tab_id is None:
        return
    if not isinstance(tab_id, str) or not SESSION_ID_PATTERN.match(tab_id):
        raise InvalidTabIdError(
            "tab_id must match ^[A-Za-z0-9_.-]{1,64}$"
        )


@dataclass
class BrowserSession:
    """Represents a browser session."""
    session_id: str
    engine_manager: EngineManager
    profile_dir: str
    tabs: dict[str, BrowserTab] = field(default_factory=dict)
    active_tab_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_used_at: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)
    scratchpad: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "profile_dir": self.profile_dir,
            "active_tab_id": self.active_tab_id,
            "tab_count": len(self.tabs),
            "created_at": self.created_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat(),
        }

    def touch(self) -> None:
        """Update the last-used timestamp."""
        self.last_used_at = datetime.now()

    async def active_tab(self) -> Optional[BrowserTab]:
        """Return the currently active tab, if any."""
        if self.active_tab_id and self.active_tab_id in self.tabs:
            return self.tabs[self.active_tab_id]
        return None

    def add_scratchpad_entry(
        self,
        content: str,
        url: str,
        label: Optional[str] = None,
    ) -> dict:
        """Add a research clipping to the session scratchpad.

        The entry is auto-tagged with the provided URL and a server-side
        timestamp. Entries are kept in insertion order.
        """
        entry = {
            "id": len(self.scratchpad) + 1,
            "content": content,
            "url": url,
            "timestamp": datetime.now().isoformat(),
            "label": label if label is not None else "",
        }
        self.scratchpad.append(entry)
        return entry

    def get_scratchpad_entries(self) -> list[dict]:
        """Return all scratchpad entries in insertion order."""
        return list(self.scratchpad)

    def clear_scratchpad(self) -> int:
        """Clear all scratchpad entries and return the previous count."""
        count = len(self.scratchpad)
        self.scratchpad.clear()
        return count


class SessionManager:
    """Manages multiple browser sessions."""

    DEFAULT_SESSION_ID = "default"

    def __init__(
        self,
        browser_path: str = "",
        headless: bool = False,
        idle_ttl: int = SESSION_IDLE_TTL_SECONDS,
        max_non_default: int = MAX_NON_DEFAULT_SESSIONS,
        reaper_interval: int = SESSION_REAPER_INTERVAL_SECONDS,
    ):
        self.browser_path = browser_path or BROWSER_PATH
        self.headless = headless
        self.sessions: dict[str, BrowserSession] = {}
        self._create_lock = asyncio.Lock()  # protects session creation against races
        # --- Session lifecycle management ---
        self._idle_ttl = idle_ttl
        self._max_non_default = max_non_default
        self._reaper_interval = reaper_interval
        self._reaper_task: Optional[asyncio.Task] = None
        self._evict_lock = asyncio.Lock()  # prevents reaper/create from racing eviction

    def _make_profile_dir(self, session_id: str) -> str:
        """Create a dedicated profile directory for a session."""
        base = STEALTH_PROFILE_DIR
        if session_id == self.DEFAULT_SESSION_ID:
            path = base
        else:
            path = os.path.join(base, f"session-{session_id}")
        os.makedirs(path, exist_ok=True)
        return path

    async def get_or_create_session(
        self, session_id: str = DEFAULT_SESSION_ID
    ) -> BrowserSession:
        """Get an existing session or create a new one."""
        validate_session_id(session_id)
        if session_id in self.sessions:
            session = self.sessions[session_id]
            session.touch()
            return session

        return await self.create_session(session_id=session_id)

    async def create_session(
        self, session_id: Optional[str] = None
    ) -> BrowserSession:
        """Create a new browser session."""
        if session_id is None:
            session_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")

        validate_session_id(session_id)

        # --- LRU eviction: when non-default sessions hit the cap, evict the least-recently-used ---
        if session_id != self.DEFAULT_SESSION_ID:
            async with self._evict_lock:
                lru_sid = self._peek_lru_non_default()
                if lru_sid:
                    logger.info(
                        "Non-default session cap (%d) reached, evicting LRU: '%s'",
                        self._max_non_default, lru_sid,
                    )
                    await self.delete_session(lru_sid)

        async with self._create_lock:
            # Double-check after acquiring lock to prevent TOCTOU race
            if session_id in self.sessions:
                raise ValueError(f"Session already exists: {session_id}")

            profile_dir = self._make_profile_dir(session_id)
            engine_manager = EngineManager(
                browser_path=self.browser_path,
                headless=self.headless,
                profile_dir=profile_dir,
            )
            # Ensure engine is started so the session is usable immediately.
            await engine_manager.ensure_started()

            session = BrowserSession(
                session_id=session_id,
                engine_manager=engine_manager,
                profile_dir=profile_dir,
            )

            # Create the initial tab.
            tab = BrowserTab(
                tab_id=f"{session_id}-tab-0",
                session_id=session_id,
            )
            session.tabs[tab.tab_id] = tab
            session.active_tab_id = tab.tab_id

            # Insert into dict BEFORE releasing lock — prevents concurrent create
            self.sessions[session_id] = session
            logger.info("Created session %s with profile %s", session_id, profile_dir)
            return session

    def list_sessions(self) -> list[dict]:
        """Return metadata for all sessions."""
        now = datetime.now()
        result = []
        for s in self.sessions.values():
            d = s.to_dict()
            d["idle_seconds"] = int((now - s.last_used_at).total_seconds())
            result.append(d)
        return result

    async def start_reaper(self) -> None:
        """Start the background idle-session reaper. Called during server lifespan startup."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())
            logger.info(
                "Session reaper started (idle_ttl=%ds, max_non_default=%d, interval=%ds)",
                self._idle_ttl, self._max_non_default, self._reaper_interval,
            )

    async def stop_reaper(self) -> None:
        """Stop the background reaper. Called during server lifespan shutdown."""
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None

    async def _reaper_loop(self) -> None:
        """Periodically evict idle sessions and reap orphan Chrome processes."""
        while True:
            await asyncio.sleep(self._reaper_interval)
            try:
                evicted = await self._evict_idle_sessions()
                if evicted:
                    logger.info("Reaper evicted %d idle session(s)", evicted)
                leaked = await self._reap_orphan_chrome()
                if leaked:
                    logger.warning("Reaper killed %d orphan Chrome process(es)", leaked)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Session reaper error: %s", e)

    async def _evict_idle_sessions(self) -> int:
        """Close and clean up non-default sessions that exceed the idle TTL. Returns the number evicted."""
        now = datetime.now()
        to_evict = []
        for sid, session in self.sessions.items():
            if sid == self.DEFAULT_SESSION_ID:
                continue
            idle_seconds = (now - session.last_used_at).total_seconds()
            if idle_seconds >= self._idle_ttl:
                to_evict.append((sid, idle_seconds))

        for sid, idle_secs in to_evict:
            logger.info(
                "Evicting idle session '%s' (idle %.0fs >= %ds TTL)",
                sid, idle_secs, self._idle_ttl,
            )
            await self.delete_session(sid)
        return len(to_evict)

    async def _reap_orphan_chrome(self) -> int:
        """Kill Chrome processes whose profile_dir matches no tracked session.

        Defense-in-depth against leaks from paths other than delete_session
        (e.g. daemon crash recovery, _restart_browser, unexpected exceptions).
        The default session profile is never touched.
        """
        # Race-safety: create_session() creates the profile dir, starts the
        # engine, and only then inserts the session into self.sessions.  If the
        # reaper runs between those two points it would see an untracked
        # profile dir and kill a perfectly healthy brand-new browser.  Skip the
        # orphan scan while a session is being created.
        if self._create_lock.locked():
            logger.debug("Reaper skipping orphan check: session creation in progress")
            return 0

        base = STEALTH_PROFILE_DIR
        live_profile_dirs = {s.profile_dir for s in self.sessions.values()}

        orphan_pids: set[int] = set()
        for entry in os.listdir(base):
            if not entry.startswith("session-"):
                continue
            full_path = os.path.join(base, entry)
            if full_path in live_profile_dirs:
                continue
            pids = find_chrome_pids_by_profile_dir(full_path)
            if pids:
                orphan_pids.update(pids)

        if not orphan_pids:
            return 0
        logger.warning(
            "Found %d orphan Chrome process(es) for untracked session profiles, killing",
            len(orphan_pids),
        )
        killed = await kill_pids_with_escalation(orphan_pids)
        return killed

    def _peek_lru_non_default(self) -> Optional[str]:
        """Return the least-recently-used non-default session_id (without deleting it).

        Returns None if the number of non-default sessions is below the cap.
        """
        non_default = [
            (sid, s.last_used_at)
            for sid, s in self.sessions.items()
            if sid != self.DEFAULT_SESSION_ID
        ]
        if len(non_default) < self._max_non_default:
            return None
        return min(non_default, key=lambda x: x[1])[0]

    async def delete_session(self, session_id: str) -> bool:
        """Close and remove a session, cleaning up its profile directory."""
        validate_session_id(session_id)
        session = self.sessions.pop(session_id, None)
        if not session:
            return False
        await session.engine_manager.stop_all()
        # Kill any Chrome processes that nodriver's stop() leaked
        killed = await kill_chrome_by_profile_dir(session.profile_dir)
        if killed:
            logger.warning(
                "delete_session '%s': nodriver stop() leaked %d Chrome process(es), killed via /proc scan",
                session_id, killed,
            )
        # Clean up the profile directory to prevent SingletonLock issues on recreation
        shutil.rmtree(session.profile_dir, ignore_errors=True)
        logger.info("Deleted session %s (cleaned up %s)", session_id, session.profile_dir)
        return True

    async def get_default_session(self) -> BrowserSession:
        """Return the default session, creating it if necessary."""
        return await self.get_or_create_session(self.DEFAULT_SESSION_ID)

    async def _close_default_session_preserving_profile(self) -> str:
        """Stop the default session's Chrome process WITHOUT deleting its
        profile directory. Returns the profile_dir path.

        Shared by recover_default_session() (health-check recovery, then
        recreates the session) and stop_all() (process shutdown, does not
        recreate). The default session is the one a user deliberately logs
        into (e.g. a dedicated account for AI agents) expecting it to
        persist — unlike non-default sessions, which are ephemeral by design
        and go through the destructive delete_session() instead.

        Only Chrome's own lock files are removed here — those can be left
        behind by an unclean process death and would otherwise block a new
        Chrome instance from opening the same profile.
        """
        session = self.sessions.pop(self.DEFAULT_SESSION_ID, None)
        profile_dir = session.profile_dir if session else self._make_profile_dir(
            self.DEFAULT_SESSION_ID
        )
        if session:
            await session.engine_manager.stop_all()
        killed = await kill_chrome_by_profile_dir(profile_dir)
        if killed:
            logger.warning(
                "Stopped default session: nodriver stop() leaked %d Chrome "
                "process(es), killed via /proc scan",
                killed,
            )
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(profile_dir, lock_name))
            except FileNotFoundError:
                pass
        return profile_dir

    async def recover_default_session(self) -> BrowserSession:
        """Recover the default session after its Chrome process died unexpectedly
        (e.g. the user closed the window), WITHOUT wiping its profile directory.

        2026-07-26: real case — the health check's old recovery path called
        delete_session(), which rmtree's the whole profile, silently destroying
        the user's logged-in state every time the window happened to close.
        """
        profile_dir = await self._close_default_session_preserving_profile()
        logger.info("Recovered default session (profile preserved: %s)", profile_dir)
        return await self.get_or_create_session(self.DEFAULT_SESSION_ID)

    async def cleanup_orphan_profiles(self) -> int:
        """Remove session profile directories not tracked in _sessions.

        Also kills any Chrome processes whose --user-data-dir points at an
        orphan profile, preventing zombie processes from surviving daemon restarts.

        Called on startup to clean up profiles and processes left behind by
        crashed daemon.  Returns the number of orphan directories removed.
        """
        base = STEALTH_PROFILE_DIR
        if not os.path.isdir(base):
            return 0

        expected = {""}
        for sid in self.sessions:
            if sid == self.DEFAULT_SESSION_ID:
                expected.add("")
            else:
                expected.add(f"session-{sid}")

        live_profile_dirs = {
            s.profile_dir for s in self.sessions.values()
            if hasattr(s, "profile_dir")
        }

        removed = 0
        for entry in os.listdir(base):
            full_path = os.path.join(base, entry)
            if not os.path.isdir(full_path):
                continue
            if entry.startswith("session-") and entry not in expected:
                suffix = entry[len("session-"):]
                if SESSION_ID_PATTERN.match(suffix):
                    killed = await kill_chrome_by_profile_dir(full_path)
                    if killed:
                        logger.warning(
                            "cleanup_orphan_profiles: killed %d leaked Chrome process(es) for %s",
                            killed, full_path,
                        )
                    shutil.rmtree(full_path, ignore_errors=True)
                    removed += 1
                    logger.info("Cleaned up orphan profile: %s", full_path)

        # The default session profile lives directly in ``base``.  If the daemon
        # crashed or was restarted, an old Chrome process may still hold this
        # profile open.  Nodriver would then attach to (or fight with) that stale
        # process and its in-memory tab registry would diverge from the new
        # daemon's session state.  Kill any such orphan processes so the default
        # session starts with a clean, accurate tab registry.
        if base not in live_profile_dirs:
            pids = find_chrome_pids_by_profile_dir(base)
            if pids:
                logger.warning(
                    "cleanup_orphan_profiles: killing %d orphan Chrome process(es) "
                    "for the default profile before daemon startup",
                    len(pids),
                )
                await kill_pids_with_escalation(pids)

        return removed

    async def stop_all(self) -> None:
        """Stop all sessions, killing their Chrome processes.

        2026-07-26: real case — a plain `systemctl restart` (or any graceful
        service shutdown) calls this on its way down. It used to call
        delete_session() for every session including the default one, which
        rmtree's the whole profile directory — silently wiping the user's
        logged-in state (a dedicated account, other site logins) on
        every single restart, not just on the health-check dead-browser path.
        Non-default sessions remain fully cleaned up (ephemeral by design);
        the default session's Chrome process is stopped but its profile is
        preserved, same reasoning as recover_default_session().
        """
        for sid in list(self.sessions.keys()):
            if sid == self.DEFAULT_SESSION_ID:
                profile_dir = await self._close_default_session_preserving_profile()
                logger.info(
                    "Stopped default session on shutdown (profile preserved: %s)",
                    profile_dir,
                )
            else:
                await self.delete_session(sid)
