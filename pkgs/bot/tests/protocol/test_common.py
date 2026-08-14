from __future__ import annotations

import pytest
from bot import BotSelf, BotStatus, Status, Version
from bot.protocol.base import Model
from pydantic import ValidationError


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"platform": "QQ", "user_id": "10000"},
            id="uppercase-platform",
        ),
        pytest.param(
            {"platform": "-qq", "user_id": "10000"},
            id="leading-hyphen",
        ),
        pytest.param(
            {"platform": "qq..guild", "user_id": "10000"},
            id="empty-name-component",
        ),
    ],
)
def test_bot_self_rejects_invalid_platform_name(payload: object) -> None:
    with pytest.raises(ValidationError):
        BotSelf.model_validate(payload)


def test_bot_self_is_a_frozen_value_key() -> None:
    self_ = BotSelf(platform="qq", user_id="10000")

    assert {self_: "connected"}[BotSelf(platform="qq", user_id="10000")] == (
        "connected"
    )
    field = "user_id"
    with pytest.raises(ValidationError):
        setattr(self_, field, "10001")


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        pytest.param(
            BotSelf,
            {"platform": "qq", "user_id": "10000"},
            id="bot-self",
        ),
        pytest.param(
            Version,
            {
                "impl": "lst-bot",
                "version": "1.0.0",
                "onebot_version": "12",
            },
            id="version",
        ),
        pytest.param(
            BotStatus,
            {
                "self": {"platform": "qq", "user_id": "10000"},
                "online": True,
            },
            id="bot-status",
        ),
        pytest.param(
            Status,
            {
                "good": True,
                "bots": [
                    {
                        "self": {"platform": "qq", "user_id": "10000"},
                        "online": True,
                    },
                ],
            },
            id="status",
        ),
    ],
)
def test_common_models_round_trip_json(
    model: type[Model],
    payload: dict[str, object],
) -> None:
    value = model.model_validate(payload)

    assert model.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        pytest.param(
            Version,
            {"impl": "lst-bot", "version": "1.0.0"},
            id="version-missing-onebot-version",
        ),
        pytest.param(
            Status,
            {"good": True},
            id="status-missing-bots",
        ),
    ],
)
def test_common_models_require_wire_fields(
    model: type[Model],
    payload: object,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_version_rejects_invalid_implementation_name() -> None:
    with pytest.raises(ValidationError):
        Version(impl="BadName", version="1.0.0", onebot_version="12")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(float("nan"), id="nan-python"),
        pytest.param(float("inf"), id="positive-infinity-python"),
        pytest.param(float("-inf"), id="negative-infinity-python"),
    ],
)
def test_model_rejects_nested_non_finite_python_numbers(value: float) -> None:
    with pytest.raises(ValidationError):
        Model.model_validate({"extension": {"values": [value]}})


def test_model_revalidates_nested_instances() -> None:
    self_ = BotSelf.model_construct(
        platform="qq",
        user_id="10000",
        extension=float("nan"),
    )

    with pytest.raises(ValidationError):
        BotStatus.model_validate({"self": self_, "online": True})


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("NaN", id="nan-json"),
        pytest.param("Infinity", id="positive-infinity-json"),
        pytest.param("-Infinity", id="negative-infinity-json"),
    ],
)
def test_model_rejects_non_finite_json_numbers(token: str) -> None:
    with pytest.raises(ValidationError):
        Model.model_validate_json(f'{{"value": {token}}}')


def test_model_preserves_nested_json_extension_values_and_null() -> None:
    payload = {
        "null": None,
        "object": {"array": [True, 1, 1.5, "value", None]},
    }

    value = Model.model_validate(payload)

    assert value.model_dump(mode="json") == payload
    assert Model.model_validate_json(value.model_dump_json()) == value


def test_model_rejects_non_json_extension_value() -> None:
    with pytest.raises(ValidationError):
        Model.model_validate({"value": object()})
