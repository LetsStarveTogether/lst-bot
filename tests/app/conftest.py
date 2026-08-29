from os import environ

import pytest

from lst_bot.settings import Settings


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse]
def isolate_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(environ):
        if name.casefold() in Settings.model_fields:
            monkeypatch.delenv(name)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("DOSU_MCP_ENDPOINT", "https://example.com/mcp")
    monkeypatch.setenv("DOSU_API_KEY", "test")
