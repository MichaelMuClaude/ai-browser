"""Unit tests for NativeEngine window discovery without requiring X11."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.core.native_os import NativeEngine


def _result(stdout: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


@pytest.mark.asyncio
async def test_find_window_prefers_active_browser_when_pid_search_misses():
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")
    engine.browser_pid = 1234

    async def fake_xdotool(args, **kwargs):
        if args[:3] == ["search", "--pid", "1234"]:
            return _result("", 1)
        if args == ["getactivewindow"]:
            return _result("777\n")
        if args == ["getwindowclassname", "777"]:
            return _result("Google-chrome\n")
        raise AssertionError(f"unexpected xdotool args: {args}")

    engine._run_xdotool = AsyncMock(side_effect=fake_xdotool)

    assert await engine._find_window_id() == "777"


@pytest.mark.asyncio
async def test_ensure_window_ready_relocates_lost_window():
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")
    engine.browser_pid = 1234
    engine.window_id = "old"

    with patch.object(engine, "_window_exists", AsyncMock(side_effect=[False, True])):
        with patch.object(engine, "_find_window_id", AsyncMock(return_value="new")) as find_window:
            with patch("src.core.native_os.asyncio.sleep", AsyncMock()):
                engine._run_xdotool = AsyncMock(return_value=_result())

                await engine._ensure_window_ready()

    find_window.assert_awaited_once_with()
    assert engine.window_id == "new"
    assert engine._run_xdotool.await_args_list[0].args[0] == ["windowactivate", "new"]
    assert engine._run_xdotool.await_args_list[1].args[0] == ["windowraise", "new"]


@pytest.mark.asyncio
async def test_ensure_window_ready_stops_once_window_is_foreground():
    """No extra activate/raise retries once getactivewindow confirms our window."""
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")
    engine.window_id = "42"

    async def fake_xdotool(args, **kwargs):
        if args == ["getactivewindow"]:
            return _result("42\n")
        return _result()

    with patch.object(engine, "_window_exists", AsyncMock(return_value=True)):
        with patch("src.core.native_os.asyncio.sleep", AsyncMock()):
            engine._run_xdotool = AsyncMock(side_effect=fake_xdotool)
            await engine._ensure_window_ready()

    activate_calls = [
        c for c in engine._run_xdotool.await_args_list if c.args[0] == ["windowactivate", "42"]
    ]
    assert len(activate_calls) == 1


@pytest.mark.asyncio
async def test_ensure_window_ready_retries_when_another_window_stays_foreground():
    """Real case (2026-07-26): another app's window stayed on top after raise —
    a Kmart screenshot ended up with a terminal panel bled into it. Ensure the
    retry loop actually retries (up to 3x) instead of silently proceeding after
    a single raise attempt, and doesn't raise even if it never wins the race."""
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")
    engine.window_id = "42"

    async def fake_xdotool(args, **kwargs):
        if args == ["getactivewindow"]:
            return _result("999\n")  # some other window always stays foreground
        return _result()

    with patch.object(engine, "_window_exists", AsyncMock(return_value=True)):
        with patch("src.core.native_os.asyncio.sleep", AsyncMock()):
            engine._run_xdotool = AsyncMock(side_effect=fake_xdotool)
            await engine._ensure_window_ready()  # must not raise

    activate_calls = [
        c for c in engine._run_xdotool.await_args_list if c.args[0] == ["windowactivate", "42"]
    ]
    assert len(activate_calls) == 3


@pytest.mark.asyncio
async def test_refresh_window_id_updates_when_browser_focus_moves():
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")
    engine.window_id = "old"

    with patch.object(engine, "_find_window_id", AsyncMock(return_value="new")):
        await engine._refresh_window_id()

    assert engine.window_id == "new"


@pytest.mark.asyncio
async def test_screenshot_retries_after_window_refresh():
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")
    engine._started = True
    engine.window_id = "old"

    async def recover(operation, error):
        engine.window_id = "new"

    with patch.object(engine, "_ensure_window_ready", AsyncMock()):
        with patch.object(engine, "_recover_for_retry", AsyncMock(side_effect=recover)) as recover_retry:
            with patch.object(
                engine,
                "_screenshot_once",
                AsyncMock(side_effect=[RuntimeError("lost window"), b"png-data"]),
            ) as screenshot_once:
                result = await engine.screenshot()

    assert result == b"png-data"
    assert screenshot_once.await_count == 2
    recover_retry.assert_awaited_once()
    assert recover_retry.await_args.args[0] == "screenshot"
    assert engine.window_id == "new"


@pytest.mark.asyncio
async def test_click_retries_after_window_refresh():
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")
    engine._started = True
    engine.window_id = "old"

    async def recover(operation, error):
        engine.window_id = "new"

    engine._run_xdotool = AsyncMock(
        side_effect=[
            RuntimeError("lost window"),
            _result(),
            _result(),
        ]
    )

    with patch("src.core.native_os.asyncio.sleep", AsyncMock()):
        with patch.object(engine, "_ensure_window_ready", AsyncMock()):
            with patch.object(engine, "_recover_for_retry", AsyncMock(side_effect=recover)) as recover_retry:
                result = await engine.click(100, 120)

    assert result == {"x": 100, "y": 120, "window_id": "new", "method": "native_os"}
    assert engine._run_xdotool.await_count == 3
    recover_retry.assert_awaited_once()
    assert recover_retry.await_args.args[0] == "click"
    assert engine._run_xdotool.await_args_list[1].args[0] == [
        "mousemove",
        "--window",
        "new",
        "100",
        "120",
    ]


@pytest.mark.asyncio
async def test_type_text_retries_after_window_refresh():
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")
    engine._started = True
    engine.window_id = "old"

    async def recover(operation, error):
        engine.window_id = "new"

    engine._run_xdotool = AsyncMock(side_effect=[RuntimeError("lost window"), _result()])

    with patch.object(engine, "_ensure_window_ready", AsyncMock()):
        with patch.object(engine, "_ensure_english_input_method", AsyncMock()):
            with patch.object(engine, "_recover_for_retry", AsyncMock(side_effect=recover)) as recover_retry:
                result = await engine.type_text("hello")

    assert result == {"text": "hello", "window_id": "new", "method": "native_os"}
    assert engine._run_xdotool.await_count == 2
    recover_retry.assert_awaited_once()
    assert recover_retry.await_args.args[0] == "type_text"
    assert engine._run_xdotool.await_args_list[1].args[0] == [
        "type",
        "--window",
        "new",
        "--delay",
        "30",
        "hello",
    ]


@pytest.mark.asyncio
async def test_ensure_english_input_method_switches_ibus():
    """Real case (2026-07-26): system IBus sitting on libpinyin (Chinese pinyin)
    turned typed "floor lamp" into garbled Chinese candidate characters, because
    xdotool's synthetic keystrokes pass through the system input method first.
    type_text() must force IBus to English before every type, not just once."""
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")

    with patch("src.core.native_os.shutil.which", return_value="/usr/bin/ibus"):
        with patch("src.core.native_os.subprocess.run") as mock_run:
            await engine._ensure_english_input_method()

    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["ibus", "engine", "xkb:us::eng"]


@pytest.mark.asyncio
async def test_ensure_english_input_method_noop_without_ibus():
    """Machines without ibus installed shouldn't error — just skip the switch."""
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")

    with patch("src.core.native_os.shutil.which", return_value=None):
        with patch("src.core.native_os.subprocess.run") as mock_run:
            await engine._ensure_english_input_method()

    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_english_input_method_respects_config_toggle():
    """AI_BROWSER_NATIVE_FORCE_ENGLISH_IME=false must fully disable the switch."""
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")

    with patch("src.core.native_os.NATIVE_FORCE_ENGLISH_IME", False):
        with patch("src.core.native_os.shutil.which", return_value="/usr/bin/ibus"):
            with patch("src.core.native_os.subprocess.run") as mock_run:
                await engine._ensure_english_input_method()

    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_english_input_method_failure_does_not_raise():
    """A broken ibus call must not block typing outright."""
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")

    with patch("src.core.native_os.shutil.which", return_value="/usr/bin/ibus"):
        with patch("src.core.native_os.subprocess.run", side_effect=OSError("boom")):
            await engine._ensure_english_input_method()  # must not raise


@pytest.mark.asyncio
async def test_type_text_switches_input_method_before_typing():
    """type_text() must call the IME switch before sending the xdotool type."""
    engine = NativeEngine(browser_cmd=["google-chrome", "--new-window"], window_title="Chrome")
    engine._started = True
    engine.window_id = "42"
    engine._run_xdotool = AsyncMock(return_value=_result())

    with patch.object(engine, "_ensure_window_ready", AsyncMock()):
        with patch.object(engine, "_ensure_english_input_method", AsyncMock()) as ensure_english:
            await engine.type_text("floor lamp")

    ensure_english.assert_awaited_once_with()
