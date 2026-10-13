"""Base engine abstraction — all browser engines implement this interface."""
from abc import ABC, abstractmethod


class BrowserEngine(ABC):
    """Abstract base class for browser engines."""

    @abstractmethod
    async def start(self) -> None:
        """Launch the browser."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Close the browser."""
        ...

    @abstractmethod
    async def navigate(self, url: str) -> dict:
        """Navigate to a URL. Returns {"url": str, "title": str}."""
        ...

    @abstractmethod
    async def go_back(self) -> dict:
        """Go back in browser history. Returns {"url": str, "title": str}."""
        ...

    @abstractmethod
    async def go_forward(self) -> dict:
        """Go forward in browser history. Returns {"url": str, "title": str}."""
        ...

    @abstractmethod
    async def reload(self) -> dict:
        """Reload the current page. Returns {"url": str, "title": str}."""
        ...

    @abstractmethod
    async def screenshot(self) -> bytes:
        """Take a screenshot. Returns PNG bytes."""
        ...

    @abstractmethod
    async def click(self, x: int, y: int) -> dict:
        """Click at coordinates. Returns {"x": int, "y": int}."""
        ...

    @abstractmethod
    async def type_text(self, text: str) -> dict:
        """Type text. Returns {"text": str}."""
        ...

    @abstractmethod
    async def scroll(self, direction: str = "down", amount: int = 3) -> dict:
        """Scroll the page. Returns {"direction": str, "amount": int}."""
        ...

    @abstractmethod
    async def press_key(self, key: str) -> dict:
        """Press a key (e.g. Return, Tab, Escape). Returns {"key": str}."""
        ...

    @abstractmethod
    async def clear_field(self) -> dict:
        """Clear the currently focused input field. Returns {"cleared": bool}."""
        ...

    @abstractmethod
    async def select(self, selector: str, value: str) -> dict:
        """Select an option in a <select> element. Returns {"selector": str, "value": str}."""
        ...

    @abstractmethod
    async def check(self, selector: str) -> dict:
        """Check a checkbox or radio button. Returns {"selector": str}."""
        ...

    @abstractmethod
    async def uncheck(self, selector: str) -> dict:
        """Uncheck a checkbox. Returns {"selector": str}."""
        ...

    @abstractmethod
    async def get_cookies(self, domain: str = "") -> list:
        """Get cookies. Returns list of cookie dicts."""
        ...

    @abstractmethod
    async def set_cookie(self, name: str, value: str, domain: str, path: str = "/",
                         expires: float = -1, secure: bool = False,
                         http_only: bool = False) -> dict:
        """Set a cookie. Returns {"name": str, "domain": str, "path": str}."""
        ...

    @abstractmethod
    async def clear_cookies(self, domain: str = "") -> dict:
        """Clear cookies. Returns {"cleared": ...}."""
        ...

    @abstractmethod
    async def get_text(self, main_content_only: bool = False) -> dict:
        """Get page text content. Returns {"text": str}.

        main_content_only=True skips nav/header/footer boilerplate and prefers
        a recognized main-content container when the page has one.
        """
        ...

    @abstractmethod
    async def get_title(self) -> str:
        """Get page title."""
        ...

    @abstractmethod
    async def get_url(self) -> str:
        """Get current URL."""
        ...
