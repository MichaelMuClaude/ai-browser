"""Configuration for ai-browser."""
import os

# Version — single source of truth
VERSION = "0.3.0"

# Server
HOST = os.environ.get("AI_BROWSER_HOST", "127.0.0.1")
PORT = int(os.environ.get("AI_BROWSER_PORT", "8888"))

# Browser
BROWSER_PATH = os.environ.get("AI_BROWSER_PATH", "/usr/bin/google-chrome")  # Dedicated Chrome binary used for AI sessions
HEADLESS = os.environ.get("AI_BROWSER_HEADLESS", "false").lower() == "true"

# Chrome profile directories
STEALTH_PROFILE_DIR = os.path.expanduser(
    os.environ.get("AI_BROWSER_PROFILE_BASE_DIR", "~/.config/ai-browser-chrome-profile")
)
NATIVE_PROFILE_DIR = os.path.expanduser("~/.config/ai-browser-native-profile")

# Engine selection: "auto" | "stealth" | "native"
DEFAULT_ENGINE = os.environ.get("AI_BROWSER_ENGINE", "auto")

# Page an engine opens on startup (e.g. when the systemd service boots).
# about:blank left the visible AI profile window looking broken/empty at boot.
STARTUP_URL = os.environ.get("AI_BROWSER_STARTUP_URL", "https://www.google.com")

# Screenshot
SCREENSHOT_FORMAT = os.environ.get("AI_BROWSER_SCREENSHOT_FORMAT", "png")
SCREENSHOT_QUALITY = int(os.environ.get("AI_BROWSER_QUALITY", "85"))

# Window geometry (for native engine)
WINDOW_WIDTH = int(os.environ.get("AI_BROWSER_WIDTH", "1280"))
WINDOW_HEIGHT = int(os.environ.get("AI_BROWSER_HEIGHT", "800"))

# --- Timing / retry constants ---
# Page load polling (used by both stealth and native engines)
NAVIGATE_LOAD_POLL_INTERVAL = 0.5
NAVIGATE_LOAD_MAX_POLLS = 20  # 20 * 0.5s = 10s total timeout
NAVIGATE_LOAD_TIMEOUT = NAVIGATE_LOAD_MAX_POLLS * NAVIGATE_LOAD_POLL_INTERVAL

# Hard cap on the initial nodriver `tab.get(url)` call itself (stealth engine).
# 2026-07-26: real case — kmart.com.au hung this call indefinitely (no anti-bot
# signal ever surfaces because the request never returns), wedging the tab for
# every subsequent action too. Without this, a single unresponsive site can
# freeze the whole session.
STEALTH_NAVIGATE_TIMEOUT_SECONDS = float(
    os.environ.get("AI_BROWSER_STEALTH_NAVIGATE_TIMEOUT", "20")
)

# Native engine fallback cooldown after anti-bot block detection
NATIVE_FALLBACK_COOLDOWN_SECONDS = 300

# Force IBus to this input engine before every native `type_text()` call.
# xdotool sends synthetic X11 key events that pass through the system input
# method first — if IBus is sitting on a CJK engine (e.g. libpinyin on this
# machine), those keys get reinterpreted as pinyin and the typed text comes
# out as garbled candidate characters instead of literal English (real case,
# 2026-07-26: "floor lamp" typed as garbled Chinese). Set
# AI_BROWSER_NATIVE_FORCE_ENGLISH_IME=false to disable (e.g. on a machine
# without ibus installed at all, or one where this isn't wanted).
NATIVE_FORCE_ENGLISH_IME = (
    os.environ.get("AI_BROWSER_NATIVE_FORCE_ENGLISH_IME", "true").lower() == "true"
)
NATIVE_ENGLISH_IME_ENGINE = os.environ.get("AI_BROWSER_NATIVE_IME_ENGINE", "xkb:us::eng")

# Native engine startup / window readiness
NATIVE_STARTUP_WAIT_SECONDS = 3.0
NATIVE_WINDOW_READY_DELAY = 0.5

# xdotool defaults
XDOTOOL_DEFAULT_TIMEOUT = 5.0
XDOTOOL_DEFAULT_RETRIES = 1
XDOTOOL_RETRY_DELAY = 0.2

# Screenshot subprocess timeout
SCREENSHOT_TIMEOUT = 10

# Native typing delay (ms between keystrokes)
NATIVE_TYPE_DELAY_MS = 30

# --- Session lifecycle ---

# Non-default sessions are auto-closed after this many seconds of inactivity.
# Even if an agent invents novel session_ids, they expire and clean up on their own.
SESSION_IDLE_TTL_SECONDS = int(os.environ.get("AI_BROWSER_SESSION_IDLE_TTL", "600"))  # 10 min

# How often the background reaper checks for idle sessions.
SESSION_REAPER_INTERVAL_SECONDS = int(os.environ.get("AI_BROWSER_SESSION_REAPER_INTERVAL", "60"))

# Maximum number of non-default sessions allowed simultaneously.
# When a new session would exceed this, the least-recently-used one is evicted.
MAX_NON_DEFAULT_SESSIONS = int(os.environ.get("AI_BROWSER_MAX_SESSIONS", "3"))

# --- Smart fetch (claude engine) ---
# Default engine order: false = stealth-first (claude path is opt-in).
SMART_FETCH_CLAUDE_FIRST = (
    os.environ.get("AI_BROWSER_SMART_FETCH_CLAUDE_FIRST", "false").lower() == "true"
)
# Hard timeout for a single claude -p invocation.
CLAUDE_ENGINE_TIMEOUT_SECONDS = int(os.environ.get("AI_BROWSER_CLAUDE_TIMEOUT", "8"))
# Outputs shorter than this are treated as failed extractions.
CLAUDE_ENGINE_MIN_OUTPUT_CHARS = int(os.environ.get("AI_BROWSER_CLAUDE_MIN_OUTPUT", "100"))
# Default max chars returned to the caller.
CLAUDE_ENGINE_MAX_LENGTH = int(os.environ.get("AI_BROWSER_SMART_FETCH_MAX", "20000"))
# Prompt sent to Claude Code CLI when acting as a fast HTTP fetch transport.
CLAUDE_ENGINE_PROMPT_TEMPLATE = (
    "Visit this URL and return the page's main visible text content.\n\n"
    "URL: {url}\n\n"
    "Rules:\n"
    "- Return ONLY the visible text content of the page body. No commentary, no markdown, no preamble.\n"
    '- If the page shows a CAPTCHA / "access denied" / Cloudflare challenge / '
    '"verifying you are human" / 403 / 429 / 503 error, return exactly the single word: BLOCKED\n'
    "- If the URL fails to load, return exactly: BLOCKED\n"
    "- Do not summarize. Do not answer questions about the content. Just extract and return the raw visible text.\n"
    "- If the page is very long, return the first ~{max_length} characters.\n"
)
