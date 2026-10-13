"""Engine manager — handles automatic engine switching based on anti-bot detection."""
import ipaddress
import time
from typing import Optional
from urllib.parse import urlparse
from .stealth_cdp import StealthEngine
from .native_os import NativeEngine
from .block_detection import detect_block, BlockResult, BlockSignal
from ..config import NATIVE_FALLBACK_COOLDOWN_SECONDS
from ..log_config import get_logger

logger = get_logger(__name__)

# Hostnames that always resolve to the local machine.
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _is_local_url(url: str) -> bool:
    """Return True if URL points to localhost or a private/local IP address.

    Local addresses cannot be anti-bot challenge pages, so the engine manager
    forces the stealth (CDP) engine for them and skips block detection.
    """
    host = urlparse(url).hostname or ""
    host_lower = host.lower()
    if host_lower in _LOCAL_HOSTNAMES:
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback or addr.is_private or addr.is_link_local
    except ValueError:
        # Not a valid IP address (e.g. a normal domain name).
        return False


class EngineManager:
    """Manages engine lifecycle and automatic switching."""

    def __init__(
        self,
        browser_path: str = "",
        headless: bool = False,
        profile_dir: str = "",
    ):
        self.browser_path = browser_path
        self.headless = headless
        self.profile_dir = profile_dir
        self.stealth: Optional[StealthEngine] = None
        self.native: Optional[NativeEngine] = None
        self.current_name: str = "stealth"
        self._fallback_until: float = 0  # timestamp until which we stay on native
        self._last_nav_html: str = ""  # html snippet from last navigation (for block detection)

    async def ensure_started(self) -> str:
        """Ensure at least one engine is running. Returns engine name.

        Raises RuntimeError if both engines fail to start.
        """
        if not self.stealth:
            self.stealth = StealthEngine(
                headless=self.headless,
                browser_path=self.browser_path,
                profile_dir=self.profile_dir,
            )
            try:
                await self.stealth.start()
                logger.info("Stealth engine started")
            except Exception as e:
                logger.error("Stealth engine start failed: %s", e)
                self.stealth = None
                # Try native as fallback
                await self._ensure_native()
                if self.native:
                    return "native"
                # Bug 3 fix: both engines failed — raise instead of silently returning success
                raise RuntimeError(
                    "Both stealth and native engines failed to start"
                )
        return self.current_name

    async def _ensure_native(self):
        """Start native engine if not running."""
        if not self.native:
            import os
            from ..config import NATIVE_PROFILE_DIR
            os.makedirs(NATIVE_PROFILE_DIR, exist_ok=True)
            # Use a separate profile dir so native Chrome doesn't share an instance with stealth Chrome
            browser_cmd = (
                [self.browser_path, "--new-window", f"--user-data-dir={NATIVE_PROFILE_DIR}"]
                if self.browser_path else None
            )
            window_title = "Edge" if self.browser_path and "edge" in self.browser_path.lower() else "Chrome"
            self.native = NativeEngine(browser_cmd=browser_cmd, window_title=window_title)
            try:
                await self.native.start()
                logger.info("Native engine started")
            except Exception as e:
                logger.error("Native engine start failed: %s", e)
                self.native = None

    def get_engine(self):
        """Get the currently active engine."""
        if time.time() < self._fallback_until and self.native:
            return self.native
        if self.current_name == "native" and self.native:
            return self.native
        if self.stealth:
            return self.stealth
        if self.native:
            return self.native
        raise RuntimeError("No engine available")

    async def navigate_with_fallback(self, url: str) -> dict:
        """Navigate with automatic engine fallback on anti-bot detection."""
        await self.ensure_started()

        # Phase 6 Task 1: local addresses can never be anti-bot challenge pages.
        # Force stealth and skip block detection so JS-based tools keep working.
        if _is_local_url(url):
            logger.info("Local URL detected, forcing stealth engine: %s", url)
            self.current_name = "stealth"
            self._fallback_until = 0
            if not self.stealth:
                raise RuntimeError(
                    "Stealth engine required for local URLs but is not available"
                )
            result = await self.stealth.navigate(url)
            self._last_nav_html = result.get("html", "")
            result["blocked"] = False
            result["block_confidence"] = 0.0
            result["block_signals"] = []
            result["engine_used"] = "stealth"
            result["fallback_triggered"] = False
            logger.info(
                "Navigated to local URL %s | title=%r | engine=stealth",
                result.get("url", url),
                result.get("title", ""),
            )
            return result

        # Check if we should try switching back to stealth after cooldown
        # Bug 4 fix: this check is at the top level, not inside a dead elif branch
        if (
            self.current_name == "native"
            and self._fallback_until > 0  # only auto-switch back after a forced fallback, not manual native mode
            and time.time() >= self._fallback_until
            and self.stealth
        ):
            # Cooldown expired — try stealth again
            logger.info("Native fallback cooldown expired, switching back to stealth")
            self.current_name = "stealth"
            self._fallback_until = 0

        engine = self.get_engine()
        if not engine:
            raise RuntimeError("No engine available")

        is_forced_native = (
            self.current_name == "native" or time.time() < self._fallback_until
        )

        # Bug 7 fix: use independent boolean to track fallback
        fallback_triggered = False

        if is_forced_native:
            # User explicitly wants native, or we are in fallback cooldown.
            result = await engine.navigate(url)
            self._last_nav_html = result.get("html", "")
            block_result = await self._detect_block()
            is_blocked = block_result.blocked
        else:
            # Auto/stealth mode: try stealth first, fallback on block detection.
            # A stealth navigate that raises (e.g. STEALTH_NAVIGATE_TIMEOUT_SECONDS
            # exceeded — real case: kmart.com.au never returns) is treated the same
            # as a detected block, so the same native-fallback path below still runs
            # instead of the exception just propagating and wedging the session.
            try:
                result = await engine.navigate(url)
                self._last_nav_html = result.get("html", "")
                block_result = await self._detect_block()
                is_blocked = block_result.blocked
            except Exception as e:
                if engine is not self.stealth:
                    raise
                logger.warning(
                    "Stealth engine navigate failed for %s (%s), treating as blocked",
                    url,
                    e,
                )
                result = {}
                is_blocked = True
                block_result = BlockResult(
                    blocked=True,
                    confidence=1.0,
                    signals=[BlockSignal(name="stealth_navigate_error", value=str(e), confidence=1.0)],
                )

            if is_blocked and engine is self.stealth:
                logger.warning(
                    "Block detected on stealth engine (confidence=%.2f, signals=%s), "
                    "switching to native for %s",
                    block_result.confidence,
                    [s.name for s in block_result.signals],
                    url,
                )
                await self._ensure_native()
                if self.native:
                    self._fallback_until = time.time() + NATIVE_FALLBACK_COOLDOWN_SECONDS
                    self.current_name = "native"
                    engine = self.native
                    fallback_triggered = True
                    result = await self.native.navigate(url)
                    self._last_nav_html = result.get("html", "")
                    block_result = await self._detect_block()
                    is_blocked = block_result.blocked
                    if is_blocked:
                        logger.warning("Also blocked on native engine for %s", url)

        result["blocked"] = is_blocked
        result["block_confidence"] = block_result.confidence
        result["block_signals"] = [
            {"name": s.name, "value": s.value, "confidence": s.confidence}
            for s in block_result.signals
        ]
        result["engine_used"] = "native" if self.current_name == "native" else "stealth"
        result["fallback_triggered"] = fallback_triggered
        logger.info(
            "Navigated to %s | title=%r | engine=%s | blocked=%s | confidence=%.2f | fallback=%s",
            result.get("url", url),
            result.get("title", ""),
            result["engine_used"],
            result["blocked"],
            result["block_confidence"],
            result["fallback_triggered"],
        )
        return result

    async def _detect_block(self) -> BlockResult:
        """Check if the current page is showing anti-bot content.

        Bug 5 fix: engine exceptions (e.g. Chrome crashed) are treated as blocked=True,
        not silently swallowed as blocked=False.
        """
        engine = self.get_engine()
        try:
            title = await engine.get_title()
            body_text = ""
            url = ""
            # For stealth engine, also check body text and URL
            if isinstance(engine, StealthEngine) and engine.tab:
                text_result = await engine.get_text()
                body_text = text_result.get("text", "")[:2000]
                url_result = await engine.get_url()
                url = url_result or ""

            return detect_block(
                title=title or "",
                body_text=body_text,
                url=url,
                html=self._last_nav_html,
            )
        except Exception as e:
            # Engine error (e.g. Chrome crashed) — treat as blocked so fallback triggers
            logger.error("Block detection engine error (treating as blocked): %s", e)
            return BlockResult(
                blocked=True,
                confidence=1.0,
                signals=[],
            )

    async def stop_all(self):
        """Stop all engines."""
        if self.stealth:
            await self.stealth.stop()
        if self.native:
            await self.native.stop()

    # --- Navigation helpers ---

    async def go_back(self) -> dict:
        """Go back in browser history."""
        engine = self.get_engine()
        result = await engine.go_back()
        result["engine_used"] = self.current_name
        return result

    async def go_forward(self) -> dict:
        """Go forward in browser history."""
        engine = self.get_engine()
        result = await engine.go_forward()
        result["engine_used"] = self.current_name
        return result

    async def reload(self) -> dict:
        """Reload the current page."""
        engine = self.get_engine()
        result = await engine.reload()
        result["engine_used"] = self.current_name
        return result

    async def select(self, selector: str, value: str) -> dict:
        """Select an option in a <select> element."""
        engine = self.get_engine()
        return await engine.select(selector, value)

    async def check(self, selector: str) -> dict:
        """Check a checkbox or radio button."""
        engine = self.get_engine()
        return await engine.check(selector)

    async def uncheck(self, selector: str) -> dict:
        """Uncheck a checkbox."""
        engine = self.get_engine()
        return await engine.uncheck(selector)

    # --- Multi-tab passthrough (stealth only) ---

    async def open_tab(self, url: str = "about:blank") -> dict:
        """Open a new tab. Only supported on stealth engine."""
        await self.ensure_started()
        engine = self.get_engine()
        if isinstance(engine, StealthEngine):
            return await engine.open_tab(url)
        return {"error": "multi-tab not supported in native engine"}

    async def switch_tab(self, tab_id: str) -> dict:
        """Switch to a tab. Only supported on stealth engine."""
        engine = self.get_engine()
        if isinstance(engine, StealthEngine):
            return await engine.switch_tab(tab_id)
        return {"error": "multi-tab not supported in native engine"}

    async def close_tab(self, tab_id: str) -> dict:
        """Close a tab. Only supported on stealth engine."""
        engine = self.get_engine()
        if isinstance(engine, StealthEngine):
            return await engine.close_tab(tab_id)
        return {"error": "multi-tab not supported in native engine"}

    async def list_tabs(self) -> list:
        """List all tabs. Only supported on stealth engine."""
        engine = self.get_engine()
        if isinstance(engine, StealthEngine):
            return await engine.list_tabs()
        return []

    # --- Cookie management (stealth only) ---

    async def get_cookies(self, domain: str = "") -> list:
        engine = self.get_engine()
        return await engine.get_cookies(domain=domain)

    async def set_cookie(self, name: str, value: str, domain: str, path: str = "/",
                         expires: float = -1, secure: bool = False,
                         http_only: bool = False) -> dict:
        engine = self.get_engine()
        return await engine.set_cookie(name, value, domain, path, expires, secure, http_only)

    async def clear_cookies(self, domain: str = "") -> dict:
        engine = self.get_engine()
        return await engine.clear_cookies(domain=domain)
