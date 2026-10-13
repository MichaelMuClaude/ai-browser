"""MCP Server for AI Browser — exposes browser tools to MCP-compatible AI clients.

This MCP server does not start its own browser. Instead, it acts as a thin
protocol adapter that calls the local ai-browser HTTP API. This ensures that
HTTP API clients and MCP clients share the exact same browser state.
"""
import json
import base64
import asyncio
import sys

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.server.lowlevel import NotificationOptions
from mcp.types import Tool, TextContent, ImageContent

from .client import BrowserClient, BrowserClientError
from .config import VERSION
from .errors import ErrorCode, make_error
from .log_config import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


class MCPServer:
    """MCP server that wraps the ai-browser HTTP client."""

    def __init__(self):
        self.app = Server("ai-browser")
        self.client = BrowserClient()
        self._setup_handlers()

    @staticmethod
    def _build_structured_error(exc: Exception, context: dict | None = None) -> dict:
        """Map an exception to a structured error response."""
        context = context or {}
        message = str(exc)
        text = message.lower()
        body = ""
        status = None
        if isinstance(exc, BrowserClientError):
            body = (exc.response_body or "").lower()
            status = exc.status_code

        # Tab errors (may come from HTTP body or from a tool result error string)
        if "tab" in text and "not found" in text:
            return make_error(ErrorCode.TAB_NOT_FOUND, message, context)
        if "tab" in text and ("no longer alive" in text or "stale" in text):
            return make_error(ErrorCode.TAB_STALE, message, context)

        # URL / navigation
        if (status == 400 and "url scheme" in body) or "url scheme" in text:
            return make_error(ErrorCode.URL_INVALID, message, context)
        if "timeout" in text or "timed out" in text:
            return make_error(ErrorCode.NAVIGATE_TIMEOUT, message, context)

        # Engine / capability
        if "not supported on native engine" in body or "multi-tab" in text:
            return make_error(ErrorCode.ENGINE_NOT_SUPPORTED, message, context)
        if status == 404 and "session" in body:
            return make_error(ErrorCode.SESSION_NOT_FOUND, message, context)
        if status == 404 and "tab" in body:
            return make_error(ErrorCode.TAB_NOT_FOUND, message, context)

        return make_error(ErrorCode.ENGINE_UNAVAILABLE, message, context)

    def _split_look_result(self, result: dict) -> list:
        """Split a look bundle into ImageContent + TextContent."""
        b64 = result.get("screenshot_base64", "")
        text_payload = {
            k: v for k, v in result.items() if k != "screenshot_base64"
        }
        return [
            ImageContent(type="image", data=b64, mimeType="image/png"),
            TextContent(type="text", text=json.dumps(text_payload, ensure_ascii=False)),
        ]

    def _setup_handlers(self):

        @self.app.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="browser_smart_fetch",
                    description=(
                        "Direct URL text extraction — NOT the default way to browse. "
                        "Use only when another tool already gave you a specific URL and "
                        "you just want its text, without vision or clicking. "
                        "For normal searching/clicking/browsing, use the search→look→click "
                        "loop instead. "
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "URL to fetch"},
                            "max_length": {"type": "integer", "description": "Max chars of text to return (default 20000)", "default": 20000},
                            "fallback_screenshot": {"type": "boolean", "description": "If true and the native engine cannot extract text, capture and return a base64 PNG screenshot", "default": False},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session."},
                        },
                        "required": ["url"],
                    },
                ),
                Tool(
                    name="browser_navigate",
                    description="Navigate the browser to a URL. Auto-switches to native engine if anti-bot blocking is detected. Requires the ai-browser HTTP service to be running. Do NOT invent session_id — omit it to use the shared default session.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "URL to navigate to"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                            "return_look": {"type": "boolean", "default": True, "description": "If true (default), automatically capture a look bundle (annotated screenshot + page text + elements) after the action and return it as image + text. If false, return only the action result."},
                            "main_content_only": {"type": "boolean", "default": False, "description": "If true, the returned page text skips nav/header/footer boilerplate and prefers the main-content container. Useful on sites with large nav menus (e-commerce, news)."},
                        },
                        "required": ["url"],
                    },
                ),
                Tool(
                    name="browser_go_back",
                    description="Go back to the previous page in browser history (equivalent to clicking the browser back button).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                            "return_look": {"type": "boolean", "default": True, "description": "If true (default), automatically capture a look bundle (annotated screenshot + page text + elements) after the action and return it as image + text. If false, return only the action result."},
                        },
                    },
                ),
                Tool(
                    name="browser_go_forward",
                    description="Go forward to the next page in browser history (equivalent to clicking the browser forward button).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                            "return_look": {"type": "boolean", "default": True, "description": "If true (default), automatically capture a look bundle (annotated screenshot + page text + elements) after the action and return it as image + text. If false, return only the action result."},
                        },
                    },
                ),
                Tool(
                    name="browser_reload",
                    description="Reload the current page (equivalent to pressing F5).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                            "return_look": {"type": "boolean", "default": True, "description": "If true (default), automatically capture a look bundle (annotated screenshot + page text + elements) after the action and return it as image + text. If false, return only the action result."},
                        },
                    },
                ),
                Tool(
                    name="browser_screenshot",
                    description="Take a raw screenshot of the current page.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_screenshot_annotated",
                    description="Take a screenshot with interactive elements annotated. Returns the annotated image AND a text list of clickable elements with coordinates. Use this instead of browser_screenshot when you need to know where to click.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_look",
                    description="Look at the current page: takes an annotated screenshot AND extracts the page text, title, URL, and clickable elements in ONE call. This is the core 'see what's on screen' action. Use this after any navigation or whenever you need to decide what to click next. Returns an image + a text summary of the page content and clickable elements.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "max_length": {"type": "integer", "default": 20000, "description": "Max chars of page text"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit for default shared session."},
                        },
                    },
                ),
                Tool(
                    name="browser_click",
                    description="Click at the given x, y coordinates on the page",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "x": {"type": "integer", "description": "X coordinate"},
                            "y": {"type": "integer", "description": "Y coordinate"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                            "return_look": {"type": "boolean", "default": True, "description": "If true (default), automatically capture a look bundle (annotated screenshot + page text + elements) after the action and return it as image + text. If false, return only the action result."},
                        },
                        "required": ["x", "y"],
                    },
                ),
                Tool(
                    name="browser_type",
                    description="Type text into the focused element. Set clear=true to replace the existing field content.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "Text to type"},
                            "clear": {"type": "boolean", "default": False, "description": "If true, clear the focused field before typing"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": ["text"],
                    },
                ),
                Tool(
                    name="browser_clear_field",
                    description="Clear the currently focused input field (Ctrl+A then Delete). Use after clicking into a field to replace its content.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_scroll",
                    description="Scroll the page up or down",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "direction": {"type": "string", "enum": ["up", "down"], "default": "down"},
                            "amount": {"type": "integer", "default": 3},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                            "return_look": {"type": "boolean", "default": True, "description": "If true (default), automatically capture a look bundle (annotated screenshot + page text + elements) after the action and return it as image + text. If false, return only the action result."},
                        },
                    },
                ),
                Tool(
                    name="browser_select",
                    description="Select an option in a <select> dropdown by CSS selector. The value can match either the option's value attribute or its visible text.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector for the <select> element"},
                            "value": {"type": "string", "description": "Option value or visible text to select"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": ["selector", "value"],
                    },
                ),
                Tool(
                    name="browser_check",
                    description="Check a checkbox or radio button by CSS selector. Idempotent: does nothing if already checked.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector for the input element"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": ["selector"],
                    },
                ),
                Tool(
                    name="browser_uncheck",
                    description="Uncheck a checkbox by CSS selector. Idempotent: does nothing if already unchecked.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector for the checkbox input"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": ["selector"],
                    },
                ),
                Tool(
                    name="browser_press_key",
                    description="Press a keyboard key (e.g. Return, Tab, Escape)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "Key name"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": ["key"],
                    },
                ),
                Tool(
                    name="browser_get_text",
                    description="Get the text content of the current page",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                            "main_content_only": {"type": "boolean", "default": False, "description": "If true, skip nav/header/footer boilerplate and prefer the main-content container. Useful on sites with large nav menus (e-commerce, news)."},
                        },
                    },
                ),
                Tool(
                    name="browser_get_title",
                    description="Get the title of the current page",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_get_url",
                    description="Get the current page URL",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_set_engine",
                    description="Switch browser engine: 'stealth' (fast CDP) or 'native' (xdotool, strongest anti-detection)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "engine": {"type": "string", "enum": ["stealth", "native", "auto"]},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": ["engine"],
                    },
                ),
                Tool(
                    name="browser_wait_for_element",
                    description="Wait for an element containing specified text or matching a CSS selector to appear on the page. At least one of 'text' or 'selector' must be provided. Useful for SPAs where content loads asynchronously. Returns found=true/false and elapsed time.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "Wait for page to contain this text"},
                            "selector": {"type": "string", "description": "Wait for this CSS selector to exist in DOM"},
                            "timeout": {"type": "integer", "description": "Max seconds to wait (default 10)", "default": 10},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": [],
                    },
                ),
                Tool(
                    name="browser_find_element",
                    description="Find an interactive element (button, link, input) by its text content. Returns the element's center coordinates so you can immediately click it. More reliable than guessing from annotated screenshots.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "Text to search for (case-insensitive substring match)"},
                            "element_type": {"type": "string", "enum": ["button", "link", "input", "any"], "default": "any", "description": "Filter by element type"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": ["text"],
                    },
                ),
                Tool(
                    name="browser_execute_js",
                    description="Execute arbitrary JavaScript on the current page and return the result. Only works with the stealth (CDP) engine. Use for extracting structured data, triggering events, or running custom DOM queries.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "script": {"type": "string", "description": "JavaScript code to execute (should return a serializable value)"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": ["script"],
                    },
                ),
                Tool(
                    name="browser_search",
                    description="Search the given engine for a query and return the results page as a look bundle: annotated screenshot + page text + clickable elements + search_url. This is the recommended starting point for research.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"},
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
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                            "main_content_only": {"type": "boolean", "default": False, "description": "If true, the returned page text skips nav/header/footer boilerplate and prefers the main-content container. Useful on sites with large nav menus (e-commerce, news)."},
                        },
                        "required": ["query"],
                    },
                ),
                Tool(
                    name="browser_extract_links",
                    description="Extract all links from the current page. Returns text, URL, and coordinates for each link. Optional filter_text narrows results by anchor text, same_domain_only restricts to same-domain links.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "filter_text": {"type": "string", "description": "Only return links whose anchor text (or context, if with_context) contains this string (case-insensitive)"},
                            "same_domain_only": {"type": "boolean", "description": "Only return links on the same domain as the current page"},
                            "with_context": {"type": "boolean", "default": False, "description": "Include each link's surrounding card text (title/price/rating) as `context`. Useful on e-commerce listing pages where the link itself has no visible text (image links, 'Add' buttons)."},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_extract_table",
                    description="Extract a table from the current page as structured JSON with headers and rows. Use index to select which table (0-based).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer", "description": "Table index (0-based, default 0)", "default": 0},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_get_meta",
                    description="Extract page metadata: title, description, Open Graph tags (og:title, og:description, og:image), canonical URL, and current URL.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_open_tab",
                    description="Open a new browser tab and navigate to a URL. Returns tab_id and page metadata. Useful for parallel browsing.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "URL to navigate to in the new tab"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                            "return_look": {"type": "boolean", "default": True, "description": "If true (default), automatically capture a look bundle (annotated screenshot + page text + elements) after the action and return it as image + text. If false, return only the action result."},
                        },
                        "required": ["url"],
                    },
                ),
                Tool(
                    name="browser_switch_tab",
                    description="Switch to a specific browser tab by tab_id. All subsequent operations (navigate, screenshot, click, etc.) will act on this tab.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "tab_id": {"type": "string", "description": "Tab ID to switch to (e.g. tab-0, tab-1)"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                            "return_look": {"type": "boolean", "default": True, "description": "If true (default), automatically capture a look bundle (annotated screenshot + page text + elements) after the action and return it as image + text. If false, return only the action result."},
                        },
                        "required": ["tab_id"],
                    },
                ),
                Tool(
                    name="browser_close_tab",
                    description="Close a browser tab by tab_id. The active tab will switch to another open tab if available.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "tab_id": {"type": "string", "description": "Tab ID to close"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": ["tab_id"],
                    },
                ),
                Tool(
                    name="browser_list_tabs",
                    description="List all open browser tabs with their tab_id, title, URL, and active status.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_get_cookies",
                    description="Get browser cookies. Optionally filter by domain.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "domain": {"type": "string", "description": "Filter cookies by domain (optional)"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_set_cookie",
                    description="Set a browser cookie.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Cookie name"},
                            "value": {"type": "string", "description": "Cookie value"},
                            "domain": {"type": "string", "description": "Cookie domain (e.g. .example.com)"},
                            "path": {"type": "string", "description": "Cookie path (default: /)"},
                            "expires": {"type": "number", "description": "Expiry timestamp (Unix seconds, -1 = session)"},
                            "secure": {"type": "boolean", "description": "HTTPS only"},
                            "http_only": {"type": "boolean", "description": "HttpOnly flag"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": ["name", "value", "domain"],
                    },
                ),
                Tool(
                    name="browser_clear_cookies",
                    description="Clear browser cookies. Optionally clear only cookies for a specific domain.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "domain": {"type": "string", "description": "Clear only cookies for this domain (optional, empty = clear all)"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_scratchpad_add",
                    description=(
                        "Add a research note or verbatim clipping to the browser session's scratchpad. "
                        "The entry is automatically tagged with the current page URL and a timestamp. "
                        "Use this while doing multi-source research to preserve findings from earlier pages "
                        "before navigating away, especially when context windows are tight."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Note text or verbatim clipping to save"},
                            "label": {"type": "string", "description": "Optional free-text label (e.g. 'price', 'claim')"},
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                        "required": ["content"],
                    },
                ),
                Tool(
                    name="browser_scratchpad_get",
                    description=(
                        "Retrieve all entries from the browser session's scratchpad, in insertion order. "
                        "Each entry includes id, content, url, timestamp, and optional label. Use this to "
                        "synthesize findings after visiting multiple sources."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
                Tool(
                    name="browser_scratchpad_clear",
                    description=(
                        "Clear all entries from the browser session's scratchpad. Call this at the start "
                        "of a new research task if you are reusing a session."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string", "description": "Rarely needed. Omit to use the default shared session. Non-default sessions auto-expire after 10 min idle."},
                        },
                    },
                ),
            ]

        @self.app.call_tool()
        async def call_tool(name: str, arguments: dict) -> list:
            try:
                session_id = arguments.get("session_id")

                if name == "browser_smart_fetch":
                    url = arguments["url"]
                    try:
                        result = await self.client.smart_fetch(
                            url,
                            max_length=arguments.get("max_length", 20000),
                            session_id=session_id,
                            fallback_screenshot=arguments.get("fallback_screenshot", False),
                        )
                    except BrowserClientError as e:
                        err = self._build_structured_error(e, {"url": url})
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_navigate":
                    url = arguments["url"]
                    return_look = arguments.get("return_look", True)
                    try:
                        result = await self.client.navigate(
                            url,
                            session_id=session_id,
                            return_look=return_look,
                            main_content_only=arguments.get("main_content_only", False),
                        )
                    except BrowserClientError as e:
                        err = self._build_structured_error(e, {"url": url})
                        return [TextContent(type="text", text=json.dumps(err))]
                    if result.get("blocked"):
                        err = make_error(
                            ErrorCode.NAVIGATE_BLOCKED,
                            f"Page was blocked on {result.get('engine_used', 'unknown')} engine",
                            {"url": url, "block_confidence": result.get("block_confidence")},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    if return_look and result.get("screenshot_base64"):
                        return self._split_look_result(result)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_go_back":
                    return_look = arguments.get("return_look", True)
                    try:
                        result = await self.client.go_back(session_id=session_id, return_look=return_look)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e)
                        return [TextContent(type="text", text=json.dumps(err))]
                    if return_look and result.get("screenshot_base64"):
                        return self._split_look_result(result)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_go_forward":
                    return_look = arguments.get("return_look", True)
                    try:
                        result = await self.client.go_forward(session_id=session_id, return_look=return_look)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e)
                        return [TextContent(type="text", text=json.dumps(err))]
                    if return_look and result.get("screenshot_base64"):
                        return self._split_look_result(result)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_reload":
                    return_look = arguments.get("return_look", True)
                    try:
                        result = await self.client.reload(session_id=session_id, return_look=return_look)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e)
                        return [TextContent(type="text", text=json.dumps(err))]
                    if return_look and result.get("screenshot_base64"):
                        return self._split_look_result(result)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_screenshot":
                    data = await self.client.screenshot(session_id=session_id)
                    b64 = base64.b64encode(data).decode("utf-8")
                    return [ImageContent(type="image", data=b64, mimeType="image/png")]

                elif name == "browser_screenshot_annotated":
                    data, elements_text, element_count = await self.client.screenshot_annotated(session_id=session_id)
                    b64 = base64.b64encode(data).decode("utf-8")
                    return [
                        ImageContent(type="image", data=b64, mimeType="image/png"),
                        TextContent(
                            type="text",
                            text=f"Found {element_count} interactive elements:\n{elements_text}",
                        ),
                    ]

                elif name == "browser_look":
                    result = await self.client.look(
                        max_length=arguments.get("max_length", 20000),
                        session_id=session_id,
                    )
                    b64 = result.get("screenshot_base64", "")
                    text_payload = {
                        k: v for k, v in result.items() if k != "screenshot_base64"
                    }
                    return [
                        ImageContent(type="image", data=b64, mimeType="image/png"),
                        TextContent(type="text", text=json.dumps(text_payload, ensure_ascii=False)),
                    ]

                elif name == "browser_click":
                    return_look = arguments.get("return_look", True)
                    result = await self.client.click(arguments["x"], arguments["y"], session_id=session_id, return_look=return_look)
                    if return_look and result.get("screenshot_base64"):
                        return self._split_look_result(result)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_type":
                    result = await self.client.type_text(
                        arguments["text"],
                        session_id=session_id,
                        clear=arguments.get("clear", False),
                    )
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_clear_field":
                    result = await self.client.clear_field(session_id=session_id)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_scroll":
                    return_look = arguments.get("return_look", True)
                    result = await self.client.scroll(
                        arguments.get("direction", "down"),
                        arguments.get("amount", 3),
                        session_id=session_id,
                        return_look=return_look,
                    )
                    if return_look and result.get("screenshot_base64"):
                        return self._split_look_result(result)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_press_key":
                    result = await self.client.press_key(arguments["key"], session_id=session_id)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_select":
                    selector = arguments["selector"]
                    value = arguments["value"]
                    try:
                        result = await self.client.select(selector, value, session_id=session_id)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e, {"selector": selector, "value": value})
                        return [TextContent(type="text", text=json.dumps(err))]
                    if result.get("error"):
                        err = make_error(
                            ErrorCode.ELEMENT_NOT_FOUND,
                            result["error"],
                            {"selector": selector, "value": value},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_check":
                    selector = arguments["selector"]
                    try:
                        result = await self.client.check(selector, session_id=session_id)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e, {"selector": selector})
                        return [TextContent(type="text", text=json.dumps(err))]
                    if result.get("error"):
                        err = make_error(
                            ErrorCode.ELEMENT_NOT_FOUND,
                            result["error"],
                            {"selector": selector},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_uncheck":
                    selector = arguments["selector"]
                    try:
                        result = await self.client.uncheck(selector, session_id=session_id)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e, {"selector": selector})
                        return [TextContent(type="text", text=json.dumps(err))]
                    if result.get("error"):
                        err = make_error(
                            ErrorCode.ELEMENT_NOT_FOUND,
                            result["error"],
                            {"selector": selector},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_get_text":
                    result = await self.client.get_text(
                        session_id=session_id,
                        main_content_only=arguments.get("main_content_only", False),
                    )
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_get_title":
                    result = await self.client.get_title(session_id=session_id)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_get_url":
                    result = await self.client.get_url(session_id=session_id)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_set_engine":
                    result = await self.client.set_engine(arguments["engine"], session_id=session_id)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_wait_for_element":
                    try:
                        result = await self.client.wait_for_element(
                            text=arguments.get("text"),
                            selector=arguments.get("selector"),
                            timeout=arguments.get("timeout", 10),
                            session_id=session_id,
                        )
                    except BrowserClientError as e:
                        err = self._build_structured_error(
                            e,
                            {"text": arguments.get("text"), "selector": arguments.get("selector")},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    if not result.get("found"):
                        err = make_error(
                            ErrorCode.ELEMENT_NOT_FOUND,
                            f"Element not found before timeout: {arguments.get('text') or arguments.get('selector')}",
                            {"text": arguments.get("text"), "selector": arguments.get("selector")},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_find_element":
                    try:
                        result = await self.client.find_element(
                            text=arguments["text"],
                            element_type=arguments.get("element_type", "any"),
                            session_id=session_id,
                        )
                    except BrowserClientError as e:
                        err = self._build_structured_error(e, {"text": arguments["text"]})
                        return [TextContent(type="text", text=json.dumps(err))]
                    if not result.get("found"):
                        err = make_error(
                            ErrorCode.ELEMENT_NOT_FOUND,
                            f"Element not found: {arguments['text']}",
                            {"text": arguments["text"], "element_type": arguments.get("element_type", "any")},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_execute_js":
                    try:
                        result = await self.client.execute_js(
                            script=arguments["script"],
                            session_id=session_id,
                        )
                    except BrowserClientError as e:
                        err = self._build_structured_error(e)
                        return [TextContent(type="text", text=json.dumps(err))]
                    if result.get("error"):
                        err = make_error(
                            ErrorCode.JS_ERROR,
                            f"JavaScript execution failed: {result['error']}",
                            {"script_preview": arguments["script"][:200]},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_search":
                    result = await self.client.search(
                        query=arguments["query"],
                        engine=arguments.get("engine", "google"),
                        session_id=session_id,
                        main_content_only=arguments.get("main_content_only", False),
                    )
                    if result.get("screenshot_base64"):
                        return self._split_look_result(result)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_extract_links":
                    result = await self.client.extract_links(
                        filter_text=arguments.get("filter_text"),
                        same_domain_only=arguments.get("same_domain_only", False),
                        with_context=arguments.get("with_context", False),
                        session_id=session_id,
                    )
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_extract_table":
                    index = arguments.get("index", 0)
                    try:
                        result = await self.client.extract_table(index=index, session_id=session_id)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e, {"index": index})
                        return [TextContent(type="text", text=json.dumps(err))]
                    if result.get("error"):
                        err = make_error(
                            ErrorCode.NO_TABLE_FOUND,
                            f"Could not extract table at index {index}: {result['error']}",
                            {"index": index, "total_tables": result.get("total_tables", 0)},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_get_meta":
                    result = await self.client.get_meta(session_id=session_id)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_open_tab":
                    url = arguments["url"]
                    return_look = arguments.get("return_look", True)
                    try:
                        result = await self.client.open_tab(url=url, session_id=session_id, return_look=return_look)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e, {"url": url})
                        return [TextContent(type="text", text=json.dumps(err))]
                    if result.get("error"):
                        err = make_error(
                            ErrorCode.ENGINE_NOT_SUPPORTED,
                            result["error"],
                            {"url": url},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    if return_look and result.get("screenshot_base64"):
                        return self._split_look_result(result)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_switch_tab":
                    tab_id = arguments["tab_id"]
                    return_look = arguments.get("return_look", True)
                    try:
                        result = await self.client.switch_tab(tab_id=tab_id, session_id=session_id, return_look=return_look)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e, {"tab_id": tab_id})
                        return [TextContent(type="text", text=json.dumps(err))]
                    if result.get("error"):
                        err = self._build_structured_error(
                            Exception(result["error"]),
                            {"tab_id": tab_id},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    if return_look and result.get("screenshot_base64"):
                        return self._split_look_result(result)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_close_tab":
                    tab_id = arguments["tab_id"]
                    try:
                        result = await self.client.close_tab(tab_id=tab_id, session_id=session_id)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e, {"tab_id": tab_id})
                        return [TextContent(type="text", text=json.dumps(err))]
                    if result.get("error"):
                        err = self._build_structured_error(
                            Exception(result["error"]),
                            {"tab_id": tab_id},
                        )
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_list_tabs":
                    result = await self.client.list_tabs(session_id=session_id)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_get_cookies":
                    domain = arguments.get("domain", "")
                    result = await self.client.get_cookies(domain=domain, session_id=session_id)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_set_cookie":
                    result = await self.client.set_cookie(
                        name=arguments["name"],
                        value=arguments["value"],
                        domain=arguments["domain"],
                        path=arguments.get("path", "/"),
                        expires=arguments.get("expires", -1),
                        secure=arguments.get("secure", False),
                        http_only=arguments.get("http_only", False),
                        session_id=session_id,
                    )
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_clear_cookies":
                    domain = arguments.get("domain", "")
                    result = await self.client.clear_cookies(domain=domain, session_id=session_id)
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_scratchpad_add":
                    try:
                        result = await self.client.scratchpad_add(
                            content=arguments["content"],
                            label=arguments.get("label"),
                            session_id=session_id,
                        )
                    except BrowserClientError as e:
                        err = self._build_structured_error(e)
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_scratchpad_get":
                    try:
                        result = await self.client.scratchpad_get(session_id=session_id)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e)
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                elif name == "browser_scratchpad_clear":
                    try:
                        result = await self.client.scratchpad_clear(session_id=session_id)
                    except BrowserClientError as e:
                        err = self._build_structured_error(e)
                        return [TextContent(type="text", text=json.dumps(err))]
                    return [TextContent(type="text", text=json.dumps(result))]

                else:
                    return [TextContent(type="text", text=f"Unknown tool: {name}")]

            except BrowserClientError as e:
                logger.error("MCP tool %s failed: %s", name, e)
                err = self._build_structured_error(e)
                return [TextContent(type="text", text=json.dumps(err))]
            except Exception as e:
                logger.error("MCP tool %s unexpected error: %s", name, e)
                err = self._build_structured_error(e)
                return [TextContent(type="text", text=json.dumps(err))]

    async def run_stdio(self):
        """Run MCP server over stdio (for Claude Desktop, etc.)."""
        try:
            health = await self.client.health()
            logger.info("Connected to ai-browser HTTP service: %s", health)
        except BrowserClientError as e:
            logger.error("Cannot connect to ai-browser HTTP service: %s", e)
            print(
                "Error: ai-browser HTTP service is not running. "
                "Please start it with: python -m src.main server",
                file=sys.stderr,
            )
            sys.exit(1)

        async with stdio_server() as (read_stream, write_stream):
            await self.app.run(
                read_stream, write_stream,
                InitializationOptions(
                    server_name="ai-browser",
                    server_version=VERSION,
                    capabilities=self.app.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities=None,
                    ),
                ),
            )


def main():
    """Run MCP server."""
    server = MCPServer()
    asyncio.run(server.run_stdio())


if __name__ == "__main__":
    main()
