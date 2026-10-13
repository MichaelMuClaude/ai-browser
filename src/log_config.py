"""Structured logging configuration for ai-browser.

Avoids naming the module `logging.py` to prevent shadowing the standard library.
"""
import logging
import os
import sys
from typing import Optional


DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"


def setup_logging(level: Optional[str] = None, fmt: Optional[str] = None) -> None:
    """Configure root logging for ai-browser.

    Args:
        level: Log level name (DEBUG, INFO, WARNING, ERROR). Defaults to the
               AI_BROWSER_LOG_LEVEL environment variable or INFO.
        fmt: Log format string. Defaults to DEFAULT_FORMAT.
    """
    if level is None:
        level = os.environ.get("AI_BROWSER_LOG_LEVEL", "INFO").upper()
    if fmt is None:
        fmt = DEFAULT_FORMAT

    root = logging.getLogger()
    root.setLevel(level)

    # Remove existing handlers to avoid duplicates if called multiple times.
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger with the given name.

    If logging has not been explicitly configured yet, this sets up a default
    configuration so that log messages are visible.
    """
    logger = logging.getLogger(name)
    if not logging.getLogger().handlers:
        setup_logging()
    return logger
