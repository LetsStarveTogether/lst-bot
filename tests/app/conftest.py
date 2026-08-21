from __future__ import annotations

from os import environ

import pytest

from lst_bot.settings import Settings


@pytest.fixture(autouse=True)
def isolate_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(environ):
        if name.casefold() in Settings.model_fields:
            monkeypatch.delenv(name)
