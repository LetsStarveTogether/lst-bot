# ruff: file-ignore[line-too-long] - the versioned wire manifest is intentionally one route per line

from asyncio import CancelledError, Event, TaskGroup, get_running_loop, timeout
from collections.abc import Awaitable
from http import HTTPMethod
from textwrap import dedent
from typing import cast
from unittest.mock import AsyncMock

import orjson
import pytest
from bot.gateways import qq_api
from bot.gateways.qq_api import (
    QQ_ROUTES,
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
    QQStreamMessageRequest,
)
from pydantic import JsonValue, ValidationError
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager

from .support import Pool, client, response


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


def test_official_v1_26_route_manifest() -> None:
    expected = dedent("""
        qq.get_gateway GET /gateway QQRequest QQGatewayInfo
        qq.get_gateway_bot GET /gateway/bot QQRequest QQGatewayBotInfo
        qq.get_bot GET /users/@me QQRequest QQIdentifiedUser
        qq.list_bot_guilds GET /users/@me/guilds QQGuildListParams QQGuildList
        qq.generate_share_link POST /v2/generate_url_link QQShareLinkRequest QQShareLink
        qq.get_guild GET /guilds/{guild_id} QQGuildParams QQGuild
        qq.list_guild_channels GET /guilds/{guild_id}/channels QQGuildParams QQChannelList
        qq.create_channel POST /guilds/{guild_id}/channels QQChannelCreateRequest QQChannel
        qq.get_channel GET /channels/{channel_id} QQChannelParams QQChannel
        qq.update_channel PATCH /channels/{channel_id} QQChannelUpdateRequest QQChannel
        qq.delete_channel DELETE /channels/{channel_id} QQChannelParams QQNoContent
        qq.ack_interaction PUT /interactions/{interaction_id} QQInteractionAckRequest QQNoContent
        qq.send_c2c_message POST /v2/users/{user_openid}/messages QQSendC2CMessageRequest QQSentMessage
        qq.send_c2c_stream_message POST /v2/users/{user_openid}/stream_messages QQStreamMessageRequest QQSentMessage
        qq.recall_c2c_message DELETE /v2/users/{user_openid}/messages/{message_id} QQRecallC2CMessageRequest QQNoContent
        qq.upload_c2c_file POST /v2/users/{user_openid}/files QQUploadC2CFileRequest QQFileInfo
        qq.prepare_c2c_file_upload POST /v2/users/{user_id}/upload_prepare QQPrepareC2CFileRequest QQFilePrepareResult
        qq.finish_c2c_file_upload POST /v2/users/{user_id}/upload_part_finish QQFinishC2CFileRequest QQNoContent
        qq.send_group_message POST /v2/groups/{group_openid}/messages QQSendGroupMessageRequest QQSentMessage
        qq.recall_group_message DELETE /v2/groups/{group_openid}/messages/{message_id} QQRecallGroupMessageRequest QQNoContent
        qq.upload_group_file POST /v2/groups/{group_openid}/files QQUploadGroupFileRequest QQFileInfo
        qq.prepare_group_file_upload POST /v2/groups/{group_id}/upload_prepare QQPrepareGroupFileRequest QQFilePrepareResult
        qq.finish_group_file_upload POST /v2/groups/{group_id}/upload_part_finish QQFinishGroupFileRequest QQNoContent
        qq.get_group_info GET /v2/groups/{group_openid}/info QQGroupParams QQGroupInfo
        qq.get_group_bot_state GET /v2/groups/{group_openid}/bot_state QQGroupParams QQGroupBotState
        qq.list_group_join_requests GET /v2/groups/{group_openid}/join_request_list QQJoinRequestListParams QQJoinRequestList
        qq.approve_group_join_request POST /v2/groups/{group_openid}/approval_join_request/{member_openid} QQApproveJoinRequest QQNoContent
        qq.get_group_restrictions GET /v2/groups/{group_openid}/restrict_chat_setting QQGroupParams QQGroupRestrictions
        qq.update_group_restrictions POST /v2/groups/{group_openid}/restrict_chat_setting QQUpdateRestrictionsRequest QQNoContent
        qq.list_group_approval_strategies GET /v2/groups/join_approval_strategy QQStrategyListParams QQStrategyList
        qq.create_group_approval_strategy POST /v2/groups/join_approval_strategy QQCreateStrategyRequest QQStrategyResult
        qq.update_group_approval_strategy PATCH /v2/groups/join_approval_strategy/{strategy_id} QQUpdateStrategyRequest QQStrategyUpdateResult
        qq.delete_group_approval_strategy DELETE /v2/groups/join_approval_strategy/{strategy_id} QQDeleteStrategyRequest QQNoContent
        qq.execute_group_approval_strategy POST /v2/groups/join_approval_strategy/{strategy_id}/execute QQDeleteStrategyRequest QQNoContent
        qq.update_group_approval_whitelist POST /v2/groups/join_approval_strategy/{strategy_id}/whitelist_users QQUpdateStrategyWhitelistRequest QQStrategyWhitelistResult
        qq.get_menu GET /v2/menu QQRequest QQMenuResult
        qq.put_menu PUT /v2/menu QQPutMenuRequest QQVersionResult
        qq.list_panels GET /v2/panels QQPanelListParams QQPanelList
        qq.create_panel POST /v2/panels QQCreatePanelRequest QQPanelIDResult
        qq.get_panel GET /v2/panels/{panel_id} QQPanelParams QQPanelRecord
        qq.update_panel PUT /v2/panels/{panel_id} QQUpdatePanelRequest QQVersionResult
        qq.delete_panel DELETE /v2/panels/{panel_id} QQPanelParams QQNoContent
        qq.update_panel_targets PUT /v2/panels/{panel_id}/target QQUpdatePanelTargetsRequest QQNoContent
        qq.send_channel_message POST /channels/{channel_id}/messages QQSendChannelMessageRequest QQMessage
        qq.recall_channel_message DELETE /channels/{channel_id}/messages/{message_id} QQRecallChannelMessageRequest QQNoContent
        qq.create_dm POST /users/@me/dms QQCreateDMRequest QQDirectMessage
        qq.send_dm_message POST /dms/{guild_id}/messages QQSendDMMessageRequest QQMessage
        qq.recall_dm_message DELETE /dms/{guild_id}/messages/{message_id} QQRecallDMMessageRequest QQNoContent
        qq.get_channel_online_numbers GET /channels/{channel_id}/online_nums QQChannelParams QQOnlineNumbers
        qq.list_guild_members GET /guilds/{guild_id}/members QQMemberListParams QQMemberList
        qq.list_guild_role_members GET /guilds/{guild_id}/roles/{role_id}/members QQRoleMemberListParams QQRoleMemberList
        qq.get_guild_member GET /guilds/{guild_id}/members/{user_id} QQMemberParams QQMember
        qq.delete_guild_member DELETE /guilds/{guild_id}/members/{user_id} QQDeleteMemberRequest QQNoContent
        qq.list_guild_roles GET /guilds/{guild_id}/roles QQGuildParams QQGuildRoles
        qq.create_guild_role POST /guilds/{guild_id}/roles QQCreateRoleRequest QQUpdateRoleResult
        qq.update_guild_role PATCH /guilds/{guild_id}/roles/{role_id} QQUpdateRoleRequest QQUpdateRoleResult
        qq.delete_guild_role DELETE /guilds/{guild_id}/roles/{role_id} QQRoleParams QQNoContent
        qq.add_guild_member_role PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id} QQMemberRoleRequest QQNoContent
        qq.remove_guild_member_role DELETE /guilds/{guild_id}/members/{user_id}/roles/{role_id} QQMemberRoleRequest QQNoContent
        qq.get_member_channel_permissions GET /channels/{channel_id}/members/{user_id}/permissions QQChannelMemberPermissionParams QQChannelPermissions
        qq.update_member_channel_permissions PUT /channels/{channel_id}/members/{user_id}/permissions QQUpdateMemberPermissionRequest QQNoContent
        qq.get_role_channel_permissions GET /channels/{channel_id}/roles/{role_id}/permissions QQChannelRolePermissionParams QQChannelPermissions
        qq.update_role_channel_permissions PUT /channels/{channel_id}/roles/{role_id}/permissions QQUpdateRolePermissionRequest QQNoContent
        qq.add_message_reaction PUT /channels/{channel_id}/messages/{message_id}/reactions/{emoji_type}/{emoji_id} QQEmojiParams QQNoContent
        qq.remove_message_reaction DELETE /channels/{channel_id}/messages/{message_id}/reactions/{emoji_type}/{emoji_id} QQEmojiParams QQNoContent
        qq.list_message_reaction_users GET /channels/{channel_id}/messages/{message_id}/reactions/{emoji_type}/{emoji_id} QQReactionUsersParams QQReactionUsers
        qq.mute_guild PATCH /guilds/{guild_id}/mute QQGuildMuteRequest QQNoContent
        qq.mute_guild_member PATCH /guilds/{guild_id}/members/{user_id}/mute QQMemberMuteRequest QQNoContent
        qq.mute_guild_members PATCH /guilds/{guild_id}/mute QQMultiMemberMuteRequest QQMultiMemberMuteResult
        qq.get_pins GET /channels/{channel_id}/pins QQChannelParams QQPinsMessage
        qq.add_pin PUT /channels/{channel_id}/pins/{message_id} QQPinsParams QQPinsMessage
        qq.delete_pin DELETE /channels/{channel_id}/pins/{message_id} QQPinsParams QQNoContent
        qq.clean_pins DELETE /channels/{channel_id}/pins/all QQChannelParams QQNoContent
        qq.list_schedules GET /channels/{channel_id}/schedules QQScheduleListParams QQScheduleList
        qq.get_schedule GET /channels/{channel_id}/schedules/{schedule_id} QQScheduleParams QQSchedule
        qq.create_schedule POST /channels/{channel_id}/schedules QQScheduleRequest QQSchedule
        qq.update_schedule PATCH /channels/{channel_id}/schedules/{schedule_id} QQScheduleUpdateRequest QQSchedule
        qq.delete_schedule DELETE /channels/{channel_id}/schedules/{schedule_id} QQScheduleParams QQNoContent
        qq.control_audio POST /channels/{channel_id}/audio QQAudioControlRequest QQNoContent
        qq.put_mic PUT /channels/{channel_id}/mic QQChannelParams QQNoContent
        qq.delete_mic DELETE /channels/{channel_id}/mic QQChannelParams QQNoContent
        qq.get_api_permissions GET /guilds/{guild_id}/api_permission QQGuildParams QQAPIPermissions
        qq.require_api_permission POST /guilds/{guild_id}/api_permission/demand QQAPIPermissionDemandRequest QQAPIPermissionDemand
        qq.list_forum_threads GET /channels/{channel_id}/threads QQChannelParams QQForumThreadList
        qq.get_forum_thread GET /channels/{channel_id}/threads/{thread_id} QQForumThreadParams QQForumThreadDetail
        qq.create_forum_thread PUT /channels/{channel_id}/threads QQForumCreateRequest QQForumCreateResult
        qq.delete_forum_thread DELETE /channels/{channel_id}/threads/{thread_id} QQForumThreadParams QQNoContent
        qq.get_message_setting GET /guilds/{guild_id}/message/setting QQGuildParams QQMessageSetting
        qq.create_guild_announce POST /guilds/{guild_id}/announces QQGuildAnnounceRequest QQAnnounce
        qq.delete_guild_announce DELETE /guilds/{guild_id}/announces/{message_id} QQGuildAnnounceDeleteRequest QQNoContent
        qq.clean_guild_announces DELETE /guilds/{guild_id}/announces/all QQGuildParams QQNoContent
    """).strip()

    expected = {
        action: (method, path, request, response)
        for action, method, path, request, response in map(
            str.split, expected.splitlines()
        )
    }
    actual = {
        action.value: (
            str(route.method),
            route.path,
            route.request.__name__,
            route.response.__name__,
        )
        for action, route in QQ_ROUTES.items()
    }
    assert actual == expected


@pytest.mark.parametrize(
    ("permission_type", "subjects"),
    [
        (0, {"specify_user_ids": ["user"]}),
        (1, {}),
        (2, {}),
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
        (0, {"specify_role_ids": ["role"]}),
        (0, {"specify_user_ids": ["user"], "specify_role_ids": ["role"]}),
        (0, {"specify_user_ids": []}),
        (1, {"specify_user_ids": ["user"]}),
        (1, {"specify_role_ids": ["role"]}),
        (2, {"specify_user_ids": ["user"]}),
        (2, {"specify_role_ids": ["role"]}),
        (3, {}),
        (3, {"specify_user_ids": ["user"]}),
        (3, {"specify_user_ids": ["user"], "specify_role_ids": ["role"]}),
        (3, {"specify_role_ids": []}),
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
        "id": "button",
        "render_data": {"label": "Open", "visited_label": "Opened", "style": 1},
        "action": {"type": 0, "permission": {"type": 1}, "data": "open"},
        "group_id": "group",
    }
    assert (
        QQKeyboardButton.model_validate(payload).model_dump(exclude_none=True)
        == payload
    )

    for field in ("visited_label", "style"):
        render_data = dict(payload["render_data"])
        render_data.pop(field)
        with pytest.raises(ValidationError):
            QQKeyboardButton.model_validate(payload | {"render_data": render_data})


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
    for msg_seq in (0, 65535):
        assert (
            QQStreamMessageRequest(
                user_openid="user",
                input_mode="replace",
                input_state=1,
                index=0,
                content_type="markdown",
                content_raw="answer",
                event_id="event",
                msg_id="message",
                msg_seq=msg_seq,
            ).msg_seq
            == msg_seq
        )

    with pytest.raises(ValidationError, match="mutually exclusive"):
        QQSendC2CMessageRequest(
            user_openid="user",
            msg_type=0,
            content="answer",
            msg_id="message",
            is_wakeup=True,
        )
    for msg_seq in (-1, 65536):
        with pytest.raises(ValidationError):
            QQStreamMessageRequest(
                user_openid="user",
                input_mode="replace",
                input_state=1,
                index=0,
                content_type="markdown",
                content_raw="answer",
                event_id="event",
                msg_id="message",
                msg_seq=msg_seq,
            )
    prepared = QQFilePrepareResult.model_validate({
        "upload_id": "upload",
        "block_size": 1024,
        "parts": [{"index": 1, "presigned_url": "https://upload.example/1"}],
        "concurrency": 2,
        "retry_timeout": 30,
    })
    assert prepared.parts[0].index == 1
    with pytest.raises(ValueError, match="HTTPS"):
        QQRestClient("app", "secret", base_url="http://qq.example")


async def test_ack_interaction_sends_callback_app_id() -> None:
    pool = Pool(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, {}),
    )

    result = await client(pool).request_qq(
        QQAction.ACK_INTERACTION,
        interaction_id="interaction",
    )

    assert isinstance(result, QQNoContent)
    assert pool.requests[1][0:2] == (
        HTTPMethod.PUT,
        "https://qq.example/interactions/interaction",
    )
    headers = cast(dict[str, str], pool.requests[1][2]["headers"])
    assert headers["X-Callback-AppID"] == "app"
    assert pool.requests[1][2]["json"] == {"code": 0}


async def test_file_upload_supports_inline_data_and_chunk_completion() -> None:
    uploaded = {"file_uuid": "file", "file_info": "info", "ttl": 60}
    pool = Pool(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, uploaded),
        response(200, uploaded),
    )
    rest = client(pool)

    await rest.request_qq(
        QQAction.UPLOAD_C2C_FILE,
        user_openid="user",
        file_type=1,
        file_data="YQ==",
        srv_send_msg=False,
    )
    await rest.request_qq(
        QQAction.UPLOAD_GROUP_FILE,
        group_openid="group",
        upload_id="upload",
    )

    assert [request[2]["json"] for request in pool.requests[1:]] == [
        {"file_type": 1, "file_data": "YQ==", "srv_send_msg": False},
        {"upload_id": "upload"},
    ]


async def test_c2c_and_group_messages_support_embed_and_ark() -> None:
    sent = {"id": "message", "timestamp": "2026-08-20T00:00:00Z"}
    pool = Pool(
        response(200, {"access_token": "token", "expires_in": 7200}),
        response(200, sent),
        response(200, sent),
    )
    rest = client(pool)

    await rest.request_qq(
        QQAction.SEND_GROUP_MESSAGE,
        group_openid="group",
        msg_type=3,
        ark={"template_id": 23, "kv": []},
    )
    await rest.request_qq(
        QQAction.SEND_C2C_MESSAGE,
        user_openid="user",
        msg_type=4,
        embed={"title": "title"},
    )

    assert [request[2]["json"] for request in pool.requests[1:]] == [
        {"msg_type": 3, "ark": {"template_id": 23, "kv": []}},
        {"msg_type": 4, "embed": {"title": "title"}},
    ]


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


async def test_second_http_401_invalidates_the_refreshed_token() -> None:
    pool = Pool(
        response(200, {"access_token": "stale", "expires_in": 7200}),
        response(401, body=b"<html>unauthorized</html>"),
        response(200, {"access_token": "fresh", "expires_in": 7200}),
        response(401, headers={"X-Trace-ID": "second-401"}),
        response(200, {"access_token": "next", "expires_in": 7200}),
    )
    rest = client(pool)

    with pytest.raises(QQAPIError) as caught:
        await rest.request_qq(QQAction.LIST_BOT_GUILDS)

    assert (caught.value.status, caught.value.trace_id) == (401, "second-401")
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


async def test_failed_pool_cleanup_leaves_close_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = Pool(response(200, {"access_token": "token", "expires_in": 7200}))
    clear = AsyncMock(side_effect=[RuntimeError("cleanup failed"), None])
    monkeypatch.setattr(pool, "clear", clear, raising=False)

    def pool_factory() -> AsyncPoolManager:
        return cast(AsyncPoolManager, pool)

    monkeypatch.setattr(qq_api, "AsyncPoolManager", pool_factory)
    rest = QQRestClient("app", "secret", base_url="https://qq.example")

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await rest.close()
    with pytest.raises(RuntimeError, match="closed"):
        await rest.access_token()

    await rest.close()
    assert clear.await_count == 2
    with pytest.raises(RuntimeError, match="closed"):
        await rest.access_token()


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
                    orjson.dumps({"access_token": "token", "expires_in": 7200}),
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
            GatedResponse(orjson.dumps([]), final_entered, final_release),
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
