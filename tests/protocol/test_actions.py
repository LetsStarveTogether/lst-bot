from __future__ import annotations

from warnings import catch_warnings, simplefilter

import orjson
import pytest
from pydantic import ValidationError

from lst_bot import (
    Action,
    ActionCall,
    ActionRequest,
    ActionResponse,
    FileStage,
    Msg,
    MsgTargetTag,
    Retcode,
    UploadFileTag,
)
from lst_bot.protocol.actions import (
    FragmentedUploadPrepareParams,
    LatestEventsActionCall,
    LatestEventsParams,
    SendGroupMsgParams,
    UploadFileUrlParams,
)


@pytest.mark.parametrize(
    "payload",
    [
        {"params": {}},
        {"action": "send_message"},
        {"action": 1, "params": {}},
        {"action": "send_message", "params": []},
        {"action": "send_message", "params": {}, "echo": 1},
        {"action": "send_message", "params": {}, "self": {"platform": "qq"}},
    ],
)
def test_action_request_rejects_invalid_protocol_shape(payload: object) -> None:
    with pytest.raises(ValidationError):
        ActionRequest.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"retcode": Retcode.OK, "data": None, "message": ""},
        {"status": "ok", "data": None, "message": ""},
        {"status": "ok", "retcode": Retcode.OK, "message": ""},
        {"status": "ok", "retcode": Retcode.OK, "data": None},
        {
            "status": "done",
            "retcode": Retcode.OK,
            "data": None,
            "message": "",
        },
        {"status": "ok", "retcode": "0", "data": None, "message": ""},
        {"status": "ok", "retcode": 1, "data": None, "message": ""},
        {
            "status": "failed",
            "retcode": Retcode.OK,
            "data": None,
            "message": "bad",
        },
    ],
)
def test_action_response_rejects_invalid_protocol_shape(payload: object) -> None:
    with pytest.raises(ValidationError):
        ActionResponse.model_validate(payload)


def test_action_response_dump_includes_required_data_and_non_empty_echo() -> None:
    response = ActionResponse.ok(echo="")
    expected = {
        "status": "ok",
        "retcode": Retcode.OK,
        "data": None,
        "message": "",
    }

    assert response.model_dump(mode="json", by_alias=True) == expected
    assert orjson.loads(response.model_dump_json(by_alias=True)) == expected
    assert (
        ActionResponse.failed(
            Retcode.UNSUPPORTED_ACTION,
            "no",
            echo="x",
        ).model_dump(mode="json", by_alias=True)["echo"]
        == "x"
    )


def test_action_params_use_discriminators_for_nested_protocol_tags() -> None:
    action_call = ActionCall.model_validate({
        "action": "send_message",
        "params": {"group_id": "20000", "message": "hello"},
    })
    assert action_call.root.params.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    ) == {
        "detail_type": "group",
        "group_id": "20000",
        "message": [{"type": "text", "data": {"text": "hello"}}],
    }
    action_call = ActionCall.model_validate({
        "action": "upload_file",
        "params": {
            "type": "url",
            "name": "logo.png",
            "url": "https://example.test/logo.png",
        },
    })
    assert action_call.root.params.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    ) == {
        "type": "url",
        "name": "logo.png",
        "url": "https://example.test/logo.png",
    }

    with pytest.raises(ValidationError):
        ActionCall.model_validate({
            "action": "upload_file_fragmented",
            "params": {"stage": "transfer", "file_id": "file-1", "offset": 0},
        })


def test_action_literal_fields_default_to_protocol_tags() -> None:
    assert (
        SendGroupMsgParams(group_id="20000", message=Msg.t("hello")).detail_type
        == MsgTargetTag.GROUP
    )
    assert (
        UploadFileUrlParams(name="logo.png", url="https://example.test/logo.png").type
        == UploadFileTag.URL
    )
    assert (
        FragmentedUploadPrepareParams(name="logo.png", total_size=1024).stage
        == FileStage.PREPARE
    )
    assert LatestEventsActionCall(params=LatestEventsParams()).action == (
        Action.GET_LATEST_EVENTS
    )


def test_action_discriminators_accept_model_instances_for_validation_and_dump() -> None:
    action_call = ActionCall.model_validate({
        "action": "send_message",
        "params": {"group_id": "20000", "message": "hello"},
    })
    validated = ActionCall.model_validate({
        "action": "send_message",
        "params": action_call.root.params,
    })

    assert validated.root.params.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=True,
    ) == {
        "detail_type": "group",
        "group_id": "20000",
        "message": [{"type": "text", "data": {"text": "hello"}}],
    }

    with catch_warnings(record=True) as caught:
        simplefilter("always")
        payload = ActionCall.model_validate(validated.root).model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )

    assert payload == {
        "action": "send_message",
        "params": {
            "detail_type": "group",
            "group_id": "20000",
            "message": [{"type": "text", "data": {"text": "hello"}}],
        },
    }
    assert caught == []


def test_sha256_params_normalize_uppercase_hex() -> None:
    action_call = ActionCall.model_validate({
        "action": "upload_file",
        "params": {
            "type": "data",
            "name": "bytes.bin",
            "data": "/w==",
            "sha256": "ABCDEF0123456789ABCDEF0123456789"
            "ABCDEF0123456789ABCDEF0123456789",
        },
    })

    assert (
        action_call.root.params.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )["sha256"]
        == "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    )
