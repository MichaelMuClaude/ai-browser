#!/usr/bin/env python3
"""CLI interface for ai-browser — fully rewritten to use BrowserClient."""
import argparse
import asyncio
import json
import os
import sys

from .client import BrowserClient, BrowserClientError


def _run(coro):
    """Run an async coroutine from sync context."""
    return asyncio.run(coro)


# ── helper: wrap client method calls with optional session_id ──────────────

def _invoke(client, method_name, session_id, *args, **kwargs):
    """Call a BrowserClient async method, injecting session_id."""
    # Methods that don't accept session_id parameter
    no_session_methods = {'health', 'list_sessions'}
    if method_name in no_session_methods:
        return _run(getattr(client, method_name)())
    if session_id is not None:
        kwargs['session_id'] = session_id
    return _run(getattr(client, method_name)(*args, **kwargs))


def _print_look_result(args, result):
    """Print a look bundle result: save screenshot, print text, metadata to stderr."""
    if not args.no_screenshot:
        path = args.output or "/tmp/ai-browser-look.png"
        screenshot = result.get("screenshot_base64")
        if screenshot:
            import base64
            with open(path, "wb") as f:
                f.write(base64.b64decode(screenshot))
            print(f"Screenshot saved to {path}")
    # 2026-07-23: print click/nav warnings (click_target_warning,
    # possible_new_tab) to stdout, front and center — not just buried in the
    # stderr metadata dump — so a caller can't get a silent 200 for a click
    # that actually did nothing (the real feedback case this fixes: click
    # landed on non-interactive content, page never navigated, no error).
    if result.get("click_target_warning"):
        print(f"⚠️  {result['click_target_warning']}")
    if result.get("possible_new_tab"):
        print("⚠️  URL unchanged after click — may have opened in a new tab, or the click was a no-op. Use `ai-browser tabs` to check for a new tab.")
    text = result.get("text", "")
    if text:
        print(text)
    metadata = {
        k: v for k, v in result.items()
        if k not in ("screenshot_base64", "text")
    }
    print(json.dumps(metadata, indent=2, ensure_ascii=False), file=sys.stderr)


# ── command handlers ────────────────────────────────────────────────────────

def cmd_health(args, client):
    result = _invoke(client, 'health', None)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_create_session(args, client):
    result = _run(client.create_session(args.session_id or None))
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_list_sessions(args, client):
    # list_sessions() doesn't accept session_id param in BrowserClient
    result = _invoke(client, 'list_sessions', None)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_delete_session(args, client):
    if not args.session_id:
        print("Error: delete-session requires a session_id argument.", file=sys.stderr)
        sys.exit(1)
    result = _run(client.delete_session(args.session_id))
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_navigate(args, client):
    result = _invoke(client, 'navigate', args.session, args.url, return_look=not args.no_look,
                     main_content_only=getattr(args, 'main_content_only', False))
    if args.no_look:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_look_result(args, result)


def cmd_go_back(args, client):
    result = _invoke(client, 'go_back', args.session, return_look=not args.no_look)
    if args.no_look:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_look_result(args, result)


def cmd_go_forward(args, client):
    result = _invoke(client, 'go_forward', args.session, return_look=not args.no_look)
    if args.no_look:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_look_result(args, result)


def cmd_reload(args, client):
    result = _invoke(client, 'reload', args.session, return_look=not args.no_look)
    if args.no_look:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_look_result(args, result)


def cmd_screenshot(args, client):
    png_bytes = _invoke(client, 'screenshot', args.session)
    path = args.output or "/tmp/ai-browser-screenshot.png"
    with open(path, "wb") as f:
        f.write(png_bytes)
    print(f"Screenshot saved to {path}")


def cmd_screenshot_annotated(args, client):
    png_bytes, elements_text, element_count = _invoke(client, 'screenshot_annotated', args.session)
    path = args.output or "/tmp/ai-browser-screenshot-annotated.png"
    with open(path, "wb") as f:
        f.write(png_bytes)
    print(f"Screenshot saved to {path}")
    print(f"Element count: {element_count}")
    if elements_text:
        print(f"Elements text:\n{elements_text}")


def cmd_look(args, client):
    result = _invoke(client, 'look', args.session, max_length=args.max_length)
    if not args.no_screenshot:
        path = args.output or "/tmp/ai-browser-look.png"
        screenshot = result.get("screenshot_base64")
        if screenshot:
            import base64
            with open(path, "wb") as f:
                f.write(base64.b64decode(screenshot))
            print(f"Screenshot saved to {path}")
    text = result.get("text", "")
    if text:
        print(text)
    metadata = {
        k: v for k, v in result.items()
        if k not in ("screenshot_base64", "text")
    }
    import json
    print(json.dumps(metadata, indent=2, ensure_ascii=False), file=sys.stderr)


def cmd_click(args, client):
    result = _invoke(client, 'click', args.session, args.x, args.y, return_look=not args.no_look)
    if args.no_look:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_look_result(args, result)


def cmd_type_text(args, client):
    result = _invoke(client, 'type_text', args.session, args.text)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_scroll(args, client):
    result = _invoke(client, 'scroll', args.session,
                     direction=args.direction, amount=args.amount,
                     return_look=not args.no_look)
    if args.no_look:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_look_result(args, result)


def cmd_press_key(args, client):
    result = _invoke(client, 'press_key', args.session, args.key)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_select(args, client):
    # Both selector and value as positional to stay consistent with check/uncheck style
    result = _invoke(client, 'select', args.session, args.selector, args.value)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_check(args, client):
    result = _invoke(client, 'check', args.session, args.selector)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_uncheck(args, client):
    result = _invoke(client, 'uncheck', args.session, args.selector)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_get_text(args, client):
    result = _invoke(client, 'get_text', args.session,
                     main_content_only=getattr(args, 'main_content_only', False))
    print(result.get("text", "")[:5000])


def cmd_get_title(args, client):
    result = _invoke(client, 'get_title', args.session)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_get_url(args, client):
    result = _invoke(client, 'get_url', args.session)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_set_engine(args, client):
    result = _invoke(client, 'set_engine', args.session, args.name)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_execute_js(args, client):
    if args.stdin:
        script = sys.stdin.read()
    elif args.script:
        script = args.script
    else:
        print("Error: execute-js requires --script SCRIPT or --stdin", file=sys.stderr)
        sys.exit(1)
    result = _invoke(client, 'execute_js', args.session, script)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_wait_for_element(args, client):
    if not args.text and not args.selector:
        print("Error: wait-for-element requires at least --text or --selector.", file=sys.stderr)
        sys.exit(1)
    result = _invoke(client, 'wait_for_element', args.session,
                     text=args.text, selector=args.selector, timeout=args.timeout)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_find_element(args, client):
    result = _invoke(client, 'find_element', args.session, args.text, element_type=args.type)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_search(args, client):
    result = _invoke(client, 'search', args.session, args.query,
                     engine=args.engine, return_look=not args.no_look,
                     main_content_only=getattr(args, 'main_content_only', False))
    if args.no_look:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_look_result(args, result)


def cmd_smart_fetch(args, client):
    result = _invoke(
        client,
        'smart_fetch',
        args.session,
        args.url,
        max_length=args.max_length,
        timeout=args.timeout,
        fallback_screenshot=args.fallback_screenshot,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result.get("text", ""))
        metadata = {
            k: v for k, v in result.items()
            if k != "text"
        }
        print(json.dumps(metadata, indent=2, ensure_ascii=False), file=sys.stderr)


def cmd_extract_links(args, client):
    result = _invoke(client, 'extract_links', args.session,
                     filter_text=args.filter if hasattr(args, 'filter') and args.filter else None,
                     same_domain_only=args.same_domain,
                     with_context=getattr(args, 'with_context', False))
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_extract_table(args, client):
    result = _invoke(client, 'extract_table', args.session, index=args.index)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_get_meta(args, client):
    result = _invoke(client, 'get_meta', args.session)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_open_tab(args, client):
    url = args.url if args.url else "about:blank"
    result = _invoke(client, 'open_tab', args.session, url, return_look=not args.no_look)
    if args.no_look:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_look_result(args, result)


def cmd_switch_tab(args, client):
    result = _invoke(client, 'switch_tab', args.session, args.tab_id, return_look=not args.no_look)
    if args.no_look:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_look_result(args, result)


def cmd_close_tab(args, client):
    result = _invoke(client, 'close_tab', args.session, args.tab_id)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_list_tabs(args, client):
    result = _invoke(client, 'list_tabs', args.session)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_get_cookies(args, client):
    domain = args.domain if hasattr(args, 'domain') and args.domain else ""
    result = _invoke(client, 'get_cookies', args.session, domain=domain)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_set_cookie(args, client):
    if not args.domain:
        print("Error: set-cookie requires --domain DOMAIN.", file=sys.stderr)
        sys.exit(1)
    expires = args.expires if hasattr(args, 'expires') and args.expires is not None else -1
    result = _invoke(client, 'set_cookie', args.session,
                     args.name, args.value, args.domain,
                     path=args.path or "/",
                     expires=expires,
                     secure=args.secure,
                     http_only=args.http_only)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_clear_cookies(args, client):
    domain = args.domain if hasattr(args, 'domain') and args.domain else ""
    result = _invoke(client, 'clear_cookies', args.session, domain=domain)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_scratchpad_add(args, client):
    result = _invoke(client, 'scratchpad_add', args.session, args.content, label=args.label)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_scratchpad_show(args, client):
    result = _invoke(client, 'scratchpad_get', args.session)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_scratchpad_clear(args, client):
    result = _invoke(client, 'scratchpad_clear', args.session)
    print(json.dumps(result, indent=2, ensure_ascii=False))



def _extract_session():
    """Extract --session from sys.argv and remove it in-place.

    Returns the session value or None. Modifies sys.argv so argparse
    never sees --session, solving the positional-arg-after issue.
    Supports both ``--session VALUE`` and ``--session=VALUE``.
    """
    i = 0
    session = None
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == '--session':
            if i + 1 < len(sys.argv):
                session = sys.argv[i + 1]
                sys.argv.pop(i)
                sys.argv.pop(i)  # value shifts down after first removal
            else:
                print("error: argument --session: expected one argument", file=sys.stderr)
                sys.exit(2)
            continue
        if arg.startswith('--session='):
            session = arg.split('=', 1)[1]
            sys.argv.pop(i)
            continue
        i += 1
    return session


def main():
    parser = argparse.ArgumentParser(
        description="AI Browser CLI — controls ai-browser via HTTP API."
    )
    parser.add_argument("--session", default=None, help="Session ID for the browser session")

    # Remove --session from sys.argv before argparse runs so users can place it
    # anywhere on the command line (including after subcommand positional args).
    session_from_flags = _extract_session()

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # --- lifecycle ---
    p = sub.add_parser("health", help="Check server health status")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("create-session", help="Create a new browser session")
    p.add_argument("session_id", nargs="?", default=None, help="Session ID (optional)")
    p.set_defaults(func=cmd_create_session)

    p = sub.add_parser("list-sessions", help="List active sessions")
    p.set_defaults(func=cmd_list_sessions)

    p = sub.add_parser("delete-session", help="Delete a browser session")
    p.add_argument("session_id", help="Session ID to delete")
    p.set_defaults(func=cmd_delete_session)

    # --- navigation ---
    p = sub.add_parser("navigate", help="Navigate to a URL")
    p.add_argument("url", help="URL to navigate to")
    p.add_argument("--no-look", action="store_true", help="Return only the action result, no look bundle")
    p.add_argument("-o", "--output", default=None, help="Screenshot output path (default: /tmp/ai-browser-look.png)")
    p.add_argument("--no-screenshot", action="store_true", help="Skip saving the screenshot from the look bundle")
    p.add_argument("--main-content-only", action="store_true", help="Skip nav/header/footer boilerplate in the returned text")
    p.set_defaults(func=cmd_navigate)

    p = sub.add_parser("go-back", help="Go back in browser history")
    p.add_argument("--no-look", action="store_true", help="Return only the action result, no look bundle")
    p.add_argument("-o", "--output", default=None, help="Screenshot output path (default: /tmp/ai-browser-look.png)")
    p.add_argument("--no-screenshot", action="store_true", help="Skip saving the screenshot from the look bundle")
    p.set_defaults(func=cmd_go_back)

    p = sub.add_parser("go-forward", help="Go forward in browser history")
    p.add_argument("--no-look", action="store_true", help="Return only the action result, no look bundle")
    p.add_argument("-o", "--output", default=None, help="Screenshot output path (default: /tmp/ai-browser-look.png)")
    p.add_argument("--no-screenshot", action="store_true", help="Skip saving the screenshot from the look bundle")
    p.set_defaults(func=cmd_go_forward)

    p = sub.add_parser("reload", help="Reload the current page")
    p.add_argument("--no-look", action="store_true", help="Return only the action result, no look bundle")
    p.add_argument("-o", "--output", default=None, help="Screenshot output path (default: /tmp/ai-browser-look.png)")
    p.add_argument("--no-screenshot", action="store_true", help="Skip saving the screenshot from the look bundle")
    p.set_defaults(func=cmd_reload)

    # --- tabs ---
    p = sub.add_parser("open-tab", help="Open a new tab")
    p.add_argument("url", nargs="?", default="about:blank", help="URL (default: about:blank)")
    p.add_argument("--no-look", action="store_true", help="Return only the action result, no look bundle")
    p.add_argument("-o", "--output", default=None, help="Screenshot output path (default: /tmp/ai-browser-look.png)")
    p.add_argument("--no-screenshot", action="store_true", help="Skip saving the screenshot from the look bundle")
    p.set_defaults(func=cmd_open_tab)

    p = sub.add_parser("switch-tab", help="Switch to a tab")
    p.add_argument("tab_id", help="Tab ID to switch to")
    p.add_argument("--no-look", action="store_true", help="Return only the action result, no look bundle")
    p.add_argument("-o", "--output", default=None, help="Screenshot output path (default: /tmp/ai-browser-look.png)")
    p.add_argument("--no-screenshot", action="store_true", help="Skip saving the screenshot from the look bundle")
    p.set_defaults(func=cmd_switch_tab)

    p = sub.add_parser("close-tab", help="Close a tab")
    p.add_argument("tab_id", help="Tab ID to close")
    p.set_defaults(func=cmd_close_tab)

    p = sub.add_parser("list-tabs", help="List all open tabs")
    p.set_defaults(func=cmd_list_tabs)

    # --- page inspection ---
    p = sub.add_parser("title", help="Get page title (JSON)")
    p.set_defaults(func=cmd_get_title)

    p = sub.add_parser("url", help="Get current URL (JSON)")
    p.set_defaults(func=cmd_get_url)

    p = sub.add_parser("text", help="Get page text (first 5000 chars)")
    p.add_argument("--main-content-only", action="store_true", help="Skip nav/header/footer boilerplate")
    p.set_defaults(func=cmd_get_text)

    p = sub.add_parser("get-meta", help="Extract page metadata")
    p.set_defaults(func=cmd_get_meta)

    # --- screenshot ---
    p = sub.add_parser("screenshot", help="Take a raw screenshot")
    p.add_argument("-o", "--output", default=None, help="Output file path (default: /tmp/ai-browser-screenshot.png)")
    p.set_defaults(func=cmd_screenshot)

    p = sub.add_parser("screenshot-annotated", help="Take an annotated screenshot with element list")
    p.add_argument("-o", "--output", default=None, help="Output file path (default: /tmp/ai-browser-screenshot-annotated.png)")
    p.set_defaults(func=cmd_screenshot_annotated)

    p = sub.add_parser("look", help="Look at the current page (screenshot + text + elements)")
    p.add_argument("--max-length", type=int, default=20000, help="Max chars of page text (default: 20000)")
    p.add_argument("-o", "--output", default=None, help="Output file path (default: /tmp/ai-browser-look.png)")
    p.add_argument("--no-screenshot", action="store_true", help="Skip saving the screenshot, only print text/metadata")
    p.set_defaults(func=cmd_look)

    # --- interaction ---
    p = sub.add_parser("click", help="Click at x,y coordinates")
    p.add_argument("x", type=int, help="X coordinate")
    p.add_argument("y", type=int, help="Y coordinate")
    p.add_argument("--no-look", action="store_true", help="Return only the action result, no look bundle")
    p.add_argument("-o", "--output", default=None, help="Screenshot output path (default: /tmp/ai-browser-look.png)")
    p.add_argument("--no-screenshot", action="store_true", help="Skip saving the screenshot from the look bundle")
    p.set_defaults(func=cmd_click)

    p = sub.add_parser("type", help="Type text into focused element")
    p.add_argument("text", help="Text to type")
    p.set_defaults(func=cmd_type_text)

    p = sub.add_parser("scroll", help="Scroll the page")
    p.add_argument("-d", "--direction", default="down", choices=["up", "down"], help="Direction (default: down)")
    p.add_argument("-n", "--amount", type=int, default=3, help="Amount (default: 3)")
    p.add_argument("--no-look", action="store_true", help="Return only the action result, no look bundle")
    p.add_argument("-o", "--output", default=None, help="Screenshot output path (default: /tmp/ai-browser-look.png)")
    p.add_argument("--no-screenshot", action="store_true", help="Skip saving the screenshot from the look bundle")
    p.set_defaults(func=cmd_scroll)

    p = sub.add_parser("key", help="Press a key")
    p.add_argument("key", help="Key name (Return, Tab, Escape, etc.)")
    p.set_defaults(func=cmd_press_key)

    p = sub.add_parser("select", help="Select an option in a <select> element")
    p.add_argument("selector", help="CSS selector for the <select>")
    p.add_argument("value", help="Option value or visible text to select")
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("check", help="Check a checkbox/radio button")
    p.add_argument("selector", help="CSS selector for the input element")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("uncheck", help="Uncheck a checkbox")
    p.add_argument("selector", help="CSS selector for the input element")
    p.set_defaults(func=cmd_uncheck)

    # --- engine ---
    p = sub.add_parser("engine", help="Switch browser engine")
    p.add_argument("name", choices=["stealth", "native", "auto"], help="Engine name")
    p.set_defaults(func=cmd_set_engine)

    # --- js / element finding ---
    p = sub.add_parser("execute-js", help="Execute JavaScript on the current page")
    p.add_argument("--script", default=None, help="JavaScript code to execute")
    p.add_argument("--stdin", action="store_true", help="Read JS from stdin (recommended)")
    p.set_defaults(func=cmd_execute_js)

    p = sub.add_parser("wait-for-element", help="Wait for an element to appear")
    p.add_argument("--text", default=None, help="Text content to wait for")
    p.add_argument("--selector", default=None, help="CSS selector to wait for")
    p.add_argument("--timeout", type=int, default=10, help="Timeout in seconds (default: 10)")
    p.set_defaults(func=cmd_wait_for_element)

    p = sub.add_parser("find-element", help="Find an interactive element by text")
    p.add_argument("text", help="Text to search for")
    p.add_argument("--type", default="any", choices=["button", "link", "input", "any"], help="Element type (default: any)")
    p.set_defaults(func=cmd_find_element)

    # --- search / extraction ---
    p = sub.add_parser("search", help="Search and navigate to results")
    p.add_argument("query", help="Search query")
    p.add_argument("--engine", default="google", choices=["google", "bing", "duckduckgo"],
                   help="Search engine (default: google)")
    p.add_argument("--no-look", action="store_true", help="Return only the action result, no look bundle")
    p.add_argument("-o", "--output", help="Screenshot output path (default: /tmp/ai-browser-look.png)")
    p.add_argument("--no-screenshot", action="store_true", help="Skip saving the screenshot from the look bundle")
    p.add_argument("--main-content-only", action="store_true", help="Skip nav/header/footer boilerplate in the returned text")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("smart-fetch", help="One-shot URL fetch with automatic engine selection")
    p.add_argument("url", help="URL to fetch")
    p.add_argument("--max-length", type=int, default=20000, help="Max chars to return (default: 20000)")
    p.add_argument("--timeout", type=int, default=None, help="Claude engine timeout in seconds")
    p.add_argument("--fallback-screenshot", action="store_true", help="Capture screenshot when native engine cannot extract text")
    p.add_argument("--json", action="store_true", help="Output full JSON response to stdout")
    p.set_defaults(func=cmd_smart_fetch)

    p = sub.add_parser("extract-links", help="Extract all links from the page")
    p.add_argument("--filter", default=None, help="Filter by anchor text (or context, if --with-context)")
    p.add_argument("--same-domain", action="store_true", help="Only same-domain links")
    p.add_argument("--with-context", action="store_true", help="Include each link's surrounding card text (title/price/rating) as `context`")
    p.set_defaults(func=cmd_extract_links)

    p = sub.add_parser("extract-table", help="Extract a table as structured data")
    p.add_argument("--index", type=int, default=0, help="Table index (default: 0)")
    p.set_defaults(func=cmd_extract_table)

    # --- cookies ---
    p = sub.add_parser("get-cookies", help="Get browser cookies")
    p.add_argument("--domain", default="", help="Filter by domain")
    p.set_defaults(func=cmd_get_cookies)

    p = sub.add_parser("set-cookie", help="Set a browser cookie")
    p.add_argument("name", help="Cookie name")
    p.add_argument("value", help="Cookie value")
    p.add_argument("--domain", required=True, help="Cookie domain (required)")
    p.add_argument("--path", default="/", help="Cookie path (default: /)")
    p.add_argument("--expires", type=float, default=-1, help="Expiry timestamp, -1=session (default: -1)")
    p.add_argument("--secure", action="store_true", help="HTTPS only")
    p.add_argument("--http-only", action="store_true", help="HttpOnly flag")
    p.set_defaults(func=cmd_set_cookie)

    p = sub.add_parser("clear-cookies", help="Clear browser cookies")
    p.add_argument("--domain", default="", help="Filter by domain (empty=all)")
    p.set_defaults(func=cmd_clear_cookies)

    # --- scratchpad ---
    p = sub.add_parser("scratchpad", help="Session-scoped research scratchpad")
    scratch_sub = p.add_subparsers(dest="scratch_command", help="Scratchpad commands")

    p_add = scratch_sub.add_parser("add", help="Add a note tagged with current URL")
    p_add.add_argument("content", help="Note content")
    p_add.add_argument("--label", default=None, help="Optional label")
    p_add.set_defaults(func=cmd_scratchpad_add)

    p_show = scratch_sub.add_parser("show", help="Show all scratchpad entries")
    p_show.set_defaults(func=cmd_scratchpad_show)

    p_clear = scratch_sub.add_parser("clear", help="Clear all scratchpad entries")
    p_clear.set_defaults(func=cmd_scratchpad_clear)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Apply the session value extracted before argparse ran.
    if session_from_flags is not None:
        args.session = session_from_flags

    base_url = os.environ.get("AI_BROWSER_BASE_URL", "http://127.0.0.1:8888")

    try:
        client = BrowserClient(base_url=base_url)
        args.func(args, client)
    except (BrowserClientError, OSError) as e:
        if "Cannot connect" in str(e) or "Request to" in str(e):
            print(f"Error: Cannot connect to ai-browser server at {base_url}. Is it running?", file=sys.stderr)
        else:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
