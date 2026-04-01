"""
Tests for src/config.py

These tests verify that the Settings model validates correctly.
They use environment variable injection to avoid requiring a real .env file.
"""
import os
import pytest
from pydantic import ValidationError


def make_valid_env() -> dict:
    return {
        "ANTHROPIC_API_KEY": "sk-ant-test-key",
        "ANTHROPIC_MODEL": "claude-sonnet-4-6",
        "NOTION_API_KEY": "secret_test_key",
        "NOTION_WORKSPACE_ROOT_PAGE_ID": "test-page-id",
        "CLICKUP_API_KEY": "pk_test_key",
        "CLICKUP_WORKSPACE_ID": "test-workspace-id",
    }


def test_settings_loads_valid_env(monkeypatch):
    env = make_valid_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    # Re-import to pick up monkeypatched env
    from importlib import reload
    import src.config as config_module
    reload(config_module)

    settings = config_module.Settings()
    assert settings.anthropic_model == "claude-sonnet-4-6"
    assert settings.app_env == "development"
    assert settings.log_level == "INFO"


def test_invalid_anthropic_key_raises(monkeypatch):
    env = make_valid_env()
    env["ANTHROPIC_API_KEY"] = "invalid-key"
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from src.config import Settings
    with pytest.raises(ValidationError, match="sk-ant-"):
        Settings()


def test_invalid_notion_key_raises(monkeypatch):
    env = make_valid_env()
    env["NOTION_API_KEY"] = "bad-key"
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from src.config import Settings
    with pytest.raises(ValidationError, match="secret_"):
        Settings()


def test_invalid_clickup_key_raises(monkeypatch):
    env = make_valid_env()
    env["CLICKUP_API_KEY"] = "bad-key"
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from src.config import Settings
    with pytest.raises(ValidationError, match="pk_"):
        Settings()


def test_optional_fields_have_defaults(monkeypatch):
    env = make_valid_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from src.config import Settings
    s = Settings()
    assert s.slack_bot_token is None
    assert s.clickup_default_space_id is None
    assert s.slack_channel_internal == "#agency-pipeline"
