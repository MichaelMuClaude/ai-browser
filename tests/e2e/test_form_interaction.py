"""E2E tests for form interaction tools (Phase 4 Task 2)."""
import asyncio
import pytest

from src.client import BrowserClient, BrowserClientError


@pytest.mark.smoke
def test_select_by_value(base_url):
    """browser_select: select an option by value attribute."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        # Inject a select element into the page
        await client.execute_js("""(function() {
            const sel = document.createElement('select');
            sel.id = 'test-select';
            sel.innerHTML = '<option value="a">A</option><option value="b">B</option>';
            document.body.appendChild(sel);
            return 'ok';
        })()""")
        result = await client.select("#test-select", "b")
        selected = await client.execute_js(
            "document.querySelector('#test-select').value"
        )
        await client.close()
        return {"result": result, "selected": selected}

    result = asyncio.run(_run())
    assert result["selected"]["result"] == "b"


@pytest.mark.smoke
def test_select_by_text(base_url):
    """browser_select: select an option by visible text."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        await client.execute_js("""(function() {
            const sel = document.createElement('select');
            sel.id = 'test-select-text';
            sel.innerHTML = '<option value="x">Alpha</option><option value="y">Beta</option>';
            document.body.appendChild(sel);
            return 'ok';
        })()""")
        result = await client.select("#test-select-text", "Beta")
        selected = await client.execute_js(
            "document.querySelector('#test-select-text').value"
        )
        await client.close()
        return {"result": result, "selected": selected}

    result = asyncio.run(_run())
    assert result["selected"]["result"] == "y"


@pytest.mark.smoke
def test_check_and_uncheck(base_url):
    """browser_check / browser_uncheck: toggle a checkbox."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        await client.execute_js("""(function() {
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.id = 'test-cb';
            cb.checked = false;
            document.body.appendChild(cb);
            return 'ok';
        })()""")
        await client.check("#test-cb")
        checked = await client.execute_js("document.querySelector('#test-cb').checked")
        await client.uncheck("#test-cb")
        unchecked = await client.execute_js("document.querySelector('#test-cb').checked")
        await client.close()
        return {"checked": checked, "unchecked": unchecked}

    result = asyncio.run(_run())
    assert result["checked"]["result"] is True
    assert result["unchecked"]["result"] is False


@pytest.mark.smoke
def test_check_radio(base_url):
    """browser_check: select a radio button."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        await client.execute_js("""(function() {
            ['r1','r2'].forEach(id => {
                const r = document.createElement('input');
                r.type = 'radio';
                r.name = 'test-radio';
                r.id = id;
                r.value = id;
                document.body.appendChild(r);
            });
            return 'ok';
        })()""")
        await client.check("#r2")
        checked = await client.execute_js("document.querySelector('#r2').checked")
        await client.close()
        return {"checked": checked}

    result = asyncio.run(_run())
    assert result["checked"]["result"] is True


@pytest.mark.smoke
def test_select_not_found(base_url):
    """browser_select: HTTP API returns 400 / ELEMENT_NOT_FOUND when element missing."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        try:
            await client.select("#nonexistent-select", "foo")
        except BrowserClientError as e:
            await client.close()
            return {"status_code": e.status_code, "body": e.response_body}
        await client.close()
        return {"status_code": None}

    result = asyncio.run(_run())
    assert result["status_code"] == 400
    assert "Element not found" in result["body"]


@pytest.mark.smoke
def test_clear_field_replaces_content(base_url):
    """/api/clear and /api/type?clear=true reliably replace an input's value.

    Regression test for the kogan.com search-box bug where repeated Backspace
    and ctrl+a failed to clear the field and new text was appended.
    """
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        await client.execute_js("""(function() {
            const input = document.createElement('input');
            input.id = 'clearable';
            input.type = 'text';
            document.body.appendChild(input);
            input.focus();
            input.value = 'old value';
            return 'ok';
        })()""")

        await client.clear_field()
        after_clear = await client.execute_js(
            "document.querySelector('#clearable').value"
        )

        await client.execute_js(
            "document.querySelector('#clearable').value = 'old value'; 'ok';"
        )
        await client.type_text("replacement", clear=True)
        after_replace = await client.execute_js(
            "document.querySelector('#clearable').value"
        )

        # ctrl+a + new text should also replace, not append.
        await client.execute_js(
            "document.querySelector('#clearable').value = 'old value'; 'ok';"
        )
        await client.execute_js(
            "document.querySelector('#clearable').focus();"
        )
        await client.press_key("ctrl+a")
        await client.type_text("selected-replacement")
        after_select_all = await client.execute_js(
            "document.querySelector('#clearable').value"
        )

        await client.close()
        return {
            "after_clear": after_clear["result"],
            "after_replace": after_replace["result"],
            "after_select_all": after_select_all["result"],
        }

    result = asyncio.run(_run())
    assert result["after_clear"] == ""
    assert result["after_replace"] == "replacement"
    assert result["after_select_all"] == "selected-replacement"
