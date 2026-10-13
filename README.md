# AI Browser

> A local browser service for AI agents: drive a real Chrome browser over an HTTP API, MCP, or CLI.

## Goals

Let different AI clients (Claude, Codex, local models, and others) share one browser capability:

- Open real websites in a real browser
- Observe pages through screenshots
- Click, type, scroll, and press keys by coordinates
- Use the fast stealth CDP engine for normal pages
- Switch to the native OS engine on anti-bot or human-verification pages

**Core principle: slower is acceptable, but public web pages should remain reachable, and the interface should stay generic.**

## Scope of use

This project is for automating websites you are authorized to access, such as your own accounts, public information, or systems you have permission to use.

- Do not use it to share credentials, operate accounts you do not control, bypass paywalls, or access systems without permission.
- The anti-detection features exist so that legitimate browser automation can complete its work. They are not intended to circumvent a site's terms of service or security controls.
- Follow each site's terms of service and `robots.txt`.
- You are responsible for any login state (cookies, profiles) you use. Never publish a profile directory.

## Current implementation

- `FastAPI` HTTP service, listening on `http://127.0.0.1:8888` by default
- MCP server (stdio), for MCP-compatible clients, with 37 tools
- CLI debugging tool
- Dual-engine architecture:
  - `StealthEngine`: controls a real Chrome via `nodriver`; the default, fast engine
  - `NativeEngine`: sends OS-level mouse and keyboard events via `xdotool`; the fallback engine
- `EngineManager`: manages engine lifecycle and switches engines automatically when it detects Cloudflare, CAPTCHA, DataDome, or similar blocking
- `SessionManager`: isolates multiple sessions, each with its own Chrome profile
- AI-friendly screenshots: `screenshot_annotated` extracts interactive page elements and annotates the screenshot with coordinate labels
- `smart_fetch`: fetches text quickly first, and falls back to the browser engines when the fast path fails, is blocked, or times out
- Session-scoped scratchpad for saving excerpts during multi-source research

## Workflow

```
AI sends an instruction
    ↓
HTTP API / MCP / CLI
    ↓
SessionManager selects a session (default: "default")
    ↓
EngineManager selects an engine
    ↓
Stealth CDP performs the action
    ↓
On blocking, switch to Native OS
    ↓
Screenshot / annotate elements / click / type / scroll
    ↓
The AI decides the next step from the page state
```

## Common entry points

```bash
# Start the HTTP API service
python -m src.main server

# Start the MCP server (stdio)
python -m src.main mcp

# CLI
python -m src.main cli health
python -m src.main cli navigate "https://example.com"
python -m src.main cli screenshot -o /tmp/ai-browser.png
```

The full list of HTTP routes is in `src/server.py`. The main groups are:

- Health and startup: `/api/health`, `/api/start`, `/api/engine`
- Navigation and extraction: `/api/navigate`, `/api/smart_fetch`, `/api/text`, `/api/title`, `/api/links`, `/api/table`, `/api/meta`
- Screenshots and observation: `/api/screenshot`, `/api/screenshot_annotated`, `/api/look`
- Interaction: `/api/click`, `/api/type`, `/api/scroll`, `/api/key`, `/api/select`, `/api/check`, `/api/uncheck`
- Sessions and tabs: `/api/sessions`, `/api/tabs`
- Cookies and scratchpad: `/api/cookies`, `/api/scratchpad`

The 37 MCP tools are prefixed with `browser_`, for example `browser_navigate`, `browser_click`, `browser_screenshot_annotated`, `browser_smart_fetch`, `browser_search`, and `browser_scratchpad_add`. Every tool accepts an optional `session_id` parameter.

## Directory structure

```
ai-browser/
├── README.md
├── AGENTS.md                # Guidelines for AI contributors
├── CONTRIBUTING.md
├── LICENSE
├── pyproject.toml
├── requirements.txt
├── pytest.ini
├── run.sh
├── docs/
│   └── ai-user-guide.md     # Usage guide for AI users
├── src/
│   ├── main.py              # Unified entry point: server / mcp / cli
│   ├── server.py            # FastAPI HTTP API
│   ├── mcp_server.py        # MCP server
│   ├── client.py            # HTTP client
│   ├── cli.py               # CLI debugging tool
│   ├── config.py            # Configuration
│   ├── log_config.py        # Logging configuration
│   ├── screenshot.py        # Screenshot annotation and element extraction
│   ├── look.py / smart_fetch.py
│   └── core/
│       ├── base.py          # Engine abstract interface
│       ├── stealth_cdp.py   # nodriver engine
│       ├── native_os.py     # xdotool engine
│       ├── engine_manager.py
│       ├── session.py       # Session management
│       ├── tab.py           # Tab abstraction
│       └── block_detection.py
├── integrations/
│   └── hermes/              # Native tool wrapper for the Hermes agent (optional)
├── deployment/
│   └── local/               # Local deployment scripts, systemd template, env template
└── tests/
    ├── unit/                # Unit tests
    ├── e2e/                 # End-to-end tests (require a running service)
    └── benchmarks/          # Latency benchmarks (run manually)
```

## Configuration

Main settings live in `src/config.py` and can be overridden with environment variables:

- `AI_BROWSER_HOST`, default `127.0.0.1`
- `AI_BROWSER_PORT`, default `8888`
- `AI_BROWSER_PATH`, default `/usr/bin/google-chrome`
- `AI_BROWSER_HEADLESS`, default `false`
- `AI_BROWSER_ENGINE`, default `auto`
- `AI_BROWSER_WIDTH` / `AI_BROWSER_HEIGHT`: window size for the native OS engine
- `AI_BROWSER_PROFILE_BASE_DIR`: root directory for Chrome profiles

Chrome uses separate profile directories, by default:

```text
~/.config/ai-browser-chrome-profile
~/.config/ai-browser-native-profile
```

## Runtime requirements

- Python 3.10+
- Google Chrome
- An X11 graphical session (only needed for the native OS engine)
- `xdotool`
- `imagemagick` / `xwd` / `convert` (fallback for native OS screenshots)
- Python dependencies in `requirements.txt`

Installation:

```bash
python -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/pip install -e '.[dev]'   # includes pytest
```

Local deployment, using the default method that does not require sudo:

```bash
deployment/local/install.sh
```

For system dependencies or starting at boot, add the corresponding flags. See `deployment/local/README.md`.

## Testing

```bash
# Unit tests (no running service required)
./venv/bin/python -m pytest tests/unit -v

# End-to-end tests (start the HTTP service first)
./venv/bin/python -m pytest tests/e2e -m smoke

# Native OS engine tests (requires X11)
./venv/bin/python -m pytest tests/e2e/test_native_engine.py -m native
```

## Integrations

- **MCP clients:** start with `python -m src.main mcp` and register it using the client's MCP configuration.
- **Hermes agent:** see [`integrations/hermes/README.md`](integrations/hermes/README.md).
- **Other HTTP clients:** call `http://127.0.0.1:8888/api/...` directly. See [`docs/ai-user-guide.md`](docs/ai-user-guide.md).

## Contributing

Contributions are welcome. Please read:

- [`CONTRIBUTING.md`](CONTRIBUTING.md): the quick entry point
- [`AGENTS.md`](AGENTS.md): the full guidelines, including every layer a change touches, the public contract, and PR requirements. AI agents should follow this file.

## License

MIT License. See [`LICENSE`](LICENSE).
