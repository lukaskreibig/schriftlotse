from __future__ import annotations

import json
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
        page.locator('input[value="beste_qualitaet"]').check()
        assert page.locator("#start").is_visible()
        assert page.locator("#start").evaluate(
            "element => { const r = element.getBoundingClientRect(); "
            "const top = document.elementFromPoint(r.left + r.width / 2, r.top + 2); "
            "return top === element || element.contains(top); }"
        )
        page.get_by_role("button", name="Einstellungen").click()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        for selector in ("#save-settings", "#openrouter-key", "#key-status", "#system-status"):
            box = page.locator(selector).bounding_box()
            assert box is not None
            assert box["x"] >= 0 and box["y"] >= 0
            assert box["x"] + box["width"] <= width + 1
            assert box["y"] + box["height"] <= height + 1
        browser.close()


def test_search_shows_loading_completion_and_empty_feedback(ui_server) -> None:
    url, _root = ui_server
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        page = browser.new_page(viewport={"width": 1320, "height": 860})
        pending = []

        def hold_search(route) -> None:
            pending.append(route)

        page.route("**/api/search", hold_search)
        page.goto(url)
        page.get_by_role("button", name="Prüfen & suchen").click()
        page.locator("#query").fill("Hermann")
        page.locator("#search-button").click()

        page.wait_for_function("() => document.querySelector('#search-button').ariaBusy === 'true'")
        assert "Suche läuft" in page.locator("#search-button").inner_text()
        assert "Archiv wird durchsucht" in page.locator("#search-status").inner_text()
        assert "ähnliche Lesarten" in page.locator("#results").inner_text()
        assert pending

        pending.pop().fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps([]),
        )
        page.wait_for_function("() => document.querySelector('#search-button').ariaBusy === null")
        assert page.locator("#search-button").inner_text() == "Suchen"
        assert "0 Treffer in" in page.locator("#search-status").inner_text()
        assert "Keine Treffer für „Hermann“" in page.locator("#empty-results").inner_text()
        browser.close()


def test_system_refresh_shows_an_in_place_loading_state(ui_server) -> None:
    url, _root = ui_server
    with playwright.sync_playwright() as runtime:
        browser = _browser(runtime)
        page = browser.new_page(viewport={"width": 1320, "height": 860})
        pending = []
        hold_refresh = {"enabled": False}
        system_payload = {
            "local": True,
            "version": "0.2.0",
            "documents": 0,
            "pages": 0,
            "lines": 0,
            "models_installed": 0,
            "models_total": 9,
            "tesseract_available": False,
            "database": "/tmp/schriftlotse.sqlite3",
            "output": "/tmp/SchriftLotse",
            "cache": "/tmp/SchriftLotse/cache",
            "openrouter_configured": False,
        }

        def route_system(route) -> None:
            if hold_refresh["enabled"]:
                pending.append(route)
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(system_payload),
            )

        page.route("**/api/system", route_system)
        page.goto(url)
        page.get_by_role("button", name="Einstellungen").click()
        page.locator("#system-status .system-row").first.wait_for()
        hold_refresh["enabled"] = True
        page.locator("#refresh-system").click()
        page.wait_for_function(
            "() => document.querySelector('#refresh-system').ariaBusy === 'true'"
        )
        assert "Wird geprüft" in page.locator("#refresh-system").inner_text()
        assert "Lokale Komponenten werden geprüft" in page.locator("#system-status").inner_text()
        assert pending

        pending.pop().fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(system_payload),
        )
        page.wait_for_function("() => document.querySelector('#refresh-system').ariaBusy === null")
        assert page.locator("#refresh-system").inner_text() == "Aktualisieren"
        assert page.locator("#system-status .system-row").count() >= 7
        browser.close()
