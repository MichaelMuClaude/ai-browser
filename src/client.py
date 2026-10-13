"""HTTP client for ai-browser service.

Provides an async Python client that talks to the local ai-browser HTTP API.
This is used by the MCP server (and optionally the CLI) so that all consumers
share the same browser state.

NOTE: the Hermes integration (integrations/hermes/ai_browser_tool.py) imports
this class directly. The following methods are consumed externally:
  search, look, click, scroll, navigate, type_text, clear_field,
  go_back, go_forward, open_tab, switch_tab, list_tabs,
  scratchpad_add, scratchpad_get, scratchpad_clear, smart_fetch, close
Renaming or removing them requires updating the Hermes side.
"""
from typing import Optional, Tuple
import asyncio
import os

import httpx

# Keep client.py self-contained so it can still be loaded as a standalone
# module via importlib.util.spec_from_file_location (used by the Hermes
# ai_browser_tool.py).  Do not add relative imports that depend on the
# ``src`` package being imported normally.
DEFAULT_SMART_FETCH_TIMEOUT_SECONDS = 25

# Retry configuration for transient connection-level errors only.
# HTTPStatusError (API-level 4xx/5xx) is NOT retried; those are surfaced
# immediately to the caller so the Hermes wrappers can classify them.
_REQUEST_RETRY_ATTEMPTS = 2
_REQUEST_RETRY_BACKOFF_SECONDS = 0.5


class BrowserClientError(Exception):
    """Raised when the ai-browser HTTP service returns an error."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class BrowserClient:
    """Async client for the ai-browser HTTP API."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        self.base_url = base_url or os.environ.get(
            "AI_BROWSER_BASE_URL", "http://127.0.0.1:8888"
        )
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        **kwargs,
    ) -> httpx.Response:
        """Send an HTTP request and raise on failure.

        Retries only on httpx.ConnectError (a connection could not be
        established, so the daemon definitely did not receive the request).
        Other transport failures such as ReadTimeout, WriteError and
        PoolTimeout are NOT retried because the request may already have
        reached the daemon and been acted upon. HTTPStatusError is never
        retried; it represents an API-level response and is surfaced
        immediately so callers can inspect ``status_code``.
        """
        client = await self._get_client()
        last_error: Optional[httpx.RequestError] = None
        for attempt in range(_REQUEST_RETRY_ATTEMPTS):
            try:
                response = await client.request(method, path, params=params, **kwargs)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as e:
                raise BrowserClientError(
                    f"Request to {path} failed: {e.response.status_code} {e.response.text}",
                    status_code=e.response.status_code,
                    response_body=e.response.text,
                ) from e
            except httpx.ConnectError as e:
                last_error = e
                if attempt < _REQUEST_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_REQUEST_RETRY_BACKOFF_SECONDS * (2**attempt))
                # Continue to next retry attempt.
            except httpx.RequestError as e:
                # Non-connect transport failures (ReadTimeout, WriteError,
                # PoolTimeout, etc.) surface immediately without retry.
                raise BrowserClientError(f"Request to {path} failed: {e}") from e
        assert last_error is not None
        raise BrowserClientError(f"Request to {path} failed: {last_error}") from last_error

    def _session_params(self, session_id: Optional[str]) -> Optional[dict]:
        """Return query params for session selection."""
        if session_id is None:
            return None
        return {"session_id": session_id}

    async def health(self) -> dict:
        """Check service health."""
        resp = await self._request("GET", "/api/health")
        return resp.json()

    async def create_session(self, session_id: Optional[str] = None) -> dict:
        """Create a new browser session."""
        payload = {"session_id": session_id} if session_id else {}
        resp = await self._request("POST", "/api/sessions", json=payload)
        return resp.json()

    async def list_sessions(self) -> dict:
        """List active sessions."""
        resp = await self._request("GET", "/api/sessions")
        return resp.json()

    async def delete_session(self, session_id: str) -> dict:
        """Delete a session."""
        resp = await self._request("DELETE", f"/api/sessions/{session_id}")
        return resp.json()

    async def navigate(
        self,
        url: str,
        session_id: Optional[str] = None,
        return_look: bool = True,
        main_content_only: bool = False,
    ) -> dict:
        """Navigate to a URL."""
        resp = await self._request(
            "POST",
            "/api/navigate",
            params=self._session_params(session_id),
            json={
                "url": url,
                "return_look": return_look,
                "main_content_only": main_content_only,
            },
        )
        return resp.json()

    async def go_back(
        self, session_id: Optional[str] = None, return_look: bool = True
    ) -> dict:
        """Go back in browser history."""
        params = self._session_params(session_id) or {}
        params["return_look"] = return_look
        resp = await self._request("POST", "/api/back", params=params)
        return resp.json()

    async def go_forward(
        self, session_id: Optional[str] = None, return_look: bool = True
    ) -> dict:
        """Go forward in browser history."""
        params = self._session_params(session_id) or {}
        params["return_look"] = return_look
        resp = await self._request("POST", "/api/forward", params=params)
        return resp.json()

    async def reload(
        self, session_id: Optional[str] = None, return_look: bool = True
    ) -> dict:
        """Reload the current page."""
        params = self._session_params(session_id) or {}
        params["return_look"] = return_look
        resp = await self._request("POST", "/api/reload", params=params)
        return resp.json()

    async def screenshot(self, session_id: Optional[str] = None) -> bytes:
        """Take a raw screenshot."""
        resp = await self._request(
            "GET",
            "/api/screenshot",
            params=self._session_params(session_id),
        )
        return resp.content

    async def screenshot_annotated(
        self, session_id: Optional[str] = None
    ) -> Tuple[bytes, str, int]:
        """Take an annotated screenshot.

        Returns:
            Tuple of (png_bytes, elements_text, element_count).
        """
        response = await self._request(
            "GET",
            "/api/screenshot_annotated",
            params=self._session_params(session_id),
        )
        elements_text = response.headers.get("X-Elements-Text", "")
        element_count = int(response.headers.get("X-Element-Count", "0"))
        return response.content, elements_text, element_count

    async def look(
        self, max_length: int = 20000, session_id: Optional[str] = None
    ) -> dict:
        """Look at the current page: screenshot + text + elements in one call."""
        params = self._session_params(session_id) or {}
        params["max_length"] = max_length
        resp = await self._request(
            "GET",
            "/api/look",
            params=params,
        )
        return resp.json()

    async def click(
        self,
        x: int,
        y: int,
        session_id: Optional[str] = None,
        return_look: bool = True,
    ) -> dict:
        """Click at coordinates."""
        resp = await self._request(
            "POST",
            "/api/click",
            params=self._session_params(session_id),
            json={"x": x, "y": y, "return_look": return_look},
        )
        return resp.json()

    async def type_text(
        self,
        text: str,
        session_id: Optional[str] = None,
        clear: bool = False,
    ) -> dict:
        """Type text. If ``clear`` is True, clear the focused field first."""
        payload = {"text": text, "clear": clear}
        resp = await self._request(
            "POST",
            "/api/type",
            params=self._session_params(session_id),
            json=payload,
        )
        return resp.json()

    async def clear_field(self, session_id: Optional[str] = None) -> dict:
        """Clear the currently focused input field."""
        resp = await self._request(
            "POST",
            "/api/clear",
            params=self._session_params(session_id),
            json={},
        )
        return resp.json()

    async def scroll(
        self,
        direction: str = "down",
        amount: int = 3,
        session_id: Optional[str] = None,
        return_look: bool = True,
    ) -> dict:
        """Scroll the page."""
        resp = await self._request(
            "POST",
            "/api/scroll",
            params=self._session_params(session_id),
            json={
                "direction": direction,
                "amount": amount,
                "return_look": return_look,
            },
        )
        return resp.json()

    async def press_key(self, key: str, session_id: Optional[str] = None) -> dict:
        """Press a key."""
        resp = await self._request(
            "POST",
            "/api/key",
            params=self._session_params(session_id),
            json={"key": key},
        )
        return resp.json()

    async def select(
        self, selector: str, value: str, session_id: Optional[str] = None
    ) -> dict:
        """Select an option in a <select> element."""
        resp = await self._request(
            "POST",
            "/api/select",
            params=self._session_params(session_id),
            json={"selector": selector, "value": value},
        )
        return resp.json()

    async def check(self, selector: str, session_id: Optional[str] = None) -> dict:
        """Check a checkbox or radio button."""
        resp = await self._request(
            "POST",
            "/api/check",
            params=self._session_params(session_id),
            json={"selector": selector},
        )
        return resp.json()

    async def uncheck(self, selector: str, session_id: Optional[str] = None) -> dict:
        """Uncheck a checkbox."""
        resp = await self._request(
            "POST",
            "/api/uncheck",
            params=self._session_params(session_id),
            json={"selector": selector},
        )
        return resp.json()

    async def get_text(
        self, session_id: Optional[str] = None, main_content_only: bool = False
    ) -> dict:
        """Get page text."""
        params = self._session_params(session_id) or {}
        if main_content_only:
            params["main_content_only"] = True
        resp = await self._request(
            "GET",
            "/api/text",
            params=params,
        )
        return resp.json()

    async def get_title(self, session_id: Optional[str] = None) -> dict:
        """Get page title."""
        resp = await self._request(
            "GET",
            "/api/title",
            params=self._session_params(session_id),
        )
        return resp.json()

    async def get_url(self, session_id: Optional[str] = None) -> dict:
        """Get current URL."""
        resp = await self._request(
            "GET",
            "/api/url",
            params=self._session_params(session_id),
        )
        return resp.json()

    async def set_engine(
        self, engine: str, session_id: Optional[str] = None
    ) -> dict:
        """Switch engine."""
        resp = await self._request(
            "POST",
            "/api/engine",
            params=self._session_params(session_id),
            json={"engine": engine},
        )
        return resp.json()

    async def execute_js(
        self, script: str, session_id: Optional[str] = None
    ) -> dict:
        """Execute JavaScript on the current page."""
        resp = await self._request(
            "POST",
            "/api/execute_js",
            params=self._session_params(session_id),
            json={"script": script},
        )
        return resp.json()

    async def wait_for_element(
        self,
        text: Optional[str] = None,
        selector: Optional[str] = None,
        timeout: int = 10,
        session_id: Optional[str] = None,
    ) -> dict:
        """Wait for an element to appear on the page."""
        resp = await self._request(
            "POST",
            "/api/wait_for_element",
            params=self._session_params(session_id),
            json={"text": text, "selector": selector, "timeout": timeout},
            timeout=timeout + 5,  # override default 30s to match server-side wait
        )
        return resp.json()

    async def find_element(
        self,
        text: str,
        element_type: str = "any",
        session_id: Optional[str] = None,
    ) -> dict:
        """Find an interactive element by text, return coordinates."""
        resp = await self._request(
            "POST",
            "/api/find_element",
            params=self._session_params(session_id),
            json={"text": text, "element_type": element_type},
        )
        return resp.json()

    async def search(
        self,
        query: str,
        engine: str = "google",
        session_id: Optional[str] = None,
        return_look: bool = True,
        main_content_only: bool = False,
    ) -> dict:
        """Search the given engine and return the results page as a look bundle."""
        resp = await self._request(
            "POST",
            "/api/search",
            params=self._session_params(session_id),
            json={
                "query": query,
                "engine": engine,
                "return_look": return_look,
                "main_content_only": main_content_only,
            },
        )
        return resp.json()

    async def smart_fetch(
        self,
        url: str,
        max_length: int = 20000,
        timeout: int | None = None,
        session_id: Optional[str] = None,
        fallback_screenshot: bool = False,
    ) -> dict:
        """One-shot fetch: try stealth/native first, optional claude fallback."""
        payload = {"url": url, "max_length": max_length}
        if timeout is not None:
            payload["timeout"] = timeout
        if session_id is not None:
            payload["session_id"] = session_id
        if fallback_screenshot:
            payload["fallback_screenshot"] = fallback_screenshot
        resp = await self._request(
            "POST",
            "/api/smart_fetch",
            params=self._session_params(session_id),
            json=payload,
            timeout=max(30, (timeout or DEFAULT_SMART_FETCH_TIMEOUT_SECONDS) + 5),
        )
        return resp.json()

    async def extract_links(
        self,
        filter_text: Optional[str] = None,
        same_domain_only: bool = False,
        with_context: bool = False,
        session_id: Optional[str] = None,
    ) -> dict:
        """Extract all links from the current page."""
        params = {}
        if session_id:
            params["session_id"] = session_id
        if filter_text:
            params["filter_text"] = filter_text
        if same_domain_only:
            params["same_domain_only"] = "true"
        if with_context:
            params["with_context"] = "true"
        resp = await self._request("GET", "/api/links", params=params or None)
        return resp.json()

    async def extract_table(
        self, index: int = 0, session_id: Optional[str] = None
    ) -> dict:
        """Extract a table from the page as structured data."""
        params = {"index": index}
        if session_id:
            params["session_id"] = session_id
        resp = await self._request("GET", "/api/table", params=params)
        return resp.json()

    async def get_meta(self, session_id: Optional[str] = None) -> dict:
        """Extract page metadata."""
        params = {}
        if session_id:
            params["session_id"] = session_id
        resp = await self._request("GET", "/api/meta", params=params or None)
        return resp.json()

    async def open_tab(
        self,
        url: str = "about:blank",
        session_id: Optional[str] = None,
        return_look: bool = True,
    ) -> dict:
        """Open a new browser tab."""
        params = {}
        if session_id:
            params["session_id"] = session_id
        resp = await self._request(
            "POST",
            "/api/tabs",
            json={"url": url, "return_look": return_look},
            params=params or None,
        )
        return resp.json()

    async def switch_tab(
        self, tab_id: str, session_id: Optional[str] = None, return_look: bool = True
    ) -> dict:
        """Switch to a specific browser tab."""
        params = {}
        if session_id:
            params["session_id"] = session_id
        params["return_look"] = return_look
        resp = await self._request(
            "POST", f"/api/tabs/{tab_id}/activate", params=params or None
        )
        return resp.json()

    async def close_tab(self, tab_id: str, session_id: Optional[str] = None) -> dict:
        """Close a browser tab."""
        params = {}
        if session_id:
            params["session_id"] = session_id
        resp = await self._request("DELETE", f"/api/tabs/{tab_id}", params=params or None)
        return resp.json()

    async def list_tabs(self, session_id: Optional[str] = None) -> dict:
        """List all open browser tabs."""
        params = {}
        if session_id:
            params["session_id"] = session_id
        resp = await self._request("GET", "/api/tabs", params=params or None)
        return resp.json()

    async def get_cookies(self, domain: str = "", session_id: Optional[str] = None) -> dict:
        """Get browser cookies, optionally filtered by domain."""
        params = self._session_params(session_id) or {}
        if domain:
            params["domain"] = domain
        resp = await self._request("GET", "/api/cookies", params=params or None)
        return resp.json()

    async def set_cookie(self, name: str, value: str, domain: str, path: str = "/",
                         expires: float = -1, secure: bool = False, http_only: bool = False,
                         session_id: Optional[str] = None) -> dict:
        """Set a browser cookie."""
        resp = await self._request(
            "POST", "/api/cookies",
            json={"name": name, "value": value, "domain": domain, "path": path,
                  "expires": expires, "secure": secure, "http_only": http_only},
            params=self._session_params(session_id) or None,
        )
        return resp.json()

    async def clear_cookies(self, domain: str = "", session_id: Optional[str] = None) -> dict:
        """Clear browser cookies."""
        params = self._session_params(session_id) or {}
        if domain:
            params["domain"] = domain
        resp = await self._request("DELETE", "/api/cookies", params=params or None)
        return resp.json()

    async def scratchpad_add(
        self,
        content: str,
        label: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """Add a note to the session scratchpad, tagged with current URL."""
        payload = {"content": content}
        if label is not None:
            payload["label"] = label
        resp = await self._request(
            "POST",
            "/api/scratchpad",
            params=self._session_params(session_id),
            json=payload,
        )
        return resp.json()

    async def scratchpad_get(self, session_id: Optional[str] = None) -> dict:
        """Get all scratchpad entries for the session."""
        resp = await self._request(
            "GET",
            "/api/scratchpad",
            params=self._session_params(session_id),
        )
        return resp.json()

    async def scratchpad_clear(self, session_id: Optional[str] = None) -> dict:
        """Clear the session scratchpad."""
        resp = await self._request(
            "DELETE",
            "/api/scratchpad",
            params=self._session_params(session_id),
        )
        return resp.json()

    async def close(self):
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
