# Local Deployment Kit

This folder contains a simple local-machine deployment path for `ai-browser`.

Use it when setting up a new Linux machine that should run the browser service locally for AI agents.

## Files

- `install.sh` — guided installer for local deployment.
- `ai-browser.env.example` — environment variable template.
- `ai-browser.service.template` — systemd user service template.

## Quick Start

From the project root:

```bash
deployment/local/install.sh
```

Common options:

```bash
# Install apt packages too. Requires sudo.
deployment/local/install.sh --install-system-deps

# Install and enable the systemd user service.
deployment/local/install.sh --install-service

# Install deps, service, then start service.
deployment/local/install.sh --install-system-deps --install-service --start-service

# Use a custom Chrome path or port.
deployment/local/install.sh --browser-path /usr/bin/google-chrome --port 8890
```

## After Install

Health check:

```bash
curl http://127.0.0.1:8888/api/health
```

AI usage guide:

```text
../../docs/ai-user-guide.md
```

Use that guide after deployment to learn the HTTP and MCP workflows for browsing, searching, screenshots, coordinates, sessions, tabs, and engine selection.

Logs if installed as a systemd user service:

```bash
journalctl --user -u ai-browser.service -f
```

Native OS verification must run from an X11 desktop session:

```bash
./venv/bin/python -m pytest tests/e2e/test_native_engine.py -m native -v
```

## Notes

- The installer defaults to `AI_BROWSER_HOST=127.0.0.1` for safety.
- Native OS fallback requires `DISPLAY` and often `XAUTHORITY`.
- Do not expose the HTTP service directly to an untrusted network.
