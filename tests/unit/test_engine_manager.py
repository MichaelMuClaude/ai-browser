"""Unit tests for EngineManager — engine switching, fallback, and block detection logic.

These tests mock StealthEngine and NativeEngine to test EngineManager's
switching logic without requiring a real browser.
"""
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.engine_manager import EngineManager, _is_local_url
from src.core.block_detection import BlockResult, BlockSignal


class FakeEngine:
    """Minimal fake engine for testing."""

    def __init__(self, name: str = "fake"):
        self.name = name
        self.started = False
        self.navigate = AsyncMock(return_value={"url": "https://example.com", "title": "Example", "html": ""})
        self.get_title = AsyncMock(return_value="Example Domain")
        self.get_text = AsyncMock(return_value={"text": "Hello world"})
        self.get_url = AsyncMock(return_value="https://example.com")
        self.screenshot = AsyncMock(return_value=b"fake-png")
        self.stop = AsyncMock()
        self.start = AsyncMock()

    def __repr__(self):
        return f"FakeEngine({self.name})"


def _make_engine_mgr():
    """Create an EngineManager with no real engines."""
    mgr = EngineManager(browser_path="/fake/chrome", headless=True, profile_dir="/tmp/fake")
    mgr.stealth = None
    mgr.native = None
    return mgr


# --- Tests ---

class TestEngineManagerStart:
    """Tests for ensure_started()."""

    @pytest.mark.asyncio
    async def test_ensure_started_stealth_success(self):
        """Stealth engine starts successfully → returns 'stealth'."""
        mgr = _make_engine_mgr()
        with patch("src.core.engine_manager.StealthEngine") as MockStealth:
            fake = FakeEngine("stealth")
            fake.start = AsyncMock()
            MockStealth.return_value = fake
            result = await mgr.ensure_started()
            assert result == "stealth"
            assert mgr.stealth is fake

    @pytest.mark.asyncio
    async def test_ensure_started_both_fail_raises(self):
        """Both engines fail → RuntimeError (Bug 3 fix)."""
        mgr = _make_engine_mgr()
        with patch("src.core.engine_manager.StealthEngine") as MockStealth, \
             patch("src.core.engine_manager.NativeEngine") as MockNative:
            fake_stealth = FakeEngine("stealth")
            fake_stealth.start = AsyncMock(side_effect=Exception("Chrome not found"))
            MockStealth.return_value = fake_stealth

            fake_native = FakeEngine("native")
            fake_native.start = AsyncMock(side_effect=Exception("No display"))
            MockNative.return_value = fake_native

            with pytest.raises(RuntimeError, match="Both stealth and native engines failed"):
                await mgr.ensure_started()

    @pytest.mark.asyncio
    async def test_ensure_started_stealth_fail_native_ok(self):
        """Stealth fails but native succeeds → returns 'native'."""
        mgr = _make_engine_mgr()
        with patch("src.core.engine_manager.StealthEngine") as MockStealth, \
             patch("src.core.engine_manager.NativeEngine") as MockNative:
            fake_stealth = FakeEngine("stealth")
            fake_stealth.start = AsyncMock(side_effect=Exception("Chrome not found"))
            MockStealth.return_value = fake_stealth

            fake_native = FakeEngine("native")
            fake_native.start = AsyncMock()
            MockNative.return_value = fake_native

            result = await mgr.ensure_started()
            assert result == "native"
            assert mgr.native is fake_native


class TestEngineManagerSwitch:
    """Tests for engine switching logic."""

    def test_get_engine_prefers_stealth(self):
        """get_engine returns stealth when available and current_name is stealth."""
        mgr = _make_engine_mgr()
        mgr.stealth = FakeEngine("stealth")
        mgr.current_name = "stealth"
        engine = mgr.get_engine()
        assert engine is mgr.stealth

    def test_get_engine_returns_native_when_current(self):
        """get_engine returns native when current_name is native."""
        mgr = _make_engine_mgr()
        mgr.native = FakeEngine("native")
        mgr.current_name = "native"
        engine = mgr.get_engine()
        assert engine is mgr.native

    def test_get_engine_fallback_to_native(self):
        """get_engine falls back to native if stealth is None."""
        mgr = _make_engine_mgr()
        mgr.stealth = None
        mgr.native = FakeEngine("native")
        mgr.current_name = "stealth"  # default
        engine = mgr.get_engine()
        assert engine is mgr.native

    def test_get_engine_no_engine_raises(self):
        """get_engine raises RuntimeError when no engine is available."""
        mgr = _make_engine_mgr()
        mgr.stealth = None
        mgr.native = None
        with pytest.raises(RuntimeError, match="No engine available"):
            mgr.get_engine()

    def test_get_engine_in_fallback_cooldown(self):
        """During fallback cooldown, native is returned even if current_name is stealth."""
        mgr = _make_engine_mgr()
        mgr.stealth = FakeEngine("stealth")
        mgr.native = FakeEngine("native")
        mgr.current_name = "stealth"
        mgr._fallback_until = time.time() + 100  # cooldown active
        engine = mgr.get_engine()
        assert engine is mgr.native


class TestNavigateWithFallback:
    """Tests for navigate_with_fallback() — the core switching logic."""

    @pytest.mark.asyncio
    async def test_stealth_success_no_block(self):
        """Stealth navigates and no block detected → stays on stealth."""
        mgr = _make_engine_mgr()
        mgr.stealth = FakeEngine("stealth")
        mgr.current_name = "stealth"

        with patch.object(mgr, "_detect_block", AsyncMock(return_value=BlockResult(blocked=False, confidence=0.0, signals=[]))):
            with patch.object(mgr, "ensure_started", AsyncMock()):
                result = await mgr.navigate_with_fallback("https://example.com")
                assert result["blocked"] is False
                assert result["engine_used"] == "stealth"
                assert result["fallback_triggered"] is False

    @pytest.mark.asyncio
    async def test_stealth_blocked_switches_to_native(self):
        """Stealth blocked → switches to native, sets cooldown."""
        mgr = _make_engine_mgr()
        fake_stealth = FakeEngine("stealth")
        fake_native = FakeEngine("native")
        mgr.stealth = fake_stealth
        mgr.native = fake_native
        mgr.current_name = "stealth"

        # First detect → blocked, second detect (after switch) → not blocked
        detect_results = [
            BlockResult(blocked=True, confidence=0.9, signals=[BlockSignal(name="cf_ray", value="x", confidence=0.7)]),
            BlockResult(blocked=False, confidence=0.0, signals=[]),
        ]
        detect_mock = AsyncMock(side_effect=detect_results)

        with patch.object(mgr, "_detect_block", detect_mock):
            with patch.object(mgr, "ensure_started", AsyncMock()):
                with patch.object(mgr, "_ensure_native", AsyncMock()):
                    result = await mgr.navigate_with_fallback("https://blocked-site.com")

        assert result["blocked"] is False
        assert result["engine_used"] == "native"
        assert result["fallback_triggered"] is True
        assert mgr.current_name == "native"
        assert mgr._fallback_until > 0

    @pytest.mark.asyncio
    async def test_fallback_cooldown_expires_switches_back(self):
        """After cooldown expires, auto-switches back to stealth."""
        mgr = _make_engine_mgr()
        fake_stealth = FakeEngine("stealth")
        fake_native = FakeEngine("native")
        mgr.stealth = fake_stealth
        mgr.native = fake_native
        mgr.current_name = "native"
        mgr._fallback_until = time.time() - 1  # cooldown already expired

        with patch.object(mgr, "_detect_block", AsyncMock(return_value=BlockResult(blocked=False, confidence=0.0, signals=[]))):
            with patch.object(mgr, "ensure_started", AsyncMock()):
                result = await mgr.navigate_with_fallback("https://example.com")

        # Should have switched back to stealth
        assert mgr.current_name == "stealth"
        assert mgr._fallback_until == 0
        assert result["engine_used"] == "stealth"

    @pytest.mark.asyncio
    async def test_manual_native_no_auto_switch_back(self):
        """Manually set to native (fallback_until=0) → no auto-switch-back."""
        mgr = _make_engine_mgr()
        fake_stealth = FakeEngine("stealth")
        fake_native = FakeEngine("native")
        mgr.stealth = fake_stealth
        mgr.native = fake_native
        mgr.current_name = "native"
        mgr._fallback_until = 0  # manual native, not fallback

        with patch.object(mgr, "_detect_block", AsyncMock(return_value=BlockResult(blocked=False, confidence=0.0, signals=[]))):
            with patch.object(mgr, "ensure_started", AsyncMock()):
                result = await mgr.navigate_with_fallback("https://example.com")

        # Should stay on native — no auto-switch-back for manual mode
        assert mgr.current_name == "native"
        assert result["engine_used"] == "native"

    @pytest.mark.asyncio
    async def test_stealth_navigate_raises_switches_to_native(self):
        """Stealth navigate() raising (e.g. STEALTH_NAVIGATE_TIMEOUT_SECONDS exceeded)
        is treated like a detected block and falls back to native, instead of the
        exception propagating and wedging the session."""
        mgr = _make_engine_mgr()
        fake_stealth = FakeEngine("stealth")
        fake_native = FakeEngine("native")
        fake_stealth.navigate = AsyncMock(side_effect=TimeoutError("Navigation to https://kmart.com.au timed out after 20.0s"))
        mgr.stealth = fake_stealth
        mgr.native = fake_native
        mgr.current_name = "stealth"

        with patch.object(mgr, "_detect_block", AsyncMock(return_value=BlockResult(blocked=False, confidence=0.0, signals=[]))):
            with patch.object(mgr, "ensure_started", AsyncMock()):
                with patch.object(mgr, "_ensure_native", AsyncMock()):
                    result = await mgr.navigate_with_fallback("https://kmart.com.au")

        assert result["engine_used"] == "native"
        assert result["fallback_triggered"] is True
        assert mgr.current_name == "native"
        fake_native.navigate.assert_awaited_once_with("https://kmart.com.au")

    @pytest.mark.asyncio
    async def test_native_navigate_raises_reraises(self):
        """A raising navigate() when the active engine is native (no stealth to fall
        back to) propagates instead of being silently swallowed — the new except
        clause only absorbs failures from the stealth engine."""
        mgr = _make_engine_mgr()
        fake_native = FakeEngine("native")
        fake_native.navigate = AsyncMock(side_effect=RuntimeError("native engine crashed"))
        mgr.stealth = None
        mgr.native = fake_native
        mgr.current_name = "stealth"  # no stealth engine available -> get_engine() falls back to native
        mgr._fallback_until = 0

        with patch.object(mgr, "ensure_started", AsyncMock()):
            with pytest.raises(RuntimeError, match="native engine crashed"):
                await mgr.navigate_with_fallback("https://example.com")

    @pytest.mark.asyncio
    async def test_stealth_blocked_native_also_blocked(self):
        """Both stealth and native are blocked → result is blocked but no crash."""
        mgr = _make_engine_mgr()
        fake_stealth = FakeEngine("stealth")
        fake_native = FakeEngine("native")
        mgr.stealth = fake_stealth
        mgr.native = fake_native
        mgr.current_name = "stealth"

        detect_results = [
            BlockResult(blocked=True, confidence=0.9, signals=[]),
            BlockResult(blocked=True, confidence=0.8, signals=[]),
        ]

        with patch.object(mgr, "_detect_block", AsyncMock(side_effect=detect_results)):
            with patch.object(mgr, "ensure_started", AsyncMock()):
                with patch.object(mgr, "_ensure_native", AsyncMock()):
                    result = await mgr.navigate_with_fallback("https://heavily-blocked.com")

        assert result["blocked"] is True
        assert result["engine_used"] == "native"
        assert result["fallback_triggered"] is True


class TestDetectBlock:
    """Tests for _detect_block()."""

    @pytest.mark.asyncio
    async def test_engine_exception_treated_as_blocked(self):
        """Engine error during detection → treated as blocked (Bug 5 fix)."""
        mgr = _make_engine_mgr()
        fake_engine = FakeEngine("stealth")
        fake_engine.get_title = AsyncMock(side_effect=Exception("Chrome crashed"))

        with patch.object(mgr, "get_engine", return_value=fake_engine):
            result = await mgr._detect_block()
            assert result.blocked is True
            assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_normal_page_not_blocked(self):
        """Normal page → not blocked."""
        mgr = _make_engine_mgr()
        fake_engine = FakeEngine("stealth")
        mgr.stealth = fake_engine

        with patch.object(mgr, "get_engine", return_value=fake_engine):
            result = await mgr._detect_block()
            assert result.blocked is False


class TestMultiTab:
    """Tests for multi-tab passthrough."""

    @pytest.mark.asyncio
    async def test_open_tab_native_returns_error(self):
        """open_tab on native engine returns error, not exception."""
        mgr = _make_engine_mgr()
        mgr.native = FakeEngine("native")
        mgr.stealth = None
        mgr.current_name = "native"

        with patch.object(mgr, "ensure_started", AsyncMock()):
            with patch.object(mgr, "get_engine", return_value=mgr.native):
                result = await mgr.open_tab("https://example.com")
                assert "error" in result

    @pytest.mark.asyncio
    async def test_list_tabs_native_returns_empty(self):
        """list_tabs on native engine returns empty list."""
        mgr = _make_engine_mgr()
        mgr.native = FakeEngine("native")
        mgr.stealth = None
        mgr.current_name = "native"

        with patch.object(mgr, "get_engine", return_value=mgr.native):
            result = await mgr.list_tabs()
            assert result == []


class TestIsLocalUrl:
    """Tests for _is_local_url() helper."""

    @pytest.mark.parametrize("url", [
        "http://localhost:8080/path",
        "https://localhost/",
        "http://127.0.0.1:9999",
        "http://[::1]:8080",
        "http://192.168.1.1",
        "http://10.0.0.1",
        "http://172.16.0.1",
        "http://172.31.255.255",
    ])
    def test_local_addresses(self, url):
        """Localhost and private/local IPs are recognized."""
        assert _is_local_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://example.com",
        "http://8.8.8.8",
        "https://1.1.1.1",
        "http://172.32.0.1",  # outside RFC1918 172.16/12 range
    ])
    def test_non_local_addresses(self, url):
        """Public domains and IPs are not local."""
        assert _is_local_url(url) is False


class TestNavigateLocalUrl:
    """Tests for navigate_with_fallback() local URL forcing."""

    @pytest.mark.asyncio
    async def test_local_url_forces_stealth(self):
        """Local URLs always use stealth and skip block detection."""
        mgr = _make_engine_mgr()
        fake_stealth = FakeEngine("stealth")
        mgr.stealth = fake_stealth
        mgr.current_name = "stealth"

        detect_mock = AsyncMock(return_value=BlockResult(blocked=False, confidence=0.0, signals=[]))
        with patch.object(mgr, "_detect_block", detect_mock):
            with patch.object(mgr, "ensure_started", AsyncMock()):
                result = await mgr.navigate_with_fallback("http://localhost:9999")

        assert result["engine_used"] == "stealth"
        assert result["blocked"] is False
        assert result["fallback_triggered"] is False
        detect_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_local_url_resets_native_cooldown(self):
        """Navigating to a local URL resets native fallback cooldown."""
        mgr = _make_engine_mgr()
        fake_stealth = FakeEngine("stealth")
        fake_native = FakeEngine("native")
        mgr.stealth = fake_stealth
        mgr.native = fake_native
        mgr.current_name = "native"
        mgr._fallback_until = time.time() + 100  # active cooldown

        with patch.object(mgr, "_detect_block", AsyncMock()):
            with patch.object(mgr, "ensure_started", AsyncMock()):
                result = await mgr.navigate_with_fallback("http://127.0.0.1:8080")

        assert mgr.current_name == "stealth"
        assert mgr._fallback_until == 0
        assert result["engine_used"] == "stealth"
        assert result["blocked"] is False
