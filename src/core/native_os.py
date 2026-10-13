"""Native OS engine using xdotool — strongest anti-detection."""
import asyncio
import shutil
import subprocess
import time
from typing import Optional

from .base import BrowserEngine
from ..config import (
    NAVIGATE_LOAD_MAX_POLLS,
    NAVIGATE_LOAD_POLL_INTERVAL,
    NATIVE_ENGLISH_IME_ENGINE,
    NATIVE_FORCE_ENGLISH_IME,
    NATIVE_STARTUP_WAIT_SECONDS,
    NATIVE_WINDOW_READY_DELAY,
    XDOTOOL_DEFAULT_TIMEOUT,
    XDOTOOL_DEFAULT_RETRIES,
    XDOTOOL_RETRY_DELAY,
    SCREENSHOT_TIMEOUT,
)
from ..log_config import get_logger

logger = get_logger(__name__)


class XdotoolError(RuntimeError):
    """Raised when an xdotool command fails unexpectedly."""
    pass


class NativeEngine(BrowserEngine):
    """Browser engine using xdotool for OS-level mouse/keyboard events."""

    def __init__(self, browser_cmd: list = None, window_title: str = "Chrome"):
        self.browser_cmd = browser_cmd or ["google-chrome", "--new-window"]
        self.window_title = window_title
        self.browser_pid: Optional[int] = None
        self.window_id: Optional[str] = None
        self._started = False

    def _browser_class_candidates(self) -> list[str]:
        """Return likely X11 WM_CLASS values for the configured browser."""
        haystack = " ".join(self.browser_cmd + [self.window_title]).lower()
        if "edge" in haystack:
            return ["microsoft-edge", "Microsoft-edge", "edge"]
        if "chromium" in haystack:
            return ["chromium", "Chromium", "chrome", "Google-chrome"]
        return ["google-chrome", "Google-chrome", "chrome", "Chromium"]

    def _run_xdotool_sync(
        self,
        args: list,
        timeout: float = 5.0,
        retries: int = 1,
        capture: bool = True,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run an xdotool command synchronously with retry.

        This is the blocking implementation. Async callers should use
        _run_xdotool() instead which wraps this in asyncio.to_thread().
        """
        cmd = ["xdotool"] + args
        last_error = None
        result = None  # avoid UnboundLocalError if all retries fail via exception

        for attempt in range(retries + 1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=capture,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                if result.returncode == 0:
                    return result
                stderr = (result.stderr or "").strip()
                last_error = f"returncode={result.returncode}, stderr={stderr!r}"
                logger.warning("xdotool attempt %d failed: %s", attempt + 1, last_error)
            except subprocess.TimeoutExpired as e:
                last_error = f"timeout after {timeout}s"
                logger.warning("xdotool attempt %d timed out", attempt + 1)
            except Exception as e:
                last_error = str(e)
                logger.warning("xdotool attempt %d error: %s", attempt + 1, last_error)

            if attempt < retries:
                time.sleep(XDOTOOL_RETRY_DELAY)

        error_msg = f"xdotool command failed after {retries + 1} attempts: {' '.join(cmd)} ({last_error})"
        if check:
            raise XdotoolError(error_msg)
        # Return the last failed result (may be None if all attempts raised exceptions)
        if result is None:
            raise XdotoolError(error_msg)
        return result

    async def _run_xdotool(
        self,
        args: list,
        timeout: float = 5.0,
        retries: int = 1,
        capture: bool = True,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Async wrapper around _run_xdotool_sync.

        Bug 9 fix: Wraps the blocking subprocess call in asyncio.to_thread
        so the event loop is not blocked.
        """
        return await asyncio.to_thread(
            self._run_xdotool_sync,
            args,
            timeout,
            retries,
            capture,
            check,
        )

    async def start(self) -> None:
        """Launch browser and locate its window."""
        logger.info("Starting native engine with cmd: %s", self.browser_cmd)
        proc = subprocess.Popen(
            self.browser_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.browser_pid = proc.pid
        await asyncio.sleep(NATIVE_STARTUP_WAIT_SECONDS)  # wait for browser to open

        self.window_id = await self._find_window_id()
        if not self.window_id:
            raise RuntimeError(
                "Could not find browser window for PID "
                f"{self.browser_pid} or title '{self.window_title}'"
            )

        await self._ensure_window_ready()
        self._started = True
        logger.info(
            "Native engine ready: pid=%s window_id=%s", self.browser_pid, self.window_id
        )

    async def _launch_with_url(self, url: str) -> None:
        """Launch a fresh browser window directly with the target URL.

        This bypasses the unreliable xdotool address-bar workflow.
        """
        # Close any existing native window first.
        if self.window_id and await self._window_exists(self.window_id):
            try:
                await self._run_xdotool(["windowclose", self.window_id], check=False)
            except Exception as e:
                logger.warning("Failed to close previous native window: %s", e)
            self.window_id = None

        cmd = list(self.browser_cmd)
        if url not in cmd:
            cmd.append(url)

        logger.info("Launching native browser with URL: %s", url)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.browser_pid = proc.pid
        await asyncio.sleep(NATIVE_STARTUP_WAIT_SECONDS)

        self.window_id = await self._find_window_id()
        if not self.window_id:
            raise RuntimeError(
                f"Could not find browser window for PID {self.browser_pid} "
                f"or title '{self.window_title}'"
            )
        await self._ensure_window_ready()
        await self._refresh_window_id()

    async def stop(self) -> None:
        """Close the browser window."""
        if self.window_id and await self._window_exists(self.window_id):
            try:
                await self._run_xdotool(["windowclose", self.window_id], check=False)
            except Exception as e:
                logger.warning("Failed to close native window: %s", e)
        self.browser_pid = None
        self.window_id = None
        self._started = False

    async def _find_window_id(self) -> Optional[str]:
        """Find browser window ID, preferring PID match, active browser, then title/class."""
        # Strategy 1: search by PID.
        if self.browser_pid:
            try:
                result = await self._run_xdotool(
                    ["search", "--pid", str(self.browser_pid), "--onlyvisible"],
                    retries=0,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip().split("\n")[-1]
            except Exception as e:
                logger.warning("PID window search failed: %s", e)

        # Strategy 2: active browser window. Chrome may hand off to a child process
        # or reuse an existing instance, so PID search can miss the visible window.
        try:
            result = await self._run_xdotool(
                ["getactivewindow"],
                retries=0,
                check=False,
            )
            active_id = result.stdout.strip() if result.returncode == 0 else ""
            if active_id:
                class_result = await self._run_xdotool(
                    ["getwindowclassname", active_id],
                    retries=0,
                    check=False,
                )
                window_class = class_result.stdout.strip().lower()
                candidates = [c.lower() for c in self._browser_class_candidates()]
                if class_result.returncode == 0 and any(c in window_class for c in candidates):
                    return active_id
        except Exception as e:
            logger.warning("Active window search failed: %s", e)

        # Strategy 3: search by window title.
        try:
            result = await self._run_xdotool(
                ["search", "--onlyvisible", "--name", self.window_title],
                retries=0,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split("\n")[-1]
        except Exception as e:
            logger.warning("Title window search failed: %s", e)

        # Strategy 4: fallback to browser window class.
        for window_class in self._browser_class_candidates():
            try:
                result = await self._run_xdotool(
                    ["search", "--onlyvisible", "--class", window_class],
                    retries=0,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip().split("\n")[-1]
            except Exception as e:
                logger.warning("Class window search failed for %s: %s", window_class, e)

        return None

    async def _refresh_window_id(self) -> None:
        """Refresh window_id if Chrome replaced or moved focus to a different window."""
        new_id = await self._find_window_id()
        if new_id and new_id != self.window_id:
            logger.info("Native browser window id changed: %s -> %s", self.window_id, new_id)
            self.window_id = new_id

    async def _recover_for_retry(self, operation: str, error: Exception) -> None:
        """Refresh native window state before retrying a failed operation once."""
        logger.warning(
            "Native %s failed; refreshing window before retry "
            "(window_id=%s, error=%s)",
            operation,
            self.window_id,
            error,
        )
        await self._ensure_window_ready()
        await self._refresh_window_id()

    async def _window_exists(self, window_id: str) -> bool:
        """Check if a window ID is still valid."""
        try:
            result = await self._run_xdotool(
                ["getwindowname", window_id],
                timeout=2,
                retries=0,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    async def _window_geometry(self, window_id: str) -> dict:
        """Return window geometry as dict with X, Y, WIDTH, HEIGHT."""
        try:
            result = await self._run_xdotool(
                ["getwindowgeometry", "--shell", window_id],
                retries=0,
                check=False,
            )
        except Exception:
            return {}

        geo_vars = {}
        if result.returncode != 0:
            return geo_vars
        for line in result.stdout.strip().split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                try:
                    geo_vars[k.strip()] = int(v.strip())
                except ValueError:
                    geo_vars[k.strip()] = v.strip()
        return geo_vars

    async def _ensure_window_ready(self) -> None:
        """Activate, raise, and resize the browser window.

        Also verifies the window actually became the foreground window before
        returning, retrying a few times if not. Without this, a stacking race
        (or another app reasserting itself as always-on-top) can leave a
        different window on top — window-ID screenshot tools then silently
        capture that other window's content instead of (or bled together
        with) the browser's. Real case, 2026-07-26: a terminal panel showed
        up inside a Kmart product-page screenshot taken via `import -window`
        right after this method returned.
        """
        if not self.window_id or not await self._window_exists(self.window_id):
            new_id = await self._find_window_id()
            if not new_id:
                raise RuntimeError(
                    f"Native browser window lost (pid={self.browser_pid})"
                )
            self.window_id = new_id

        for attempt in range(3):
            await self._run_xdotool(["windowactivate", self.window_id], check=False)
            await asyncio.sleep(0.2)
            await self._run_xdotool(["windowraise", self.window_id], check=False)
            await self._run_xdotool(
                ["windowsize", self.window_id, "1280", "800"],
                check=False,
            )
            await asyncio.sleep(NATIVE_WINDOW_READY_DELAY)

            active = await self._run_xdotool(
                ["getactivewindow"], retries=0, check=False
            )
            active_id = active.stdout.strip() if active.returncode == 0 else ""
            if active_id == self.window_id:
                return
            logger.warning(
                "Native window %s did not become foreground after raise "
                "(active=%s), retry %d/3",
                self.window_id,
                active_id,
                attempt + 1,
            )
        logger.error(
            "Native window %s never became the foreground window — "
            "screenshots/input may hit the wrong window",
            self.window_id,
        )

    async def navigate(self, url: str) -> dict:
        """Navigate to URL by launching a fresh browser window with the URL.

        The previous xdotool address-bar approach (ctrl+l + type + Return) was
        unreliable and often left Chrome on the "New Tab" page. Launching Chrome
        with the URL directly is simpler and more robust for anti-bot fallback.
        """
        if not self._started:
            raise RuntimeError("Browser not started")

        await self._launch_with_url(url)

        # Wait for page load — poll title until it stabilises or timeout.
        old_title = ""
        for _ in range(NAVIGATE_LOAD_MAX_POLLS):
            try:
                result = await self._run_xdotool(
                    ["getwindowname", self.window_id],
                    timeout=XDOTOOL_DEFAULT_TIMEOUT,
                    retries=XDOTOOL_DEFAULT_RETRIES,
                    check=False,
                )
                new_title = result.stdout.strip() if result.returncode == 0 else ""
            except Exception:
                await self._ensure_window_ready()
                new_title = ""
            if new_title and new_title != old_title and "Loading" not in new_title:
                break
            old_title = new_title
            await asyncio.sleep(NAVIGATE_LOAD_POLL_INTERVAL)
        await asyncio.sleep(NAVIGATE_LOAD_POLL_INTERVAL)  # extra settle time

        title = await self.get_title()
        return {
            "url": url,
            "title": title,
            "window_id": self.window_id,
            "method": "native_os",
        }

    async def go_back(self) -> dict:
        if not self._started:
            raise RuntimeError("Browser not started")
        await self._ensure_window_ready()
        await self._run_xdotool(["key", "--window", self.window_id, "alt+Left"], check=False)
        await asyncio.sleep(1)
        return {"url": await self.get_url(), "title": await self.get_title(), "window_id": self.window_id}

    async def go_forward(self) -> dict:
        if not self._started:
            raise RuntimeError("Browser not started")
        await self._ensure_window_ready()
        await self._run_xdotool(["key", "--window", self.window_id, "alt+Right"], check=False)
        await asyncio.sleep(1)
        return {"url": await self.get_url(), "title": await self.get_title(), "window_id": self.window_id}

    async def reload(self) -> dict:
        if not self._started:
            raise RuntimeError("Browser not started")
        await self._ensure_window_ready()
        await self._run_xdotool(["key", "--window", self.window_id, "F5"], check=False)
        await asyncio.sleep(1)
        return {"url": await self.get_url(), "title": await self.get_title(), "window_id": self.window_id}

    async def _screenshot_once(self) -> bytes:
        """Capture the current native window once using available tools."""
        geo = await self._window_geometry(self.window_id)
        x = geo.get("X", 0)
        y = geo.get("Y", 0)
        w = geo.get("WIDTH", 1280)
        h = geo.get("HEIGHT", 800)

        methods = []

        # Method 1: import -window (ImageMagick)
        if shutil.which("import"):
            methods.append(("import_window", self._screenshot_import_window))

        # Method 2: xwd + convert
        if shutil.which("xwd") and shutil.which("convert"):
            methods.append(("xwd_convert", self._screenshot_xwd_convert))

        # Method 3: full-screen capture + crop
        if shutil.which("import"):
            methods.append(
                (
                    "import_screen_crop",
                    lambda: self._screenshot_import_screen_crop(x, y, w, h),
                )
            )

        last_error = None
        for method_name, method_fn in methods:
            try:
                data = await asyncio.to_thread(method_fn)
                if data:
                    logger.info("Screenshot succeeded using method: %s", method_name)
                    return data
            except Exception as e:
                last_error = e
                logger.warning("Screenshot method %s failed: %s", method_name, e)

        raise RuntimeError(f"Screenshot failed (last error: {last_error})")

    async def screenshot(self) -> bytes:
        """Take a screenshot using ImageMagick or fallback tools."""
        if not self._started:
            raise RuntimeError("Browser not started")
        await self._ensure_window_ready()

        try:
            return await self._screenshot_once()
        except Exception as first_error:
            try:
                await self._recover_for_retry("screenshot", first_error)
                return await self._screenshot_once()
            except Exception as retry_error:
                raise RuntimeError(
                    "Native screenshot failed after retry "
                    f"(window_id={self.window_id}, original={first_error}, "
                    f"retry={retry_error})"
                ) from retry_error

    def _run_capture_subprocess(
        self,
        cmd: list,
        input_data: bytes | None = None,
        timeout: float = SCREENSHOT_TIMEOUT,
    ) -> bytes:
        """Run a subprocess and return captured stdout on success."""
        result = subprocess.run(
            cmd,
            input=input_data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
        stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"{' '.join(cmd)} failed: {stderr}")

    def _screenshot_import_window(self) -> bytes:
        return self._run_capture_subprocess(
            ["import", "-window", self.window_id, "png:-"]
        )

    def _screenshot_xwd_convert(self) -> bytes:
        xwd_data = self._run_capture_subprocess(
            ["xwd", "-id", self.window_id, "-silent"]
        )
        return self._run_capture_subprocess(
            ["convert", "xwd:-", "png:-"],
            input_data=xwd_data,
        )

    def _screenshot_import_screen_crop(self, x: int, y: int, w: int, h: int) -> bytes:
        screen_data = self._run_capture_subprocess(
            ["import", "-screen", "png:-"]
        )
        return self._run_capture_subprocess(
            ["convert", "png:-", "-crop", f"{w}x{h}+{x}+{y}", "png:-"],
            input_data=screen_data,
        )

    async def click(self, x: int, y: int) -> dict:
        """Click at the given coordinates."""
        if not self._started:
            raise RuntimeError("Browser not started")
        await self._ensure_window_ready()

        async def do_click():
            await self._run_xdotool(
                ["mousemove", "--window", self.window_id, str(x), str(y)]
            )
            await asyncio.sleep(0.15)
            await self._run_xdotool(["click", "--window", self.window_id, "1"])
            await asyncio.sleep(0.1)

        try:
            await do_click()
        except Exception as first_error:
            try:
                await self._recover_for_retry("click", first_error)
                await do_click()
            except Exception as retry_error:
                raise RuntimeError(
                    "Native click failed after retry "
                    f"(window_id={self.window_id}, x={x}, y={y}, "
                    f"original={first_error}, retry={retry_error})"
                ) from retry_error
        return {"x": x, "y": y, "window_id": self.window_id, "method": "native_os"}

    async def _ensure_english_input_method(self) -> None:
        """Force IBus to an English keyboard engine before typing.

        Best-effort: silently does nothing if `ibus` isn't installed, and
        only logs (never raises) on failure — a broken IME switch shouldn't
        block typing outright, since some environments may not use IBus at
        all.
        """
        if not NATIVE_FORCE_ENGLISH_IME or not shutil.which("ibus"):
            return
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["ibus", "engine", NATIVE_ENGLISH_IME_ENGINE],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception as e:
            logger.warning("Failed to switch IBus to English engine: %s", e)

    async def type_text(self, text: str) -> dict:
        """Type text into the focused element."""
        if not self._started:
            raise RuntimeError("Browser not started")
        await self._ensure_window_ready()
        await self._ensure_english_input_method()

        async def do_type():
            await self._run_xdotool(
                ["type", "--window", self.window_id, "--delay", "30", text],
                timeout=min(30, max(5, len(text) // 5)),
            )

        try:
            await do_type()
        except Exception as first_error:
            try:
                await self._recover_for_retry("type_text", first_error)
                await do_type()
            except Exception as retry_error:
                raise RuntimeError(
                    "Native type_text failed after retry "
                    f"(window_id={self.window_id}, text_length={len(text)}, "
                    f"original={first_error}, retry={retry_error})"
                ) from retry_error
        return {"text": text, "window_id": self.window_id, "method": "native_os"}

    async def scroll(self, direction: str = "down", amount: int = 3) -> dict:
        """Scroll the page by sending Up/Down keys."""
        if not self._started:
            raise RuntimeError("Browser not started")
        await self._ensure_window_ready()

        key = "Down" if direction == "down" else "Up"
        for _ in range(amount):
            await self._run_xdotool(["key", "--window", self.window_id, key])
            await asyncio.sleep(0.05)
        return {"direction": direction, "amount": amount}

    async def press_key(self, key: str) -> dict:
        """Press a keyboard keyboard key."""
        if not self._started:
            raise RuntimeError("Browser not started")
        await self._ensure_window_ready()

        await self._run_xdotool(["key", "--window", self.window_id, key])
        return {"key": key}

    async def clear_field(self) -> dict:
        """Native engine cannot reliably clear a focused field without CDP."""
        return {
            "error": "native engine does not support clear_field; switch to stealth engine",
            "cleared": False,
        }

    def _parse_xy_selector(self, selector: str) -> tuple[int, int] | None:
        """Parse 'x,y' coordinate selector used by native engine fallbacks."""
        import re
        match = re.match(r"^(\d+),(\d+)$", selector.strip())
        if match:
            return int(match.group(1)), int(match.group(2))
        return None

    async def select(self, selector: str, value: str) -> dict:
        """Native engine cannot select by value without CDP."""
        if not self._started:
            raise RuntimeError("Browser not started")
        return {
            "error": "native engine does not support select by value; switch to stealth engine",
            "selector": selector,
            "value": value,
        }

    async def check(self, selector: str) -> dict:
        """Check by coordinate if selector is 'x,y', otherwise unsupported."""
        if not self._started:
            raise RuntimeError("Browser not started")
        coords = self._parse_xy_selector(selector)
        if coords:
            x, y = coords
            await self.click(x, y)
            return {"selector": selector, "x": x, "y": y, "method": "native_os_click"}
        return {
            "error": "native engine check/uncheck requires 'x,y' coordinate selector",
            "selector": selector,
        }

    async def uncheck(self, selector: str) -> dict:
        """Uncheck by coordinate if selector is 'x,y', otherwise unsupported."""
        if not self._started:
            raise RuntimeError("Browser not started")
        coords = self._parse_xy_selector(selector)
        if coords:
            x, y = coords
            await self.click(x, y)
            return {"selector": selector, "x": x, "y": y, "method": "native_os_click"}
        return {
            "error": "native engine check/uncheck requires 'x,y' coordinate selector",
            "selector": selector,
        }

    async def get_cookies(self, domain: str = "") -> list:
        """Cookie access not supported in native engine."""
        return []

    async def set_cookie(self, name: str, value: str, domain: str, path: str = "/",
                         expires: float = -1, secure: bool = False,
                         http_only: bool = False) -> dict:
        """Cookie management not supported in native engine."""
        return {"error": "ENGINE_NOT_SUPPORTED", "message": "Cookie management requires stealth engine"}

    async def clear_cookies(self, domain: str = "") -> dict:
        """Cookie management not supported in native engine."""
        return {"error": "ENGINE_NOT_SUPPORTED", "message": "Cookie management requires stealth engine"}

    async def get_text(self, main_content_only: bool = False) -> dict:
        """Native engine cannot safely read page text without disruptive Ctrl+A."""
        if not self._started:
            raise RuntimeError("Browser not started")
        return {
            "text": "",
            "note": "Native engine does not support get_text. "
                    "Use screenshot or switch to stealth engine.",
        }

    async def get_title(self) -> str:
        """Return the current window title."""
        if not self._started:
            raise RuntimeError("Browser not started")
        try:
            result = await self._run_xdotool(
                ["getwindowname", self.window_id],
                timeout=XDOTOOL_DEFAULT_TIMEOUT,
                retries=0,
                check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    async def get_url(self) -> str:
        """Native engine cannot easily get URL without CDP."""
        if not self._started:
            raise RuntimeError("Browser not started")
        return ""
