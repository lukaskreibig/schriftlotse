from __future__ import annotations

from schriftlotse.config import Settings


def test_settings_save_is_atomic_and_corruption_falls_back(app_paths) -> None:
    settings = Settings(default_quality="schnell", openrouter_profile="balanced")
    settings.save(app_paths)

    assert Settings.load(app_paths).default_quality == "schnell"
    assert not list(app_paths.data.glob("settings-*.json"))

    app_paths.settings.write_text("{broken", encoding="utf-8")
    assert Settings.load(app_paths) == Settings()
