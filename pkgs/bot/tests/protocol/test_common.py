import pytest
from bot import BotSelf, Status, Version
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
        pytest.param(
            {"platform": "qq\n", "user_id": "10000"},
            id="trailing-newline",
        ),
    ],
)
def test_bot_self_rejects_invalid_platform_name(payload: object) -> None:
    with pytest.raises(ValidationError):
        BotSelf.model_validate(payload)


def test_bot_self_is_a_value_key() -> None:
    self_ = BotSelf(platform="qq", user_id="10000")

    assert {self_: "connected"}[BotSelf(platform="qq", user_id="10000")] == (
        "connected"
    )


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


def test_model_hides_invalid_input() -> None:
    marker = f"sensitive-{id(object())}"

    with pytest.raises(ValidationError) as error:
        Status.model_validate({"good": marker, "bots": []})

    assert marker not in repr(error.value)
    assert marker not in str(error.value)


def test_models_are_frozen() -> None:
    status = Status(good=True, bots=[])
    field = "good"

    with pytest.raises(ValidationError):
        setattr(status, field, False)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("NaN", id="nan-json"),
        pytest.param("Infinity", id="positive-infinity-json"),
        pytest.param("-Infinity", id="negative-infinity-json"),
        pytest.param("1e400", id="overflow-json"),
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


def test_model_rejects_non_json_extension_value() -> None:
    with pytest.raises(ValidationError):
        Model.model_validate({"value": object()})
