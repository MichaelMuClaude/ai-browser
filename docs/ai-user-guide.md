# AI User Guide

This guide is for AI agents using `ai-browser` to browse the web, inspect pages, and interact with sites.

Use this as the first reference when you need to operate the browser rather than modify the project.

## Mental Model

`ai-browser` is a local browser daemon:

- HTTP API is the main shared state interface.
- MCP server is a protocol adapter that calls the HTTP API.
- CLI is mostly for human debugging.
- All interfaces control the same browser state when they point at the same HTTP service.

Default HTTP service:

```text
http://127.0.0.1:8888
```

## Before You Start

Check health:

```bash
curl http://127.0.0.1:8888/api/health
```

Good response:

```json
{
  "status": "ok",
  "engine": "stealth",
  "stealth_active": true,
  "native_active": true,
  "sessions": 1
}
```

Notes:

- `engine=stealth` is the normal fast path.
- `native_active=false` means Native OS fallback is not currently available, usually because X11 is unavailable or native startup failed.
- Stealth can still browse most normal pages even if native is inactive.

## Basic HTTP Workflow

Navigate:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/navigate \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.google.com/search?q=OpenAI+API+models"}'
```

Read title:

```bash
curl -sS http://127.0.0.1:8888/api/title
```

Read page text:

```bash
curl -sS http://127.0.0.1:8888/api/text
```

Take screenshot:

```bash
curl -sS -o /tmp/ai-browser.png http://127.0.0.1:8888/api/screenshot
```

Take annotated screenshot:

```bash
curl -sS -D /tmp/ai-browser.headers \
  -o /tmp/ai-browser-annotated.png \
  http://127.0.0.1:8888/api/screenshot_annotated
```

Read element count and coordinate list:

```bash
grep -i '^x-element' /tmp/ai-browser.headers
```

## Recommended Search Pattern

For search tasks:

1. Navigate directly to a search URL.
2. Check `blocked`, `engine_used`, and title.
3. Use `/api/text` first.
4. Use `/api/screenshot_annotated` when deciding where to click.
5. Click by coordinates only after inspecting the element list or screenshot.

Example:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/navigate \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.google.com/search?q=site%3Aopenai.com+API+models"}'

curl -sS http://127.0.0.1:8888/api/text

curl -sS -D /tmp/elements.headers \
  -o /tmp/search-annotated.png \
  http://127.0.0.1:8888/api/screenshot_annotated
```

If the first result looks correct in `X-Elements-Text`, click its coordinates:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/click \
  -H 'Content-Type: application/json' \
  -d '{"x":61,"y":181}'
```

## Leveraging Online AI Answer Engines

For question-answering tasks (not just "find a link"), prefer reading an AI-synthesized answer over manually crawling multiple pages. `ai-browser`'s stealth engine has been confirmed to render these correctly:

**Google AI Overview** — a normal Google search often renders an AI-generated summary above the organic results. Navigate to the search URL and read `/api/text`; the overview text is usually included directly, no extra interaction needed:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/navigate \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.google.com/search?q=what+is+the+CVSS+score+of+CVE-2024-3094"}'

curl -sS http://127.0.0.1:8888/api/text
```

If the overview looks truncated or missing on first read, it may still be streaming in — wait briefly and re-read, or use `browser_wait_for_element` / `/api/wait_for_element` with a distinctive text fragment before reading again.

**Perplexity.ai** — a full answer engine that cites sources. Navigate to a search URL with the question as the query, wait for the answer to finish streaming, then read the text:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/navigate \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.perplexity.ai/search?q=what+is+the+CVSS+score+of+CVE-2024-3094"}'

# give the answer a few seconds to stream, or wait_for_element on a stable selector
curl -sS http://127.0.0.1:8888/api/text
```

Why this matters: these answer engines have already synthesized and cited multiple sources, so this is usually faster and more reliable than the "search → click a result → extract → repeat" pattern for open-ended questions. Reserve manual multi-page crawling for tasks that need primary-source verification or structured data extraction (tables, links) rather than a synthesized answer.

## Multi-Source Research With Scratchpad

When a task requires comparing or verifying across multiple primary sources (prices, specs, claims), do not rely on your context window to hold every page. Use the session-scoped scratchpad to clip findings as you go, then synthesize in one final step.

Workflow:

1. `DELETE /api/scratchpad?session_id=...` — clear any leftover entries if reusing a session.
2. Navigate to source A and read it.
3. `POST /api/scratchpad` with the finding; the server stamps it with the current URL and timestamp.
4. Navigate to source B, read it, and add another entry.
5. Repeat for all sources.
6. `GET /api/scratchpad` to retrieve all entries with provenance, then synthesize.

Example:

```bash
# Source A
curl -sS -X POST 'http://127.0.0.1:8888/api/navigate?session_id=research' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://store-a.example.com/widget"}'

curl -sS -X POST 'http://127.0.0.1:8888/api/scratchpad?session_id=research' \
  -H 'Content-Type: application/json' \
  -d '{"content":"Store A price: $49","label":"price"}'

# Source B
curl -sS -X POST 'http://127.0.0.1:8888/api/navigate?session_id=research' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://store-b.example.com/widget"}'

curl -sS -X POST 'http://127.0.0.1:8888/api/scratchpad?session_id=research' \
  -H 'Content-Type: application/json' \
  -d '{"content":"Store B price: $52","label":"price"}'

# Synthesize
curl -sS 'http://127.0.0.1:8888/api/scratchpad?session_id=research'
```

Scratchpad properties:

- Per-session, in-memory only. It disappears when the session is deleted or the daemon restarts.
- No automatic capture — you decide what to save.
- Each entry has `id`, `content`, `url`, `timestamp`, and optional `label`.
- Different sessions are fully isolated; entries never leak between `session_id`s.

## Page Interaction

Click:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/click \
  -H 'Content-Type: application/json' \
  -d '{"x":400,"y":300}'
```

Type:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/type \
  -H 'Content-Type: application/json' \
  -d '{"text":"hello world"}'
```

Press key:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/key \
  -H 'Content-Type: application/json' \
  -d '{"key":"Enter"}'
```

Scroll:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/scroll \
  -H 'Content-Type: application/json' \
  -d '{"direction":"down","amount":3}'
```

Forms:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/select \
  -H 'Content-Type: application/json' \
  -d '{"selector":"#country","value":"AU"}'

curl -sS -X POST http://127.0.0.1:8888/api/check \
  -H 'Content-Type: application/json' \
  -d '{"selector":"#agree"}'

curl -sS -X POST http://127.0.0.1:8888/api/uncheck \
  -H 'Content-Type: application/json' \
  -d '{"selector":"#subscribe"}'
```

## Data Extraction

Links:

```bash
curl -sS http://127.0.0.1:8888/api/links
```

Table:

```bash
curl -sS 'http://127.0.0.1:8888/api/table?index=0'
```

Metadata:

```bash
curl -sS http://127.0.0.1:8888/api/meta
```

Execute JavaScript:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/execute_js \
  -H 'Content-Type: application/json' \
  -d '{"script":"document.title"}'
```

Use JavaScript sparingly. Prefer built-in extraction endpoints when possible.

## Sessions And Tabs

**You almost never need to create a session.** The default session is shared
across all agents and is sufficient for browsing, searching, and reading pages.
Non-default sessions auto-expire after 10 minutes of inactivity and are capped
at 3 concurrent instances — they exist for rare cases where you need fully
isolated cookies/profile state (e.g., logging into different accounts
simultaneously).

Do NOT invent `session_id` values for "different topics" or "different search
queries" — that creates unnecessary Chrome instances that pile up. Just use the
default session for everything unless you have a specific reason to isolate.

### Creating an isolated session (advanced — rarely needed)

Only use explicit sessions when you need isolated login/cookie state.
Sessions auto-expire after 10 minutes idle.

Create session:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"research-task"}'
```

Navigate in that session:

```bash
curl -sS -X POST 'http://127.0.0.1:8888/api/navigate?session_id=research-task' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

Open tab:

```bash
curl -sS -X POST 'http://127.0.0.1:8888/api/tabs?session_id=research-task' \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

List tabs:

```bash
curl -sS 'http://127.0.0.1:8888/api/tabs?session_id=research-task'
```

Switch tab:

```bash
curl -sS -X POST 'http://127.0.0.1:8888/api/tabs/tab-1/activate?session_id=research-task'
```

## MCP Workflow

The MCP server is useful for MCP-compatible clients. It expects the HTTP service to already be running.

Start MCP server:

```bash
cd /path/to/ai-browser
./venv/bin/python -m src.main mcp
```

Typical MCP tool order:

1. `browser_navigate`
2. `browser_get_text`
3. `browser_screenshot_annotated`
4. `browser_click` / `browser_type` / `browser_press_key`
5. `browser_get_url` / `browser_get_title`

Important MCP tools:

| Tool | Use |
|------|-----|
| `browser_navigate` | Open a URL |
| `browser_get_text` | Read visible text |
| `browser_screenshot_annotated` | Get screenshot plus clickable coordinates |
| `browser_click` | Click coordinates |
| `browser_type` | Type into focused element |
| `browser_press_key` | Press Enter, Tab, Escape, arrows |
| `browser_execute_js` | Evaluate JavaScript |
| `browser_extract_links` | Extract links |
| `browser_extract_table` | Extract table data |
| `browser_get_meta` | Extract page metadata |
| `browser_open_tab` | Open a new tab |
| `browser_switch_tab` | Switch active tab |
| `browser_list_tabs` | List tabs |
| `browser_scratchpad_add` | Save a research note tagged with current URL |
| `browser_scratchpad_get` | Retrieve all saved research notes |
| `browser_scratchpad_clear` | Clear all saved research notes |

All MCP tools support optional `session_id` where relevant.

## Engine Guidance

Use `stealth` for normal browsing:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/engine \
  -H 'Content-Type: application/json' \
  -d '{"engine":"stealth"}'
```

Use `native` only when a site blocks CDP-style automation and the machine has X11:

```bash
curl -sS -X POST http://127.0.0.1:8888/api/engine \
  -H 'Content-Type: application/json' \
  -d '{"engine":"native"}'
```

Native limitations:

- It can click, type, scroll, press keys, and screenshot.
- It cannot reliably read page text or DOM.
- Coordinate-based operation matters more.
- It needs `DISPLAY` / X11.

## Interpreting Block Signals

`/api/navigate` returns block metadata:

```json
{
  "blocked": false,
  "block_confidence": 0,
  "block_signals": [],
  "engine_used": "stealth",
  "fallback_triggered": false
}
```

If `blocked=true` or `fallback_triggered=true`:

- Inspect title/text/screenshot.
- Try waiting or reloading.
- If native is active, use screenshots and coordinates instead of DOM/text endpoints.
- Do not assume DataDome or strong bot protection is bypassable; the current project treats some of these as known limits.

## Practical Rules For AIs

- Always check health before a task.
- Prefer `/api/text` for summarizing and search result reading.
- Prefer `/api/screenshot_annotated` before clicking.
- Do not click blindly.
- Keep service bound to localhost unless the owner explicitly configured secure remote access.
- **Do NOT invent `session_id`.** Always use the default session (omit the
  parameter). Non-default sessions are for rare isolation needs and auto-expire
  after 10 min idle.
- Update `DEVLOG.md` after meaningful project changes, not after ordinary browsing.
- If the browser behaves strangely, capture:
  - `/api/health`
  - `/api/title`
  - `/api/url`
  - `/api/screenshot`
  - failing command and response

## Known Caveats

- The browser service may need a restart after code changes.
- Native OS fallback requires a real X11 environment.
- Some sites still block both stealth and native approaches.
- `screenshot_annotated` returns coordinate text in the `X-Elements-Text` header for PNG mode; for richer structured element data use `format=base64`.

