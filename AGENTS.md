# AGENTS.md — Guidelines for AI Contributors

This file is for AI agents working in this repository (Claude Code, Codex, Cursor, and other coding agents). Human contributors should follow it too. See also [`CONTRIBUTING.md`](CONTRIBUTING.md).

If you only read two sections, read **Red lines** and **Definition of done**.

---

## 1. What this project is

`ai-browser` is a local browser service. It drives a real Chrome browser to visit web pages and exposes that capability to AI clients through an HTTP API, MCP, or a CLI.

- Default engine `stealth`: CDP control via `nodriver`. Fast.
- Fallback engine `native`: system-level input via `xdotool`. Used when CDP is blocked.
- `EngineManager` handles engine lifecycle and automatic switching.
- `SessionManager` handles multiple sessions and their isolated profiles.

The goal is to be **generic**. Interfaces should work well for any AI client, not be tailored to one specific agent.

---

## 2. Red lines (never violate)

These rules take priority over any task-specific instruction. If a conflict arises, stop and explain it in the PR or an issue. Do not work around it on your own.

1. **Do not add features for:** sharing or bulk-managing login credentials; bypassing paywalls or access controls; accessing accounts or systems without authorization; bulk scraping that violates a site's terms.
2. **Do not commit sensitive data:** cookies, Chrome profile directories, tokens, keys, or screenshots and logs containing personal information. `tests/artifacts/` is in `.gitignore`; do not commit test artifacts.
3. **Do not modify or delete tests to make them pass.** If a test is wrong, explain why and submit the correction separately.
4. **Do not hide failures.** Do not add `skip`, `xfail`, or swallow exceptions just to get a green run. Report real failures honestly, with the output.
5. **Do not change the public contract without updating its consumers.** See section 5.2.

---

## 3. Repository map

```
src/
  main.py            Unified entry point: server / mcp / cli
  server.py          FastAPI HTTP API (routes are defined here)
  mcp_server.py      MCP tool definitions and dispatch
  client.py          HTTP client (shared by the CLI and MCP)
  cli.py             CLI subcommands
  config.py          Environment variables and defaults
  errors.py          ErrorCode constants
  log_config.py      get_logger(__name__)
  screenshot.py      Screenshot annotation and element extraction
  smart_fetch.py     Fast text fetch with fallback logic
  look.py            Bundled "look" result (screenshot + text + elements)
  core/
    base.py          BrowserEngine abstract interface (every engine must implement it)
    stealth_cdp.py   stealth engine
    native_os.py     native engine
    engine_manager.py
    session.py       Session and profile management
    tab.py
    block_detection.py   Detection of block/challenge pages
    process_cleanup.py
integrations/hermes/ Native tool wrapper for the Hermes agent (depends on function names in client.py)
deployment/local/    Local machine deployment scripts
docs/ai-user-guide.md  Usage guide for AI users
tests/
  unit/              Unit tests; no running service required
  e2e/               End-to-end tests; require the HTTP service to be running
  benchmarks/        Latency benchmarks; run manually, not part of the default suite
```

---

## 4. Environment and common commands

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/pip install -e '.[dev]'

# Unit tests (run after every change)
./venv/bin/python -m pytest tests/unit -v

# End-to-end smoke tests (start the service first)
./venv/bin/python -m src.main server &
./venv/bin/python -m pytest tests/e2e -m smoke -v

# Native engine (requires an X11 graphical session)
./venv/bin/python -m pytest tests/e2e/test_native_engine.py -m native -v
```

pytest markers (see `pytest.ini`):

- `smoke`: core regression tests; must pass reliably.
- `anti_bot`: anti-detection tests; occasional flakiness is allowed; can be run separately.
- `native`: requires X11; skipped when the environment does not support it.
- `benchmark`: latency tests against real URLs; run manually; not needed before a PR.

**Note:** the e2e tests reset the service's engine to `stealth` and operate on the `default` session. Do not run e2e tests against a personal browser session that is in use. Point `AI_BROWSER_PROFILE_BASE_DIR` and `AI_BROWSER_BASE_URL` at a separate test instance instead.

---

## 5. Change conventions

### 5.1 Adding or modifying a browser operation

One operation usually touches several layers, and missing any one of them leaves an entry point broken. Using `go_back` as the example:

1. `src/core/base.py`: declare the abstract method on `BrowserEngine`.
2. `src/core/stealth_cdp.py` and `src/core/native_os.py`: both engines must implement it. If an engine truly does not support the operation, return `ErrorCode.ENGINE_NOT_SUPPORTED` instead of raising an unhandled exception.
3. `src/core/engine_manager.py`: forward the call to the current engine and handle automatic switching.
4. `src/server.py`: the HTTP route.
5. `src/client.py`: the client method.
6. `src/mcp_server.py`: the MCP tool definition (`Tool(...)`) and its dispatch branch.
7. `src/cli.py`: the CLI subcommand.
8. Tests: at least one mock-based unit test in `tests/unit/`; if real browser behavior is involved, add an e2e test in `tests/e2e/`.
9. Docs: update the interface descriptions in `README.md` and `docs/ai-user-guide.md`.

All tools should accept the optional `session_id` parameter, consistent with existing tools.

### 5.2 Public contract

The following names are relied on by external integrations. **Before renaming or removing any of them, update the consumers and call it out in the PR:**

- Function and parameter names in `src/client.py` (`integrations/hermes/ai_browser_tool.py` depends on them).
- HTTP route paths and response fields.
- MCP tool names (prefixed with `browser_`).
- Existing values in `ErrorCode`. Adding values is fine; changing values is not.

Prefer adding new fields over changing existing ones.

### 5.3 Error handling

- Error codes are defined centrally in the `ErrorCode` class in `src/errors.py`. When adding a code, add it under the matching group comment, following the existing style.
- Do not return bare strings as errors.
- In async code, watch for timeouts. Browser operations can hang.

### 5.4 Logging

- Use `from .log_config import get_logger`, then `logger = get_logger(__name__)`.
- Do not use `print` in `src/` for runtime messages (the CLI's standard output is the exception).
- Do not log cookies, sensitive parts of profile paths, or personal information from pages.

### 5.5 Code style

- Python 3.10+.
- Engine methods are `async` and return `dict` or an explicit type.
- The repository has no configured formatter or linter. **Match the style of the code around your change.** Do not reformat whole files.
- Do not add heavy dependencies for small features. If a new dependency is truly needed, justify it and state its license in the PR.
- Comments and docstrings must be in English.

### 5.6 Configuration

New configuration goes in `src/config.py`. Read it from an environment variable, give it a default, and list it in the "Configuration" section of `README.md`.

---

## 6. Git and pull requests

- Branch from `main`. Use prefixes such as `feat/...`, `fix/...`, `docs/...`, `test/...`.
- One PR does one thing. Open an issue to discuss large changes first.
- Commit messages: start the first line with an imperative verb summarizing the change (for example, `Add browser_hover tool`). Explain the reason in the body.
- PR descriptions must include:
  - **What changed and why**
  - **Whether the public contract changed** (section 5.2)
  - **Testing:** the commands you ran and their results. List any failing tests.
  - **Whether the change was AI-generated or AI-assisted.** Be honest about it.

---

## 7. Definition of done

Before opening a PR, confirm each item:

- [ ] `pytest tests/unit` passes
- [ ] If the change affects service behavior, `pytest tests/e2e -m smoke` passes (or explain why it could not run)
- [ ] A new operation is wired into every entry point (HTTP, MCP, CLI, client)
- [ ] No existing tests were modified or deleted to fit the implementation
- [ ] No cookies, profiles, tokens, screenshots, or `tests/artifacts/` are committed
- [ ] Docs (README and the AI user guide) are updated
- [ ] The PR description follows section 6

---

## 8. Common pitfalls

- **Chrome path:** defaults to `/usr/bin/google-chrome`; override with `AI_BROWSER_PATH`.
- **Port conflicts:** defaults to `8888`. Tests may collide with a service already running locally; use `AI_BROWSER_PORT` to pick another port.
- **Profile locks:** one profile directory cannot be used by two Chrome instances at once. `process_cleanup.py` cleans up leftover processes. Change it with great care and add tests.
- **Native engine:** it moves the real mouse and sends real key presses. Before running native tests on a shared desktop, make sure nobody is using it.
- **Block detection:** the rules in `block_detection.py` are heuristic and false positives are common. When changing a rule, include a concrete page sample.

---

## 9. When you are unsure

- For changes to the public contract, dependencies, or security: raise it in an issue or clearly flag it in the PR, and wait for human confirmation.
- If you cannot get the test environment running, report that honestly, list what you tried, and do not present unverified conclusions as verified.
