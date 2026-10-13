"""Browser tab abstraction.

A BrowserTab represents a single rendering context (tab) within a browser
session. For the first iteration, each session owns exactly one active tab;
the abstraction is introduced so future versions can support multiple tabs
without breaking the API.
"""
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class BrowserTab:
    """Represents a browser tab."""
    tab_id: str
    session_id: str
    url: str = ""
    title: str = ""
    engine_ref: str = "stealth"  # "stealth" or "native"
    created_at: datetime = field(default_factory=datetime.now)
    last_used_at: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tab_id": self.tab_id,
            "session_id": self.session_id,
            "url": self.url,
            "title": self.title,
            "engine_ref": self.engine_ref,
            "created_at": self.created_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat(),
        }

    def touch(self) -> None:
        """Update the last-used timestamp."""
        self.last_used_at = datetime.now()
