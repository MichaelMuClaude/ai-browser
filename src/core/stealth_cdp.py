"""Stealth CDP engine using nodriver — default fast engine."""
import nodriver as uc
import base64
import io
import asyncio
import time
from typing import Optional
from .base import BrowserEngine
from .process_cleanup import kill_chrome_by_profile_dir
from ..config import (
    NAVIGATE_LOAD_MAX_POLLS,
    NAVIGATE_LOAD_POLL_INTERVAL,
    STARTUP_URL,
    STEALTH_NAVIGATE_TIMEOUT_SECONDS,
    STEALTH_PROFILE_DIR,
)
from ..log_config import get_logger

logger = get_logger(__name__)

# 2026-07-26: real pain point — plain `document.body.innerText` on e-commerce
# sites (Amazon, Kmart) buries the actual product listings under thousands of
# characters of nav menus / department lists / cookie banners, forcing every
# caller to manually skip past them. This mode hides known boilerplate
# elements (never removes — fully reversible) and prefers a recognized
# main-content container when one exists and has substantial text.
_MAIN_CONTENT_JS = """(() => {
    const hideSelectors = ['nav', 'header', 'footer', 'aside', 'script', 'style', 'noscript',
        '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]', '[aria-hidden="true"]'];
    const hidden = [];
    document.querySelectorAll(hideSelectors.join(',')).forEach(el => {
        hidden.push([el, el.style.display]);
        el.style.display = 'none';
    });
    const mainSelectors = ['main', '[role="main"]', '#main', '#content', '.main-content', 'article'];
    let text = '';
    for (const sel of mainSelectors) {
        const el = document.querySelector(sel);
        if (el && el.innerText && el.innerText.trim().length > 200) {
            text = el.innerText;
            break;
        }
    }
    if (!text) {
        text = document.body.innerText;
    }
    hidden.forEach(([el, display]) => { el.style.display = display; });
    return text;
})()"""


class StealthEngine(BrowserEngine):
    """Browser engine using nodriver (undetected-chromedriver successor)."""

    def __init__(self, headless: bool = False, browser_path: str = "", profile_dir: str = ""):
        self.headless = headless
        self.browser_path = browser_path or None
        self.profile_dir = profile_dir
        self.browser: Optional[uc.Browser] = None
        self.tab: Optional[uc.Tab] = None
        # Multi-tab tracking: tab_id -> uc.Tab object
        self._tabs: dict[str, uc.Tab] = {}
        self._next_tab_num: int = 1
        # Diagnostic log for Page.frameNavigated events (Amazon stability investigation).
        self._frame_navigations: list[dict] = []

    async def start(self) -> None:
        import os
        # Use a dedicated profile dir so we don't conflict with an existing Edge/Chrome instance
        profile_dir = self.profile_dir or STEALTH_PROFILE_DIR
        os.makedirs(profile_dir, exist_ok=True)
        self.browser = await uc.start(
            headless=self.headless,
            browser_executable_path=self.browser_path,
            user_data_dir=profile_dir,
            # 2026-07-23: real feedback case — target="_blank" links (Google's
            # sitelinks-preview chips, among others) silently failed to open a
            # new tab at all, even via CDP-dispatched clicks that should count
            # as a trusted user gesture. --disable-popup-blocking removes any
            # ambiguity about whether Chrome's popup heuristic is what's
            # eating the new-tab open.
            browser_args=["--disable-popup-blocking"],
        )
        self.tab = await self.browser.get(STARTUP_URL)
        # Register initial tab
        tab_id = "tab-0"
        self._tabs[tab_id] = self.tab
        self._next_tab_num = 1

    async def stop(self) -> None:
        if self.browser:
            try:
                self.browser.stop()
            except Exception:
                pass
            self.browser = None
            self.tab = None
            self._tabs.clear()
            self._next_tab_num = 1
            self._frame_navigations.clear()

    def start_frame_navigated_listener(self) -> None:
        """Register a CDP Page.frameNavigated listener on the active tab.

        The listener records every frame navigation with timestamp, URL and
        navigation type into ``self._frame_navigations``. This is intended as
        a diagnostic tool for investigating tab stability issues such as
        Amazon's reported post-load degradation to ``about:blank``.
        """
        if not self.tab:
            return
        self._frame_navigations.clear()

        def handler(event: uc.cdp.page.FrameNavigated):
            frame = event.frame
            entry = {
                "timestamp": time.time(),
                "url": getattr(frame, "url", ""),
                "type": str(getattr(event, "type_", None)),
                "name": getattr(frame, "name", None),
            }
            self._frame_navigations.append(entry)

        self.tab.add_handler(uc.cdp.page.FrameNavigated, handler)

    def get_frame_navigations(self) -> list[dict]:
        """Return a copy of the frame navigation log."""
        return list(self._frame_navigations)

    def clear_frame_navigations(self) -> None:
        """Clear the frame navigation log."""
        self._frame_navigations.clear()

    async def _recover_dead_tab(self) -> None:
        """Replace a tab whose CDP session has died with a fresh one.

        If the browser-level CDP connection itself is dead (not just the
        tab), opening a new tab on it also fails — in that case restart the
        whole browser process against the same profile_dir so cookies/login
        state persisted on disk survive.
        """
        dead_tab = self.tab
        try:
            new_tab = await self.browser.get("about:blank")
        except uc.ProtocolException:
            await self._restart_browser()
            return
        self.tab = new_tab
        for tid, t in list(self._tabs.items()):
            if t is dead_tab:
                self._tabs[tid] = new_tab

    async def _restart_browser(self) -> None:
        """Fully restart the browser process, reusing the same profile_dir."""
        try:
            if self.browser:
                self.browser.stop()
        except Exception:
            pass
        self.browser = None
        self.tab = None
        self._tabs.clear()
        self._next_tab_num = 1
        # Ensure old Chrome processes are fully dead before starting a new instance
        killed = await kill_chrome_by_profile_dir(self.profile_dir)
        if killed:
            logger.warning(
                "_restart_browser: killed %d leaked Chrome process(es) before restart",
                killed,
            )
        await self.start()

    async def _evaluate_tolerant(self, js: str, retries: int = 3, delay: float = 0.3):
        """Evaluate JS, tolerating the transient error CDP raises when the
        page navigates away (e.g. a redirect chain) mid-evaluation.

        The result is normalized to unwrap nodriver's tuple/RemoteObject
        wrappers so callers can compare against plain Python values.
        """
        for attempt in range(retries):
            try:
                raw = await self.tab.evaluate(js, return_by_value=True)
                return self._normalize_eval_result(raw)
            except uc.ProtocolException as e:
                if "navigated or closed" in str(e) and attempt < retries - 1:
                    await asyncio.sleep(delay)
                    continue
                raise

    @staticmethod
    def _normalize_eval_result(raw):
        """Unwrap nodriver evaluate() return values.

        nodriver can return:
        - A tuple (value, exception_details)
        - A plain value
        - A RemoteObject dataclass wrapper for primitives
        """
        if isinstance(raw, tuple):
            raw = raw[0]
        if hasattr(raw, "type_") and hasattr(raw, "value"):
            raw = raw.value
        return raw

    async def _eval_string(
        self, js: str, retries: int = 3, delay: float = 0.3
    ) -> str | None:
        """Evaluate JS expecting a string result. Returns the string, or None if
        the JS context is disrupted (RemoteObject wrapper or non-string type).

        Retries on type mismatch — the disruption is transient (page navigating).
        Raises only after exhausting retries, so callers can decide to tolerate.
        """
        for attempt in range(retries):
            try:
                raw = await self.tab.evaluate(js, return_by_value=True)
            except uc.ProtocolException as e:
                if "navigated or closed" in str(e) and attempt < retries - 1:
                    await asyncio.sleep(delay)
                    continue
                raise
            raw = self._normalize_eval_result(raw)
            if isinstance(raw, str):
                return raw
            if raw is None:
                return None
            if attempt < retries - 1:
                await asyncio.sleep(delay)
                continue
            logger.warning(
                "_eval_string: got non-string after %d retries for %r: %r",
                retries,
                js[:60],
                type(raw).__name__,
            )
            return None
        return None

    async def navigate(self, url: str) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        # Navigate within the current tab (not browser.get which goes to the first tab).
        # nodriver's tab.get() has no internal timeout — a site that never fires the
        # navigation-complete CDP event (real case: kmart.com.au) hangs this call
        # forever, wedging the tab for every later action too. Bound it explicitly.
        try:
            await asyncio.wait_for(self.tab.get(url), timeout=STEALTH_NAVIGATE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Navigation to {url} timed out after {STEALTH_NAVIGATE_TIMEOUT_SECONDS}s "
                "(stealth engine, tab.get() never returned)"
            ) from None
        except uc.ProtocolException as e:
            if "Session with given id not found" in str(e):
                await self._recover_dead_tab()
                await asyncio.wait_for(
                    self.tab.get(url), timeout=STEALTH_NAVIGATE_TIMEOUT_SECONDS
                )
            else:
                raise
        # Wait for page to actually load (timeout 10s). Redirect-heavy sites
        # (e.g. Google services) can navigate again mid-poll — tolerate that.
        for _ in range(NAVIGATE_LOAD_MAX_POLLS):
            ready = await self._evaluate_tolerant("document.readyState")
            if ready == "complete":
                break
            await asyncio.sleep(NAVIGATE_LOAD_POLL_INTERVAL)
        title = await self.get_title()
        final_url = await self.get_url()
        html = await self._evaluate_tolerant("document.documentElement.outerHTML")
        html_str = str(html) if html else ""
        return {
            "url": final_url,
            "requested_url": url,
            "title": title,
            "html": html_str[:5000],
        }

    async def go_back(self) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        await self.tab.evaluate("history.back()")
        await asyncio.sleep(1)
        return {"url": await self.get_url(), "title": await self.get_title()}

    async def go_forward(self) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        await self.tab.evaluate("history.forward()")
        await asyncio.sleep(1)
        return {"url": await self.get_url(), "title": await self.get_title()}

    async def reload(self) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        await self.tab.reload()
        for _ in range(NAVIGATE_LOAD_MAX_POLLS):
            ready = await self.tab.evaluate("document.readyState", return_by_value=True)
            if ready == "complete":
                break
            await asyncio.sleep(NAVIGATE_LOAD_POLL_INTERVAL)
        return {"url": await self.get_url(), "title": await self.get_title()}

    async def screenshot(self) -> bytes:
        if not self.tab:
            raise RuntimeError("Browser not started")
        # CDP capture_screenshot returns a tuple with base64 string
        data = await self.tab.send(uc.cdp.page.capture_screenshot())
        b64_str = data[0] if isinstance(data, tuple) else data
        return base64.b64decode(b64_str)

    async def click(self, x: int, y: int) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        # Use nodriver's built-in mouse_click at coordinates
        await self.tab.mouse_click(x / 1, y / 1)  # nodriver expects float coords
        return {"x": x, "y": y}

    async def type_text(self, text: str) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        # Use CDP input_.insert_text for typing
        await self.tab.send(uc.cdp.input_.insert_text(text=text))
        return {"text": text}

    async def scroll(self, direction: str = "down", amount: int = 3) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        delta = amount * 300 if direction == "down" else -amount * 300
        await self.tab.evaluate(f"window.scrollBy(0, {delta})")
        return {"direction": direction, "amount": amount}

    async def press_key(self, key: str) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        key = key.strip()
        if not key:
            raise ValueError("Empty key")

        # Canonical special-key names and Windows virtual-key codes.
        VK = {
            "Backspace": 8,
            "Tab": 9,
            "Enter": 13,
            "Return": 13,
            "Shift": 16,
            "Control": 17,
            "Ctrl": 17,
            "Alt": 18,
            "Escape": 27,
            "Space": 32,
            "End": 35,
            "Home": 36,
            "ArrowLeft": 37,
            "ArrowUp": 38,
            "ArrowRight": 39,
            "ArrowDown": 40,
            "Delete": 46,
            "F1": 112,
            "F2": 113,
            "F3": 114,
            "F4": 115,
            "F5": 116,
            "F6": 117,
            "F7": 118,
            "F8": 119,
            "F9": 120,
            "F10": 121,
            "F11": 122,
            "F12": 123,
        }
        CODE = {
            "Backspace": "Backspace",
            "Tab": "Tab",
            "Enter": "Enter",
            "Return": "Enter",
            "Escape": "Escape",
            "Space": "Space",
            "End": "End",
            "Home": "Home",
            "ArrowLeft": "ArrowLeft",
            "ArrowUp": "ArrowUp",
            "ArrowRight": "ArrowRight",
            "ArrowDown": "ArrowDown",
            "Delete": "Delete",
            "Shift": "ShiftLeft",
            "Control": "ControlLeft",
            "Ctrl": "ControlLeft",
            "Alt": "AltLeft",
        }
        MODIFIERS = {
            "ctrl": "Control",
            "control": "Control",
            "shift": "Shift",
            "alt": "Alt",
            "meta": "Meta",
            "command": "Meta",
            "cmd": "Meta",
            "win": "Meta",
        }

        def _modifier_bit(name: str) -> int:
            return {"Control": 2, "Shift": 8, "Alt": 1, "Meta": 4}.get(name, 0)

        # Parse combos such as "ctrl+a", "shift+End", "End", "BackSpace".
        parts = [p.strip() for p in key.split("+")]
        modifier_names = []
        main_key = None
        for p in parts:
            lower = p.lower()
            if lower in MODIFIERS:
                modifier_names.append(MODIFIERS[lower])
            else:
                main_key = p

        if main_key is None:
            raise ValueError(f"Invalid key combo: {key!r}")

        # Normalize main key: use canonical casing if it matches a known special key.
        canonical = main_key
        for known in VK:
            if known.lower() == main_key.lower():
                canonical = known
                break

        vk = VK.get(canonical, 0)
        if vk == 0 and len(canonical) == 1:
            vk = ord(canonical.upper())

        code = CODE.get(canonical, "")
        if not code and len(canonical) == 1:
            code = f"Key{canonical.upper()}"

        # Send modifier keyDown events, accumulating the modifier bitmask.
        mod_mask = 0
        for mod in modifier_names:
            mod_vk = VK.get(mod, 0)
            mod_code = CODE.get(mod, mod)
            await self.tab.send(uc.cdp.input_.dispatch_key_event(
                type_="keyDown",
                key=mod,
                code=mod_code,
                windows_virtual_key_code=mod_vk,
                modifiers=mod_mask,
            ))
            mod_mask |= _modifier_bit(mod)
            await asyncio.sleep(0.01)

        # Send the main key while modifiers are held.
        # Do not inject a raw character for modifier combos; the modifiers
        # and virtual-key code already describe the shortcut (ctrl+a,
        # shift+End, ctrl+c, etc.). Supplying `text` here would cause the
        # browser to fire an extra char/keypress event and type the letter.
        text = canonical if len(canonical) == 1 and not modifier_names else None
        await self.tab.send(uc.cdp.input_.dispatch_key_event(
            type_="keyDown",
            key=canonical,
            code=code,
            windows_virtual_key_code=vk,
            modifiers=mod_mask,
            text=text,
        ))
        await asyncio.sleep(0.01)
        await self.tab.send(uc.cdp.input_.dispatch_key_event(
            type_="keyUp",
            key=canonical,
            code=code,
            windows_virtual_key_code=vk,
            modifiers=mod_mask,
            text=text,
        ))

        # Release modifiers in reverse order.
        for mod in reversed(modifier_names):
            mod_vk = VK.get(mod, 0)
            mod_code = CODE.get(mod, mod)
            await self.tab.send(uc.cdp.input_.dispatch_key_event(
                type_="keyUp",
                key=mod,
                code=mod_code,
                windows_virtual_key_code=mod_vk,
                modifiers=mod_mask,
            ))
            mod_mask &= ~_modifier_bit(mod)
            await asyncio.sleep(0.01)

        return {"key": key}

    async def clear_field(self) -> dict:
        """Clear the currently focused input field using Ctrl+A then Delete.

        This is more reliable than repeated Backspace because it selects and
        removes the entire value in one go, and it triggers the same events a
        real user would produce.
        """
        if not self.tab:
            raise RuntimeError("Browser not started")

        raw = await self.tab.evaluate("""
            (function() {
                const el = document.activeElement;
                if (!el) return {ok: false, error: "no focused element"};
                const tag = el.tagName.toLowerCase();
                const isEditable = tag === "input" || tag === "textarea" || el.isContentEditable;
                if (!isEditable) return {ok: false, error: "active element is not editable"};
                el.focus();
                if (typeof el.select === "function") el.select();
                else {
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    const sel = window.getSelection();
                    sel.removeAllRanges();
                    sel.addRange(range);
                }
                return {ok: true, tag: tag};
            })()
        """)
        info = self._normalize_eval_result(raw)
        if isinstance(info, dict) and not info.get("ok"):
            return {"cleared": False, "error": info.get("error", "unknown")}

        await self.press_key("ctrl+a")
        await asyncio.sleep(0.05)
        await self.press_key("Delete")
        return {"cleared": True}

    def _extract_js_error(self, result) -> str | None:
        """If evaluate() returned a JS exception object, extract its description."""
        if hasattr(result, "exception_id"):
            # nodriver ExceptionDetails dataclass
            exc = getattr(result, "exception", None)
            if exc and hasattr(exc, "description"):
                return exc.description
            return str(result)
        if isinstance(result, dict) and "exception_id" in result:
            return (result.get("exception") or {}).get("description", str(result))
        return None

    async def select(self, selector: str, value: str) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        import json
        js = f"""(function() {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error('Element not found: ' + {json.dumps(selector)});
            if (el.tagName.toLowerCase() !== 'select') throw new Error('Element is not a select: ' + {json.dumps(selector)});
            const opt = Array.from(el.options).find(
                o => o.value === {json.dumps(value)} || o.text === {json.dumps(value)}
            );
            if (!opt) throw new Error('Option not found: ' + {json.dumps(value)});
            el.value = opt.value;
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return {{value: el.value, text: opt.text}};
        }})()"""
        try:
            result = await self.tab.evaluate(js, return_by_value=True)
            err = self._extract_js_error(result)
            if err:
                return {"selector": selector, "value": value, "error": err}
            return {"selector": selector, "value": value, "result": result}
        except Exception as e:
            return {"selector": selector, "value": value, "error": str(e)}

    async def check(self, selector: str) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        import json
        js = f"""(function() {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error('Element not found: ' + {json.dumps(selector)});
            const tag = el.tagName.toLowerCase();
            const type = (el.type || '').toLowerCase();
            if (tag !== 'input' || (type !== 'checkbox' && type !== 'radio')) {{
                throw new Error('Element is not a checkbox/radio: ' + {json.dumps(selector)});
            }}
            if (!el.checked) {{ el.click(); }}
            return {{checked: el.checked, value: el.value}};
        }})()"""
        try:
            result = await self.tab.evaluate(js, return_by_value=True)
            err = self._extract_js_error(result)
            if err:
                return {"selector": selector, "error": err}
            return {"selector": selector, "result": result}
        except Exception as e:
            return {"selector": selector, "error": str(e)}

    async def uncheck(self, selector: str) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        import json
        js = f"""(function() {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error('Element not found: ' + {json.dumps(selector)});
            const tag = el.tagName.toLowerCase();
            const type = (el.type || '').toLowerCase();
            if (tag !== 'input' || type !== 'checkbox') {{
                throw new Error('Element is not a checkbox: ' + {json.dumps(selector)});
            }}
            if (el.checked) {{ el.click(); }}
            return {{checked: el.checked, value: el.value}};
        }})()"""
        try:
            result = await self.tab.evaluate(js, return_by_value=True)
            err = self._extract_js_error(result)
            if err:
                return {"selector": selector, "error": err}
            return {"selector": selector, "result": result}
        except Exception as e:
            return {"selector": selector, "error": str(e)}

    async def _send_with_recovery(self, make_cmd):
        """Send a CDP command, recovering from a dead tab/browser session once."""
        try:
            return await self.tab.send(make_cmd())
        except uc.ProtocolException as e:
            if "Session with given id not found" not in str(e):
                raise
            await self._recover_dead_tab()
            return await self.tab.send(make_cmd())

    async def get_cookies(self, domain: str = "") -> list:
        if not self.browser:
            raise RuntimeError("Browser not started")
        result = await self._send_with_recovery(uc.cdp.storage.get_cookies)
        cookies = [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": c.secure,
                "http_only": c.http_only,
                "expires": c.expires,
            }
            for c in result
        ]
        if domain:
            cookies = [c for c in cookies if domain in c["domain"]]
        return cookies

    async def set_cookie(self, name: str, value: str, domain: str, path: str = "/",
                         expires: float = -1, secure: bool = False,
                         http_only: bool = False) -> dict:
        if not self.browser:
            raise RuntimeError("Browser not started")
        await self._send_with_recovery(lambda: uc.cdp.network.set_cookie(
            name=name,
            value=value,
            domain=domain,
            path=path,
            expires=expires if expires >= 0 else None,
            secure=secure,
            http_only=http_only,
        ))
        return {"name": name, "domain": domain, "path": path}

    async def clear_cookies(self, domain: str = "") -> dict:
        if not self.browser:
            raise RuntimeError("Browser not started")
        if domain:
            # Delete cookies matching domain one by one
            all_cookies = await self.get_cookies(domain=domain)
            for c in all_cookies:
                await self._send_with_recovery(lambda c=c: uc.cdp.network.delete_cookies(
                    name=c["name"], domain=c["domain"], path=c["path"]
                ))
            return {"cleared": len(all_cookies), "domain": domain}
        else:
            await self._send_with_recovery(uc.cdp.network.clear_browser_cookies)
            return {"cleared": "all"}

    async def get_text(self, main_content_only: bool = False) -> dict:
        if not self.tab:
            raise RuntimeError("Browser not started")
        js = _MAIN_CONTENT_JS if main_content_only else "document.body.innerText"
        text = await self._eval_string(js)
        return {"text": text or ""}

    async def get_title(self) -> str:
        if not self.tab:
            raise RuntimeError("Browser not started")
        return await self._eval_string("document.title") or ""

    async def get_url(self) -> str:
        if not self.tab:
            raise RuntimeError("Browser not started")
        return await self._eval_string("window.location.href") or ""

    # --- Multi-tab support ---

    async def open_tab(self, url: str = "about:blank") -> dict:
        """Open a new browser tab and navigate to url. Returns tab_id and metadata."""
        if not self.browser:
            raise RuntimeError("Browser not started")
        # 2026-07-26: real case — opening a new tab to amazon.com.au took long
        # enough to blow past a 30s client timeout. Unlike navigate()'s
        # tab.get(), this browser.get(new_tab=True) call had no timeout at
        # all, so an unresponsive site could hang it forever (same underlying
        # nodriver gotcha fixed for navigate() on 2026-07-26 — see
        # STEALTH_NAVIGATE_TIMEOUT_SECONDS above).
        try:
            new_tab = await asyncio.wait_for(
                self.browser.get(url, new_tab=True), timeout=STEALTH_NAVIGATE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Opening new tab for {url} timed out after "
                f"{STEALTH_NAVIGATE_TIMEOUT_SECONDS}s (stealth engine, browser.get() never returned)"
            ) from None
        tab_id = f"tab-{self._next_tab_num}"
        self._next_tab_num += 1
        self._tabs[tab_id] = new_tab
        # Switch active tab to the new one
        self.tab = new_tab
        # Wait for execution context and page load
        for _ in range(NAVIGATE_LOAD_MAX_POLLS):
            try:
                ready = await self._wait_for_evaluate(
                    new_tab, "document.readyState", timeout=NAVIGATE_LOAD_POLL_INTERVAL * 2
                )
                if ready == "complete":
                    break
            except TimeoutError:
                pass
            await asyncio.sleep(NAVIGATE_LOAD_POLL_INTERVAL)
        title = await self._get_title_for_tab(new_tab)
        final_url = await self._get_url_for_tab(new_tab)
        return {"tab_id": tab_id, "url": final_url, "title": title}

    async def _wait_for_evaluate(
        self,
        tab: uc.Tab,
        js: str,
        *,
        timeout: Optional[float] = None,
        return_by_value: bool = True,
    ):
        """Evaluate JS on a tab, retrying briefly if the execution context isn't ready."""
        timeout = timeout or (NAVIGATE_LOAD_MAX_POLLS * NAVIGATE_LOAD_POLL_INTERVAL)
        deadline = asyncio.get_event_loop().time() + timeout
        last_error = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                return await tab.evaluate(js, return_by_value=return_by_value)
            except Exception as e:
                last_error = e
                msg = str(e)
                if "Cannot find default execution context" in msg or "context was destroyed" in msg:
                    await asyncio.sleep(NAVIGATE_LOAD_POLL_INTERVAL)
                    continue
                raise
        raise TimeoutError(f"Timeout waiting for execution context: {last_error}")

    async def _is_tab_alive(self, tab: uc.Tab) -> bool:
        """Check if a tab object is still attached to the browser."""
        try:
            await tab.evaluate("document.readyState", return_by_value=True)
            return True
        except Exception:
            return False

    async def switch_tab(self, tab_id: str) -> dict:
        """Switch active tab to tab_id. Returns metadata."""
        if tab_id not in self._tabs:
            raise ValueError(f"Tab '{tab_id}' not found. Available: {list(self._tabs.keys())}")
        tab = self._tabs[tab_id]
        if not await self._is_tab_alive(tab):
            self._tabs.pop(tab_id, None)
            raise ValueError(f"Tab '{tab_id}' is no longer alive. Available: {list(self._tabs.keys())}")
        # Activate the tab in the browser
        try:
            await tab.activate()
        except Exception:
            pass  # nodriver may not need explicit activate for CDP
        self.tab = tab
        title = await self._get_title_for_tab(tab)
        url = await self._get_url_for_tab(tab)
        return {"tab_id": tab_id, "url": url, "title": title}

    async def close_tab(self, tab_id: str) -> dict:
        """Close a browser tab. Returns status."""
        if tab_id not in self._tabs:
            raise ValueError(f"Tab '{tab_id}' not found. Available: {list(self._tabs.keys())}")
        tab = self._tabs.pop(tab_id)
        if await self._is_tab_alive(tab):
            try:
                await tab.close()
            except Exception:
                pass
        # If we closed the active tab, switch to another one
        if self.tab is tab:
            if self._tabs:
                # Switch to the last remaining tab
                self.tab = list(self._tabs.values())[-1]
                try:
                    await self.tab.activate()
                except Exception:
                    pass
            else:
                self.tab = None
        return {"tab_id": tab_id, "closed": True, "remaining_tabs": list(self._tabs.keys())}

    async def list_tabs(self) -> list:
        """List all open tabs with id, title, and url.

        2026-07-23 real bug: this only ever read self._tabs, which is a dict
        WE maintain by hand and only add to when open_tab() is explicitly
        called. It never learns about tabs the page itself opens (e.g. a
        target="_blank" search result link) — those genuinely exist in
        Chrome (confirmed via the raw CDP /json/list endpoint: the new tab
        was really there) but list_tabs() reported only 1 tab forever,
        making every such click look like a silent failure. Fix: sync with
        the browser's real target list first via update_targets(), and
        adopt any real page target we haven't assigned a tab_id to yet.
        """
        try:
            await self.browser.update_targets()
            # TargetInfo isn't hashable (real bug hit during testing:
            # "TypeError: unhashable type: 'TargetInfo'") — compare by
            # target_id string instead of putting the objects themselves in a set.
            known_target_ids = {t.target.target_id for t in self._tabs.values()}
            for real_tab in self.browser.tabs:
                if real_tab.target.target_id not in known_target_ids:
                    new_id = f"tab-{self._next_tab_num}"
                    self._next_tab_num += 1
                    self._tabs[new_id] = real_tab
        except Exception:
            pass  # fall back to whatever we already had tracked
        result = []
        for tid, tab in self._tabs.items():
            try:
                title = await self._get_title_for_tab(tab)
                url = await self._get_url_for_tab(tab)
            except Exception:
                title = ""
                url = ""
            is_active = tab is self.tab
            result.append({"tab_id": tid, "title": title, "url": url, "active": is_active})
        return result

    async def _get_title_for_tab(self, tab: uc.Tab) -> str:
        """Get title for a specific tab object."""
        for attempt in range(3):
            try:
                raw = await self._wait_for_evaluate(tab, "document.title")
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                return ""
            value = self._normalize_eval_result(raw)
            if isinstance(value, str):
                return value
            if attempt < 2:
                await asyncio.sleep(0.3)
                continue
        return ""

    async def _get_url_for_tab(self, tab: uc.Tab) -> str:
        """Get URL for a specific tab object."""
        for attempt in range(3):
            try:
                raw = await self._wait_for_evaluate(tab, "window.location.href")
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(0.3)
                    continue
                return ""
            value = self._normalize_eval_result(raw)
            if isinstance(value, str):
                return value
            if attempt < 2:
                await asyncio.sleep(0.3)
                continue
        return ""
