"""Unit tests for session management helpers."""
import asyncio
import os
import shutil
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from src.core.session import SessionManager, BrowserSession


class TestCleanupOrphanProfiles:
    """Tests for startup cleanup of leftover profile directories."""

    @pytest.mark.asyncio
    async def test_removes_unknown_session_directories(self, tmp_path):
        """Orphan session-* directories are removed on cleanup."""
        base = tmp_path / "ai-browser-chrome-profile"
        base.mkdir()
        orphan = base / "session-orphan123"
        orphan.mkdir()
        keeper = base / "session-keeper456"
        keeper.mkdir()

        mgr = SessionManager(browser_path="/fake/chrome", headless=True)
        # Simulate an active tracked session
        mgr.sessions["keeper456"] = None  # value not used by cleanup

        with patch("src.core.session.STEALTH_PROFILE_DIR", str(base)):
            removed = await mgr.cleanup_orphan_profiles()

        assert removed == 1
        assert not orphan.exists()
        assert keeper.exists()

    @pytest.mark.asyncio
    async def test_keeps_base_directory(self, tmp_path):
        """The base profile directory itself is never removed."""
        base = tmp_path / "ai-browser-chrome-profile"
        base.mkdir()

        mgr = SessionManager(browser_path="/fake/chrome", headless=True)
        with patch("src.core.session.STEALTH_PROFILE_DIR", str(base)):
            removed = await mgr.cleanup_orphan_profiles()

        assert removed == 0
        assert base.exists()

    @pytest.mark.asyncio
    async def test_ignores_invalid_session_names(self, tmp_path):
        """Directories with unsafe names are left alone."""
        base = tmp_path / "ai-browser-chrome-profile"
        base.mkdir()
        unsafe = base / "session-invalid!name"
        unsafe.mkdir()

        mgr = SessionManager(browser_path="/fake/chrome", headless=True)
        with patch("src.core.session.STEALTH_PROFILE_DIR", str(base)):
            removed = await mgr.cleanup_orphan_profiles()

        assert removed == 0
        assert unsafe.exists()


@pytest.fixture
def mock_engine_manager():
    """Return a patcher that replaces EngineManager with a lightweight async mock."""
    def _make_mock():
        mock = MagicMock()
        mock.return_value.ensure_started = AsyncMock()
        mock.return_value.stop_all = AsyncMock()
        mock.return_value.current_name = "stealth"
        return mock
    return _make_mock


@pytest.fixture
def temp_profile_base(tmp_path):
    """Return a temp base dir to use as the chrome profile root."""
    base = tmp_path / "ai-browser-chrome-profile"
    base.mkdir()
    return base


@pytest.fixture
def session_mgr(temp_profile_base, mock_engine_manager):
    """Return a SessionManager with mocked EngineManager and temp profile dir."""
    with patch("src.core.session.EngineManager", mock_engine_manager()):
        with patch("src.core.session.os.path.expanduser", return_value=str(temp_profile_base)):
            mgr = SessionManager(
                browser_path="/fake/chrome",
                headless=True,
                idle_ttl=600,
                max_non_default=3,
                reaper_interval=60,
            )
            yield mgr


class TestIdleEviction:
    """Tests for the background reaper's idle TTL eviction."""

    async def test_evict_idle_sessions_removes_expired(self, session_mgr):
        """A non-default session idle longer than TTL is evicted and cleaned up."""
        session = await session_mgr.create_session(session_id="expired-session")
        session.last_used_at = datetime.now() - timedelta(seconds=700)
        profile_dir = session.profile_dir

        evicted = await session_mgr._evict_idle_sessions()

        assert evicted == 1
        assert "expired-session" not in session_mgr.sessions
        assert not os.path.isdir(profile_dir)

    async def test_evict_idle_sessions_keeps_default(self, session_mgr):
        """The default session is never evicted, even when idle beyond TTL."""
        session = await session_mgr.get_default_session()
        session.last_used_at = datetime.now() - timedelta(seconds=700)

        evicted = await session_mgr._evict_idle_sessions()

        assert evicted == 0
        assert SessionManager.DEFAULT_SESSION_ID in session_mgr.sessions

    async def test_evict_idle_sessions_keeps_fresh(self, session_mgr):
        """A non-default session used within TTL is not evicted."""
        session = await session_mgr.create_session(session_id="fresh-session")
        session.last_used_at = datetime.now() - timedelta(seconds=30)

        evicted = await session_mgr._evict_idle_sessions()

        assert evicted == 0
        assert "fresh-session" in session_mgr.sessions


class TestLruEviction:
    """Tests for LRU eviction when the non-default session cap is reached."""

    async def test_lru_eviction_on_cap_reached(self, session_mgr):
        """When cap is reached, creating a new session evicts the LRU one."""
        session_mgr._max_non_default = 2

        first = await session_mgr.create_session(session_id="first")
        second = await session_mgr.create_session(session_id="second")
        # Make 'first' the LRU by aging its timestamp
        first.last_used_at = datetime.now() - timedelta(seconds=60)
        second.last_used_at = datetime.now()

        third = await session_mgr.get_or_create_session(session_id="third")

        assert "first" not in session_mgr.sessions
        assert "second" in session_mgr.sessions
        assert "third" in session_mgr.sessions
        assert third.session_id == "third"

    async def test_lru_eviction_not_triggered_under_cap(self, session_mgr):
        """No eviction happens while the number of non-default sessions is below cap."""
        session_mgr._max_non_default = 3

        await session_mgr.create_session(session_id="first")
        await session_mgr.create_session(session_id="second")

        third = await session_mgr.get_or_create_session(session_id="third")

        assert len([s for s in session_mgr.sessions if s != "default"]) == 3
        assert "first" in session_mgr.sessions
        assert "second" in session_mgr.sessions
        assert "third" in session_mgr.sessions

    def test_peek_lru_returns_oldest(self, session_mgr):
        """_peek_lru_non_default returns the least-recently-used non-default session."""
        session_mgr._max_non_default = 2
        session_mgr.sessions["default"] = BrowserSession(
            session_id="default",
            engine_manager=MagicMock(),
            profile_dir="/tmp/default",
            last_used_at=datetime.now(),
        )
        session_mgr.sessions["oldest"] = BrowserSession(
            session_id="oldest",
            engine_manager=MagicMock(),
            profile_dir="/tmp/oldest",
            last_used_at=datetime.now() - timedelta(seconds=120),
        )
        session_mgr.sessions["newest"] = BrowserSession(
            session_id="newest",
            engine_manager=MagicMock(),
            profile_dir="/tmp/newest",
            last_used_at=datetime.now(),
        )

        lru = session_mgr._peek_lru_non_default()

        assert lru == "oldest"

    def test_peek_lru_returns_none_under_cap(self, session_mgr):
        """_peek_lru_non_default returns None when below the cap."""
        session_mgr._max_non_default = 3
        session_mgr.sessions["only"] = BrowserSession(
            session_id="only",
            engine_manager=MagicMock(),
            profile_dir="/tmp/only",
            last_used_at=datetime.now(),
        )

        assert session_mgr._peek_lru_non_default() is None


class TestReaperLifecycle:
    """Tests for the background reaper task."""

    async def test_reaper_loop_evicts_on_interval(self, session_mgr):
        """The reaper loop calls _evict_idle_sessions at each interval."""
        session_mgr._reaper_interval = 0.001  # 1 ms interval for fast test
        session_mgr._evict_idle_sessions = AsyncMock(return_value=0)
        session_mgr._reap_orphan_chrome = AsyncMock(return_value=0)

        task = asyncio.create_task(session_mgr._reaper_loop())
        await asyncio.sleep(0.005)  # let it tick at least once
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert session_mgr._evict_idle_sessions.await_count >= 1

    async def test_reaper_cancelled_cleanly(self, session_mgr):
        """stop_reaper cancels the task without raising an exception."""
        await session_mgr.start_reaper()
        assert session_mgr._reaper_task is not None

        await session_mgr.stop_reaper()

        assert session_mgr._reaper_task is None


class TestReaperRaceSafety:
    """Tests that the reaper does not race with session creation."""

    @pytest.mark.asyncio
    async def test_reaper_skips_while_session_creating(self, tmp_path):
        """A session whose profile dir exists but is not yet in self.sessions
        must not be reaped as an orphan while create_session holds the lock.
        """
        from unittest.mock import patch

        base = tmp_path / "ai-browser-chrome-profile"
        base.mkdir()
        create_started = asyncio.Event()
        create_continue = asyncio.Event()

        async def slow_ensure_started(*args, **kwargs):
            create_started.set()
            await create_continue.wait()

        mock_em = MagicMock()
        mock_em.ensure_started = AsyncMock(side_effect=slow_ensure_started)
        mock_em.stop_all = AsyncMock()
        mock_em.current_name = "stealth"

        with patch("src.core.session.EngineManager", return_value=mock_em):
            with patch("src.core.session.os.path.expanduser", return_value=str(base)):
                mgr = SessionManager(
                    browser_path="/fake/chrome",
                    headless=True,
                    max_non_default=10,
                )
                create_task = asyncio.create_task(
                    mgr.create_session(session_id="racing")
                )
                await create_started.wait()

                # At this point profile dir exists, engine is "starting",
                # and the session has NOT yet been inserted into self.sessions.
                # The reaper must see the locked create_lock and skip.
                killed = await mgr._reap_orphan_chrome()
                assert killed == 0

                create_continue.set()
                session = await create_task
                assert session.session_id == "racing"
                assert "racing" in mgr.sessions


class TestSessionHelpers:
    """Tests for SessionManager helper methods."""

    async def test_list_sessions_includes_idle_seconds(self, session_mgr):
        """list_sessions() enriches each session dict with idle_seconds."""
        session = await session_mgr.create_session(session_id="listed")
        session.last_used_at = datetime.now() - timedelta(seconds=42)

        result = session_mgr.list_sessions()

        entry = next(s for s in result if s["session_id"] == "listed")
        assert "idle_seconds" in entry
        assert entry["idle_seconds"] >= 42

    async def test_delete_session_idempotent_under_race(self, session_mgr):
        """Concurrent delete_session calls are idempotent and do not raise."""
        await session_mgr.create_session(session_id="race")

        async def _delete():
            return await session_mgr.delete_session("race")

        results = await asyncio.gather(_delete(), _delete())

        # One succeeds, the other finds nothing to delete
        assert sorted(results) == [False, True]


@pytest.fixture
def isolated_session_mgr(tmp_path, mock_engine_manager):
    """SessionManager properly isolated to a temp profile dir.

    Patches `src.core.session.STEALTH_PROFILE_DIR` directly rather than
    `os.path.expanduser` — the latter (used by the `session_mgr` fixture
    above) doesn't actually take effect, since STEALTH_PROFILE_DIR is a
    plain string already resolved once at config.py's own import time, not
    recomputed per call. Tests using `session_mgr` were silently creating
    real directories under the user's actual `~/.config/ai-browser-chrome-
    profile/` (confirmed 2026-07-26 — found real litter there after a test
    run). Not fixing that fixture here (out of scope), but new tests use
    this correctly-isolated one instead.
    """
    base = tmp_path / "ai-browser-chrome-profile"
    base.mkdir()
    with patch("src.core.session.EngineManager", mock_engine_manager()):
        with patch("src.core.session.STEALTH_PROFILE_DIR", str(base)):
            mgr = SessionManager(browser_path="/fake/chrome", headless=True)
            yield mgr, base


class TestRecoverDefaultSession:
    """Tests for recover_default_session() (2026-07-26).

    Real case: the health check's old recovery path called delete_session(),
    which rmtree's the whole profile directory. For non-default sessions
    that's correct (ephemeral by design), but the default session is the one
    a user deliberately logs into (e.g. a dedicated account for AI
    agents) expecting it to persist. Wiping it every time the Chrome window
    happened to close silently destroyed that login state.
    """

    async def test_preserves_profile_directory_contents(self, isolated_session_mgr):
        mgr, base = isolated_session_mgr
        session = await mgr.get_or_create_session("default")
        profile_dir = session.profile_dir
        marker = os.path.join(profile_dir, "Default", "Login Data")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as f:
            f.write("pretend-cookie-db")

        with patch("src.core.session.kill_chrome_by_profile_dir", AsyncMock(return_value=0)):
            recovered = await mgr.recover_default_session()

        assert os.path.isfile(marker), "profile contents must survive recovery"
        assert recovered.profile_dir == profile_dir

    async def test_removes_stale_lock_files(self, isolated_session_mgr):
        """Chrome's own lock files ARE removed — a stale lock left behind by
        an unclean process death would otherwise block the new Chrome
        instance from opening the same profile."""
        mgr, base = isolated_session_mgr
        session = await mgr.get_or_create_session("default")
        profile_dir = session.profile_dir
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            open(os.path.join(profile_dir, name), "w").close()

        with patch("src.core.session.kill_chrome_by_profile_dir", AsyncMock(return_value=0)):
            await mgr.recover_default_session()

        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            assert not os.path.exists(os.path.join(profile_dir, name))

    async def test_kills_leaked_chrome_process(self, isolated_session_mgr):
        mgr, base = isolated_session_mgr
        await mgr.get_or_create_session("default")

        kill_mock = AsyncMock(return_value=2)
        with patch("src.core.session.kill_chrome_by_profile_dir", kill_mock):
            await mgr.recover_default_session()

        kill_mock.assert_awaited_once()

    async def test_works_when_no_session_currently_tracked(self, isolated_session_mgr):
        """Recovery must work even if the default session object was already
        gone from `self.sessions` (e.g. a prior failed recovery attempt)."""
        mgr, base = isolated_session_mgr

        with patch("src.core.session.kill_chrome_by_profile_dir", AsyncMock(return_value=0)):
            recovered = await mgr.recover_default_session()

        assert recovered.session_id == "default"


class TestStopAllPreservesDefaultProfile:
    """Tests for stop_all() (2026-07-26).

    Real case: a plain `systemctl restart` triggers FastAPI's graceful
    shutdown hook, which calls stop_all(). It used to call delete_session()
    for every session including the default one — wiping the user's logged-in
    profile on every single restart, not just the health-check dead-browser
    path fixed earlier the same day. This was the actual, more common cause:
    the user reported the profile still vanished after the first fix landed,
    specifically after a service restart.
    """

    async def test_default_session_profile_survives_stop_all(self, isolated_session_mgr):
        mgr, base = isolated_session_mgr
        session = await mgr.get_or_create_session("default")
        profile_dir = session.profile_dir
        marker = os.path.join(profile_dir, "Default", "Cookies")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as f:
            f.write("pretend-cookies-db")

        with patch("src.core.session.kill_chrome_by_profile_dir", AsyncMock(return_value=0)):
            await mgr.stop_all()

        assert os.path.isfile(marker), "default profile must survive stop_all()"
        assert "default" not in mgr.sessions

    async def test_non_default_session_still_fully_cleaned_up(self, isolated_session_mgr):
        """Non-default sessions remain ephemeral — stop_all() must still
        rmtree them, only the default session is special-cased."""
        mgr, base = isolated_session_mgr
        await mgr.get_or_create_session("default")
        other = await mgr.create_session(session_id="scratch")
        other_profile_dir = other.profile_dir

        with patch("src.core.session.kill_chrome_by_profile_dir", AsyncMock(return_value=0)):
            await mgr.stop_all()

        assert not os.path.isdir(other_profile_dir)
        assert mgr.sessions == {}

    async def test_stop_all_with_no_default_session_created_yet(self, isolated_session_mgr):
        """stop_all() must not raise when only non-default sessions exist."""
        mgr, base = isolated_session_mgr
        await mgr.create_session(session_id="scratch")

        with patch("src.core.session.kill_chrome_by_profile_dir", AsyncMock(return_value=0)):
            await mgr.stop_all()  # must not raise

        assert mgr.sessions == {}
