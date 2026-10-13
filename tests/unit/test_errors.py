"""Unit tests for structured error codes."""
import pytest

from src.errors import ErrorCode, RETRYABLE, make_error
from src.client import BrowserClientError
from src.mcp_server import MCPServer


class TestMakeError:
    """Tests for make_error() helper."""

    def test_basic_error(self):
        err = make_error(ErrorCode.ELEMENT_NOT_FOUND, "button missing")
        assert err["error"] is True
        assert err["code"] == ErrorCode.ELEMENT_NOT_FOUND
        assert err["message"] == "button missing"
        assert err["retryable"] is False

    def test_retryable_error(self):
        err = make_error(ErrorCode.NAVIGATE_TIMEOUT, "timed out")
        assert err["retryable"] is True

    def test_error_with_detail(self):
        err = make_error(ErrorCode.TAB_NOT_FOUND, "tab gone", {"tab_id": "tab-1"})
        assert err["tab_id"] == "tab-1"


class TestErrorCodeConstants:
    """Sanity checks for error code constants."""

    def test_all_codes_are_strings(self):
        for name in dir(ErrorCode):
            if not name.startswith("_"):
                value = getattr(ErrorCode, name)
                assert isinstance(value, str)

    def test_retryable_set_contents(self):
        assert ErrorCode.NAVIGATE_TIMEOUT in RETRYABLE
        assert ErrorCode.ENGINE_UNAVAILABLE in RETRYABLE
        assert ErrorCode.ELEMENT_NOT_FOUND not in RETRYABLE


class TestMCPServerStructuredErrorMapping:
    """Tests for MCPServer._build_structured_error()."""

    def test_url_invalid(self):
        exc = BrowserClientError(
            "Request failed: 400 URL scheme must be one of http, https",
            status_code=400,
            response_body="URL scheme must be one of http, https",
        )
        err = MCPServer._build_structured_error(exc, {"url": "javascript:alert(1)"})
        assert err["code"] == ErrorCode.URL_INVALID
        assert err["error"] is True

    def test_session_not_found(self):
        exc = BrowserClientError(
            "Request failed: 404 Session not found: foo",
            status_code=404,
            response_body="Session not found: foo",
        )
        err = MCPServer._build_structured_error(exc)
        assert err["code"] == ErrorCode.SESSION_NOT_FOUND

    def test_tab_not_found_from_http(self):
        exc = BrowserClientError(
            "Request failed: 404 Tab 'tab-99' not found",
            status_code=404,
            response_body="Tab 'tab-99' not found",
        )
        err = MCPServer._build_structured_error(exc, {"tab_id": "tab-99"})
        assert err["code"] == ErrorCode.TAB_NOT_FOUND

    def test_tab_not_found_from_result_error(self):
        exc = Exception("Tab 'tab-99' not found. Available: ['tab-0']")
        err = MCPServer._build_structured_error(exc, {"tab_id": "tab-99"})
        assert err["code"] == ErrorCode.TAB_NOT_FOUND

    def test_tab_stale(self):
        exc = Exception("Tab 'tab-1' is no longer alive. Available: ['tab-0']")
        err = MCPServer._build_structured_error(exc, {"tab_id": "tab-1"})
        assert err["code"] == ErrorCode.TAB_STALE
        assert err["retryable"] is True

    def test_engine_not_supported_native_tab(self):
        exc = Exception("multi-tab not supported in native engine")
        err = MCPServer._build_structured_error(exc)
        assert err["code"] == ErrorCode.ENGINE_NOT_SUPPORTED

    def test_navigate_timeout(self):
        exc = BrowserClientError(
            "Request to /api/navigate timed out",
            status_code=504,
            response_body="Gateway timeout",
        )
        err = MCPServer._build_structured_error(exc, {"url": "https://example.com"})
        assert err["code"] == ErrorCode.NAVIGATE_TIMEOUT
        assert err["retryable"] is True

    def test_engine_unavailable_fallback(self):
        exc = BrowserClientError(
            "Request failed: 500 Browser not started",
            status_code=500,
            response_body="Browser not started",
        )
        err = MCPServer._build_structured_error(exc)
        assert err["code"] == ErrorCode.ENGINE_UNAVAILABLE

    def test_unexpected_exception(self):
        exc = RuntimeError("something weird")
        err = MCPServer._build_structured_error(exc)
        assert err["code"] == ErrorCode.ENGINE_UNAVAILABLE
