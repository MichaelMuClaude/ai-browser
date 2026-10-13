"""Vision-first "look" bundle helpers.

A look is one atomic call that returns: annotated screenshot + page text + title +
URL + clickable elements + light block detection. It is the core primitive of the
vision-first browsing model.
"""
import asyncio
import base64
import json
import re
from urllib.parse import urlsplit

from .config import NAVIGATE_LOAD_MAX_POLLS, NAVIGATE_LOAD_POLL_INTERVAL
from .core.block_detection import detect_block
from .core.stealth_cdp import StealthEngine
from .screenshot import annotate_screenshot, extract_elements_js, format_elements_text


async def _settle(engine, action: str = "default") -> list[dict]:
    """Wait for the page to stabilize after an action.

    Returns any extra block-detection signals discovered during settle
    (e.g. url_degraded_to_blank).
    """
    if isinstance(engine, StealthEngine):
        if action == "scroll":
            await asyncio.sleep(0.3)
        else:
            for _ in range(NAVIGATE_LOAD_MAX_POLLS):
                try:
                    ready = await engine._evaluate_tolerant("document.readyState")
                    if ready == "complete":
                        break
                except Exception:
                    pass
                await asyncio.sleep(NAVIGATE_LOAD_POLL_INTERVAL)
    else:
        # Native engine cannot reliably query readyState; use a fixed delay.
        await asyncio.sleep(0.3 if action == "scroll" else 1.0)

    signals: list[dict] = []
    # about:blank degradation detection (stealth only — native get_url is always empty).
    if isinstance(engine, StealthEngine):
        try:
            url = await engine.get_url()
            # For open_tab, the new tab legitimately starts at about:blank before
            # navigation begins. Wait a bit longer for the real URL to arrive so we
            # don't emit a false-positive degradation signal.
            if action == "open_tab" and (not url or url == "about:blank"):
                for _ in range(10):
                    await asyncio.sleep(0.1)
                    url = await engine.get_url()
                    if url and url != "about:blank":
                        break
            if not url or url == "about:blank":
                signals.append(
                    {
                        "name": "url_degraded_to_blank",
                        "value": url or "",
                        "confidence": 0.5,
                    }
                )
        except Exception:
            pass

    return signals


# 2026-07-28: real case — searching Google for certain queries (seen with
# CJK-heavy natural-language queries) makes Google render an image-card-heavy
# results layout where document.body.innerText comes back nearly empty (just
# nav/footer chrome), even though the page has real result links with real
# hrefs. Confirmed via a live repro: element_count=29 but extracted text was
# under 150 chars, while the elements array had working hrefs to Taobao/Zhihu/
# Reddit. A caller reading only `text` (the common case) sees what looks like
# a failed/empty search. This is a fallback, not a fix to the extraction JS —
# thin text can't be un-thin, so we surface the links we do have instead.
_THIN_TEXT_LINK_FALLBACK_MIN_LENGTH = 400
_THIN_TEXT_LINK_FALLBACK_MAX_LINKS = 15
# Cap each displayed href so one absurdly long query-string URL can't eat the
# whole fallback budget (flagged in Codex review, 2026-07-28).
_THIN_TEXT_LINK_FALLBACK_MAX_HREF_DISPLAY_LENGTH = 200


def _is_chrome_domain(href: str, exclude_domains: set) -> bool:
    """True if href's hostname exactly matches, or is a subdomain of, one of
    exclude_domains (case-insensitive) — or a regional-TLD variant of a ".com"
    entry (google.com.au, google.co.uk, ...) for the same name.

    Host-based, not substring (Codex review, 2026-07-28): a plain `domain in
    href` check would wrongly exclude a real result whose URL merely contains
    "google.com" in a query string (`?from=google.com`) or a lookalike domain
    (`google.com.evil.example`), and would miss a differently-cased host
    (`WWW.GOOGLE.COM`).

    The regional-TLD check exists because a live re-test (2026-07-28) found a
    real "Google apps" chrome link pointing to google.com.au on a search that
    itself navigated to www.google.com — Google's own chrome can link to any
    of its country TLDs regardless of which one you're currently on. The
    pattern is anchored to the end of the hostname (`$`), so it still doesn't
    match the lookalike-domain case above: "google.com.evil.example" has
    trailing labels after "google.com", so the anchor fails and it's treated
    as a real (non-excluded) result, same as before.
    """
    hostname = (urlsplit(href).hostname or "").lower()
    if not hostname:
        return False
    for domain in (d.lower() for d in exclude_domains):
        if hostname == domain or hostname.endswith("." + domain):
            return True
        if domain.endswith(".com"):
            name = re.escape(domain[: -len(".com")])
            if re.search(rf"(^|\.){name}\.[a-z]{{2,3}}(\.[a-z]{{2,3}})?$", hostname):
                return True
    return False


def _thin_text_link_fallback(
    text: str, elements: list, exclude_domains: set, max_length: int = 20000
) -> str:
    """Append real result links to `text` when the extracted text is too thin
    to be useful but the page has real (non-chrome) links with hrefs.

    `exclude_domains` should contain the search engine's own domain (and any
    of its chrome/account subdomains) so the fallback lists actual result
    links, not "Go to Google Home" / "Sign in" style navigation. Only http/https
    hrefs are considered — `javascript:`/`mailto:`/`tel:` anchors aren't real
    result links. The final text is capped to `max_length` so the fallback
    can't push a "thin" (and therefore never-truncated) text past the same
    budget `_capture_look()` already enforces on the original text.
    """
    if len(text.strip()) >= _THIN_TEXT_LINK_FALLBACK_MIN_LENGTH:
        return text

    links = []
    seen_hrefs = set()
    for el in elements:
        href = (el.get("href") or "").strip()
        if not href or href in seen_hrefs:
            continue
        if urlsplit(href).scheme.lower() not in ("http", "https"):
            continue
        if _is_chrome_domain(href, exclude_domains):
            continue
        seen_hrefs.add(href)
        label = (el.get("text") or "").strip()
        display_href = href
        if len(display_href) > _THIN_TEXT_LINK_FALLBACK_MAX_HREF_DISPLAY_LENGTH:
            display_href = display_href[:_THIN_TEXT_LINK_FALLBACK_MAX_HREF_DISPLAY_LENGTH] + "…"
        links.append(f"- {label or '(no title)'}: {display_href}")
        if len(links) >= _THIN_TEXT_LINK_FALLBACK_MAX_LINKS:
            break

    if not links:
        return text

    fallback_note = (
        "\n\n[ai-browser note: extracted page text was sparse (possibly an "
        "image-card results layout) — real result links found on the page:]\n"
        + "\n".join(links)
    )
    return (text + fallback_note)[:max_length]


def _active_tab_id(engine) -> str:
    """Resolve the active tab_id for a stealth engine."""
    if isinstance(engine, StealthEngine):
        for tab_id, tab in engine._tabs.items():
            if tab is engine.tab:
                return tab_id
    return "unknown"


async def _capture_look(
    session, max_length: int = 20000, main_content_only: bool = False
) -> dict:
    """Capture a full look bundle for the current tab of the given session.

    Args:
        session: BrowserSession object.
        max_length: Maximum characters of page text to return.
        main_content_only: skip nav/header/footer boilerplate in `text` (stealth
            engine only — see StealthEngine.get_text()).

    Returns:
        Dict with screenshot_base64, text, title, url, elements, block, etc.
    """
    engine = session.engine_manager.get_engine()
    engine_name = "stealth" if isinstance(engine, StealthEngine) else "native"

    # Screenshot is available on both engines.
    raw_screenshot = await engine.screenshot()

    elements: list[dict] = []
    elements_text = ""
    text = ""
    text_truncated = False
    text_unavailable = False

    if isinstance(engine, StealthEngine) and engine.tab:
        try:
            raw_js = extract_elements_js().strip()
            js_code = "JSON.stringify(" + raw_js + ")"
            result = await engine.tab.evaluate(js_code, return_by_value=True)
            if isinstance(result, tuple):
                result = result[0]
            if isinstance(result, str):
                elements = json.loads(result)
            elif isinstance(result, list):
                elements = result
        except Exception:
            elements = []

        elements_text = format_elements_text(elements)

        text_result = await engine.get_text(main_content_only=main_content_only)
        text = text_result.get("text", "")
        if len(text) > max_length:
            text = text[:max_length]
            text_truncated = True

        title = await engine.get_title()
        url = await engine.get_url()
    else:
        # Native engine cannot inject JS or extract page text.
        text_unavailable = True
        title = await engine.get_title()
        url = await engine.get_url()

    # Run block detection on the text/title/url we were able to collect.
    block_result = detect_block(
        title=title or "",
        body_text=text[:2000],
        url=url or "",
    )

    # Merge any signals that were discovered during settle (caller responsibility).
    # We just return the base block result here; action endpoints merge settle signals.
    block = {
        "blocked": block_result.blocked,
        "confidence": block_result.confidence,
        "signals": [
            {"name": s.name, "value": s.value, "confidence": s.confidence}
            for s in block_result.signals
        ],
    }

    annotated = annotate_screenshot(raw_screenshot, elements)

    look = {
        "screenshot_base64": base64.b64encode(annotated).decode("utf-8"),
        "screenshot_media_type": "image/png",
        "text": text,
        "text_truncated": text_truncated,
        "title": title or "",
        "url": url or "",
        "elements": elements,
        "elements_text": elements_text,
        "element_count": len(elements),
        "engine": engine_name,
        "tab_id": _active_tab_id(engine),
        "block": block,
    }
    if text_unavailable:
        look["text_unavailable"] = True
    return look
