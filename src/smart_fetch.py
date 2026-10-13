"""Smart fetch orchestration layer.

Provides a one-shot URL fetch that tries stealth/native Chrome engines first
and optionally falls back to the Claude Code CLI path when explicitly enabled.
"""
import asyncio
import base64
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import (
    CLAUDE_ENGINE_TIMEOUT_SECONDS,
    CLAUDE_ENGINE_MIN_OUTPUT_CHARS,
    CLAUDE_ENGINE_MAX_LENGTH,
    CLAUDE_ENGINE_PROMPT_TEMPLATE,
)
from .core.block_detection import detect_block, BlockSignal


def _serialize_block_signals(signals) -> List[dict]:
    """Normalize block signals from BlockSignal objects or dicts."""
    result = []
    for s in signals:
        if isinstance(s, BlockSignal):
            result.append({"name": s.name, "value": s.value, "confidence": s.confidence})
        elif isinstance(s, dict):
            result.append({
                "name": s.get("name", ""),
                "value": s.get("value", ""),
                "confidence": s.get("confidence", 0.0),
            })
    return result


@dataclass
class ClaudeEngineResult:
    """Result of a single Claude engine invocation."""

    ok: bool
    text: str = ""
    title: str = ""
    reason: Optional[str] = None
    raw: str = ""


@dataclass
class SmartFetchResult:
    """Final response returned by SmartFetcher.fetch()."""

    url: str
    final_url: str
    title: str
    text: str
    text_truncated: bool
    engine_used: str
    blocked: bool
    block_confidence: float
    block_signals: List[dict] = field(default_factory=list)
    claude_engine_attempted: bool = False
    claude_engine_succeeded: bool = False
    claude_engine_failure_reason: Optional[str] = None
    fallback_triggered: bool = False
    elapsed_ms: int = 0
    text_unavailable: bool = False
    text_unavailable_reason: Optional[str] = None
    screenshot_base64: Optional[str] = None
    screenshot_media_type: Optional[str] = None

    def as_dict(self) -> dict:
        """Serialize to the public API response shape."""
        result = {
            "url": self.url,
            "final_url": self.final_url,
            "title": self.title,
            "text": self.text,
            "text_truncated": self.text_truncated,
            "engine_used": self.engine_used,
            "blocked": self.blocked,
            "block_confidence": self.block_confidence,
            "block_signals": self.block_signals,
            "claude_engine_attempted": self.claude_engine_attempted,
            "claude_engine_succeeded": self.claude_engine_succeeded,
            "claude_engine_failure_reason": self.claude_engine_failure_reason,
            "fallback_triggered": self.fallback_triggered,
            "elapsed_ms": self.elapsed_ms,
            "text_unavailable": self.text_unavailable,
            "text_unavailable_reason": self.text_unavailable_reason,
        }
        if self.screenshot_base64 is not None:
            result["screenshot_base64"] = self.screenshot_base64
        if self.screenshot_media_type is not None:
            result["screenshot_media_type"] = self.screenshot_media_type
        return result


class SmartFetcher:
    """Orchestrate "stealth/native first, optional claude fallback" URL fetches."""

    def __init__(
        self,
        browser_client_factory: Callable[[], "BrowserClient"],  # noqa: F821
        logger,
        session_manager=None,
    ):
        self.browser_client_factory = browser_client_factory
        self.logger = logger
        self.session_manager = session_manager
        self._claude_bin = self._resolve_claude_bin()

    def _resolve_claude_bin(self) -> str:
        """Locate an executable claude binary and cache the path."""
        env_bin = os.environ.get("AI_BROWSER_CLAUDE_BIN", "")
        if env_bin:
            if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
                return env_bin
            # Explicitly configured but missing -> treat as unavailable.
            return ""

        for candidate in (
            shutil.which("claude"),
            os.path.expanduser("~/.local/bin/claude"),
            "/usr/local/bin/claude",
        ):
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return ""

    @staticmethod
    def _looks_like_refusal(text: str) -> bool:
        """Heuristic detection of common Claude refusal openings."""
        lower = text.lower()
        prefixes = (
            "i can't",
            "i'm unable to",
            "i am unable to",
            "error:",
            "unable to access",
        )
        return any(lower.startswith(p) for p in prefixes)

    @staticmethod
    def _guess_title(text: str) -> str:
        """Infer a title from the first non-empty line of extracted text."""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:80]
        return ""

    def _parse_claude_output(self, stdout: str, url: str) -> ClaudeEngineResult:
        """Validate Claude stdout and convert it to a ClaudeEngineResult."""
        text = stdout.strip()
        if text == "BLOCKED":
            return ClaudeEngineResult(ok=False, reason="claude_reported_blocked")
        if len(text) < CLAUDE_ENGINE_MIN_OUTPUT_CHARS:
            return ClaudeEngineResult(
                ok=False, reason="output_too_short", raw=text
            )
        if self._looks_like_refusal(text):
            return ClaudeEngineResult(ok=False, reason="claude_refused", raw=text)
        block_result = detect_block(title="", body_text=text[:2000], url=url)
        if block_result.blocked:
            return ClaudeEngineResult(
                ok=False, reason="content_blocked", raw=text
            )
        return ClaudeEngineResult(
            ok=True, text=text, title=self._guess_title(text)
        )

    async def _call_claude(
        self, url: str, timeout: int = CLAUDE_ENGINE_TIMEOUT_SECONDS
    ) -> ClaudeEngineResult:
        """Shell out to the Claude Code CLI and parse the result."""
        if not self._claude_bin:
            return ClaudeEngineResult(ok=False, reason="bin_not_found")

        prompt = CLAUDE_ENGINE_PROMPT_TEMPLATE.format(
            url=url, max_length=CLAUDE_ENGINE_MAX_LENGTH
        )
        cmd = [
            self._claude_bin,
            "-p",
            prompt,
            "--dangerously-skip-permissions",
        ]

        self.logger.info("smart_fetch claude invocation: %s", cmd[0])
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                self.logger.warning("claude subprocess timed out after %ss", timeout)
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                self._close_pipes(proc)
                return ClaudeEngineResult(ok=False, reason="timeout")

            self._close_pipes(proc)
            await proc.wait()

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                self.logger.warning(
                    "claude subprocess exited %d: %s",
                    proc.returncode,
                    stderr[:500],
                )
                return ClaudeEngineResult(
                    ok=False,
                    reason="nonzero_exit",
                    raw=stdout,
                )

            return self._parse_claude_output(stdout, url)
        except Exception as exc:  # pragma: no cover - defensive fallback
            self.logger.warning("claude invocation failed: %s", exc)
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return ClaudeEngineResult(ok=False, reason="nonzero_exit")

    @staticmethod
    def _close_pipes(proc):
        """Close subprocess stdout/stderr pipes to avoid resource leaks."""
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass
        if proc.stderr is not None:
            try:
                proc.stderr.close()
            except Exception:
                pass

    async def _fallback_navigate_and_text(
        self,
        url: str,
        max_length: int,
        session_id: Optional[str],
        fallback_screenshot: bool = False,
    ) -> SmartFetchResult:
        """Use stealth/native via EngineManager (server) or BrowserClient (tests)."""
        start = time.monotonic()

        if self.session_manager is not None:
            session = await self.session_manager.get_or_create_session(
                session_id or "default"
            )
            engine_mgr = session.engine_manager
            nav_result = await engine_mgr.navigate_with_fallback(url)
            engine = engine_mgr.get_engine()
            text_result = await engine.get_text()
        else:
            client = self.browser_client_factory()
            try:
                nav_result = await client.navigate(url, session_id=session_id)
                text_result = await client.get_text(session_id=session_id)
                engine = client
            finally:
                await client.close()

        # Resolve nav_result if it's a coroutine (e.g., from an AsyncMock).
        if asyncio.iscoroutine(nav_result):
            nav_result = await nav_result
        if asyncio.iscoroutine(text_result):
            text_result = await text_result

        elapsed_ms = int((time.monotonic() - start) * 1000)

        engine_used = nav_result.get("engine_used", "stealth")
        blocked = bool(nav_result.get("blocked", False))
        text = ""
        text_truncated = False
        text_unavailable = False
        text_unavailable_reason = None

        if not blocked and isinstance(text_result, dict):
            text = text_result.get("text", "")
            if len(text) > max_length:
                text = text[:max_length]
                text_truncated = True

        # Native engine cannot extract text: be honest instead of silently
        # returning empty text with blocked=false.
        if not blocked and engine_used == "native" and not text:
            text_unavailable = True
            text_unavailable_reason = "native_engine_cannot_extract_text"

        screenshot_base64 = None
        screenshot_media_type = None
        if (
            fallback_screenshot
            and text_unavailable
            and engine_used == "native"
        ):
            screenshot_base64, screenshot_media_type = await self._try_screenshot(
                engine, client if self.session_manager is None else None
            )

        return SmartFetchResult(
            url=url,
            final_url=nav_result.get("url", url),
            title=nav_result.get("title", ""),
            text=text,
            text_truncated=text_truncated,
            engine_used=engine_used,
            blocked=blocked,
            block_confidence=nav_result.get("block_confidence", 0.0),
            block_signals=_serialize_block_signals(
                nav_result.get("block_signals", [])
            ),
            fallback_triggered=nav_result.get("fallback_triggered", False),
            elapsed_ms=elapsed_ms,
            text_unavailable=text_unavailable,
            text_unavailable_reason=text_unavailable_reason,
            screenshot_base64=screenshot_base64,
            screenshot_media_type=screenshot_media_type,
        )

    async def _try_screenshot(
        self,
        engine: Optional[object],
        client: Optional[object],
    ) -> tuple[Optional[str], Optional[str]]:
        """Attempt to capture a screenshot when text is unavailable.

        Tries the engine object first, then falls back to the HTTP client.
        Returns (base64_png, media_type) or (None, None) on failure.
        """
        data = None
        if engine is not None:
            try:
                if asyncio.iscoroutinefunction(engine.screenshot):
                    data = await engine.screenshot()
                else:
                    data = engine.screenshot()
                    if asyncio.iscoroutine(data):
                        data = await data
            except Exception as exc:
                self.logger.warning("fallback screenshot via engine failed: %s", exc)
        if data is None and client is not None:
            try:
                data = await client.screenshot()
            except Exception as exc:
                self.logger.warning("fallback screenshot via client failed: %s", exc)
        if data:
            return base64.b64encode(data).decode("utf-8"), "image/png"
        return None, None

    async def fetch(
        self,
        url: str,
        max_length: int = CLAUDE_ENGINE_MAX_LENGTH,
        timeout: Optional[int] = None,
        session_id: Optional[str] = None,
        fallback_screenshot: bool = False,
    ) -> dict:
        """One-shot fetch with automatic engine selection.

        Returns a dict suitable for direct JSON serialization.
        """
        start = time.monotonic()
        timeout = timeout or CLAUDE_ENGINE_TIMEOUT_SECONDS

        # Internal test hook: set AI_BROWSER_TEST_FORCE_DISABLE_CLAUDE=1 to
        # bypass the Claude fast path without exposing the switch in the HTTP
        # request schema.
        force_disable_claude = (
            os.environ.get("AI_BROWSER_TEST_FORCE_DISABLE_CLAUDE", "").lower()
            in ("1", "true", "yes")
        )

        # --- Optional fast path: Claude engine (opt-in only) ---
        from .config import SMART_FETCH_CLAUDE_FIRST
        claude_first = SMART_FETCH_CLAUDE_FIRST and not force_disable_claude
        claude_attempted = bool(self._claude_bin) and claude_first
        claude_result: Optional[ClaudeEngineResult] = None
        if claude_attempted:
            claude_result = await self._call_claude(url, timeout=timeout)
            if claude_result and claude_result.ok:
                text = claude_result.text
                text_truncated = False
                if len(text) > max_length:
                    text = text[:max_length]
                    text_truncated = True
                elapsed_ms = int((time.monotonic() - start) * 1000)
                result = SmartFetchResult(
                    url=url,
                    final_url=url,
                    title=claude_result.title,
                    text=text,
                    text_truncated=text_truncated,
                    engine_used="claude",
                    blocked=False,
                    block_confidence=0.0,
                    block_signals=[],
                    claude_engine_attempted=True,
                    claude_engine_succeeded=True,
                    claude_engine_failure_reason=None,
                    fallback_triggered=False,
                    elapsed_ms=elapsed_ms,
                )
                self.logger.info(
                    "smart_fetch served by claude in %dms", elapsed_ms
                )
                return result.as_dict()

        # --- Fallback path: stealth/native ---
        fallback = await self._fallback_navigate_and_text(
            url, max_length, session_id, fallback_screenshot=fallback_screenshot
        )

        # Merge claude metadata into fallback result.
        fallback.claude_engine_attempted = claude_attempted
        fallback.claude_engine_succeeded = False
        fallback.claude_engine_failure_reason = (
            claude_result.reason if claude_result else None
        )
        fallback.elapsed_ms = int((time.monotonic() - start) * 1000)
        self.logger.info(
            "smart_fetch served by %s (claude_attempted=%s, reason=%s) in %dms",
            fallback.engine_used,
            claude_attempted,
            fallback.claude_engine_failure_reason,
            fallback.elapsed_ms,
        )
        return fallback.as_dict()
