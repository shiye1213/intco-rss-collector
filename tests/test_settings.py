from app.database import Database
from app.main import SettingsPayload


def test_web_crawler_setting_defaults_off(tmp_path) -> None:
    database = Database(tmp_path / "settings.db")
    database.initialize()
    payload = SettingsPayload(schedule_time="09:15")

    assert database.get_settings()["crawler_enabled"] == "false"
    assert payload.crawler_enabled is False


def test_web_crawler_setting_accepts_explicit_enable() -> None:
    payload = SettingsPayload(
        schedule_time="09:15",
        crawler_enabled=True,
    )

    assert payload.crawler_enabled is True
