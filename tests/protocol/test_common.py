from __future__ import annotations

import pytest
from pydantic import ValidationError

from lst_bot import BotSelf, Version


@pytest.mark.parametrize(
    "payload",
    [
        {"platform": "QQ", "user_id": "10000"},
        {"platform": "-qq", "user_id": "10000"},
        {"platform": "qq..guild", "user_id": "10000"},
    ],
)
def test_self_platform_uses_protocol_name_format(payload: object) -> None:
    with pytest.raises(ValidationError):
        BotSelf.model_validate(payload)


def test_self_is_frozen_value_key() -> None:
    self_ = BotSelf(platform="qq", user_id="10000")
    same_self = BotSelf(platform="qq", user_id="10000")

    assert {self_: "connected"}[same_self] == "connected"
    with pytest.raises(ValidationError):
        self_.user_id = "10001"


def test_version_impl_uses_protocol_name_format() -> None:
    assert Version(impl="lst-bot", version="0.1.0").onebot_version == "12"
    with pytest.raises(ValidationError):
        Version(impl="BadName", version="0.1.0")
