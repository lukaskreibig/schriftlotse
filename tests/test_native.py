from __future__ import annotations

from pathlib import Path

SOURCE = Path("macos/SchriftLotseApp.m").read_text(encoding="utf-8")


def test_native_wrapper_provides_file_panel_and_standard_edit_actions() -> None:
    assert "WKUIDelegate" in SOURCE
    assert "runOpenPanelWithParameters" in SOURCE
    assert "@selector(paste:)" in SOURCE
    assert 'keyEquivalent:@"v"' in SOURCE


def test_native_wrapper_owns_an_identified_dynamic_backend() -> None:
    assert "unusedLoopbackPort" in SOURCE
    assert "SCHRIFTLOTSE_INSTANCE_TOKEN" in SOURCE
    assert 'URLByAppendingPathComponent:@"api/health"' in SOURCE
