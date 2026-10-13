"""Thin wrapper tools for the ai-browser daemon (non-MCP).

Provides structured browser tools that call the local ai-browser HTTP daemon
via its own BrowserClient class.

Security design: these tools do NOT expose session_id as a parameter — all
calls use the default shared session. This makes session_id invention
structurally impossible, not merely discouraged by documentation.

Vision-first workflow (default):
    1. ai_browser_search(query)      → Google results as annotated screenshot + text + clickable elements.
    2. ai_browser_click(x, y)        → click + auto look bundle.
    3. ai_browser_scroll(direction)  → scroll + auto look bundle.
    4. ai_browser_look()             → inspect current page without acting.
    5. ai_browser_scratchpad_add()   → save research snippets while multi-sourcing.

ai_browser_smart_fetch is intentionally NOT the default: use it only when you
already have a specific URL and just want its text without vision/clicking.
"""

import importlib.util
import logging
import os

import httpx

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────────────────

_AI_BROWSER_SRC = os.environ.get(
    "AI_BROWSER_SRC",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"),
)
_AI_BROWSER_BASE_URL = os.environ.get(
    "AI_BROWSER_BASE_URL", "http://127.0.0.1:8888"
)


# ─── Load BrowserClient from ai-browser without polluting sys.path ──────────
#
# We use importlib.util.spec_from_file_location instead of sys.path.insert
# to avoid shadowing other top-level "client" modules in the Hermes environment.
# client.py has no relative imports (only stdlib + httpx), so this is safe.

_CLIENT_AVAILABLE = False
BrowserClient = None
BrowserClientError = Exception


def _load_browser_client():
    """Load BrowserClient from ai-browser's src/client.py."""
    global _CLIENT_AVAILABLE, BrowserClient, BrowserClientError

    client_path = os.path.join(_AI_BROWSER_SRC, "client.py")
    if not os.path.isfile(client_path):
        logger.warning(
            "ai-browser client.py not found at %s — ai_browser_* tools "
            "will be unavailable.",
            client_path,
        )
        return

    try:
        spec = importlib.util.spec_from_file_location(
            "_ai_browser_client", client_path
        )
        if spec is None or spec.loader is None:
            raise ImportError("spec_from_file_location returned None")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        BrowserClient = mod.BrowserClient
        BrowserClientError = mod.BrowserClientError
        _CLIENT_AVAILABLE = True
    except Exception as e:
        logger.warning(
            "Failed to load ai-browser BrowserClient from %s: %s — "
            "ai_browser_* tools will be unavailable.",
            client_path,
            e,
        )


_load_browser_client()

# Verify the methods we depend on still exist (catches API drift early).
_REQUIRED_METHODS = (
    "search",
    "navigate",
    "look",
    "click",
    "scroll",
    "type_text",
    "clear_field",
    "go_back",
    "go_forward",
    "open_tab",
    "switch_tab",
    "list_tabs",
    "scratchpad_add",
    "scratchpad_get",
    "scratchpad_clear",
    "smart_fetch",
    "close",
)
if _CLIENT_AVAILABLE:
    _missing = [m for m in _REQUIRED_METHODS if not hasattr(BrowserClient, m)]
    if _missing:
        logger.error(
            "BrowserClient missing expected methods: %s — "
            "ai_browser_* tools may malfunction.",
            _missing,
        )


# ─── Availability check ──────────────────────────────────────────────────────


def check_ai_browser_daemon() -> bool:
    """Return True when the ai-browser HTTP daemon is reachable.

    Cached for 30 s by the registry's check_fn TTL, so the HTTP probe
    runs at most twice a minute regardless of how often the tool list
    is rebuilt.
    """
    if not _CLIENT_AVAILABLE:
        return False
    try:
        resp = httpx.get(f"{_AI_BROWSER_BASE_URL}/api/health", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


# ─── Error formatting ────────────────────────────────────────────────────────


def _format_error(e: Exception) -> str:
    """User-facing error string for daemon/connectivity failures.

    Distinguishes two cases:
    - API-level errors: the daemon responded but the specific request failed
      (e.g. 500 from a bad navigation). ``BrowserClientError`` carries a
      ``status_code`` for these; report the real failure without implying the
      whole service is down.
    - Transport errors: the daemon could not be reached at all (connection
      refused, timeout connecting, DNS failure, etc.). These have no
      ``status_code``; tell the operator to check the service.
    """
    msg = str(e)
    status_code = getattr(e, "status_code", None)

    # Daemon responded, but this particular request returned an error status.
    if isinstance(e, BrowserClientError) and status_code is not None:
        return tool_error(
            f"ai-browser request failed (HTTP {status_code}): {msg}"
        )

    # Real connectivity problem: connection refused, timeout, DNS failure, ...
    if isinstance(e, (ConnectionError, OSError)) or (
        isinstance(e, BrowserClientError) and status_code is None
    ):
        return tool_error(
            f"ai-browser daemon unreachable at {_AI_BROWSER_BASE_URL}. "
            f"Ensure the service is running: systemctl --user status ai-browser"
        )

    return tool_error(f"ai-browser request failed: {msg}")


# ─── Async handlers ──────────────────────────────────────────────────────────
#
# Each handler creates a fresh BrowserClient (lightweight — just an httpx
# wrapper to localhost), calls the method WITHOUT session_id (always default
# session), and closes the client in a finally block.
#
# is_async=True lets the registry bridge these via _run_async().


async def _search(query: str, engine: str = "google") -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.search(query, engine=engine)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _look(max_length: int = 20000) -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.look(max_length=max_length)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _click(x: int, y: int) -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.click(x, y, return_look=True)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _scroll(direction: str = "down", amount: int = 3) -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.scroll(direction, amount, return_look=True)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _navigate(url: str) -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.navigate(url, return_look=True)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _type(text: str, clear: bool = False) -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.type_text(text, clear=clear)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _clear_field() -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.clear_field()
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _go_back() -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.go_back(return_look=True)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _go_forward() -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.go_forward(return_look=True)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _open_tab(url: str) -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.open_tab(url, return_look=True)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _switch_tab(tab_id: str) -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.switch_tab(tab_id, return_look=True)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _list_tabs() -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.list_tabs()
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _scratchpad_add(content: str, label: str | None = None) -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.scratchpad_add(content, label=label or None)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _scratchpad_get() -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.scratchpad_get()
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _scratchpad_clear() -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.scratchpad_clear()
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


async def _smart_fetch(url: str, max_length: int = 20000) -> str:
    client = BrowserClient(base_url=_AI_BROWSER_BASE_URL)
    try:
        result = await client.smart_fetch(url, max_length=max_length)
        return tool_result(result)
    except BrowserClientError as e:
        return _format_error(e)
    finally:
        await client.close()


# ─── Tool schemas (no session_id anywhere) ───────────────────────────────────

AI_BROWSER_SEARCH_SCHEMA = {
    "name": "ai_browser_search",
    "description": (
        "Default entry point for web research. Search the given engine and "
        "return the results page as a vision-first look bundle: annotated "
        "screenshot, page text, clickable element coordinates, title, and URL. "
        "After this, use ai_browser_click to open a result, ai_browser_scroll "
        "to see more results, and ai_browser_scratchpad_add to save key findings."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query.",
            },
            "engine": {
                "type": "string",
                "enum": ["google", "bing", "duckduckgo"],
                "default": "google",
                "description": (
                    "Search engine to use (default google). If Google shows a CAPTCHA, "
                    "'unusual traffic', or the block signals indicate the results page "
                    "was intercepted, switch to 'bing' or 'duckduckgo'. The tool will not "
                    "auto-switch — you decide."
                ),
            },
        },
        "required": ["query"],
    },
}

AI_BROWSER_LOOK_SCHEMA = {
    "name": "ai_browser_look",
    "description": (
        "Look at the current page without performing an action. Returns a "
        "look bundle: annotated screenshot + page text + clickable element "
        "coordinates + title + URL. Use this whenever you need to decide what "
        "to click next or to re-inspect the page after an unexpected result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "max_length": {
                "type": "integer",
                "description": "Max characters of page text to return (default 20000).",
            },
        },
    },
}

AI_BROWSER_CLICK_SCHEMA = {
    "name": "ai_browser_click",
    "description": (
        "Click at the given x, y coordinates on the page. Returns a look "
        "bundle (annotated screenshot + page text + clickable elements) of the "
        "page after the click. Use coordinates from the previous look bundle's "
        "clickable_elements / elements_text."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "x": {
                "type": "integer",
                "description": "X coordinate to click.",
            },
            "y": {
                "type": "integer",
                "description": "Y coordinate to click.",
            },
        },
        "required": ["x", "y"],
    },
}

AI_BROWSER_SCROLL_SCHEMA = {
    "name": "ai_browser_scroll",
    "description": (
        "Scroll the page up or down. Returns a look bundle after scrolling. "
        "Use this to reveal more search results or page content before "
        "deciding the next click."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["up", "down"],
                "description": "Scroll direction (default down).",
            },
            "amount": {
                "type": "integer",
                "description": "Scroll amount in viewport chunks (default 3).",
            },
        },
    },
}

AI_BROWSER_NAVIGATE_SCHEMA = {
    "name": "ai_browser_navigate",
    "description": (
        "Navigate the browser to a specific URL and return a look bundle. "
        "Use this narrow-case tool only when you already have a concrete URL "
        "from a previous search or message. For open-ended research, prefer "
        "ai_browser_search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full URL to navigate to.",
            },
        },
        "required": ["url"],
    },
}

AI_BROWSER_TYPE_SCHEMA = {
    "name": "ai_browser_type",
    "description": (
        "Type text into the currently focused input field. Set clear=true to "
        "replace the existing field content. Typically used after clicking into "
        "a search box or form field."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to type.",
            },
            "clear": {
                "type": "boolean",
                "description": "If true, clear the focused field before typing (default false).",
            },
        },
        "required": ["text"],
    },
}

AI_BROWSER_CLEAR_FIELD_SCHEMA = {
    "name": "ai_browser_clear_field",
    "description": (
        "Clear the currently focused input field (Ctrl+A then Delete). Use "
        "after clicking into a field to reliably replace its content before "
        "typing with ai_browser_type."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

AI_BROWSER_GO_BACK_SCHEMA = {
    "name": "ai_browser_go_back",
    "description": (
        "Go back to the previous page in browser history. Returns a look "
        "bundle of the page after going back. Useful for returning to search "
        "results after inspecting a result page."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

AI_BROWSER_GO_FORWARD_SCHEMA = {
    "name": "ai_browser_go_forward",
    "description": (
        "Go forward to the next page in browser history. Returns a look "
        "bundle of the page after going forward."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

AI_BROWSER_OPEN_TAB_SCHEMA = {
    "name": "ai_browser_open_tab",
    "description": (
        "Open a new browser tab and navigate to a URL. Returns a look bundle "
        "of the new tab. Useful for parallel multi-source research while "
        "keeping the original tab open."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to open in the new tab.",
            },
        },
        "required": ["url"],
    },
}

AI_BROWSER_SWITCH_TAB_SCHEMA = {
    "name": "ai_browser_switch_tab",
    "description": (
        "Switch to a specific browser tab by tab_id. Returns a look bundle of "
        "the target tab. Use ai_browser_list_tabs first to discover tab IDs, "
        "especially after a click opens a link in a new tab."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tab_id": {
                "type": "string",
                "description": "Tab ID to switch to (e.g. tab-0, tab-1).",
            },
        },
        "required": ["tab_id"],
    },
}

AI_BROWSER_LIST_TABS_SCHEMA = {
    "name": "ai_browser_list_tabs",
    "description": (
        "List all open browser tabs with their tab_id, title, URL, and active "
        "status. Use this after a click that may have opened a new tab in the "
        "background."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

AI_BROWSER_SCRATCHPAD_ADD_SCHEMA = {
    "name": "ai_browser_scratchpad_add",
    "description": (
        "Add a research note or verbatim clipping to the browser session's "
        "scratchpad. The entry is automatically tagged with the current page "
        "URL and a timestamp. Use this during multi-source research to preserve "
        "findings from earlier pages before navigating away."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Note text or verbatim clipping to save.",
            },
            "label": {
                "type": "string",
                "description": "Optional free-text label (e.g. 'price', 'claim').",
            },
        },
        "required": ["content"],
    },
}

AI_BROWSER_SCRATCHPAD_GET_SCHEMA = {
    "name": "ai_browser_scratchpad_get",
    "description": (
        "Retrieve all entries from the browser session's scratchpad, in "
        "insertion order. Each entry includes id, content, url, timestamp, and "
        "optional label. Use this to synthesize findings after visiting "
        "multiple sources."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

AI_BROWSER_SCRATCHPAD_CLEAR_SCHEMA = {
    "name": "ai_browser_scratchpad_clear",
    "description": (
        "Clear all entries from the browser session's scratchpad. Call this "
        "at the start of a new research task if you are reusing a session."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

AI_BROWSER_SMART_FETCH_SCHEMA = {
    "name": "ai_browser_smart_fetch",
    "description": (
        "Direct URL text extraction — NOT the default way to browse. Use only "
        "when another tool already gave you a specific URL and you just want "
        "its text, without vision or clicking. For normal searching / clicking / "
        "browsing, use the ai_browser_search → ai_browser_look → ai_browser_click "
        "loop instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full URL to fetch."},
            "max_length": {"type": "integer", "description": "Max chars (default 20000)."},
        },
        "required": ["url"],
    },
}


# ─── Registration ────────────────────────────────────────────────────────────
# toolset="ai_browser" is a registry-only toolset (not in toolsets.py TOOLSETS
# dict). It resolves correctly via registry.get_tool_names_for_toolset() when
# listed in cron job enabled_toolsets.

registry.register(
    name="ai_browser_search",
    toolset="ai_browser",
    schema=AI_BROWSER_SEARCH_SCHEMA,
    handler=lambda args, **kw: _search(
        args.get("query", ""),
        args.get("engine", "google"),
    ),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="🔍",
    max_result_size_chars=100_000,
)

registry.register(
    name="ai_browser_look",
    toolset="ai_browser",
    schema=AI_BROWSER_LOOK_SCHEMA,
    handler=lambda args, **kw: _look(args.get("max_length", 20000)),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="👁",
    max_result_size_chars=100_000,
)

registry.register(
    name="ai_browser_click",
    toolset="ai_browser",
    schema=AI_BROWSER_CLICK_SCHEMA,
    handler=lambda args, **kw: _click(args.get("x", 0), args.get("y", 0)),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="🖱",
    max_result_size_chars=100_000,
)

registry.register(
    name="ai_browser_scroll",
    toolset="ai_browser",
    schema=AI_BROWSER_SCROLL_SCHEMA,
    handler=lambda args, **kw: _scroll(
        args.get("direction", "down"),
        args.get("amount", 3),
    ),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="↕",
    max_result_size_chars=100_000,
)

registry.register(
    name="ai_browser_navigate",
    toolset="ai_browser",
    schema=AI_BROWSER_NAVIGATE_SCHEMA,
    handler=lambda args, **kw: _navigate(args.get("url", "")),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="🌐",
    max_result_size_chars=100_000,
)

registry.register(
    name="ai_browser_type",
    toolset="ai_browser",
    schema=AI_BROWSER_TYPE_SCHEMA,
    handler=lambda args, **kw: _type(
        args.get("text", ""),
        args.get("clear", False),
    ),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="⌨",
    max_result_size_chars=10_000,
)

registry.register(
    name="ai_browser_clear_field",
    toolset="ai_browser",
    schema=AI_BROWSER_CLEAR_FIELD_SCHEMA,
    handler=lambda args, **kw: _clear_field(),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="🧹",
    max_result_size_chars=10_000,
)

registry.register(
    name="ai_browser_go_back",
    toolset="ai_browser",
    schema=AI_BROWSER_GO_BACK_SCHEMA,
    handler=lambda args, **kw: _go_back(),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="◀",
    max_result_size_chars=100_000,
)

registry.register(
    name="ai_browser_go_forward",
    toolset="ai_browser",
    schema=AI_BROWSER_GO_FORWARD_SCHEMA,
    handler=lambda args, **kw: _go_forward(),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="▶",
    max_result_size_chars=100_000,
)

registry.register(
    name="ai_browser_open_tab",
    toolset="ai_browser",
    schema=AI_BROWSER_OPEN_TAB_SCHEMA,
    handler=lambda args, **kw: _open_tab(args.get("url", "")),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="🗂",
    max_result_size_chars=100_000,
)

registry.register(
    name="ai_browser_switch_tab",
    toolset="ai_browser",
    schema=AI_BROWSER_SWITCH_TAB_SCHEMA,
    handler=lambda args, **kw: _switch_tab(args.get("tab_id", "")),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="🔄",
    max_result_size_chars=100_000,
)

registry.register(
    name="ai_browser_list_tabs",
    toolset="ai_browser",
    schema=AI_BROWSER_LIST_TABS_SCHEMA,
    handler=lambda args, **kw: _list_tabs(),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="📑",
    max_result_size_chars=15_000,
)

registry.register(
    name="ai_browser_scratchpad_add",
    toolset="ai_browser",
    schema=AI_BROWSER_SCRATCHPAD_ADD_SCHEMA,
    handler=lambda args, **kw: _scratchpad_add(
        args.get("content", ""),
        args.get("label"),
    ),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="📝",
    max_result_size_chars=10_000,
)

registry.register(
    name="ai_browser_scratchpad_get",
    toolset="ai_browser",
    schema=AI_BROWSER_SCRATCHPAD_GET_SCHEMA,
    handler=lambda args, **kw: _scratchpad_get(),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="📋",
    max_result_size_chars=50_000,
)

registry.register(
    name="ai_browser_scratchpad_clear",
    toolset="ai_browser",
    schema=AI_BROWSER_SCRATCHPAD_CLEAR_SCHEMA,
    handler=lambda args, **kw: _scratchpad_clear(),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="🗑",
    max_result_size_chars=10_000,
)

registry.register(
    name="ai_browser_smart_fetch",
    toolset="ai_browser",
    schema=AI_BROWSER_SMART_FETCH_SCHEMA,
    handler=lambda args, **kw: _smart_fetch(
        args.get("url", ""),
        args.get("max_length", 20000),
    ),
    check_fn=check_ai_browser_daemon,
    is_async=True,
    emoji="🪄",
    max_result_size_chars=30_000,
)
