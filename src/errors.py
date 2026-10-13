"""Structured error codes for AI Browser.

Provides machine-readable error codes so that AI agents can decide whether to
retry, switch engines, or abort without parsing free-text messages.
"""


class ErrorCode:
    """Machine-readable error codes returned by MCP tools and HTTP API."""

    # Navigation
    NAVIGATE_TIMEOUT = "NAVIGATE_TIMEOUT"
    NAVIGATE_BLOCKED = "NAVIGATE_BLOCKED"
    URL_INVALID = "URL_INVALID"

    # Element interaction
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    ELEMENT_NOT_VISIBLE = "ELEMENT_NOT_VISIBLE"

    # Engine
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    ENGINE_NOT_SUPPORTED = "ENGINE_NOT_SUPPORTED"

    # JavaScript execution
    JS_ERROR = "JS_ERROR"
    JS_TIMEOUT = "JS_TIMEOUT"

    # Session / Tab
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    TAB_NOT_FOUND = "TAB_NOT_FOUND"
    TAB_STALE = "TAB_STALE"

    # Screenshot
    SCREENSHOT_FAILED = "SCREENSHOT_FAILED"

    # Extraction
    NO_TABLE_FOUND = "NO_TABLE_FOUND"


# Errors where an automatic retry may resolve the problem.
RETRYABLE = {
    ErrorCode.NAVIGATE_TIMEOUT,
    ErrorCode.ENGINE_UNAVAILABLE,
    ErrorCode.SCREENSHOT_FAILED,
    ErrorCode.TAB_STALE,
}


def make_error(code: str, message: str, detail: dict | None = None) -> dict:
    """Build a structured error response.

    Args:
        code: One of the ErrorCode constants.
        message: Human-readable description.
        detail: Optional extra context (e.g. URL, tab_id, selector).

    Returns:
        Dict with ``error``, ``code``, ``message``, ``retryable`` and any
        additional detail fields.
    """
    return {
        "error": True,
        "code": code,
        "message": message,
        "retryable": code in RETRYABLE,
        **(detail or {}),
    }
