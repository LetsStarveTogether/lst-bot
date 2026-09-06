from asyncio import (
    CancelledError,
    Event,
    TaskGroup,
    get_running_loop,
    timeout,
)
from collections.abc import AsyncIterator
from http import HTTPMethod
from unittest.mock import AsyncMock

import pytest
from bot import BotSelf
from bot.gateways import qq_api
from bot.gateways.qq_api import (
    QQAction,
    QQAPIError,
    QQChannel,
    QQFilePrepareResult,
    QQFileUploadFields,
    QQGuildRoles,
    QQKeyboardButton,
    QQKeyboardPermission,
    QQNoContent,
    QQRoleMemberList,
    QQSendC2CMessageRequest,
    QQSendGroupMessageRequest,
    QQStreamMessageRequest,
)
from bot.json import dumpb, loads
from httpx2 import AsyncByteStream, Request, Response
from pydantic import JsonValue, ValidationError

from tests.gateways.support import response

from .support import HttpMock, client, gateway


class GatedStream(AsyncByteStream):
    def __init__(self, body: bytes, entered: Event, release: Event) -> None:
        self.body = body
        self.entered = entered
        self.release = release
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.entered.set()
        await self.release.wait()
        yield self.body

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("permission_type", "subjects"),
    [
        (0, {"specify_user_ids": ["user"]}),
        (1, {}),
        (3, {"specify_role_ids": ["role"]}),
    ],
)
def test_keyboard_permission_subjects_match_type(
    permission_type: int,
    subjects: dict[str, list[str]],
) -> None:
    permission = QQKeyboardPermission.model_validate({
        "type": permission_type,
        **subjects,
    })
    assert permission.model_dump(mode="json", exclude_none=True) == {
        "type": permission_type,
        **subjects,
    }


@pytest.mark.parametrize(
    ("permission_type", "subjects"),
    [
        (0, {}),
        (1, {"specify_user_ids": ["user"]}),
        (1, {"specify_role_ids": ["role"]}),
        (3, {}),
    ],
)
def test_keyboard_permission_rejects_mismatched_subjects(
    permission_type: int,
    subjects: dict[str, list[str]],
) -> None:
    with pytest.raises(ValidationError):
        QQKeyboardPermission.model_validate({"type": permission_type, **subjects})


@pytest.mark.parametrize("style", [0, 1, 3, 4])
def test_keyboard_button_matches_qq_wire_contract(style: int) -> None:
    payload = {
        "render_data": {"label": "签到", "style": style},
        "action": {"type": 2, "permission": {"type": 2}, "data": "/signin"},
    }
    assert (
        QQKeyboardButton.model_validate(payload).model_dump(exclude_none=True)
        == payload
    )
    for field in ("label", "style"):
        invalid_render = dict(payload["render_data"])
        invalid_render.pop(field)
        with pytest.raises(ValidationError):
            QQKeyboardButton.model_validate(payload | {"render_data": invalid_render})
    with pytest.raises(ValidationError):
        QQKeyboardButton.model_validate(
            payload | {"render_data": {"label": "签到", "style": 2}},
        )


def test_response_models_accept_current_qq_wire_values() -> None:
    channel = QQChannel.model_validate({
        "id": "channel",
        "guild_id": "guild",
        "type": 1,
        "sub_type": 4,
        "permissions": "1",
    })
    roles = QQGuildRoles.model_validate({"roles": [], "role_num_limit": "30"})
    members = QQRoleMemberList.model_validate({
        "data": [
            {
                "user": {"id": "user"},
                "nick": "member",
                "joined_at": "2026-08-20T00:00:00Z",
            }
        ]
    })

    assert (channel.type, channel.sub_type, channel.permissions) == (1, 4, "1")
    assert roles.role_num_limit == "30"
    assert members.data[0].roles == []
    with pytest.raises(ValidationError):
        QQChannel.model_validate({
            "id": "channel",
            "guild_id": "guild",
            "permissions": 1,
        })


def test_rest_request_models_follow_current_qq_contract() -> None:
    ark = {
        "template_id": 23,
        "kv": [
            {"key": "#DESC#", "value": "机器人消息"},
            {
                "key": "#LIST#",
                "obj": [{"obj_kv": [{"key": "name", "value": "列表项"}]}],
            },
        ],
    }
    for model, target in (
        (QQSendGroupMessageRequest, {"group_openid": "group"}),
        (QQSendC2CMessageRequest, {"user_openid": "user"}),
    ):
        message = model.model_validate(target | {"content": "answer"})
        assert message.model_dump(mode="json", exclude_none=True) == {
            **target,
            "content": "answer",
            "msg_type": 0,
        }
    channel_message = qq_api.QQSendChannelMessageRequest.model_validate({
        "channel_id": "channel",
        "ark": ark,
    })
    assert channel_message.model_dump(mode="json", exclude_none=True) == {
        "channel_id": "channel",
        "ark": ark,
    }
    with pytest.raises(ValidationError, match="exactly one"):
        qq_api.QQSendChannelMessageRequest.model_validate({
            "channel_id": "channel",
            "ark": {"template_id": 23, "kv": [{"key": "#DESC#"}]},
        })

    upload = QQFileUploadFields(file_type=1, url="https://qq.example/image.png")
    assert upload.srv_send_msg is False
    with pytest.raises(ValidationError):
        QQFileUploadFields.model_validate({"url": "https://qq.example/image.png"})
    merged = QQFileUploadFields(upload_id="upload")
    assert merged.model_dump(mode="json", exclude_none=True) == {
        "srv_send_msg": False,
        "upload_id": "upload",
    }

    first_chunk = {
        "user_openid": "user",
        "input_mode": "replace",
        "input_state": 1,
        "index": 0,
        "content_type": "markdown",
        "content_raw": "正在生成回答，请稍候",
        "msg_id": "message",
        "msg_seq": 1,
    }
    assert (
        QQStreamMessageRequest.model_validate(first_chunk).model_dump(exclude_none=True)
        == first_chunk
    )
    wakeup = QQStreamMessageRequest(
        user_openid="user",
        input_state=1,
        index=0,
        content_type="text",
        content_raw="answer",
        is_wakeup=True,
    )
    assert (wakeup.input_mode, wakeup.content_type) == ("append", "text")

    assert QQStreamMessageRequest.model_validate(
        first_chunk | {"is_wakeup": True}
    ).is_wakeup
    with pytest.raises(ValidationError, match="mutually exclusive"):
        QQStreamMessageRequest.model_validate(first_chunk | {"event_id": "event"})
    with pytest.raises(ValidationError, match="mutually exclusive"):
        QQSendC2CMessageRequest(
            user_openid="user",
            msg_type=0,
            content="answer",
            msg_id="message",
            is_wakeup=True,
        )

    for msg_seq in (0, 65535):
        assert (
            QQStreamMessageRequest.model_validate(
                first_chunk | {"msg_seq": msg_seq}
            ).msg_seq
            == msg_seq
        )
    for msg_seq in (-1, 65536):
        with pytest.raises(ValidationError):
            QQStreamMessageRequest.model_validate(first_chunk | {"msg_seq": msg_seq})


async def test_rest_preserves_callback_header_and_empty_body() -> None:
    mock = HttpMock(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, body=b""),
        response(200, body=b""),
        response(200, {"url_link": "https://qq.example/share"}),
    )
    rest = client(mock)

    result = await rest.request_qq(
        QQAction.ACK_INTERACTION,
        interaction_id="interaction",
    )
    recalled = await rest.request_qq(
        QQAction.RECALL_GROUP_MESSAGE,
        group_openid="group",
        message_id="message",
    )
    await rest.request_qq(QQAction.GENERATE_SHARE_LINK)

    assert isinstance(result, QQNoContent)
    assert isinstance(recalled, QQNoContent)
    assert (mock.requests[1].method, str(mock.requests[1].url)) == (
        HTTPMethod.PUT,
        "https://qq.example/interactions/interaction",
    )
    headers = mock.requests[1].headers
    assert headers["X-Callback-AppID"] == "app"
    assert loads(mock.requests[1].content) == {"code": 0}
    assert (mock.requests[2].method, str(mock.requests[2].url)) == (
        HTTPMethod.DELETE,
        "https://qq.example/v2/groups/group/messages/message",
    )
    assert loads(mock.requests[3].content) == {}


async def test_channel_file_image_uses_multipart() -> None:
    mock = HttpMock(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, {"id": "message", "timestamp": "2026-08-21T00:00:00Z"}),
    )
    connection = gateway(mock, online=True).connection_for(
        BotSelf(platform="qq", user_id="app")
    )

    await connection.action(
        QQAction.SEND_CHANNEL_MESSAGE,
        channel_id="channel",
        content="caption",
        message_reference={"message_id": "reply"},
        file_image="aW1hZ2U=",
    )

    request = mock.requests[1]
    assert (request.method, str(request.url)) == (
        HTTPMethod.POST,
        "https://qq.example/channels/channel/messages",
    )
    headers = request.headers
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    body = request.content
    assert b'name="file_image"; filename="image"' in body
    assert b"Content-Type: application/octet-stream" in body
    assert b"\r\n\r\nimage\r\n--" in body
    assert b"aW1hZ2U=" not in body
    assert b'{"message_id":"reply"}' in body

    with pytest.raises(ValidationError):
        await connection.action(
            QQAction.SEND_DM_MESSAGE,
            guild_id="guild",
            content="caption",
            file_image="aW1hZ2U=",
        )


async def test_file_upload_supports_chunk_completion() -> None:
    uploaded = {"file_uuid": "file", "file_info": "info", "ttl": 60}
    prepare_body: dict[str, object] = {
        "file_type": 2,
        "file_size": "31457280",
        "file_name": "demo.mp4",
        "md5": "0" * 32,
        "sha1": "0" * 40,
        "md5_10m": "1" * 32,
    }
    finish_body: dict[str, object] = {
        "upload_id": "upload",
        "part_index": 0,
        "block_size": "10485760",
        "md5": "0" * 32,
    }
    merge_body: dict[str, object] = {
        "file_type": 2,
        "srv_send_msg": False,
        "file_name": "demo.mp4",
        "upload_id": "upload",
    }
    prepared_payload = {
        "upload_id": "upload",
        "block_size": "10485760",
        "parts": [
            {
                "index": 0,
                "presigned_url": "https://upload.example/1",
                "block_size": "10485760",
            }
        ],
        "upload_config": {
            "concurrency": 1,
            "retry_timeout": 300,
            "retry_delay": 1,
        },
    }
    for value in ("1", True, 1.0):
        invalid = prepared_payload | {
            "upload_config": prepared_payload["upload_config"] | {"concurrency": value}
        }
        with pytest.raises(ValidationError):
            QQFilePrepareResult.model_validate(invalid)
    mock = HttpMock(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, prepared_payload),
        response(200, {}),
        response(200, uploaded),
    )
    rest = client(mock)

    prepared = await rest.request_qq(
        QQAction.PREPARE_GROUP_FILE_UPLOAD,
        group_id="group",
        **prepare_body,
    )
    await rest.request_qq(
        QQAction.FINISH_GROUP_FILE_UPLOAD,
        group_id="group",
        **finish_body,
    )
    await rest.request_qq(
        QQAction.UPLOAD_GROUP_FILE,
        group_openid="group",
        **merge_body,
    )

    assert isinstance(prepared, QQFilePrepareResult)
    assert prepared.model_dump(mode="json", exclude_none=True) == prepared_payload
    assert [loads(request.content) for request in mock.requests[1:]] == [
        prepare_body,
        finish_body,
        merge_body,
    ]


def test_v2_messages_reject_legacy_payload_types() -> None:
    for model, payload in (
        (
            QQSendGroupMessageRequest,
            {"group_openid": "group", "msg_type": 3, "content": "answer"},
        ),
        (
            QQSendC2CMessageRequest,
            {"user_openid": "user", "msg_type": 4, "content": "answer"},
        ),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


@pytest.mark.parametrize("code", [11242, 11252, 11263, 11281])
async def test_system_errors_retry_once_with_the_same_token(code: int) -> None:
    mock = HttpMock(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, {"code": code, "message": "temporary"}),
        response(200, []),
    )

    await client(mock).request_qq(QQAction.LIST_BOT_GUILDS)

    headers = [request.headers for request in mock.requests[1:]]
    assert [value["Authorization"] for value in headers] == [
        "QQBot token",
        "QQBot token",
    ]


async def test_system_error_is_never_retried_twice() -> None:
    error = {"code": 11242, "message": "temporary"}
    mock = HttpMock(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, error),
        response(200, error),
    )

    with pytest.raises(QQAPIError) as caught:
        await client(mock).request_qq(QQAction.LIST_BOT_GUILDS)

    assert caught.value.code == 11242
    assert len(mock.requests) == 3


async def test_final_expired_token_response_clears_cached_token() -> None:
    expired = {"code": 11244, "message": "expired"}
    mock = HttpMock(
        response(200, {"access_token": "stale", "expires_in": 7200}),
        response(200, expired),
        response(200, {"access_token": "fresh", "expires_in": 7200}),
        response(200, expired),
        response(200, {"access_token": "next", "expires_in": 7200}),
    )
    rest = client(mock)

    with pytest.raises(QQAPIError) as caught:
        await rest.request_qq(QQAction.LIST_BOT_GUILDS)

    assert caught.value.code == 11244
    assert await rest.access_token() == "next"


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        (b"", None, None),
        (b"<html>unavailable</html>", None, None),
        (b'{"code":50301,"message":"unavailable"}', 50301, "unavailable"),
    ],
    ids=["empty", "html", "json"],
)
async def test_non_success_responses_preserve_http_error_context(
    body: bytes,
    code: int | None,
    message: str | None,
) -> None:
    mock = HttpMock(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(503, body=body, headers={"X-Tps-Trace-ID": "http-trace"}),
    )

    with pytest.raises(QQAPIError) as caught:
        await client(mock).request_qq(QQAction.LIST_BOT_GUILDS)

    assert (
        caught.value.status,
        caught.value.code,
        caught.value.message,
        caught.value.trace_id,
    ) == (503, code, message, "http-trace")


async def test_close_cannot_resurrect_an_inflight_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = HttpMock()
    started = Event()
    cancelled = Event()

    async def request_token(_: Request) -> Response:
        if not started.is_set():
            started.set()
            try:
                await Event().wait()
            except CancelledError:
                cancelled.set()
                raise
        return response(200, {"access_token": "next", "expires_in": 7200})

    request = AsyncMock(side_effect=request_token)
    monkeypatch.setattr(mock, "handle", request)
    rest = client(mock)

    async def token_request() -> None:
        with pytest.raises(RuntimeError, match="closed"):
            await rest.access_token()

    async with timeout(1), TaskGroup() as tasks:
        token = tasks.create_task(token_request())
        await started.wait()
        closing = tasks.create_task(rest.close())
        await cancelled.wait()
        await token
        await closing
    with pytest.raises(RuntimeError, match="closed"):
        await rest.access_token()

    await rest.start()
    assert await rest.access_token() == "next"
    assert request.await_count == 2


async def test_close_rejects_an_inflight_action_result_across_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = HttpMock()
    action_started = Event()
    action_cancelled = Event()
    release_action = Event()
    calls = 0

    async def request(_: Request) -> Response:
        nonlocal calls
        calls += 1
        if calls in {1, 3}:
            return response(
                200,
                {"access_token": f"token-{calls}", "expires_in": 7200},
            )
        if calls == 2:
            action_started.set()
            try:
                await release_action.wait()
            except CancelledError:
                action_cancelled.set()
                await release_action.wait()
        return response(200, [])

    monkeypatch.setattr(mock, "handle", request)
    rest = client(mock)

    async def old_request() -> None:
        with pytest.raises(RuntimeError, match="closed"):
            await rest.request_qq(QQAction.LIST_BOT_GUILDS)

    async with timeout(1), TaskGroup() as tasks:
        tasks.create_task(old_request())
        await action_started.wait()
        await rest.close()
        await action_cancelled.wait()
        await rest.start()
        release_action.set()

    result = await rest.request_qq(QQAction.LIST_BOT_GUILDS)
    assert result.model_dump() == []
    assert calls == 4


async def test_nonempty_invalid_json_is_never_an_empty_success() -> None:
    mock = HttpMock(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, body=b"<html>upstream failure</html>"),
    )

    with pytest.raises(RuntimeError, match="invalid JSON"):
        await client(mock).request_qq(
            QQAction.ACK_INTERACTION,
            interaction_id="interaction",
        )


async def test_no_content_response_rejects_extra_data() -> None:
    mock = HttpMock(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, {"unexpected": True}),
    )

    with pytest.raises(RuntimeError, match="invalid response"):
        await client(mock).request_qq(QQAction.DELETE_CHANNEL, channel_id="channel")


@pytest.mark.parametrize(
    ("action", "params", "payload"),
    [
        (QQAction.LIST_BOT_GUILDS, {}, {}),
        (QQAction.GET_BOT, {}, {}),
        (
            QQAction.GET_GUILD_MEMBER,
            {"guild_id": "guild", "user_id": "user"},
            {"user": {}},
        ),
        (QQAction.LIST_GUILD_MEMBERS, {"guild_id": "guild"}, [{}]),
        (
            QQAction.REQUIRE_API_PERMISSION,
            {
                "guild_id": "guild",
                "channel_id": "channel",
                "api_identify": {"path": "/users/@me", "method": "GET"},
                "desc": "reason",
            },
            {},
        ),
        (QQAction.GET_MESSAGE_SETTING, {"guild_id": "guild"}, {}),
        (
            QQAction.CREATE_GUILD_ANNOUNCE,
            {"guild_id": "guild", "channel_id": "channel", "message_id": "message"},
            {},
        ),
        (
            QQAction.SEND_CHANNEL_MESSAGE,
            {"channel_id": "channel", "content": "hi"},
            {},
        ),
    ],
)
async def test_success_response_schema_is_validated(
    action: QQAction,
    params: dict[str, object],
    payload: JsonValue,
) -> None:
    mock = HttpMock(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, payload),
    )

    with pytest.raises(RuntimeError, match="invalid response"):
        await client(mock).request_qq(action, **params)


@pytest.mark.parametrize("phase", ["request", "body"])
async def test_http_deadline_covers_request_and_response_body(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    never = Event()
    body_entered = Event()
    mock = HttpMock()

    async def blocked_request(_: Request) -> Response:
        if phase == "request":
            await never.wait()
        if phase == "body":
            return Response(200, stream=GatedStream(b"", body_entered, never))
        return response(200, {"access_token": "token", "expires_in": 7200})

    monkeypatch.setattr(mock, "handle", blocked_request)
    monkeypatch.setattr(qq_api, "_HTTP_TIMEOUT", 0.01)

    async with timeout(1):
        with pytest.raises(TimeoutError):
            await client(mock).access_token()
    assert body_entered.is_set() is (phase == "body")


@pytest.mark.parametrize("final_phase", ["request", "body"])
async def test_action_deadline_is_shared_by_token_and_action_io(
    monkeypatch: pytest.MonkeyPatch,
    final_phase: str,
) -> None:
    loop = get_running_loop()
    token_body_entered = Event()
    token_body_release = Event()
    final_entered = Event()
    final_release = Event()
    mock = HttpMock()
    calls = 0

    async def request(_: Request) -> Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return Response(
                200,
                stream=GatedStream(
                    dumpb({"access_token": "token", "expires_in": 7200}),
                    token_body_entered,
                    token_body_release,
                ),
            )
        if final_phase == "request":
            final_entered.set()
            await final_release.wait()
            return response(200, [])
        return Response(
            200,
            stream=GatedStream(dumpb([]), final_entered, final_release),
        )

    monkeypatch.setattr(mock, "handle", request)
    now = loop.time
    clock_offset = 0.0
    monkeypatch.setattr(loop, "time", lambda: now() + clock_offset)
    monkeypatch.setattr(loop, "slow_callback_duration", float("inf"))
    monkeypatch.setattr(qq_api, "_HTTP_TIMEOUT", 0.03)

    async def request_until_timeout() -> None:
        with pytest.raises(TimeoutError):
            await client(mock).request_qq(QQAction.LIST_BOT_GUILDS)

    async with timeout(1), TaskGroup() as tasks:
        task = tasks.create_task(request_until_timeout())
        await token_body_entered.wait()
        clock_offset += 0.02
        token_body_release.set()
        await final_entered.wait()
        clock_offset += 0.011
        await task
