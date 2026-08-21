import json
from base64 import b64encode
from decimal import Decimal

import pytest
from bot import (
    Action,
    ActionCall,
    ActionParamInput,
    ActionRequest,
    ActionResponse,
    BotSelf,
    ReturnAction,
)
from bot.protocol.actions import (
    FragmentedGetPrepareParams,
    FragmentedGetTransferParams,
    FragmentedUploadFinishParams,
    FragmentedUploadPrepareParams,
    FragmentedUploadTransferParams,
    LatestEventsParams,
    UploadFileBaseParams,
)
from pydantic import BaseModel, Field, ValidationError, field_serializer

ACTION_CASES: dict[str, dict[str, object]] = {
    "get_latest_events": {"limit": 10, "timeout": 0},
    "get_supported_actions": {},
    "get_status": {},
    "get_version": {},
    "get_self_info": {},
    "get_user_info": {"user_id": "42"},
    "get_friend_list": {},
    "send_message": {
        "detail_type": "private",
        "user_id": "42",
        "message": "hello",
    },
    "delete_message": {"message_id": "message-1"},
    "get_group_info": {"group_id": "20000"},
    "get_group_list": {},
    "get_group_member_info": {"group_id": "20000", "user_id": "42"},
    "get_group_member_list": {"group_id": "20000"},
    "set_group_name": {"group_id": "20000", "group_name": "group"},
    "leave_group": {"group_id": "20000"},
    "get_guild_info": {"guild_id": "30000"},
    "get_guild_list": {},
    "set_guild_name": {"guild_id": "30000", "guild_name": "guild"},
    "get_guild_member_info": {"guild_id": "30000", "user_id": "42"},
    "get_guild_member_list": {"guild_id": "30000"},
    "leave_guild": {"guild_id": "30000"},
    "get_channel_info": {"guild_id": "30000", "channel_id": "40000"},
    "get_channel_list": {"guild_id": "30000", "joined_only": None},
    "set_channel_name": {
        "guild_id": "30000",
        "channel_id": "40000",
        "channel_name": "channel",
    },
    "get_channel_member_info": {
        "guild_id": "30000",
        "channel_id": "40000",
        "user_id": "42",
    },
    "get_channel_member_list": {"guild_id": "30000", "channel_id": "40000"},
    "leave_channel": {"guild_id": "30000", "channel_id": "40000"},
    "upload_file": {
        "type": "url",
        "name": "file.bin",
        "url": "https://example.test/file",
    },
    "upload_file_fragmented": {
        "stage": "prepare",
        "name": "file.bin",
        "total_size": 1,
    },
    "get_file": {"file_id": "file-1", "type": "url"},
    "get_file_fragmented": {"stage": "prepare", "file_id": "file-1"},
}


@pytest.mark.parametrize(
    ("action", "params"),
    ACTION_CASES.items(),
)
def test_each_standard_action_round_trips_json(
    action: str,
    params: dict[str, object],
) -> None:
    call = ActionCall.model_validate({"action": action, "params": params})

    assert call.root.action == action
    assert ActionCall.model_validate_json(call.model_dump_json()) == call


def test_action_matrix_covers_every_declared_standard_action() -> None:
    assert set(ACTION_CASES) == {action.value for action in Action}


@pytest.mark.parametrize(
    ("params", "detail_type"),
    [
        pytest.param(
            {"user_id": "42", "message": "private"},
            "private",
            id="private-inferred",
        ),
        pytest.param(
            {"group_id": "20000", "message": "group"},
            "group",
            id="group-inferred",
        ),
        pytest.param(
            {"guild_id": "30000", "channel_id": "40000", "message": "channel"},
            "channel",
            id="channel-inferred",
        ),
        pytest.param(
            {
                "detail_type": "vendor.thread",
                "thread_id": "thread-1",
                "message": "extension",
            },
            "vendor.thread",
            id="extension-explicit",
        ),
    ],
)
def test_send_message_discriminator_selects_each_target_variant(
    params: dict[str, object],
    detail_type: str,
) -> None:
    call = ActionCall.model_validate({"action": "send_message", "params": params})
    normalized = call.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert normalized["params"]["detail_type"] == detail_type
    assert normalized["params"]["message"] == [
        {"type": "text", "data": {"text": params["message"]}},
    ]


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(
            {
                "type": "url",
                "name": "file.bin",
                "url": "https://example.test/file",
                "headers": {"Authorization": "Bearer token"},
            },
            id="url",
        ),
        pytest.param(
            {"type": "path", "name": "file.bin", "path": "files/file.bin"},
            id="path",
        ),
        pytest.param(
            {"type": "data", "name": "file.bin", "data": "/w=="},
            id="data",
        ),
        pytest.param(
            {"type": "vendor.storage", "name": "file.bin", "token": None},
            id="extension",
        ),
    ],
)
def test_upload_file_discriminator_selects_each_source_variant(
    params: dict[str, object],
) -> None:
    call = ActionCall.model_validate({"action": "upload_file", "params": params})

    assert isinstance(call.root.params, UploadFileBaseParams)
    assert call.root.params.type == params["type"]
    assert ActionCall.model_validate_json(call.model_dump_json()) == call


@pytest.mark.parametrize(
    ("action", "params"),
    [
        pytest.param(
            "upload_file_fragmented",
            {"stage": "prepare", "name": "file.bin", "total_size": 0},
            id="upload-prepare",
        ),
        pytest.param(
            "upload_file_fragmented",
            {"stage": "transfer", "file_id": "file-1", "offset": 0, "data": "AA=="},
            id="upload-transfer",
        ),
        pytest.param(
            "upload_file_fragmented",
            {
                "stage": "finish",
                "file_id": "file-1",
                "sha256": "0" * 64,
            },
            id="upload-finish",
        ),
        pytest.param(
            "get_file_fragmented",
            {"stage": "prepare", "file_id": "file-1"},
            id="get-prepare",
        ),
        pytest.param(
            "get_file_fragmented",
            {"stage": "transfer", "file_id": "file-1", "offset": 0, "size": 1},
            id="get-transfer",
        ),
    ],
)
def test_fragmented_file_discriminators_accept_each_stage(
    action: str,
    params: dict[str, object],
) -> None:
    call = ActionCall.model_validate({"action": action, "params": params})

    assert isinstance(
        call.root.params,
        FragmentedUploadPrepareParams
        | FragmentedUploadTransferParams
        | FragmentedUploadFinishParams
        | FragmentedGetPrepareParams
        | FragmentedGetTransferParams,
    )
    assert call.root.params.stage == params["stage"]
    assert ActionCall.model_validate_json(call.model_dump_json()) == call


def test_extension_action_preserves_nested_json_values_and_null() -> None:
    payload = {
        "action": "vendor.do_something",
        "params": {"payload": {"values": [True, 1, 1.5, "text", None]}},
    }

    call = ActionCall.model_validate(payload)

    assert call.model_dump(mode="json", by_alias=True) == payload
    assert ActionCall.model_validate_json(call.model_dump_json()) == call


def test_action_normalization_is_idempotent() -> None:
    call = ActionCall.model_validate({
        "action": "send_message",
        "params": {"group_id": "20000", "msg": "hello"},
    })
    normalized = call.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert (
        ActionCall.model_validate(normalized).model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        == normalized
    )


def test_action_request_round_trips_explicit_null_envelope_fields() -> None:
    call = ActionCall.model_validate({"action": "get_status", "params": {}})
    request = ActionRequest(
        action="get_status",
        params=call.root.params,
        echo=None,
        self_=None,
    )

    payload = request.model_dump(mode="json", by_alias=True)

    assert (
        ActionRequest.model_validate_json(request.model_dump_json()).model_dump(
            mode="json",
            by_alias=True,
        )
        == payload
    )
    assert payload == {
        "action": "get_status",
        "params": {},
        "echo": None,
        "self": None,
    }


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"params": {}}, id="missing-action"),
        pytest.param({"action": "send_message"}, id="missing-params"),
        pytest.param({"action": 1, "params": {}}, id="non-string-action"),
        pytest.param(
            {"action": "send_message", "params": []},
            id="non-object-params",
        ),
        pytest.param(
            {"action": "send_message", "params": {}, "echo": 1},
            id="non-string-echo",
        ),
        pytest.param(
            {"action": "send_message", "params": {}, "self": {"platform": "qq"}},
            id="incomplete-self",
        ),
    ],
)
def test_action_request_rejects_invalid_protocol_shape(payload: object) -> None:
    with pytest.raises(ValidationError):
        ActionRequest.model_validate(payload)


def test_action_response_round_trips_required_null_data_and_omits_null_echo() -> None:
    response = ActionResponse.ok(echo="")

    assert response.model_dump(mode="json", by_alias=True) == {
        "status": "ok",
        "retcode": 0,
        "data": None,
        "message": "",
    }
    assert ActionResponse.model_validate_json(response.model_dump_json()) == response


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"status": "failed", "retcode": 1, "data": None, "message": "failed"},
            id="failed-minimum-retcode",
        ),
        pytest.param(
            {
                "status": "failed",
                "retcode": 99999,
                "data": {"retry": False},
                "message": "failed",
                "echo": "echo-1",
            },
            id="failed-maximum-retcode",
        ),
    ],
)
def test_action_response_accepts_status_retcode_contract(
    payload: dict[str, object],
) -> None:
    response = ActionResponse.model_validate(payload)

    assert response.model_dump(mode="json", by_alias=True) == payload


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"retcode": 0, "data": None, "message": ""},
            id="missing-status",
        ),
        pytest.param(
            {"status": "ok", "data": None, "message": ""},
            id="missing-retcode",
        ),
        pytest.param(
            {"status": "ok", "retcode": 0, "message": ""},
            id="missing-data",
        ),
        pytest.param(
            {"status": "ok", "retcode": 0, "data": None},
            id="missing-message",
        ),
        pytest.param(
            {"status": "done", "retcode": 0, "data": None, "message": ""},
            id="unknown-status",
        ),
        pytest.param(
            {"status": "ok", "retcode": "0", "data": None, "message": ""},
            id="string-retcode",
        ),
        pytest.param(
            {"status": "ok", "retcode": 1, "data": None, "message": ""},
            id="ok-with-failed-retcode",
        ),
        pytest.param(
            {"status": "failed", "retcode": 0, "data": None, "message": "bad"},
            id="failed-with-ok-retcode",
        ),
        pytest.param(
            {"status": "failed", "retcode": -1, "data": None, "message": "bad"},
            id="retcode-below-minimum",
        ),
        pytest.param(
            {"status": "failed", "retcode": 100000, "data": None, "message": "bad"},
            id="retcode-above-maximum",
        ),
    ],
)
def test_action_response_rejects_invalid_protocol_shape(payload: object) -> None:
    with pytest.raises(ValidationError):
        ActionResponse.model_validate(payload)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="zero"),
        pytest.param(2**63 - 1, id="int64-maximum"),
    ],
)
def test_non_negative_int_params_accept_int64_boundaries(value: int) -> None:
    call = ActionCall.model_validate({
        "action": "get_latest_events",
        "params": {"limit": value},
    })

    assert isinstance(call.root.params, LatestEventsParams)
    assert call.root.params.limit == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="boolean"),
        pytest.param(1.0, id="float"),
        pytest.param(-1, id="negative"),
        pytest.param(2**63, id="above-int64-maximum"),
    ],
)
def test_non_negative_int_params_reject_non_int64_values(value: object) -> None:
    with pytest.raises(ValidationError):
        ActionCall.model_validate({
            "action": "get_latest_events",
            "params": {"limit": value},
        })


@pytest.mark.parametrize(
    ("action", "params"),
    [
        pytest.param(
            "upload_file",
            {"type": "data", "name": "bytes.bin"},
            id="upload-file",
        ),
        pytest.param(
            "upload_file_fragmented",
            {"stage": "transfer", "file_id": "file-1", "offset": 0},
            id="fragmented-transfer",
        ),
    ],
)
@pytest.mark.parametrize(
    "input_type",
    [
        pytest.param(bytes, id="bytes"),
        pytest.param(bytearray, id="bytearray"),
    ],
)
def test_upload_data_treats_python_bytes_as_raw_and_dumps_base64(
    action: str,
    params: dict[str, object],
    input_type: type[bytes | bytearray],
) -> None:
    raw = b"\xff\x00"
    call = ActionCall.model_validate({
        "action": action,
        "params": {**params, "data": input_type(raw)},
    })

    assert call.root.params.model_dump()["data"] == raw
    assert (
        call.model_dump(mode="json", by_alias=True, exclude_none=True)["params"]["data"]
        == b64encode(raw).decode()
    )


@pytest.mark.parametrize(
    ("action", "params"),
    [
        pytest.param(
            "upload_file",
            {"type": "data", "name": "bytes.bin", "data": "/w=="},
            id="upload-file",
        ),
        pytest.param(
            "upload_file_fragmented",
            {
                "stage": "transfer",
                "file_id": "file-1",
                "offset": 0,
                "data": "/w==",
            },
            id="fragmented-transfer",
        ),
    ],
)
def test_upload_data_decodes_json_base64_and_round_trips(
    action: str,
    params: dict[str, object],
) -> None:
    call = ActionCall.model_validate_json(
        json.dumps({
            "action": action,
            "params": params,
        }),
    )

    assert call.root.params.model_dump()["data"] == b"\xff"
    assert call.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "action": action,
        "params": params,
    }


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("%%%", id="invalid-alphabet"),
        pytest.param("a", id="invalid-padding"),
        pytest.param(None, id="null"),
    ],
)
def test_upload_data_rejects_invalid_base64(value: object) -> None:
    with pytest.raises(ValidationError):
        ActionCall.model_validate({
            "action": "upload_file",
            "params": {"type": "data", "name": "bytes.bin", "data": value},
        })


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("0" * 64, "0" * 64, id="lowercase"),
        pytest.param("ABCDEF01" * 8, "abcdef01" * 8, id="uppercase-normalized"),
        pytest.param(None, None, id="optional-null"),
    ],
)
def test_sha256_accepts_valid_or_null_value(
    value: object,
    expected: str | None,
) -> None:
    call = ActionCall.model_validate({
        "action": "upload_file",
        "params": {
            "type": "url",
            "name": "file.bin",
            "url": "https://example.test/file",
            "sha256": value,
        },
    })

    assert isinstance(call.root.params, UploadFileBaseParams)
    assert call.root.params.sha256 == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("0" * 63, id="too-short"),
        pytest.param("0" * 65, id="too-long"),
        pytest.param("z" * 64, id="non-hex"),
    ],
)
def test_sha256_rejects_invalid_value(value: str) -> None:
    with pytest.raises(ValidationError):
        ActionCall.model_validate({
            "action": "upload_file",
            "params": {
                "type": "url",
                "name": "file.bin",
                "url": "https://example.test/file",
                "sha256": value,
            },
        })


class VendorParams(BaseModel):
    enabled: bool


class VendorOperation(BaseModel):
    from_: str = Field(alias="from")
    amount: Decimal

    @field_serializer("amount", when_used="json")
    def serialize_amount(self, value: Decimal) -> str:
        return f"decimal:{value}"


class VendorRequest(BaseModel):
    from_: str = Field(alias="from")
    operations: list[VendorOperation]


def test_return_action_serializes_python_parameter_values() -> None:
    params: dict[str, ActionParamInput] = {
        "bytes": b"\xff",
        "bytearray": bytearray(b"\x00"),
        "model": VendorParams(enabled=True),
        "text": "/w==",
    }

    returned = ReturnAction.call(
        "vendor.action",
        params,
        self_=BotSelf(platform="qq", user_id="10000"),
    )

    assert returned.action_call is not None
    assert returned.action_call.model_dump(mode="json") == {
        "action": "vendor.action",
        "params": {
            "bytes": "/w==",
            "bytearray": "AA==",
            "model": {"enabled": True},
            "text": "/w==",
        },
    }


def test_action_params_recursively_serialize_models_with_aliases() -> None:
    operation = VendorOperation.model_validate({
        "from": "nested",
        "amount": Decimal("1.20"),
    })
    top_level = ActionCall.model_validate({
        "action": "vendor.action",
        "params": VendorRequest.model_validate({
            "from": "top",
            "operations": [operation],
        }),
    })
    nested = ActionCall.model_validate({
        "action": "vendor.action",
        "params": {"operations": (operation,)},
    })
    expected = {
        "from": "nested",
        "amount": "decimal:1.20",
    }

    assert top_level.model_dump(mode="json", by_alias=True) == {
        "action": "vendor.action",
        "params": {
            "from": "top",
            "operations": [expected],
        },
    }
    assert nested.model_dump(mode="json", by_alias=True)["params"] == {
        "operations": [expected]
    }


def test_return_action_factories_cover_message_call_and_request() -> None:
    message = ReturnAction.message("hello")
    request = ReturnAction.request(False, reason="denied")

    assert (message.kind, message.msg.text if message.msg else None) == (
        "message",
        "hello",
    )
    assert request.kind == "request"
    assert request.approve is False
