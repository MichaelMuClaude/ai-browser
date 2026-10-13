"""E2E tests for new MCP tools (Phase 8).

Tests the 4 new tools via the HTTP API using BrowserClient:
- browser_search
- browser_wait_for_element
- browser_execute_js
- browser_find_element
"""
import asyncio
import json
import pytest

from src.client import BrowserClient


@pytest.mark.smoke
def test_mcp_execute_js(base_url):
    """browser_execute_js: execute document.title and verify."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.execute_js("document.title")
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["result"] == "Example Domain"


@pytest.mark.smoke
def test_mcp_execute_js_boolean_false(base_url):
    """browser_execute_js: returns native False for false expressions."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.execute_js("1+1===3")
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["result"] is False


@pytest.mark.smoke
def test_mcp_execute_js_boolean_true(base_url):
    """browser_execute_js: returns native True for true expressions."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.execute_js("1+1===2")
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["result"] is True


@pytest.mark.smoke
def test_mcp_execute_js_complex(base_url):
    """browser_execute_js: return a complex value via JSON.stringify."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.execute_js(
            "JSON.stringify({title: document.title, h1: document.querySelector('h1').textContent})"
        )
        await client.close()
        return result

    result = asyncio.run(_run())
    value = result["result"]
    if isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    assert parsed["title"] == "Example Domain"
    assert "Example" in parsed["h1"]


@pytest.mark.smoke
def test_mcp_wait_for_element_text(base_url):
    """browser_wait_for_element: wait for 'Example Domain' text."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.wait_for_element(text="Example Domain", timeout=5)
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["found"] is True
    assert result["elapsed_ms"] >= 0


@pytest.mark.smoke
def test_mcp_wait_for_element_selector(base_url):
    """browser_wait_for_element: wait for CSS selector."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.wait_for_element(selector="h1", timeout=5)
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["found"] is True


@pytest.mark.smoke
def test_mcp_wait_for_element_timeout(base_url):
    """browser_wait_for_element: timeout when text not present."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.wait_for_element(
            text="THIS TEXT DOES NOT EXIST ANYWHERE", timeout=3
        )
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["found"] is False
    assert result["elapsed_ms"] >= 2500  # at least ~3s


@pytest.mark.smoke
def test_mcp_find_element(base_url):
    """browser_find_element: find 'Learn more' link on example.com."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.find_element("Learn more")
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["found"] is True
    assert "x" in result
    assert "y" in result
    assert result["tag"] == "a"


@pytest.mark.smoke
def test_mcp_find_element_link_filter(base_url):
    """browser_find_element: find with element_type=link filter."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.find_element("Learn", element_type="link")
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["found"] is True
    assert result["tag"] == "a"


@pytest.mark.smoke
def test_mcp_find_element_not_found(base_url):
    """browser_find_element: returns found=false for non-existent text."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.find_element("NONEXISTENT BUTTON TEXT XYZ123")
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["found"] is False


@pytest.mark.smoke
def test_mcp_search(base_url):
    """browser_search: search Google and verify URL contains google.com."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        result = await client.search("Python programming language")
        await client.close()
        return result

    result = asyncio.run(_run())
    assert "google.com/search" in result.get("search_url", "")
    assert "q=Python" in result.get("search_url", "")
    assert "q=q=" not in result.get("search_url", "")
    assert result.get("title", "") != ""


# --- Task 2: Data extraction tools ---

@pytest.mark.smoke
def test_extract_links(base_url):
    """browser_extract_links: extract links from example.com."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.extract_links()
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["total"] >= 1
    link = result["links"][0]
    assert "text" in link
    assert "url" in link
    assert link["url"].startswith("http")
    assert "x" in link
    assert "y" in link


@pytest.mark.smoke
def test_extract_links_filter(base_url):
    """browser_extract_links: filter_text narrows results."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.extract_links(filter_text="More")
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["total"] >= 1
    for link in result["links"]:
        assert "more" in link["text"].lower()


@pytest.mark.smoke
def test_get_meta(base_url):
    """browser_get_meta: extract metadata from example.com."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.get_meta()
        await client.close()
        return result

    result = asyncio.run(_run())
    assert result["title"] == "Example Domain"
    assert "example.com" in result["url"]
    assert "error" not in result


@pytest.mark.smoke
def test_extract_table(base_url):
    """browser_extract_table: extract table from w3schools."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://www.w3schools.com/html/html_tables.asp", return_look=False)
        # Wait for page content to load
        await client.wait_for_element(selector="table", timeout=10)
        result = await client.extract_table(index=0)
        await client.close()
        return result

    result = asyncio.run(_run())
    assert "error" not in result or result.get("total_tables", 0) > 0
    if "headers" in result:
        assert len(result["headers"]) > 0
    if "rows" in result:
        assert len(result["rows"]) > 0


@pytest.mark.smoke
def test_extract_table_no_table(base_url):
    """browser_extract_table: return error when no table on page."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        session_id = (await client.create_session())["session_id"]
        try:
            await client.navigate("https://example.com", session_id=session_id, return_look=False)
            result = await client.extract_table(index=0, session_id=session_id)
        finally:
            await client.delete_session(session_id)
            await client.close()
        return result

    result = asyncio.run(_run())
    assert "error" in result
    assert result.get("total_tables", 0) == 0


# --- Phase 3 Task 2: Multi-tab support ---

@pytest.mark.smoke
def test_open_tab_and_list(base_url):
    """browser_open_tab + browser_list_tabs: open a new tab and verify it appears."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        # Navigate first tab to example.com
        await client.navigate("https://example.com", return_look=False)
        # Open a second tab
        result = await client.open_tab("https://www.iana.org/domains/reserved")
        # List all tabs
        tabs = await client.list_tabs()
        await client.close()
        return {"open_result": result, "tabs": tabs}

    result = asyncio.run(_run())
    open_res = result["open_result"]
    assert "tab_id" in open_res
    assert open_res["tab_id"] != "tab-0"  # should be a new tab
    tabs = result["tabs"]["tabs"]
    assert len(tabs) >= 2


@pytest.mark.smoke
def test_switch_tab_screenshot(base_url):
    """browser_switch_tab: open 2 tabs, switch between them, verify different content."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        # Tab 0: example.com (already there from default)
        await client.navigate("https://example.com", return_look=False)
        title_0_resp = await client.get_title()
        title_0 = title_0_resp.get("title", "") if isinstance(title_0_resp, dict) else title_0_resp
        # Open Tab 1: iana.org
        await client.open_tab("https://www.iana.org/domains/reserved")
        title_1_resp = await client.get_title()
        title_1 = title_1_resp.get("title", "") if isinstance(title_1_resp, dict) else title_1_resp
        # Switch back to Tab 0
        await client.switch_tab("tab-0")
        title_0_again_resp = await client.get_title()
        title_0_again = title_0_again_resp.get("title", "") if isinstance(title_0_again_resp, dict) else title_0_again_resp
        await client.close()
        return {"title_0": title_0, "title_1": title_1, "title_0_again": title_0_again}

    result = asyncio.run(_run())
    assert "Example" in result["title_0"]
    assert result["title_1"] != result["title_0"]  # different pages
    assert "Example" in result["title_0_again"]  # switched back successfully


@pytest.mark.smoke
def test_close_tab(base_url):
    """browser_close_tab: open a tab, close it, verify it's gone."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        # Open a second tab
        open_res = await client.open_tab("https://example.com")
        tab_id = open_res["tab_id"]
        # Verify 2 tabs
        tabs_before = await client.list_tabs()
        # Close the new tab
        close_res = await client.close_tab(tab_id)
        # Verify it's gone
        tabs_after = await client.list_tabs()
        await client.close()
        return {"tabs_before": tabs_before, "close_res": close_res, "tabs_after": tabs_after}

    result = asyncio.run(_run())
    assert len(result["tabs_before"]["tabs"]) >= 2
    assert result["close_res"]["closed"] is True
    tab_ids_after = [t["tab_id"] for t in result["tabs_after"]["tabs"]]
    assert result["close_res"]["tab_id"] not in tab_ids_after


@pytest.mark.smoke
def test_close_tab_switches_active(base_url):
    """When closing the active tab, engine should switch to another tab."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        # Open second tab (becomes active)
        open_res = await client.open_tab("https://example.com")
        # Close it (it's the active tab)
        await client.close_tab(open_res["tab_id"])
        # Engine should have switched to tab-0
        title = await client.get_title()
        await client.close()
        return {"title": title}

    result = asyncio.run(_run())
    assert result["title"] != ""  # tab-0 should still be accessible


@pytest.mark.smoke
def test_list_tabs_initial(base_url):
    """browser_list_tabs: after navigate, tab-0 should exist and be active."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.list_tabs()
        await client.close()
        return result

    result = asyncio.run(_run())
    assert len(result["tabs"]) >= 1
    # Find tab-0 in the list
    tab0 = next((t for t in result["tabs"] if t["tab_id"] == "tab-0"), None)
    assert tab0 is not None, "tab-0 should exist"
    assert tab0["url"].startswith("https://")  # should have navigated somewhere



@pytest.mark.smoke
def test_mcp_go_back(base_url):
    """browser_go_back: navigate through history and go back."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        session_id = (await client.create_session())["session_id"]
        try:
            await client.navigate("https://example.com", session_id=session_id, return_look=False)
            await client.navigate("https://iana.org", session_id=session_id, return_look=False)
            result = await client.go_back(session_id=session_id)
        finally:
            await client.delete_session(session_id)
            await client.close()
        return result

    result = asyncio.run(_run())
    assert "Example Domain" in result["title"]


@pytest.mark.smoke
def test_mcp_go_forward(base_url):
    """browser_go_forward: navigate back then forward."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        session_id = (await client.create_session())["session_id"]
        try:
            await client.navigate("https://example.com", session_id=session_id, return_look=False)
            await client.navigate("https://iana.org", session_id=session_id, return_look=False)
            await client.go_back(session_id=session_id)
            result = await client.go_forward(session_id=session_id)
        finally:
            await client.delete_session(session_id)
            await client.close()
        return result

    result = asyncio.run(_run())
    assert "Internet Assigned" in result["title"] or "iana.org" in result.get("url", "")


@pytest.mark.smoke
def test_mcp_reload(base_url):
    """browser_reload: reload current page and verify title."""
    async def _run():
        client = BrowserClient(base_url=base_url)
        await client.navigate("https://example.com", return_look=False)
        result = await client.reload()
        await client.close()
        return result

    result = asyncio.run(_run())
    assert "Example Domain" in result["title"]
