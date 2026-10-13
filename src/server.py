"""FastAPI HTTP server — universal interface for all AI agents."""
from contextlib import asynccontextmanager
import asyncio
import base64
import json
import time
from urllib.parse import quote_plus, urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .config import HOST, PORT, BROWSER_PATH, HEADLESS, VERSION
from .client import BrowserClient
from .smart_fetch import SmartFetcher

# URL scheme whitelist — prevent SSRF and javascript: protocol injection.
_ALLOWED_URL_SCHEMES = {"http", "https"}


def _validate_url(url: str) -> None:
    """Validate that a URL uses an allowed scheme (http/https)."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        raise HTTPException(
            status_code=400,
            detail=f"URL scheme must be one of {sorted(_ALLOWED_URL_SCHEMES)}, got: {parsed.scheme or '(none)'}",
        )


def _sanitize_header_value(value: str, limit: int = 3000) -> str:
    """Return a compact latin-1 safe value for diagnostic HTTP headers."""
    compact = value.replace("\r", " ").replace("\n", " | ")[:limit]
    return compact.encode("latin-1", "ignore").decode("latin-1")


def _normalize_action_result(result: dict) -> dict:
    """Normalize navigate/search/action results to look-bundle field names.

    Converts legacy top-level fields (engine_used, blocked, block_confidence,
    block_signals) into the look-bundle format (engine, block object) so that
    callers see a single consistent schema when return_look=true.
    """
    normalized = dict(result)
    engine_used = normalized.pop("engine_used", None)
    if engine_used is not None and "engine" not in normalized:
        normalized["engine"] = engine_used

    if "block" not in normalized:
        block = {
            "blocked": normalized.pop("blocked", False),
            "confidence": normalized.pop("block_confidence", 0.0),
            "signals": normalized.pop("block_signals", []),
        }
        normalized["block"] = block
    else:
        # Remove legacy top-level keys if a modern block object already exists.
        normalized.pop("blocked", None)
        normalized.pop("block_confidence", None)
        normalized.pop("block_signals", None)
    return normalized


from .core.session import (
    SessionManager,
    InvalidSessionIdError,
    InvalidTabIdError,
    validate_session_id,
    validate_tab_id,
)
from .core.process_cleanup import has_chrome_processes_for_profile_dir
from .core.stealth_cdp import StealthEngine
from .screenshot import annotate_screenshot, extract_elements_js, format_elements_text
from .look import _capture_look, _settle, _thin_text_link_fallback
from .log_config import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)

# Session manager — single source of browser state.
session_mgr = SessionManager(browser_path=BROWSER_PATH, headless=HEADLESS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    # Clean up orphan profile directories from crashed processes
    removed = await session_mgr.cleanup_orphan_profiles()
    if removed:
        logger.info("Cleaned up %d orphan profile directories", removed)

    default_session = await session_mgr.get_default_session()
    engine_name = default_session.engine_manager.current_name
    logger.info("Active engine: %s (browser: %s)", engine_name, BROWSER_PATH)

    # Start the background idle-session reaper
    await session_mgr.start_reaper()

    # Initialize the smart fetch orchestrator (claude -> stealth -> native).
    # It uses the global session_mgr directly to avoid HTTP self-calls.
    app.state.smart_fetcher = SmartFetcher(
        browser_client_factory=lambda: BrowserClient(),
        logger=logger,
        session_manager=session_mgr,
    )
    logger.info("SmartFetcher initialized (claude=%s)", app.state.smart_fetcher._claude_bin or "unavailable")

    yield

    # Stop reaper and all sessions on shutdown
    await session_mgr.stop_reaper()
    await session_mgr.stop_all()


app = FastAPI(title="AI Browser", version=VERSION, lifespan=lifespan)


# --- Request models ---

class NavigateRequest(BaseModel):
    url: str
    return_look: bool = True
    main_content_only: bool = False


class SearchRequest(BaseModel):
    query: str
    engine: str = "google"
    main_content_only: bool = False


# --- Search engine URL templates ---

SEARCH_ENGINE_URLS: dict[str, str] = {
    "google": "https://www.google.com/search?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
}
DEFAULT_SEARCH_ENGINE = "google"

# Domains to exclude when falling back to elements' hrefs for thin search-result
# text (see _thin_text_link_fallback in look.py) — these are the engine's own
# chrome/navigation links ("Go to Google Home", "Sign in"), not real results.
_SEARCH_ENGINE_CHROME_DOMAINS: dict[str, set[str]] = {
    "google": {"google.com", "gstatic.com", "googleusercontent.com"},
    "bing": {"bing.com", "microsoft.com", "live.com"},
    "duckduckgo": {"duckduckgo.com"},
}


def _build_search_url(engine: str, query: str) -> str:
    """Build a search results URL for the given engine and query."""
    template = SEARCH_ENGINE_URLS.get(engine, SEARCH_ENGINE_URLS[DEFAULT_SEARCH_ENGINE])
    return template.format(q=quote_plus(query))


class ClickRequest(BaseModel):
    x: int
    y: int
    return_look: bool = True


class TypeRequest(BaseModel):
    text: str
    clear: bool = False


class ClearRequest(BaseModel):
    pass


class ScrollRequest(BaseModel):
    direction: str = "down"
    amount: int = 3
    return_look: bool = True


class KeyRequest(BaseModel):
    key: str


class SelectRequest(BaseModel):
    selector: str
    value: str


class CheckboxRequest(BaseModel):
    selector: str


class EngineRequest(BaseModel):
    engine: str  # "stealth" | "native" | "auto"


class CreateSessionRequest(BaseModel):
    session_id: str | None = None


class CreateTabRequest(BaseModel):
    tab_id: str | None = None


class ExecuteJsRequest(BaseModel):
    script: str


class WaitForElementRequest(BaseModel):
    text: str | None = None
    selector: str | None = None
    timeout: int = 10


class FindElementRequest(BaseModel):
    text: str
    element_type: str = "any"


class SmartFetchRequest(BaseModel):
    url: str
    max_length: int = 20000
    timeout: int | None = None
    session_id: str | None = "default"
    fallback_screenshot: bool = False


# --- Helpers ---

async def _get_session_engine(session_id: str = "default") -> "EngineManager":
    """Get the active engine for a session."""
    session = await _get_session(session_id)
    return session.engine_manager


# --- Endpoints ---

@app.get("/api/health")
async def health():
    default = session_mgr.sessions.get(SessionManager.DEFAULT_SESSION_ID)
    if default:
        engine_mgr = default.engine_manager
        stealth_active = engine_mgr.stealth is not None
        native_active = engine_mgr.native is not None
        # Verify the browser process is actually alive, not just that an
        # engine object exists.  The engine object can outlive its Chrome
        # process (e.g. after an unclean kill or nodriver stop() leak).
        browser_alive = has_chrome_processes_for_profile_dir(default.profile_dir)
        if (stealth_active or native_active) and not browser_alive:
            logger.warning(
                "Health check detected dead browser for default session "
                "(engine object exists but no Chrome process for %s); "
                "attempting recovery",
                default.profile_dir,
            )
            try:
                default = await session_mgr.recover_default_session()
                engine_mgr = default.engine_manager
                stealth_active = engine_mgr.stealth is not None
                native_active = engine_mgr.native is not None
                browser_alive = has_chrome_processes_for_profile_dir(default.profile_dir)
            except Exception as exc:
                logger.error("Failed to recreate default session: %s", exc)
                return {
                    "status": "degraded",
                    "detail": "default browser process missing and recovery failed",
                    "engine": engine_mgr.current_name if engine_mgr else None,
                    "stealth_active": stealth_active,
                    "native_active": native_active,
                    "browser_alive": False,
                    "sessions": len(session_mgr.sessions),
                }
        return {
            "status": "ok" if browser_alive else "degraded",
            "engine": engine_mgr.current_name,
            "stealth_active": stealth_active,
            "native_active": native_active,
            "browser_alive": browser_alive,
            "sessions": len(session_mgr.sessions),
        }
    return {"status": "ok", "sessions": len(session_mgr.sessions)}


@app.post("/api/engine")
async def set_engine(
    req: EngineRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Switch the active engine for a session."""
    if req.engine not in ("stealth", "native", "auto"):
        raise HTTPException(
            status_code=400, detail="Engine must be: stealth, native, or auto"
        )
    engine_mgr = await _get_session_engine(session_id)
    engine_mgr.current_name = req.engine
    if req.engine == "native":
        await engine_mgr._ensure_native()
    return {"engine": engine_mgr.current_name, "session_id": session_id}


@app.post("/api/start")
async def start_engine(
    req: EngineRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Start a specific engine for a session."""
    engine_mgr = await _get_session_engine(session_id)
    if req.engine == "stealth":
        await engine_mgr.ensure_started()
        return {"status": "stealth started", "session_id": session_id}
    elif req.engine == "native":
        await engine_mgr._ensure_native()
        return {"status": "native started", "session_id": session_id}
    raise HTTPException(status_code=400, detail="Unknown engine")


@app.post("/api/navigate")
async def navigate(
    req: NavigateRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Navigate with automatic engine fallback on anti-bot detection."""
    _validate_url(req.url)
    session = await _get_session(session_id)
    result = await session.engine_manager.navigate_with_fallback(req.url)
    result.pop("html", None)  # internal-only (block detection); use /api/text for content
    result["session_id"] = session_id
    if req.return_look:
        extra_signals = await _settle(session.engine_manager.get_engine(), action="navigate")
        look_bundle = await _capture_look(
            session, max_length=20000, main_content_only=req.main_content_only
        )
        if extra_signals:
            look_bundle["block"]["signals"].extend(extra_signals)
            look_bundle["block"]["confidence"] = min(
                1.0,
                look_bundle["block"]["confidence"]
                + sum(s.get("confidence", 0.0) for s in extra_signals),
            )
        return {**_normalize_action_result(result), **look_bundle}
    return result


@app.post("/api/search")
async def search(
    req: SearchRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Search the requested engine and return the results page as a look bundle."""
    search_url = _build_search_url(req.engine, req.query)
    _validate_url(search_url)
    # Report the engine that was actually used (fall back to Google for unknown names).
    actual_engine = req.engine if req.engine in SEARCH_ENGINE_URLS else DEFAULT_SEARCH_ENGINE
    session = await _get_session(session_id)
    result = await session.engine_manager.navigate_with_fallback(search_url)
    result.pop("html", None)
    result["session_id"] = session_id
    result["search_url"] = search_url
    result["search_engine"] = actual_engine
    extra_signals = await _settle(session.engine_manager.get_engine(), action="navigate")
    look_bundle = await _capture_look(
        session, max_length=20000, main_content_only=req.main_content_only
    )
    if extra_signals:
        look_bundle["block"]["signals"].extend(extra_signals)
        look_bundle["block"]["confidence"] = min(
            1.0,
            look_bundle["block"]["confidence"]
            + sum(s.get("confidence", 0.0) for s in extra_signals),
        )
    # 2026-07-28: live re-test after the Codex review fix (host-suffix match
    # instead of substring) turned up a real gap — Google's regional domains
    # (google.com.au, google.co.uk, ...) don't end with ".google.com", so a
    # static "google.com" entry alone misses their own chrome links (saw a
    # real "Google apps" link to google.com.au leak through). Add the actual
    # navigated hostname so regional TLDs are covered without a hardcoded list.
    exclude_domains = set(_SEARCH_ENGINE_CHROME_DOMAINS.get(actual_engine, {actual_engine}))
    navigated_host = urlparse(look_bundle.get("url", "")).hostname
    if navigated_host:
        exclude_domains.add(navigated_host.lower())
    look_bundle["text"] = _thin_text_link_fallback(
        look_bundle["text"], look_bundle.get("elements", []), exclude_domains, max_length=20000
    )
    return {**_normalize_action_result(result), **look_bundle}


@app.post("/api/smart_fetch")
async def smart_fetch_endpoint(payload: SmartFetchRequest):
    """One-shot URL fetch with automatic claude -> stealth -> native fallback."""
    _validate_url(payload.url)
    return await app.state.smart_fetcher.fetch(
        url=payload.url,
        max_length=payload.max_length,
        timeout=payload.timeout,
        session_id=payload.session_id,
        fallback_screenshot=payload.fallback_screenshot,
    )


@app.post("/api/back")
async def go_back(
    session_id: str = Query("default", description="Session ID"),
    return_look: bool = Query(True, description="Return a look bundle after the action"),
):
    """Go back in browser history."""
    session = await _get_session(session_id)
    result = await session.engine_manager.go_back()
    result["session_id"] = session_id
    if return_look:
        extra_signals = await _settle(session.engine_manager.get_engine(), action="go_back")
        look_bundle = await _capture_look(session, max_length=20000)
        if extra_signals:
            look_bundle["block"]["signals"].extend(extra_signals)
            look_bundle["block"]["confidence"] = min(
                1.0,
                look_bundle["block"]["confidence"]
                + sum(s.get("confidence", 0.0) for s in extra_signals),
            )
        return {**_normalize_action_result(result), **look_bundle}
    return result


@app.post("/api/forward")
async def go_forward(
    session_id: str = Query("default", description="Session ID"),
    return_look: bool = Query(True, description="Return a look bundle after the action"),
):
    """Go forward in browser history."""
    session = await _get_session(session_id)
    result = await session.engine_manager.go_forward()
    result["session_id"] = session_id
    if return_look:
        extra_signals = await _settle(session.engine_manager.get_engine(), action="go_forward")
        look_bundle = await _capture_look(session, max_length=20000)
        if extra_signals:
            look_bundle["block"]["signals"].extend(extra_signals)
            look_bundle["block"]["confidence"] = min(
                1.0,
                look_bundle["block"]["confidence"]
                + sum(s.get("confidence", 0.0) for s in extra_signals),
            )
        return {**_normalize_action_result(result), **look_bundle}
    return result


@app.post("/api/reload")
async def reload(
    session_id: str = Query("default", description="Session ID"),
    return_look: bool = Query(True, description="Return a look bundle after the action"),
):
    """Reload the current page."""
    session = await _get_session(session_id)
    result = await session.engine_manager.reload()
    result["session_id"] = session_id
    if return_look:
        extra_signals = await _settle(session.engine_manager.get_engine(), action="reload")
        look_bundle = await _capture_look(session, max_length=20000)
        if extra_signals:
            look_bundle["block"]["signals"].extend(extra_signals)
            look_bundle["block"]["confidence"] = min(
                1.0,
                look_bundle["block"]["confidence"]
                + sum(s.get("confidence", 0.0) for s in extra_signals),
            )
        return {**_normalize_action_result(result), **look_bundle}
    return result


@app.get("/api/screenshot")
async def screenshot(
    format: str = "png",
    session_id: str = Query("default", description="Session ID"),
):
    """Get screenshot as PNG image or base64 JSON."""
    session = await _get_existing_session(session_id)
    engine = session.engine_manager.get_engine()
    data = await engine.screenshot()
    if format == "base64":
        return {"image": base64.b64encode(data).decode("utf-8")}
    return Response(content=data, media_type="image/png")


@app.get("/api/look")
async def look(
    session_id: str = Query("default", description="Session ID"),
    max_length: int = Query(20000, description="Max chars of page text"),
):
    """Look at the current page: annotated screenshot + text + elements in one call."""
    session = await _get_existing_session(session_id)
    bundle = await _capture_look(session, max_length=max_length)
    return bundle


@app.get("/api/screenshot_annotated")
async def screenshot_annotated(
    format: str = "png",
    session_id: str = Query("default", description="Session ID"),
):
    """Get screenshot with interactive elements annotated with coordinate labels."""
    session = await _get_existing_session(session_id)
    engine_mgr = session.engine_manager
    engine = engine_mgr.get_engine()
    raw = await engine.screenshot()

    # Extract elements from the page (only works with stealth CDP engine)
    elements = []
    if isinstance(engine, StealthEngine) and engine.tab:
        try:
            raw_js = extract_elements_js().strip()
            js_code = "JSON.stringify(" + raw_js + ")"
            result = await engine.tab.evaluate(js_code, return_by_value=True)
            if isinstance(result, tuple):
                result = result[0]
            if isinstance(result, str):
                elements = json.loads(result)
            elif isinstance(result, list):
                elements = result
            logger.info("screenshot_annotated found %d elements", len(elements))
        except Exception as e:
            logger.warning("screenshot_annotated element extraction failed: %s", e)

    # Phase 6 Task 3: expose engine state so AI agents know when element
    # detection is unavailable (native engine cannot inject JS).
    engine_name = "stealth" if isinstance(engine, StealthEngine) else "native"
    elements_note = ""
    if engine_name == "native":
        elements_note = "Element detection requires the stealth (CDP) engine"

    # Annotate the screenshot
    annotated = annotate_screenshot(raw, elements)

    if format == "base64":
        return {
            "image": base64.b64encode(annotated).decode("utf-8"),
            "elements": elements,
            "elements_text": format_elements_text(elements),
            "engine": engine_name,
            "elements_note": elements_note,
        }

    # HTTP headers must be single-line and latin-1 encodable.
    safe_elements_text = _sanitize_header_value(format_elements_text(elements))
    safe_elements_note = _sanitize_header_value(elements_note)

    headers = {
        "X-Engine": engine_name,
        "X-Elements-Text": safe_elements_text,
        "X-Element-Count": str(len(elements)),
    }
    if safe_elements_note:
        headers["X-Elements-Note"] = safe_elements_note

    return Response(
        content=annotated,
        media_type="image/png",
        headers=headers,
    )


@app.post("/api/click")
async def click(
    req: ClickRequest,
    session_id: str = Query("default", description="Session ID"),
):
    if req.x < 0 or req.y < 0:
        raise HTTPException(
            status_code=400,
            detail=f"Click coordinates must be non-negative, got ({req.x}, {req.y})",
        )
    session = await _get_session(session_id)
    engine = session.engine_manager.get_engine()
    pre_click_url = ""
    if isinstance(engine, StealthEngine):
        try:
            pre_click_url = await engine.get_url()
        except Exception:
            pre_click_url = ""
    # 2026-07-23: catch imprecise-coordinate / wrong-element clicks (two real
    # feedback cases, same underlying problem — a click that returns 200 but
    # never navigates, with no way to tell why):
    #   1. click(73, 782) landed nowhere near any element at all.
    #   2. click(102, 318) landed on a real <button> (Google's "NASA Science
    #      (.gov) +6" sitelinks chip, which just expands a dropdown) that
    #      sits stacked almost exactly on top of the actual <a href> result
    #      link. The first version of this check only asked "is there SOME
    #      interactive element here" and both button and link count as
    #      "interactive", so it stayed silent — but a button with no href
    #      will never navigate, which is the same practical failure as
    #      missing the element entirely. Fixed by walking the FULL z-order
    #      stack at that point (elementsFromPoint, plural) instead of just
    #      the topmost hit, so a same-position <a href> underneath a
    #      non-navigating control can actually be found and suggested.
    click_target_warning = None
    if isinstance(engine, StealthEngine):
        try:
            target_info = await engine._evaluate_tolerant(
                f"""(() => {{
                    const stack = document.elementsFromPoint({req.x}, {req.y});
                    if (!stack.length) return {{kind: 'nothing'}};
                    const top = stack[0];
                    const topInteractive = top.closest('a,button,input,select,textarea,[role="button"],[role="link"],[onclick]');
                    if (topInteractive && topInteractive.tagName.toLowerCase() === 'a' && topInteractive.href) {{
                        return {{kind: 'ok'}};
                    }}
                    // Topmost hit isn't a navigating link — scan the rest of the
                    // z-stack at this same point for a real <a href> underneath
                    // (the Google sitelinks-chip-over-link pattern).
                    for (const el of stack) {{
                        const a = el.closest('a[href]');
                        if (a && a.href) {{
                            return {{
                                kind: 'covered',
                                blocker: topInteractive ? topInteractive.tagName.toLowerCase() : top.tagName.toLowerCase(),
                                href: a.href.slice(0, 150),
                            }};
                        }}
                    }}
                    if (topInteractive) {{
                        return {{kind: 'non-navigating', tag: topInteractive.tagName.toLowerCase()}};
                    }}
                    return {{kind: 'non-interactive', tag: top.tagName.toLowerCase()}};
                }})()"""
            )
        except Exception:
            target_info = None
        if isinstance(target_info, dict):
            kind = target_info.get("kind")
            if kind == "nothing":
                click_target_warning = (
                    f"No element found at ({req.x}, {req.y}) — click is likely a no-op. "
                    "Re-check click_x/click_y from a fresh search/look response instead of guessing coordinates."
                )
            elif kind == "non-interactive":
                click_target_warning = (
                    f"Element at ({req.x}, {req.y}) is {target_info.get('tag')}, not a link/button — "
                    "click is likely a no-op even though this call returns 200. "
                    "Re-check click_x/click_y from a fresh search/look response instead of guessing coordinates."
                )
            elif kind == "non-navigating":
                click_target_warning = (
                    f"Element at ({req.x}, {req.y}) is a <{target_info.get('tag')}> with no href — "
                    "click is likely a no-op (won't navigate) even though this call returns 200. "
                    "Re-check click_x/click_y from a fresh search/look response instead of guessing coordinates."
                )
            elif kind == "covered":
                click_target_warning = (
                    f"A <{target_info.get('blocker')}> (no href, likely a sitelinks/expand chip) is stacked on top "
                    f"of the real link at ({req.x}, {req.y}) — clicking here will trigger the {target_info.get('blocker')}, "
                    f"not navigate. The actual link underneath goes to: {target_info.get('href')}. "
                    "If you want that page, use `ai-browser navigate` with this URL directly instead of clicking here."
                )
    result = await engine.click(req.x, req.y)
    result["session_id"] = session_id
    if click_target_warning:
        result["click_target_warning"] = click_target_warning
    if req.return_look:
        extra_signals = await _settle(engine, action="click")
        look_bundle = await _capture_look(session, max_length=20000)
        if extra_signals:
            look_bundle["block"]["signals"].extend(extra_signals)
            look_bundle["block"]["confidence"] = min(
                1.0,
                look_bundle["block"]["confidence"]
                + sum(s.get("confidence", 0.0) for s in extra_signals),
            )
        # possible_new_tab detection (item 6): current tab URL unchanged after click
        # and the page is already complete, suggesting a new tab was opened instead.
        if isinstance(engine, StealthEngine) and pre_click_url:
            post_url = look_bundle.get("url", "")
            ready_state = ""
            try:
                ready_state = await engine._evaluate_tolerant("document.readyState")
            except Exception:
                pass
            # Only flag if both URLs are meaningful and unchanged.
            if (
                pre_click_url not in ("", "about:blank")
                and post_url not in ("", "about:blank")
                and pre_click_url == post_url
                and ready_state == "complete"
            ):
                look_bundle["possible_new_tab"] = True
        return {**result, **look_bundle}
    return result


@app.post("/api/type")
async def type_text(
    req: TypeRequest,
    session_id: str = Query("default", description="Session ID"),
):
    engine_mgr = await _get_session_engine(session_id)
    engine = engine_mgr.get_engine()
    if req.clear:
        clear_result = await engine.clear_field()
        if clear_result.get("error"):
            raise HTTPException(status_code=400, detail=clear_result["error"])
    result = await engine.type_text(req.text)
    return {**result, "cleared": req.clear, "session_id": session_id}


@app.post("/api/clear")
async def clear_field(
    req: ClearRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Clear the currently focused input field."""
    engine_mgr = await _get_session_engine(session_id)
    engine = engine_mgr.get_engine()
    result = await engine.clear_field()
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return {**result, "session_id": session_id}


@app.post("/api/scroll")
async def scroll(
    req: ScrollRequest,
    session_id: str = Query("default", description="Session ID"),
):
    session = await _get_session(session_id)
    engine = session.engine_manager.get_engine()
    result = await engine.scroll(req.direction, req.amount)
    result["session_id"] = session_id
    if req.return_look:
        extra_signals = await _settle(engine, action="scroll")
        look_bundle = await _capture_look(session, max_length=20000)
        if extra_signals:
            look_bundle["block"]["signals"].extend(extra_signals)
            look_bundle["block"]["confidence"] = min(
                1.0,
                look_bundle["block"]["confidence"]
                + sum(s.get("confidence", 0.0) for s in extra_signals),
            )
        return {**result, **look_bundle}
    return result


@app.post("/api/key")
async def press_key(
    req: KeyRequest,
    session_id: str = Query("default", description="Session ID"),
):
    engine_mgr = await _get_session_engine(session_id)
    engine = engine_mgr.get_engine()
    return await engine.press_key(req.key)


@app.post("/api/select")
async def select(
    req: SelectRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Select an option in a <select> element."""
    engine_mgr = await _get_session_engine(session_id)
    result = await engine_mgr.select(req.selector, req.value)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/check")
async def check(
    req: CheckboxRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Check a checkbox or radio button."""
    engine_mgr = await _get_session_engine(session_id)
    result = await engine_mgr.check(req.selector)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/uncheck")
async def uncheck(
    req: CheckboxRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Uncheck a checkbox."""
    engine_mgr = await _get_session_engine(session_id)
    result = await engine_mgr.uncheck(req.selector)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/api/text")
async def get_text(
    session_id: str = Query("default", description="Session ID"),
    main_content_only: bool = Query(
        False, description="Skip nav/header/footer boilerplate; prefer the main-content container"
    ),
):
    session = await _get_existing_session(session_id)
    engine = session.engine_manager.get_engine()
    return await engine.get_text(main_content_only=main_content_only)


@app.get("/api/title")
async def get_title(session_id: str = Query("default", description="Session ID")):
    session = await _get_existing_session(session_id)
    engine = session.engine_manager.get_engine()
    title = await engine.get_title()
    return {"title": title, "session_id": session_id}


@app.get("/api/url")
async def get_url(session_id: str = Query("default", description="Session ID")):
    session = await _get_existing_session(session_id)
    engine = session.engine_manager.get_engine()
    url = await engine.get_url()
    return {"url": url, "session_id": session_id}


# --- Advanced browser endpoints ---

# Primitive CDP RemoteObject type names that can be directly serialized
_PRIMITIVE_CDP_TYPES = {"string", "number", "boolean", "undefined", "null"}


def _normalize_evaluate_result(result):
    """Normalize nodriver's evaluate() return value to a plain Python value.
    
    nodriver can return:
    - A tuple (value, exception_details)
    - A plain value (str, int, float, bool, None)
    - A RemoteObject dataclass for primitives like False (nodriver bug:
      returns True as native bool but False as RemoteObject)
    - A RemoteObject for non-primitive types (e.g. object), which cannot be
      serialized to JSON — caller should use JSON.stringify() in JS
    """
    if isinstance(result, tuple):
        value, err = result
        if err:
            return None, str(err)
        result = value
    
    # nodriver.cdp.runtime.RemoteObject is a dataclass, not a dict
    # It wraps primitive values (especially False) instead of returning them directly
    if hasattr(result, 'type_') and hasattr(result, 'value'):
        if result.type_ not in _PRIMITIVE_CDP_TYPES:
            return None, f"Result type '{result.type_}' is not serializable; use JSON.stringify() in your script"
        return result.value, None
    
    return result, None


@app.post("/api/execute_js")
async def execute_js(
    req: ExecuteJsRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Execute arbitrary JavaScript on the current page (stealth engine only)."""
    engine_mgr = await _get_session_engine(session_id)
    engine = engine_mgr.get_engine()
    if not isinstance(engine, StealthEngine):
        # Phase 6 Task 2: structured error so AI agents know how to recover.
        return JSONResponse(
            status_code=400,
            content={
                "error": "ENGINE_NOT_SUPPORTED",
                "message": (
                    "execute_js requires the stealth (CDP) engine. "
                    "Current engine: native. "
                    'Call POST /api/engine {"engine": "stealth"} to switch.'
                ),
                "current_engine": "native",
            },
        )
    if not engine.tab:
        raise HTTPException(status_code=500, detail="No active tab")
    try:
        raw = await engine.tab.evaluate(req.script, return_by_value=True)
        value, err = _normalize_evaluate_result(raw)
        if err:
            return {"result": None, "error": err}
        return {"result": value}
    except Exception as e:
        logger.error("execute_js failed: %s", e)
        return {"result": None, "error": str(e)}


@app.post("/api/wait_for_element")
async def wait_for_element(
    req: WaitForElementRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Wait for an element matching text or CSS selector to appear on the page."""
    session = await _get_existing_session(session_id)
    engine = session.engine_manager.get_engine()

    if not isinstance(engine, StealthEngine) or not engine.tab:
        return {"found": False, "error": "not supported on native engine", "elapsed_ms": 0}

    start = time.time()
    timeout = req.timeout

    if req.text:
        js_check = f"""(function() {{
            var body = document.body.innerText || '';
            return body.includes({json.dumps(req.text)});
        }})()"""
    elif req.selector:
        js_check = f"""(function() {{
            return document.querySelector({json.dumps(req.selector)}) !== null;
        }})()"""
    else:
        raise HTTPException(status_code=400, detail="Either 'text' or 'selector' must be provided")

    while time.time() - start < timeout:
        try:
            raw = await engine.tab.evaluate(js_check, return_by_value=True)
            result, err = _normalize_evaluate_result(raw)
            if err:
                elapsed_ms = int((time.time() - start) * 1000)
                return {"found": False, "elapsed_ms": elapsed_ms, "error": str(err)}
            if result is True:
                elapsed_ms = int((time.time() - start) * 1000)
                return {"found": True, "elapsed_ms": elapsed_ms}
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            return {"found": False, "elapsed_ms": elapsed_ms, "error": str(e)}
        await asyncio.sleep(0.5)

    elapsed_ms = int((time.time() - start) * 1000)
    return {"found": False, "elapsed_ms": elapsed_ms}


@app.post("/api/find_element")
async def find_element(
    req: FindElementRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Find an interactive element matching the given text, return its coordinates."""
    session = await _get_existing_session(session_id)
    engine = session.engine_manager.get_engine()

    if not isinstance(engine, StealthEngine) or not engine.tab:
        return {"found": False, "error": "not supported on native engine"}

    # Reuse the element extraction JS from screenshot.py
    raw_js = extract_elements_js().strip()
    js_code = "JSON.stringify(" + raw_js + ")"
    try:
        raw = await engine.tab.evaluate(js_code, return_by_value=True)
        result, err = _normalize_evaluate_result(raw)
        if err:
            return {"found": False, "element_count": 0, "error": str(err)}
        if isinstance(result, str):
            elements = json.loads(result)
        elif isinstance(result, list):
            elements = result
        else:
            elements = []
    except Exception as e:
        logger.error("find_element extraction failed: %s", e)
        return {"found": False, "error": str(e)}

    # Tag mapping for element_type filter
    type_tag_map = {
        "button": {"button"},
        "link": {"a"},
        "input": {"input", "textarea", "select"},
    }

    search_text = req.text.lower()
    for el in elements:
        el_text = el.get("text", "").lower()
        if search_text not in el_text:
            continue

        tag = el.get("tag", "")
        if req.element_type != "any":
            allowed_tags = type_tag_map.get(req.element_type, set())
            if tag not in allowed_tags:
                continue

        # Return the recommended click point (left-inset) rather than the raw
        # centre, to avoid adjacent icons/buttons that share the bounding box.
        x = el.get("click_x", el["x"] + el["width"] // 2)
        y = el.get("click_y", el["y"] + el["height"] // 2)
        return {
            "found": True,
            "x": x,
            "y": y,
            "tag": tag,
            "text": el.get("text", ""),
            "role": el.get("role", ""),
            "href": el.get("href", ""),
        }

    return {"found": False, "searched_text": req.text, "element_count": len(elements)}


# --- Session / Tab management endpoints ---

# --- Data extraction endpoints (Task 2) ---

_LINKS_JS = """JSON.stringify(
  Array.from(document.querySelectorAll('a[href]')).map(a => {
    const r = a.getBoundingClientRect();
    return {
      text: (a.innerText || '').trim(),
      url: a.href,
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2)
    };
  }).filter(l => l.url && l.url.startsWith('http'))
)"""

# 2026-07-26: real pain point — on e-commerce search-result pages (Amazon,
# Kmart), a plain link's own text is often just "Add" or blank (image links),
# with the actual title/price/rating sitting in nearby siblings within the
# same product card. Cross-referencing that by hand against a separate raw
# text dump (as done manually this session) works but is slow. `context`
# walks up a few ancestors to the nearest reasonably-card-sized container and
# returns its text alongside the link, so price/rating come back in the same
# call — without needing viewport-limited coordinates like `elements` does.
_LINKS_WITH_CONTEXT_JS = """JSON.stringify(
  Array.from(document.querySelectorAll('a[href]')).map(a => {
    const r = a.getBoundingClientRect();
    let node = a;
    let context = (a.innerText || '').trim();
    for (let i = 0; i < 4 && node.parentElement; i++) {
      node = node.parentElement;
      const t = (node.innerText || '').trim();
      if (t.length > 600) break;
      if (t.length >= context.length) context = t;
    }
    return {
      text: (a.innerText || '').trim(),
      url: a.href,
      context: context,
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2)
    };
  }).filter(l => l.url && l.url.startsWith('http'))
)"""

_TABLE_JS_TEMPLATE = """JSON.stringify((function() {{
  var index = {index};
  var tables = document.querySelectorAll('table');
  var t = tables[index];
  if (!t) return {{error: 'No table found at index ' + index, total_tables: tables.length}};
  var headers = Array.from(t.querySelectorAll('th')).map(h => h.innerText.trim());
  var rows = Array.from(t.querySelectorAll('tr'));
  if (headers.length > 0) rows = rows.slice(1);
  var data = rows.map(r => Array.from(r.querySelectorAll('td')).map(c => c.innerText.trim()));
  return {{headers: headers, rows: data, table_index: index, total_tables: tables.length}};
}})())"""

_META_JS = """JSON.stringify((function() {
  var m = s => { var el = document.querySelector(s); return el && el.content ? el.content : null; };
  return {
    title: document.title,
    description: m('meta[name="description"]'),
    og_title: m('meta[property="og:title"]'),
    og_description: m('meta[property="og:description"]'),
    og_image: m('meta[property="og:image"]'),
    canonical: (document.querySelector('link[rel="canonical"]') || {}).href || null,
    url: window.location.href
  };
})())"""


@app.get("/api/links")
async def extract_links(
    filter_text: str | None = Query(None, description="Only return links whose text (or context, if with_context) contains this string"),
    same_domain_only: bool = Query(False, description="Only return links on the same domain"),
    with_context: bool = Query(
        False,
        description="Include each link's surrounding card text (title/price/rating) as `context` — "
        "useful on e-commerce listing pages where the link itself has no visible text (image links, 'Add' buttons)",
    ),
    session_id: str = Query("default", description="Session ID"),
):
    """Extract all links from the current page."""
    session = await _get_existing_session(session_id)
    engine = session.engine_manager.get_engine()
    if not isinstance(engine, StealthEngine) or not engine.tab:
        return {"links": [], "total": 0, "error": "not supported on native engine"}
    try:
        js = _LINKS_WITH_CONTEXT_JS if with_context else _LINKS_JS
        raw = await engine.tab.evaluate(js, return_by_value=True)
        value, err = _normalize_evaluate_result(raw)
        if err:
            return {"links": [], "total": 0, "error": err}
        links = json.loads(value) if isinstance(value, str) else (value or [])

        # Apply filters in Python (keep JS generic)
        if filter_text:
            ft = filter_text.lower()
            links = [
                l
                for l in links
                if ft in (l.get("text") or "").lower() or ft in (l.get("context") or "").lower()
            ]

        if same_domain_only:
            from urllib.parse import urlparse
            try:
                current_url = await engine.get_url()
                current_domain = urlparse(current_url).netloc.lower()
                links = [l for l in links if urlparse(l.get("url", "")).netloc.lower() == current_domain]
            except Exception:
                pass

        return {"links": links, "total": len(links)}
    except Exception as e:
        logger.error("extract_links failed: %s", e)
        return {"links": [], "total": 0, "error": str(e)}


@app.get("/api/table")
async def extract_table(
    index: int = Query(0, description="Table index (0-based, must be a non-negative integer)"),
    session_id: str = Query("default", description="Session ID"),
):
    """Extract a table from the current page as structured data."""
    session = await _get_existing_session(session_id)
    engine = session.engine_manager.get_engine()
    if not isinstance(engine, StealthEngine) or not engine.tab:
        return {"error": "not supported on native engine", "total_tables": 0}
    if index < 0:
        return {"error": "Table index must be non-negative", "total_tables": 0}
    try:
        js = _TABLE_JS_TEMPLATE.format(index=json.dumps(index))
        raw = await engine.tab.evaluate(js, return_by_value=True)
        value, err = _normalize_evaluate_result(raw)
        if err:
            return {"error": err, "total_tables": 0}
        result = json.loads(value) if isinstance(value, str) else (value or {})
        return result
    except Exception as e:
        logger.error("extract_table failed: %s", e)
        return {"error": str(e), "total_tables": 0}


@app.get("/api/meta")
async def get_meta(
    session_id: str = Query("default", description="Session ID"),
):
    """Extract page metadata (title, description, Open Graph, canonical URL)."""
    session = await _get_existing_session(session_id)
    engine = session.engine_manager.get_engine()
    if not isinstance(engine, StealthEngine) or not engine.tab:
        return {"error": "not supported on native engine"}
    try:
        raw = await engine.tab.evaluate(_META_JS, return_by_value=True)
        value, err = _normalize_evaluate_result(raw)
        if err:
            return {"error": err}
        result = json.loads(value) if isinstance(value, str) else (value or {})
        return result
    except Exception as e:
        logger.error("get_meta failed: %s", e)
        return {"error": str(e)}


@app.get("/api/cookies")
async def get_cookies(
    domain: str = Query("", description="Filter cookies by domain (optional)"),
    session_id: str = Query("default", description="Session ID"),
):
    """Get browser cookies, optionally filtered by domain."""
    session = await _get_existing_session(session_id)
    engine_mgr = session.engine_manager
    try:
        cookies = await engine_mgr.get_cookies(domain=domain)
        return {"cookies": cookies, "count": len(cookies)}
    except Exception as e:
        logger.error("get_cookies failed: %s", e)
        return {"error": str(e)}


class SetCookieRequest(BaseModel):
    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float = -1
    secure: bool = False
    http_only: bool = False


class ScratchpadAddRequest(BaseModel):
    content: str
    label: str | None = None


async def _get_session(session_id: str = "default") -> "BrowserSession":
    """Get or create a session, validating the session_id first."""
    try:
        return await session_mgr.get_or_create_session(session_id)
    except InvalidSessionIdError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _get_existing_session(session_id: str = "default") -> "BrowserSession":
    """Get an existing session. Raise 404 if it does not exist.

    Use this for read-only endpoints so that a typo in session_id does not
    silently spawn a new browser session.
    """
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    session = session_mgr.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    session.touch()
    return session


@app.post("/api/scratchpad")
async def scratchpad_add(
    req: ScratchpadAddRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Add a research note to the session scratchpad, tagged with current URL."""
    session = await _get_session(session_id)
    engine = session.engine_manager.get_engine()
    try:
        url = await engine.get_url()
    except Exception as e:
        logger.warning("scratchpad_add could not read current URL: %s", e)
        url = ""
    entry = session.add_scratchpad_entry(
        content=req.content,
        url=url,
        label=req.label,
    )
    return entry


@app.get("/api/scratchpad")
async def scratchpad_get(session_id: str = Query("default", description="Session ID")):
    """Get all scratchpad entries for the session, in insertion order."""
    session = await _get_session(session_id)
    return {"entries": session.get_scratchpad_entries()}


@app.delete("/api/scratchpad")
async def scratchpad_clear(
    session_id: str = Query("default", description="Session ID"),
):
    """Clear the session scratchpad."""
    session = await _get_session(session_id)
    count = session.clear_scratchpad()
    return {"cleared": True, "count": count}


@app.post("/api/cookies")
async def set_cookie(
    req: SetCookieRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Set a browser cookie."""
    engine_mgr = await _get_session_engine(session_id)
    try:
        result = await engine_mgr.set_cookie(
            req.name, req.value, req.domain, req.path,
            req.expires, req.secure, req.http_only,
        )
        return result
    except Exception as e:
        logger.error("set_cookie failed: %s", e)
        return {"error": str(e)}


@app.delete("/api/cookies")
async def clear_cookies(
    domain: str = Query("", description="Clear cookies for this domain only (empty = clear all)"),
    session_id: str = Query("default", description="Session ID"),
):
    """Clear browser cookies."""
    engine_mgr = await _get_session_engine(session_id)
    try:
        result = await engine_mgr.clear_cookies(domain=domain)
        return result
    except Exception as e:
        logger.error("clear_cookies failed: %s", e)
        return {"error": str(e)}


@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest):
    """Create a new browser session."""
    try:
        session = await session_mgr.create_session(req.session_id)
    except InvalidSessionIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return session.to_dict()


@app.get("/api/sessions")
async def list_sessions():
    """List all active sessions."""
    return {"sessions": session_mgr.list_sessions()}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a browser session."""
    if session_id == SessionManager.DEFAULT_SESSION_ID:
        raise HTTPException(
            status_code=400, detail="Cannot delete the default session"
        )
    try:
        deleted = await session_mgr.delete_session(session_id)
    except InvalidSessionIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    return {"deleted": True, "session_id": session_id}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Get session metadata."""
    try:
        session = session_mgr.sessions.get(session_id)
    except InvalidSessionIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not session:
        raise HTTPException(status_code=404, detail=f"Session not found: {session_id}")
    return session.to_dict()


class OpenTabRequest(BaseModel):
    url: str = "about:blank"
    return_look: bool = True


@app.post("/api/tabs")
async def create_tab(
    req: OpenTabRequest,
    session_id: str = Query("default", description="Session ID"),
):
    """Open a new real browser tab and navigate to the given URL."""
    _validate_url(req.url)
    session = await _get_session(session_id)
    try:
        result = await session.engine_manager.open_tab(req.url)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        result["session_id"] = session_id
        if req.return_look:
            extra_signals = await _settle(
                session.engine_manager.get_engine(), action="open_tab"
            )
            look_bundle = await _capture_look(session, max_length=20000)
            if extra_signals:
                look_bundle["block"]["signals"].extend(extra_signals)
                look_bundle["block"]["confidence"] = min(
                    1.0,
                    look_bundle["block"]["confidence"]
                    + sum(s.get("confidence", 0.0) for s in extra_signals),
                )
            return {**_normalize_action_result(result), **look_bundle}
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("open_tab failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tabs")
async def list_tabs(session_id: str = Query("default", description="Session ID")):
    """List all open browser tabs."""
    session = await _get_existing_session(session_id)
    tabs = await session.engine_manager.list_tabs()
    return {"tabs": tabs, "session_id": session_id}


@app.post("/api/tabs/{tab_id}/activate")
async def activate_tab(
    tab_id: str,
    session_id: str = Query("default", description="Session ID"),
    return_look: bool = Query(True, description="Return a look bundle after the action"),
):
    """Switch to a specific browser tab."""
    session = await _get_session(session_id)
    try:
        result = await session.engine_manager.switch_tab(tab_id)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        result["session_id"] = session_id
        if return_look:
            extra_signals = await _settle(
                session.engine_manager.get_engine(), action="switch_tab"
            )
            look_bundle = await _capture_look(session, max_length=20000)
            if extra_signals:
                look_bundle["block"]["signals"].extend(extra_signals)
                look_bundle["block"]["confidence"] = min(
                    1.0,
                    look_bundle["block"]["confidence"]
                    + sum(s.get("confidence", 0.0) for s in extra_signals),
                )
            return {**_normalize_action_result(result), **look_bundle}
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("switch_tab failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/tabs/{tab_id}")
async def close_tab(
    tab_id: str,
    session_id: str = Query("default", description="Session ID"),
):
    """Close a browser tab."""
    engine_mgr = await _get_session_engine(session_id)
    try:
        result = await engine_mgr.close_tab(tab_id)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("close_tab failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# --- Internal diagnostic endpoints (not part of the public MCP/CLI surface) ---


@app.post("/api/debug/start_frame_navigated_listener")
async def start_frame_navigated_listener(
    session_id: str = Query("default", description="Session ID"),
):
    """Start listening for CDP Page.frameNavigated events on the active tab.

    Used by e2e diagnostics to investigate post-load navigation issues such
    as the reported Amazon -> about:blank degradation.
    """
    session = await _get_session(session_id)
    engine = session.engine_manager.get_engine()
    if not isinstance(engine, StealthEngine):
        raise HTTPException(
            status_code=400,
            detail="frameNavigated listener requires the stealth engine",
        )
    engine.start_frame_navigated_listener()
    return {"status": "listener_started", "session_id": session_id}


@app.get("/api/debug/frame_navigations")
async def get_frame_navigations(
    session_id: str = Query("default", description="Session ID"),
):
    """Return the CDP frame navigation log collected since the listener started."""
    session = await _get_session(session_id)
    engine = session.engine_manager.get_engine()
    if not isinstance(engine, StealthEngine):
        raise HTTPException(
            status_code=400,
            detail="frameNavigated log requires the stealth engine",
        )
    return {"navigations": engine.get_frame_navigations(), "session_id": session_id}


@app.delete("/api/debug/frame_navigations")
async def clear_frame_navigations(
    session_id: str = Query("default", description="Session ID"),
):
    """Clear the CDP frame navigation log."""
    session = await _get_session(session_id)
    engine = session.engine_manager.get_engine()
    if not isinstance(engine, StealthEngine):
        raise HTTPException(
            status_code=400,
            detail="frameNavigated log requires the stealth engine",
        )
    engine.clear_frame_navigations()
    return {"status": "cleared", "session_id": session_id}


def main():
    """Run the server."""
    import uvicorn
    logger.info("Starting on http://%s:%s", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
