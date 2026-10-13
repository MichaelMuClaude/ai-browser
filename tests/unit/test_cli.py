"""Unit tests for ai-browser CLI — tests every subcommand via mock BrowserClient."""
import io
import os
import sys
from unittest import mock

import pytest

from src.cli import main


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def capsys_binary(capsys):
    """Wrap capsys to return (stdout_bytes, stderr_bytes)."""
    return capsys


# ── 0. bare --session at end-of-line must not hang ────────────────────────

def test_bare_session_flag_errors(capsys):
    """A trailing bare '--session' without a value should exit quickly with an argparse-style error."""
    with mock.patch.object(sys, "argv", ["cli.py", "health", "--session"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 2
    out, err = capsys.readouterr()
    assert "expected one argument" in (out + err)


# ── 1. help output lists all commands ─────────────────────────────────────

def test_help_lists_all_commands(capsys):
    cmds = [
        "health", "create-session", "list-sessions", "delete-session",
        "navigate", "go-back", "go-forward", "reload",
        "open-tab", "switch-tab", "close-tab", "list-tabs",
        "title", "url", "text", "get-meta",
        "screenshot", "screenshot-annotated", "look",
        "click", "type", "scroll", "key", "select", "check", "uncheck",
        "engine", "execute-js", "wait-for-element", "find-element",
        "search", "smart-fetch", "extract-links", "extract-table",
        "get-cookies", "set-cookie", "clear-cookies",
    ]
    with mock.patch.object(sys, "argv", ["cli.py"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 1
    out, err = capsys.readouterr()
    combined = out + err
    for cmd in cmds:
        assert cmd in combined, f"Subcommand '{cmd}' missing from help output"
    assert "--session" in combined


def test_help_no_session(capsys):
    """No subcommand → SystemExit(1), no crash."""
    with mock.patch.object(sys, "argv", ["cli.py"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 1


# ── 2. health command ─────────────────────────────────────────────────────

def test_health(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.health = mock.AsyncMock(return_value={"status": "ok"})

        with mock.patch.object(sys, "argv", ["cli.py", "health"]):
            main()

    MockClient.assert_called_once()
    assert client.health.call_count == 1
    out, _ = capsys.readouterr()
    assert '"status"' in out
    assert '"ok"' in out


# ── 3. navigate + --session ───────────────────────────────────────────────

def test_navigate_with_session(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.navigate = mock.AsyncMock(return_value={"url": "https://example.com", "title": "Example"})

        with mock.patch.object(sys, "argv", ["cli.py", "navigate", "https://example.com", "--session", "my-session"]):
            main()

    MockClient.assert_called_once()
    # navigate should receive positional arg url and session_id kwarg
    call_args = client.navigate.call_args
    assert call_args is not None
    # Check that url was passed as positional
    assert len(call_args[0]) == 1
    assert call_args[0][0] == "https://example.com"
    assert call_args[1].get("session_id") == "my-session"


# ── 4. text command — 5000 char truncation ────────────────────────────────

def test_text_truncates_to_5000(capsys):
    long_text = "A" * 6000
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.get_text = mock.AsyncMock(return_value={"text": long_text})

        with mock.patch.object(sys, "argv", ["cli.py", "text"]):
            main()

    call_args = client.get_text.call_args
    assert call_args is not None
    out, _ = capsys.readouterr()
    assert len(out.strip()) == 5000


# ── 5. screenshot writes file ─────────────────────────────────────────────

def test_screenshot_writes_file(tmp_path):
    png_content = b"PNG\x00\x01"
    out_file = str(tmp_path / "test-screenshot.png")

    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.screenshot = mock.AsyncMock(return_value=png_content)

        with mock.patch.object(sys, "argv", ["cli.py", "screenshot", "-o", out_file]):
            main()

    # Verify file was written correctly
    assert os.path.exists(out_file)
    assert open(out_file, "rb").read() == png_content


def test_screenshot_default_output(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.screenshot = mock.AsyncMock(return_value=b"PNG")

        with mock.patch.object(sys, "argv", ["cli.py", "screenshot"]):
            main()

    out, _ = capsys.readouterr()
    assert "/tmp/ai-browser-screenshot.png" in out


# ── 6. execute-js --stdin reads from sys.stdin ────────────────────────────

def test_execute_js_stdin(capsys):
    js_code = "document.title"
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.execute_js = mock.AsyncMock(return_value={"result": "Example Domain"})

        stdin_mock = io.StringIO(js_code)
        with mock.patch.object(sys, "argv", ["cli.py", "execute-js", "--stdin"]):
            with mock.patch.object(sys, "stdin", stdin_mock):
                main()

    assert client.execute_js.call_count == 1
    call_args = client.execute_js.call_args
    assert call_args[0][0] == js_code


def test_execute_js_script_arg(capsys):
    js_code = "window.location.href"
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.execute_js = mock.AsyncMock(return_value={"result": "https://example.com"})

        with mock.patch.object(sys, "argv", ["cli.py", "execute-js", "--script", js_code]):
            main()

    call_args = client.execute_js.call_args
    assert call_args[0][0] == js_code


def test_execute_js_no_input_error(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        with mock.patch.object(sys, "argv", ["cli.py", "execute-js"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
    assert exc_info.value.code == 1


# ── 7. create-session does NOT inject global --session ─────────────────────

def test_create_session_no_session_injection(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.create_session = mock.AsyncMock(return_value={"session_id": "foo"})

        # Even though --session default is passed, it should NOT be forwarded
        with mock.patch.object(sys, "argv", ["cli.py", "create-session", "foo", "--session", "default"]):
            main()

    assert client.create_session.call_count == 1
    # Should be called with just the positional session_id 'foo', nothing else
    call_args = client.create_session.call_args
    assert call_args[0][0] == "foo"
    assert call_args[1] == {}  # No kwargs injected


# ── 8. delete-session does NOT inject global --session ─────────────────────

def test_delete_session_no_session_injection(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.delete_session = mock.AsyncMock(return_value={"deleted": True})

        # Same — --session default should NOT leak into delete_session()
        with mock.patch.object(sys, "argv", ["cli.py", "delete-session", "foo", "--session", "default"]):
            main()

    assert client.delete_session.call_count == 1
    call_args = client.delete_session.call_args
    assert call_args[0][0] == "foo"
    assert call_args[1] == {}  # No kwargs injected


# ── 9. connection error handling ───────────────────────────────────────────

def test_connection_error_health(capsys):
    from src.client import BrowserClientError

    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.health = mock.AsyncMock(side_effect=BrowserClientError("connection refused"))

        with mock.patch.object(sys, "argv", ["cli.py", "health"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

    assert exc_info.value.code == 1


def test_connection_error_generic(capsys):
    """OSError / connection errors produce friendly message and SystemExit(1)."""
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        # Simulate the BrowserClient constructor raising (connection refused)
        MockClient.side_effect = OSError("Connection refused to 127.0.0.1:8888")

        with mock.patch.object(sys, "argv", ["cli.py", "health"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

    assert exc_info.value.code == 1


def test_unknown_command_causes_exit(capsys):
    """argparse handles unknown subcommands — should not reach our code."""
    with mock.patch.object(sys, "argv", ["cli.py", "nonexistent-cmd"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 2


# ── Additional command coverage (spot checks across all handlers) ───────────

def test_go_back(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.go_back = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "go-back"]):
            main()
    assert client.go_back.call_count == 1


def test_go_forward(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.go_forward = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "go-forward"]):
            main()
    assert client.go_forward.call_count == 1


def test_reload(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.reload = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "reload"]):
            main()
    assert client.reload.call_count == 1


def test_screenshot_annotated(capsys, tmp_path):
    out_file = str(tmp_path / "annotated.png")
    png_data = b"ANNOTATED"
    elems_text = "button#submit [70,40]"
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.screenshot_annotated = mock.AsyncMock(
            return_value=(png_data, elems_text, 5)
        )
        with mock.patch.object(sys, "argv", ["cli.py", "screenshot-annotated", "-o", out_file]):
            main()
    assert os.path.exists(out_file)
    assert open(out_file, "rb").read() == png_data
    out, _ = capsys.readouterr()
    assert "Element count: 5" in out
    assert elems_text in out


def test_look_saves_screenshot(capsys, tmp_path):
    import base64
    out_file = str(tmp_path / "look.png")
    png_data = b"LOOK"
    look_result = {
        "screenshot_base64": base64.b64encode(png_data).decode("utf-8"),
        "text": "page text",
        "title": "Title",
        "url": "https://example.com",
        "engine": "stealth",
        "tab_id": "tab-0",
        "block": {"blocked": False, "confidence": 0.0, "signals": []},
    }
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.look = mock.AsyncMock(return_value=look_result)
        with mock.patch.object(sys, "argv", ["cli.py", "look", "-o", out_file]):
            main()
    assert os.path.exists(out_file)
    assert open(out_file, "rb").read() == png_data
    out, err = capsys.readouterr()
    assert "page text" in out
    assert "Title" in err


def test_look_no_screenshot(capsys):
    import base64
    look_result = {
        "screenshot_base64": base64.b64encode(b"LOOK").decode("utf-8"),
        "text": "page text",
        "title": "Title",
        "url": "https://example.com",
        "engine": "stealth",
        "tab_id": "tab-0",
        "block": {"blocked": False, "confidence": 0.0, "signals": []},
    }
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.look = mock.AsyncMock(return_value=look_result)
        with mock.patch.object(sys, "argv", ["cli.py", "look", "--no-screenshot"]):
            main()
    call_args = client.look.call_args
    assert call_args[1]["max_length"] == 20000
    out, err = capsys.readouterr()
    assert "page text" in out
    assert "Title" in err


def test_click(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.click = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "click", "100", "200"]):
            main()
    call_args = client.click.call_args
    assert call_args[0][0] == 100
    assert call_args[0][1] == 200


def test_type(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.type_text = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "type", "hello world"]):
            main()
    call_args = client.type_text.call_args
    assert call_args[0][0] == "hello world"


def test_scroll(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.scroll = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "scroll", "-d", "up", "-n", "5"]):
            main()
    call_args = client.scroll.call_args
    assert call_args[1]["direction"] == "up"
    assert call_args[1]["amount"] == 5


def test_scroll_defaults(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.scroll = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "scroll"]):
            main()
    call_args = client.scroll.call_args
    assert call_args[1]["direction"] == "down"
    assert call_args[1]["amount"] == 3


def test_press_key(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.press_key = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "key", "Return"]):
            main()
    call_args = client.press_key.call_args
    assert call_args[0][0] == "Return"


def test_check(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.check = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "check", "#agree"]):
            main()
    call_args = client.check.call_args
    assert call_args[0][0] == "#agree"


def test_uncheck(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.uncheck = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "uncheck", "#agree"]):
            main()
    call_args = client.uncheck.call_args
    assert call_args[0][0] == "#agree"


def test_select(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.select = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "select", "#country", "US"]):
            main()
    call_args = client.select.call_args
    assert call_args[0][0] == "#country"
    assert call_args[0][1] == "US"


def test_engine(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.set_engine = mock.AsyncMock(return_value={"engine": "stealth"})
        with mock.patch.object(sys, "argv", ["cli.py", "engine", "stealth"]):
            main()
    call_args = client.set_engine.call_args
    assert call_args[0][0] == "stealth"


def test_wait_for_element_text(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.wait_for_element = mock.AsyncMock(return_value={"found": True, "elapsed": 1})
        with mock.patch.object(sys, "argv", ["cli.py", "wait-for-element", "--text", "submit"]):
            main()
    call_args = client.wait_for_element.call_args
    assert call_args[1]["text"] == "submit"


def test_wait_for_element_selector(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.wait_for_element = mock.AsyncMock(return_value={"found": True, "elapsed": 0})
        with mock.patch.object(sys, "argv", ["cli.py", "wait-for-element", "--selector", "#btn"]):
            main()
    call_args = client.wait_for_element.call_args
    assert call_args[1]["selector"] == "#btn"


def test_wait_for_element_timeout_default(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.wait_for_element = mock.AsyncMock(return_value={"found": True, "elapsed": 0})
        with mock.patch.object(sys, "argv", ["cli.py", "wait-for-element", "--selector", "#x"]):
            main()
    call_args = client.wait_for_element.call_args
    assert call_args[1]["timeout"] == 10


def test_wait_for_element_requires_at_least_one(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        with mock.patch.object(sys, "argv", ["cli.py", "wait-for-element"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
    assert exc_info.value.code == 1


def test_find_element(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.find_element = mock.AsyncMock(return_value={"x": 50, "y": 100})
        with mock.patch.object(sys, "argv", ["cli.py", "find-element", "Submit", "--type", "button"]):
            main()
    call_args = client.find_element.call_args
    assert call_args[0][0] == "Submit"
    assert call_args[1]["element_type"] == "button"


def test_find_element_default_type(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.find_element = mock.AsyncMock(return_value={"x": 50, "y": 100})
        with mock.patch.object(sys, "argv", ["cli.py", "find-element", "Submit"]):
            main()
    call_args = client.find_element.call_args
    assert call_args[1]["element_type"] == "any"


def test_search(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.search = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "search", "hello world"]):
            main()
    call_args = client.search.call_args
    assert call_args[0][0] == "hello world"


def test_extract_links(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.extract_links = mock.AsyncMock(return_value={"links": [{"text": "home", "href": "/"}]})
        with mock.patch.object(sys, "argv", ["cli.py", "extract-links"]):
            main()
    call_args = client.extract_links.call_args
    assert call_args[1]["filter_text"] is None
    assert call_args[1]["same_domain_only"] is False


def test_extract_links_filter(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.extract_links = mock.AsyncMock(return_value={"links": []})
        with mock.patch.object(sys, "argv", ["cli.py", "extract-links", "--filter", "image"]):
            main()
    call_args = client.extract_links.call_args
    assert call_args[1]["filter_text"] == "image"


def test_extract_table(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.extract_table = mock.AsyncMock(return_value={"headers": ["a", "b"], "rows": [["1", "2"]], "index": 0})
        with mock.patch.object(sys, "argv", ["cli.py", "extract-table", "--index", "1"]):
            main()
    call_args = client.extract_table.call_args
    assert call_args[1]["index"] == 1


def test_get_meta(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.get_meta = mock.AsyncMock(return_value={"title": "Test", "url": "https://x.com"})
        with mock.patch.object(sys, "argv", ["cli.py", "get-meta"]):
            main()
    assert client.get_meta.call_count == 1


def test_title(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.get_title = mock.AsyncMock(return_value={"title": "Hello World"})
        with mock.patch.object(sys, "argv", ["cli.py", "title"]):
            main()
    out, _ = capsys.readouterr()
    assert '"Hello World"' in out


def test_url(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.get_url = mock.AsyncMock(return_value={"url": "https://example.com"})
        with mock.patch.object(sys, "argv", ["cli.py", "url"]):
            main()
    out, _ = capsys.readouterr()
    assert '"https://example.com"' in out


def test_list_sessions(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.list_sessions = mock.AsyncMock(return_value={"sessions": []})
        with mock.patch.object(sys, "argv", ["cli.py", "list-sessions"]):
            main()
    assert client.list_sessions.call_count == 1


def test_get_cookies(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.get_cookies = mock.AsyncMock(return_value={"cookies": []})
        with mock.patch.object(sys, "argv", ["cli.py", "get-cookies"]):
            main()
    assert client.get_cookies.call_count == 1


def test_set_cookie(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.set_cookie = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "set-cookie", "name", "value", "--domain", "example.com"]):
            main()
    call_args = client.set_cookie.call_args
    assert call_args[0][0] == "name"
    assert call_args[0][1] == "value"
    assert call_args[0][2] == "example.com"


def test_set_cookie_defaults(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.set_cookie = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "set-cookie", "n", "v", "--domain", "d.com"]):
            main()
    call_args = client.set_cookie.call_args
    assert call_args[1]["path"] == "/"
    assert call_args[1]["expires"] == -1


def test_clear_cookies(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.clear_cookies = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "clear-cookies"]):
            main()
    assert client.clear_cookies.call_count == 1


def test_open_tab_default_url(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.open_tab = mock.AsyncMock(return_value={"tab_id": "tab-0"})
        with mock.patch.object(sys, "argv", ["cli.py", "open-tab"]):
            main()
    call_args = client.open_tab.call_args
    assert call_args[0][0] == "about:blank"


def test_open_tab_custom_url(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.open_tab = mock.AsyncMock(return_value={"tab_id": "tab-1"})
        with mock.patch.object(sys, "argv", ["cli.py", "open-tab", "https://example.com"]):
            main()
    call_args = client.open_tab.call_args
    assert call_args[0][0] == "https://example.com"


def test_switch_tab(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.switch_tab = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "switch-tab", "tab-0"]):
            main()
    call_args = client.switch_tab.call_args
    assert call_args[0][0] == "tab-0"


def test_close_tab(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.close_tab = mock.AsyncMock(return_value={"status": "ok"})
        with mock.patch.object(sys, "argv", ["cli.py", "close-tab", "tab-0"]):
            main()
    call_args = client.close_tab.call_args
    assert call_args[0][0] == "tab-0"


def test_list_tabs(capsys):
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.list_tabs = mock.AsyncMock(return_value={"tabs": []})
        with mock.patch.object(sys, "argv", ["cli.py", "list-tabs"]):
            main()
    assert client.list_tabs.call_count == 1


def test_text_output_default_no_session(capsys):
    """text command without --session should still work (None is passed as session_id)."""
    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.get_text = mock.AsyncMock(return_value={"text": "hello"})

        with mock.patch.object(sys, "argv", ["cli.py", "text"]):
            main()

    call_args = client.get_text.call_args
    assert call_args[1].get("session_id") is None  # No session provided → None


# ── Negative: BrowserClientError caught gracefully (not generic Exception) ─

def test_browser_client_error_message(capsys):
    from src.client import BrowserClientError

    with mock.patch("src.cli.BrowserClient") as MockClient:
        client = MockClient.return_value
        client.click = mock.AsyncMock(side_effect=BrowserClientError("element not found"))

        with mock.patch.object(sys, "argv", ["cli.py", "click", "10", "20"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
    assert exc_info.value.code == 1


# ── Verify BrowserClient is constructed with correct base_url from env ─

def test_base_url_from_env(capsys):
    old = os.environ.get("AI_BROWSER_BASE_URL")
    try:
        os.environ["AI_BROWSER_BASE_URL"] = "http://custom.host:9999"
        with mock.patch("src.cli.BrowserClient") as MockClient:
            client = MockClient.return_value
            client.health = mock.AsyncMock(return_value={"status": "ok"})

            with mock.patch.object(sys, "argv", ["cli.py", "health"]):
                main()

        call_kwargs = MockClient.call_args[1]
        assert call_kwargs.get("base_url") == "http://custom.host:9999"
    finally:
        if old is not None:
            os.environ["AI_BROWSER_BASE_URL"] = old
        elif "AI_BROWSER_BASE_URL" in os.environ:
            del os.environ["AI_BROWSER_BASE_URL"]


# ── Verify default base_url when env var not set ─

def test_default_base_url(capsys):
    old = os.environ.get("AI_BROWSER_BASE_URL")
    try:
        os.environ.pop("AI_BROWSER_BASE_URL", None)
        with mock.patch("src.cli.BrowserClient") as MockClient:
            client = MockClient.return_value
            client.health = mock.AsyncMock(return_value={"status": "ok"})

            with mock.patch.object(sys, "argv", ["cli.py", "health"]):
                main()

        call_kwargs = MockClient.call_args[1]
        assert call_kwargs.get("base_url") == "http://127.0.0.1:8888"
    finally:
        if old is not None:
            os.environ["AI_BROWSER_BASE_URL"] = old
