from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from PIL import Image

playwright = pytest.importorskip("playwright.sync_api")

from schriftlotse.app import ApplicationState, create_app  # noqa: E402
from schriftlotse.config import AppPaths  # noqa: E402


def _free_port() -> int:
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return int(connection.getsockname()[1])


@pytest.fixture(scope="module")
def ui_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("ui")
    paths = AppPaths(
        data=root / "data",
        cache=root / "cache",
        models=root / "cache" / "models",
        output=root / "output",
        database=root / "data" / "ui.sqlite3",
        settings=root / "data" / "settings.json",
    )
    original = AppPaths.__dict__["default"]
    AppPaths.default = classmethod(lambda _cls: paths)  # type: ignore[method-assign]
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(ApplicationState()), host="127.0.0.1", port=port, log_level="error"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", root
    server.should_exit = True
    thread.join(timeout=5)
    AppPaths.default = original  # type: ignore[method-assign]


def _browser(playwright_instance):
    bundled = Path(playwright_instance.chromium.executable_path)
    mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    executable = bundled if bundled.is_file() else mac_chrome
    if not executable.is_file():
        pytest.skip("Kein Chromium/Chrome für UI-End-to-End-Test installiert")
    return playwright_instance.chromium.launch(headless=True, executable_path=str(executable))


def test_complete_desktop_workflow_and_compact_layout(ui_server) -> None:
    url, root = ui_server
    scan = root / "Hermann Test.jpg"
    Image.new("RGB", (80, 60), "white").save(scan)
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        page = browser.new_page(viewport={"width": 1320, "height": 860})
        page.goto(url)
        page.get_by_role("button", name="Dateien auswählen").wait_for()

        with page.expect_file_chooser() as chooser:
            page.get_by_role("button", name="Dateien auswählen").click()
        chooser.value.set_files(str(scan))
        page.get_by_text("1 Quelle ausgewählt").wait_for()

        page.locator("#script-combobox .combobox-trigger").click()
        page.get_by_role("option", name="Handschrift / Kurrent").click()
        assert page.locator("#script").input_value() == "handschrift"
        assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")
        assert "Dokumente entziffern" in page.locator("main").aria_snapshot()

        page.get_by_role("button", name="Einstellungen").click()
        page.locator("#openrouter-key").fill("sk-or-v1-test-pasteable")
        assert page.locator("#openrouter-key").input_value() == "sk-or-v1-test-pasteable"
        assert page.locator("#setting-cloud-model option").count() == 4
        assert page.evaluate("document.documentElement.scrollHeight <= window.innerHeight")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()


@pytest.mark.parametrize("width,height", [(1100, 800), (1320, 860)])
def test_primary_read_controls_stay_inside_native_viewport(ui_server, width, height) -> None:
    url, _root = ui_server
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(url)
        for selector in ("#choose-files", "#folder", "#start", "#script-combobox"):
            box = page.locator(selector).bounding_box()
            assert box is not None
            assert box["x"] >= 0 and box["y"] >= 0
            assert box["x"] + box["width"] <= width + 1
            assert box["y"] + box["height"] <= height + 1
        page.get_by_role("button", name="Einstellungen").click()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        for selector in ("#save-settings", "#openrouter-key", "#key-status", "#system-status"):
            box = page.locator(selector).bounding_box()
            assert box is not None
            assert box["x"] >= 0 and box["y"] >= 0
            assert box["x"] + box["width"] <= width + 1
            assert box["y"] + box["height"] <= height + 1
        browser.close()
