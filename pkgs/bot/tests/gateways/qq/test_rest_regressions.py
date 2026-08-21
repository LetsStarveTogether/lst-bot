from asyncio import CancelledError, Event, TaskGroup, get_running_loop, timeout
from collections.abc import Awaitable
from http import HTTPMethod
from typing import cast
from unittest.mock import AsyncMock

import pytest
from bot.gateways import qq_api
from bot.gateways.qq_api import (
    QQAction,
    QQAPIError,
    QQChannel,
    QQFilePrepareResult,
    QQGuildRoles,
    QQKeyboardButton,
    QQKeyboardPermission,
    QQNoContent,
    QQRestClient,
    QQRoleMemberList,
    QQSendC2CMessageRequest,
    QQSendGroupMessageRequest,
    QQStreamMessageRequest,
)
from bot.json import dumpb
from pydantic import JsonValue, ValidationError
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager

from tests.gateways.support import response

from .support import Pool, client


class GatedResponse:
    def __init__(self, body: bytes, entered: Event, release: Event) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self.body = body
        self.entered = entered
        self.release = release

    @property
    def data(self) -> Awaitable[bytes]:
        return self.read()

    async def read(self) -> bytes:
        self.entered.set()
        await self.release.wait()
        return self.body


def test_falsey_external_pool_is_preserved() -> None:
    pool: list[object] = []
    assert client(pool).http_pool is pool


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
    assert permission.type == permission_type


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


def test_keyboard_button_matches_qq_wire_contract() -> None:
    payload = {
        "id": "btn_signin",
        "render_data": {"label": "签到", "style": 1},
        "action": {"type": 2, "permission": {"type": 2}, "data": "/signin"},
    }
    assert (
        QQKeyboardButton.model_validate(payload).model_dump(exclude_none=True)
        == payload
    )
    invalid_render = dict(payload["render_data"])
    invalid_render.pop("style")
    with pytest.raises(ValidationError):
        QQKeyboardButton.model_validate(payload | {"render_data": invalid_render})


def test_response_models_accept_current_qq_wire_values() -> None:
    channel = QQChannel.model_validate({
        "id": "channel",
        "guild_id": "guild",
        "type": 1,
        "sub_type": 4,
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

    assert (channel.type, channel.sub_type, roles.role_num_limit) == (1, 4, "30")
    assert members.data[0].roles == []


def test_rest_request_models_follow_current_qq_contract() -> None:
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
    pool = Pool(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, body=b""),
        response(200, body=b""),
        response(200, {"url_link": "https://qq.example/share"}),
    )
    rest = client(pool)

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
    assert pool.requests[1][0:2] == (
        HTTPMethod.PUT,
        "https://qq.example/interactions/interaction",
    )
    headers = cast(dict[str, str], pool.requests[1][2]["headers"])
    assert headers["X-Callback-AppID"] == "app"
    assert pool.requests[1][2]["json"] == {"code": 0}
    assert pool.requests[2][0:2] == (
        HTTPMethod.DELETE,
        "https://qq.example/v2/groups/group/messages/message",
    )
    assert pool.requests[3][2]["json"] == {}


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
    pool = Pool(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, prepared_payload),
        response(200, {}),
        response(200, uploaded),
    )
    rest = client(pool)

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
    assert [request[2]["json"] for request in pool.requests[1:]] == [
        prepare_body,
        finish_body,
        merge_body,
    ]


def test_v2_messages_reject_legacy_payload_types() -> None:
    for model, payload in (
        (
            QQSendGroupMessageRequest,
            {"group_openid": "group", "msg_type": 3, "ark": {}},
        ),
        (
            QQSendC2CMessageRequest,
            {"user_openid": "user", "msg_type": 4, "embed": {}},
        ),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


@pytest.mark.parametrize("code", [11242, 11252, 11263, 11281])
async def test_system_errors_retry_once_with_the_same_token(code: int) -> None:
    pool = Pool(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, {"code": code, "message": "temporary"}),
        response(200, []),
    )

    await client(pool).request_qq(QQAction.LIST_BOT_GUILDS)

    headers = [
        cast(dict[str, str], request[2]["headers"]) for request in pool.requests[1:]
    ]
    assert [value["Authorization"] for value in headers] == [
        "QQBot token",
        "QQBot token",
    ]


async def test_system_error_is_never_retried_twice() -> None:
    error = {"code": 11242, "message": "temporary"}
    pool = Pool(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, error),
        response(200, error),
    )

    with pytest.raises(QQAPIError) as caught:
        await client(pool).request_qq(QQAction.LIST_BOT_GUILDS)

    assert caught.value.code == 11242
    assert len(pool.requests) == 3


async def test_final_expired_token_response_clears_cached_token() -> None:
    expired = {"code": 11244, "message": "expired"}
    pool = Pool(
        response(200, {"access_token": "stale", "expires_in": 7200}),
        response(200, expired),
        response(200, {"access_token": "fresh", "expires_in": 7200}),
        response(200, expired),
        response(200, {"access_token": "next", "expires_in": 7200}),
    )
    rest = client(pool)

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
    pool = Pool(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(503, body=body, headers={"X-Tps-Trace-ID": "http-trace"}),
    )

    with pytest.raises(QQAPIError) as caught:
        await client(pool).request_qq(QQAction.LIST_BOT_GUILDS)

    assert (
        caught.value.status,
        caught.value.code,
        caught.value.message,
        caught.value.trace_id,
    ) == (503, code, message, "http-trace")


async def test_close_cannot_resurrect_an_inflight_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = Pool()
    started = Event()
    cancelled = Event()

    async def request_token(*args: object, **kwargs: object) -> AsyncHTTPResponse:
        _ = args, kwargs
        if not started.is_set():
            started.set()
            try:
                await Event().wait()
            except CancelledError:
                cancelled.set()
                raise
        return response(200, {"access_token": "next", "expires_in": 7200})

    request = AsyncMock(side_effect=request_token)
    monkeypatch.setattr(pool, "request", request)
    rest = client(pool)

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
    pool = Pool()
    action_started = Event()
    action_cancelled = Event()
    release_action = Event()
    calls = 0

    async def request(*args: object, **kwargs: object) -> AsyncHTTPResponse:
        nonlocal calls
        _ = args, kwargs
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

    monkeypatch.setattr(pool, "request", request)
    rest = client(pool)

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


async def test_start_recovers_from_failed_pool_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = Pool()
    replacement = Pool(response(200, {"access_token": "token", "expires_in": 7200}))
    clear = AsyncMock(side_effect=[RuntimeError("cleanup failed"), None])
    monkeypatch.setattr(pool, "clear", clear, raising=False)
    pools = [pool, replacement]

    def pool_factory() -> AsyncPoolManager:
        return cast(AsyncPoolManager, pools.pop(0))

    monkeypatch.setattr(qq_api, "AsyncPoolManager", pool_factory)
    rest = QQRestClient("app", "secret", base_url="https://qq.example")

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await rest.close()
    with pytest.raises(RuntimeError, match="closed"):
        await rest.access_token()

    await rest.start()
    assert clear.await_count == 2
    assert await rest.access_token() == "token"


async def test_start_waits_for_close_before_replacing_owned_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clearing = Event()
    release = Event()

    class BlockingClearPool(Pool):
        async def clear(self) -> None:
            clearing.set()
            await release.wait()

    first = BlockingClearPool()
    second = Pool(response(200, {"access_token": "restarted", "expires_in": 7200}))
    pools = [first, second]

    def pool_factory() -> AsyncPoolManager:
        return cast(AsyncPoolManager, pools.pop(0))

    monkeypatch.setattr(qq_api, "AsyncPoolManager", pool_factory)
    rest = QQRestClient("app", "secret", base_url="https://qq.example")
    async with timeout(1), TaskGroup() as tasks:
        closing = tasks.create_task(rest.close())
        await clearing.wait()
        starting = tasks.create_task(rest.start())
        release.set()
        await closing
        await starting
    assert await rest.access_token() == "restarted"


async def test_nonempty_invalid_json_is_never_an_empty_success() -> None:
    pool = Pool(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, body=b"<html>upstream failure</html>"),
    )

    with pytest.raises(RuntimeError, match="invalid JSON"):
        await client(pool).request_qq(
            QQAction.ACK_INTERACTION,
            interaction_id="interaction",
        )


async def test_no_content_response_rejects_extra_data() -> None:
    pool = Pool(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, {"unexpected": True}),
    )

    with pytest.raises(RuntimeError, match="invalid response"):
        await client(pool).request_qq(QQAction.DELETE_CHANNEL, channel_id="channel")


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
    pool = Pool(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, payload),
    )

    with pytest.raises(RuntimeError, match="invalid response"):
        await client(pool).request_qq(action, **params)


@pytest.mark.parametrize("phase", ["request", "body"])
async def test_http_deadline_covers_request_and_response_body(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    never = Event()
    pool = Pool()

    async def blocked_request(*args: object, **kwargs: object) -> AsyncHTTPResponse:
        _ = args, kwargs
        if phase == "request":
            await never.wait()
        if phase == "body":
            return cast(AsyncHTTPResponse, GatedResponse(b"", Event(), never))
        return response(200, {"access_token": "token", "expires_in": 7200})

    monkeypatch.setattr(pool, "request", blocked_request)
    monkeypatch.setattr(qq_api, "_HTTP_TIMEOUT", 0.01)

    async with timeout(1):
        with pytest.raises(TimeoutError):
            await client(pool).access_token()


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
    pool = Pool()
    calls = 0

    async def request(*args: object, **kwargs: object) -> AsyncHTTPResponse:
        nonlocal calls
        _ = args, kwargs
        calls += 1
        if calls == 1:
            return cast(
                AsyncHTTPResponse,
                GatedResponse(
                    dumpb({"access_token": "token", "expires_in": 7200}),
                    token_body_entered,
                    token_body_release,
                ),
            )
        if final_phase == "request":
            final_entered.set()
            await final_release.wait()
            return response(200, [])
        return cast(
            AsyncHTTPResponse,
            GatedResponse(dumpb([]), final_entered, final_release),
        )

    monkeypatch.setattr(pool, "request", request)
    now = loop.time
    clock_offset = 0.0
    monkeypatch.setattr(loop, "time", lambda: now() + clock_offset)
    monkeypatch.setattr(loop, "slow_callback_duration", float("inf"))
    monkeypatch.setattr(qq_api, "_HTTP_TIMEOUT", 0.03)

    async def request_until_timeout() -> None:
        with pytest.raises(TimeoutError):
            await client(pool).request_qq(QQAction.LIST_BOT_GUILDS)

    async with timeout(1), TaskGroup() as tasks:
        task = tasks.create_task(request_until_timeout())
        await token_body_entered.wait()
        clock_offset += 0.02
        token_body_release.set()
        await final_entered.wait()
        clock_offset += 0.011
        await task
