"""docs/configuration.md must name every setting there is: each field of
app.config.Settings as its environment variable, and each Settings -> System
key from app/system_settings.py. A setting added to the code without a line
in the guide fails here, with the names that are missing."""

from pathlib import Path

from app import system_settings
from app.config import Settings

CONFIGURATION_DOC = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"


def _doc_text() -> str:
    return CONFIGURATION_DOC.read_text(encoding="utf-8")


def _missing(names, text: str) -> list[str]:
    # The exact name in code formatting, so `LOG_DIR` isn't found inside
    # `LOG_DIRECTORY` or `password_history` inside another key.
    return sorted(name for name in names if f"`{name}`" not in text)


def env_var_names() -> list[str]:
    """Each Settings field as pydantic-settings reads it from the
    environment: the field name with the env prefix, upper-cased."""
    prefix = Settings.model_config.get("env_prefix", "")
    return [f"{prefix}{name}".upper() for name in Settings.model_fields]


def system_setting_keys() -> list[str]:
    return [
        *system_settings.BOOL_DEFAULTS,
        *system_settings.CHOICE_DEFAULTS,
        *system_settings.INT_DEFAULTS,
    ]


def test_the_lists_are_not_empty():
    # Guards the two tests below against passing on an empty list.
    assert "CREDENTIAL_ENCRYPTION_KEY" in env_var_names()
    assert "backup_interval_hours" in system_setting_keys()


def test_every_environment_variable_is_documented():
    missing = _missing(env_var_names(), _doc_text())
    assert not missing, (
        f"{CONFIGURATION_DOC.name} doesn't document these environment variables "
        f"(Settings fields in app/config.py): {', '.join(missing)}. Add each one, in "
        "backticks, with its default, accepted values and effect."
    )


def test_every_system_setting_is_documented():
    missing = _missing(system_setting_keys(), _doc_text())
    assert not missing, (
        f"{CONFIGURATION_DOC.name} doesn't document these system settings "
        f"(keys in app/system_settings.py): {', '.join(missing)}. Add each one, in "
        "backticks, with its label on screen, default, accepted values and effect."
    )
