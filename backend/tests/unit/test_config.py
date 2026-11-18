import pytest
from pydantic import ValidationError

from app.core.config import Settings

SECRET = "x" * 40


def test_production_runtime_does_not_need_owner_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_OWNER_PASSWORD", raising=False)
    s = Settings(app_env="production", app_secret_key=SECRET, db_app_password="app-pw")
    assert s.app_database_url.username == "localops_app"
    with pytest.raises(ValueError, match="DB_OWNER_PASSWORD"):
        _ = s.owner_database_url


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_secret_key": "CHANGE_ME" + "x" * 32},
        {"db_app_password": ""},
        {"db_app_password": "CHANGE_ME"},
    ],
)
def test_production_rejects_placeholders(overrides: dict[str, str]) -> None:
    values = {"app_env": "production", "app_secret_key": SECRET, "db_app_password": "pw"}
    values.update(overrides)
    with pytest.raises(ValidationError):
        Settings(**values)  # type: ignore[arg-type]


def test_short_secret_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(app_secret_key="short")


def test_password_with_special_characters_is_escaped() -> None:
    s = Settings(app_secret_key=SECRET, db_app_password="p@ss:w/rd#1")
    rendered = s.app_database_url.render_as_string(hide_password=False)
    assert "p%40ss%3Aw%2Frd%231" in rendered
