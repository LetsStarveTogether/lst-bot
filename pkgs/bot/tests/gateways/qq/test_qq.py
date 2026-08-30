# ruff: file-ignore[private-member-access]
from asyncio import Event as AsyncEvent
from asyncio import TaskGroup, timeout
from http import HTTPMethod
from string import Formatter
from typing import cast
from unittest.mock import AsyncMock, call

import pytest
from bot import (
    Action,
    ActionParamInput,
    Bot,
    BotSelf,
    ChannelMessageDeleteNoticeEvent,
    ChannelMessageEvent,
    Event,
    FriendDecreaseNoticeEvent,
    FriendIncreaseNoticeEvent,
    GroupMemberDecreaseNoticeEvent,
    GroupMemberIncreaseNoticeEvent,
    GroupMessageEvent,
    GroupRequestEvent,
    GuildMemberDecreaseNoticeEvent,
    GuildMemberIncreaseNoticeEvent,
    Injected,
    MsgInput,
    MsgSegmentType,
    NoticeEvent,
    PrivateMessageDeleteNoticeEvent,
    PrivateMessageEvent,
)
from bot.gateways import qq as qq_gateway_module
from bot.gateways.base import WebsocketsConnection
from bot.gateways.qq import QQDispatch, QQGateway
from bot.gateways.qq_api import (
    QQ_ROUTES,
    QQAccessTokenError,
    QQAccessTokenRequest,
    QQAction,
    QQAPIError,
    QQAsyncResult,
    QQAudioControlRequest,
    QQFileUploadFields,
    QQGatewayInfo,
    QQGuildAnnounceRequest,
    QQGuildList,
    QQGuildListParams,
    QQJoinRequestList,
    QQMenuItem,
    QQMenuLinkItem,
    QQNoContent,
    QQRecurringRestriction,
    QQRoleMemberListParams,
    QQSchedulePatch,
    QQSendC2CMessageRequest,
    QQSendGroupMessageRequest,
    QQSentMessage,
    QQStrategyGroups,
    QQStrategyList,
    QQStreamMessageRequest,
)
from bot.json import dumpb, loads
from bot.protocol.actions import ActionParamModel
from bot_test_support import ScriptedWebSocket
from pydantic import JsonValue, TypeAdapter, ValidationError
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from tests.gateways.support import response as _response

from . import support


def test_route_registry_is_well_formed() -> None:
    assert set(QQAction) == set(QQ_ROUTES)
    for route in QQ_ROUTES.values():
        placeholders = {
            field
            for _, field, _, _ in Formatter().parse(route.path)
            if field is not None
        }
        assert route.path.startswith("/")
        assert "?" not in route.path
        assert "#" not in route.path
        for name in placeholders:
            field = route.request.model_fields[name]
            assert field.is_required()
            assert field.serialization_alias in {None, name}


def test_request_models_reject_invalid_discriminators_and_cross_fields() -> None:
    assert QQGuildListParams(before="before", after="after").before == "before"

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        TypeAdapter(QQMenuItem).validate_python({"name": "x", "type": "future"})

    with pytest.raises(ValidationError, match="matching payload"):
        QQSendGroupMessageRequest.model_validate({
            "group_openid": "group",
            "msg_type": 7,
            "content": "wrong payload",
            "msg_id": "source-message",
        })
    with pytest.raises(ValidationError, match="mutually exclusive"):
        QQSendGroupMessageRequest(
            group_openid="group",
            msg_type=0,
            content="reply",
            msg_id="message",
            event_id="event",
        )

    for model, payload in (
        (
            QQSendGroupMessageRequest,
            {
                "group_openid": "group",
                "msg_type": 7,
                "media": {"file_info": "media"},
                "markdown": {"content": "incompatible"},
            },
        ),
        (
            QQSendC2CMessageRequest,
            {
                "user_openid": "user",
                "msg_type": 7,
                "media": {"file_info": "media"},
                "input_notify": {"input_type": 1, "input_second": 1},
            },
        ),
    ):
        with pytest.raises(ValidationError, match="incompatible payloads"):
            model.model_validate(payload)

    for model, payload in (
        (QQ_ROUTES[QQAction.UPDATE_CHANNEL].request, {"channel_id": "channel"}),
        (
            QQ_ROUTES[QQAction.UPDATE_GROUP_APPROVAL_STRATEGY].request,
            {"strategy_id": "strategy"},
        ),
        (QQSchedulePatch, {"description": None}),
    ):
        with pytest.raises(ValidationError, match="at least one change"):
            model.model_validate(payload)

    for payload, error in (
        ({"guild_id": "guild", "message_id": "message"}, "requires channel_id"),
        (
            {
                "guild_id": "guild",
                "channel_id": "channel",
                "message_id": "message",
                "announces_type": 1,
            },
            "requires type 0 and no recommended channels",
        ),
        (
            {
                "guild_id": "guild",
                "channel_id": "channel",
                "message_id": "message",
                "recommend_channels": [
                    {"channel_id": "recommended", "introduce": "intro"}
                ],
            },
            "requires type 0 and no recommended channels",
        ),
        ({"guild_id": "guild"}, "requires channels"),
    ):
        with pytest.raises(ValidationError, match=error):
            QQGuildAnnounceRequest.model_validate(payload)

    assert QQGuildAnnounceRequest.model_validate({
        "guild_id": "guild",
        "recommend_channels": [{"channel_id": "channel", "introduce": "intro"}],
    }).model_dump(exclude={"guild_id"}, exclude_none=True) == {
        "announces_type": 0,
        "recommend_channels": [{"channel_id": "channel", "introduce": "intro"}],
    }


async def test_rest_routes_cache_token_and_preserve_wire_boundaries() -> None:
    pool = support.Pool(
        {"access_token": "token", "expires_in": "7200"},
        [{"id": "guild", "name": "Guild"}],
        {"id": "sent", "timestamp": "2026-08-17T00:00:00Z"},
        _response(204),
    )
    client = support.client(pool)

    guilds = await client.request_qq(
        QQAction.LIST_BOT_GUILDS,
        after="cursor",
        limit=10,
    )
    sent = await client.request_qq(
        QQAction.SEND_GROUP_MESSAGE,
        group_openid="group/one",
        msg_type=0,
        content="hello",
        msg_id="source-message",
    )
    deleted = await client.request_qq(
        QQAction.DELETE_GUILD_ANNOUNCE,
        guild_id="guild",
        message_id="all",
    )

    assert isinstance(guilds, QQGuildList)
    assert guilds.model_dump(exclude_none=True) == [{"id": "guild", "name": "Guild"}]
    assert isinstance(sent, QQSentMessage)
    assert isinstance(deleted, QQNoContent)
    assert [request[:2] for request in pool.requests] == [
        (HTTPMethod.POST, "https://qq.example/app/getAppAccessToken"),
        (
            HTTPMethod.GET,
            "https://qq.example/users/@me/guilds?after=cursor&limit=10",
        ),
        (HTTPMethod.POST, "https://qq.example/v2/groups/group%2Fone/messages"),
        (
            HTTPMethod.DELETE,
            "https://qq.example/guilds/guild/announces/all",
        ),
    ]
    assert pool.requests[0][2]["json"] == {
        "appId": "app",
        "clientSecret": "secret",
    }
    assert pool.requests[1][2]["json"] is None
    assert pool.requests[2][2]["json"] == {
        "content": "hello",
        "msg_id": "source-message",
        "msg_type": 0,
    }
    assert pool.requests[3][2]["json"] is None
    for _, _, kwargs in pool.requests:
        assert kwargs["retries"] is False
        assert kwargs["preload_content"] is False
        assert kwargs["redirect"] is False
    for _, _, kwargs in pool.requests[1:]:
        assert kwargs["headers"] == {
            "Authorization": "QQBot token",
            "Content-Type": "application/json",
            "X-Union-Appid": "app",
        }
        assert kwargs["timeout"] == pytest.approx(30.0)


async def test_websocket_identifies_dispatches_heartbeats_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ScriptedWebSocket(
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {
            "op": 0,
            "s": 1,
            "t": "READY",
            "d": {
                "version": 1,
                "session_id": "session",
                "user": {"id": "bot", "username": "Bot", "bot": True},
                "shard": [0, 1],
            },
        },
        {
            "id": "private-event",
            "op": 0,
            "s": 2,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": "private-message",
                "author": {"user_openid": "user"},
                "content": "private",
                "timestamp": "2026-08-17T00:00:00Z",
                "message_type": 0,
                "message_scene": {"source": "c2c", "ext": []},
            },
        },
        {
            "id": "group-event",
            "op": 0,
            "s": 3,
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {
                "id": "group-message",
                "author": {"member_openid": "member"},
                "content": "group",
                "group_openid": "group",
                "timestamp": "2026-08-17T00:00:01Z",
                "message_type": 0,
                "message_scene": {"source": "group", "ext": []},
            },
        },
        {"op": 1},
        {"op": 11},
        {"op": 7},
    )
    second = ScriptedWebSocket(
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {"op": 0, "s": 4, "t": "RESUMED", "d": ""},
        {
            "id": "future-event",
            "op": 0,
            "s": 5,
            "t": "FUTURE_EVENT",
            "d": {"new_field": True},
        },
        {"op": 1},
        {"op": 11},
    )
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {"url": "wss://qq.example"},
        {"url": "wss://qq.example"},
    )
    connect = AsyncMock(side_effect=[first, second])
    reconnect_sleep = AsyncMock()
    monkeypatch.setattr(qq_gateway_module, "sleep", reconnect_sleep)

    bot = Bot()
    gateway = QQGateway(
        bot,
        app_id="app",
        client_secret=support.CREDENTIAL,
        base_url="https://qq.example",
        http_pool=cast(AsyncPoolManager, pool),
        websocket_connector=connect,
    )
    bot.add_gateway(gateway)
    events: dict[str, Event] = {}
    all_received = AsyncEvent()

    @bot.on_event(block=True)
    def collect(event: Injected[Event]) -> None:
        if isinstance(event, PrivateMessageEvent):
            events["private"] = event
        elif isinstance(event, GroupMessageEvent):
            events["group"] = event
        elif isinstance(event, NoticeEvent) and event.detail_type == "qq.future_event":
            events["unknown"] = event
        if len(events) == 3:
            all_received.set()

    async with timeout(3), bot:
        await all_received.wait()
        assert gateway._online
        identify = loads(await first.sent.get())
        first_heartbeat = loads(await first.sent.get())
        resume = loads(await second.sent.get())
        second_heartbeat = loads(await second.sent.get())

    assert connect.await_args_list == [
        call("wss://qq.example/", None),
        call("wss://qq.example/", None),
    ]
    reconnect_sleep.assert_awaited_once_with(1.0)
    assert identify == {
        "op": 2,
        "d": {
            "token": "QQBot token",
            "intents": 1 << 25,
            "shard": [0, 1],
        },
    }
    assert first_heartbeat == {"op": 1, "d": 3}
    assert resume == {
        "op": 6,
        "d": {"token": "QQBot token", "session_id": "session", "seq": 3},
    }
    assert second_heartbeat == {"op": 1, "d": 5}
    assert cast(PrivateMessageEvent, events["private"]).message.text == "private"
    assert cast(GroupMessageEvent, events["group"]).group_id == "group"
    assert events["unknown"].model_extra == {
        "qq_event_type": "FUTURE_EVENT",
        "qq_data": {"new_field": True},
        "qq_raw": True,
    }
    assert first.closed.is_set()
    assert second.closed.is_set()
    assert not gateway._online
    assert gateway._task is None
    assert gateway._session_id is None
    assert gateway._closed_event.is_set()


async def test_repeated_v2_messages_are_dispatched_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = support.gateway(support.Pool())
    events: list[Event] = []
    monkeypatch.setattr(instance, "enqueue_event", events.append)

    for sequence, message_index in enumerate(("part-1", "part-1", "part-2"), 1):
        await instance._receive_dispatch(
            qq_gateway_module.QQGatewayPayload.model_validate({
                "id": f"event-{sequence}",
                "op": 0,
                "s": sequence,
                "t": "C2C_MESSAGE_CREATE",
                "d": {
                    "id": "message",
                    "author": {"user_openid": "user"},
                    "content": "hello",
                    "timestamp": "2026-08-17T00:00:00Z",
                    "message_scene": {
                        "source": "c2c",
                        "ext": [f"msg_idx={message_index}"],
                    },
                },
            })
        )

    assert len(events) == 2
    assert instance._seq == 3


async def test_gateway_lifecycle_actually_restarts() -> None:
    websockets = [
        ScriptedWebSocket({"op": 10, "d": {"heartbeat_interval": 60_000}}),
        ScriptedWebSocket({"op": 10, "d": {"heartbeat_interval": 60_000}}),
    ]
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {"url": "wss://qq.example"},
        {"access_token": "token", "expires_in": 7200},
        {"url": "wss://qq.example"},
    )
    connector = AsyncMock(side_effect=websockets)
    gateway = support.gateway(pool, websocket_connector=connector)
    bot = gateway.bot
    bot.add_gateway(gateway)

    async with timeout(1):
        for index, websocket in enumerate(websockets, start=1):
            async with bot:
                identify = loads(await websocket.sent.get())
                assert identify["op"] == 2
                assert connector.await_count == index
            assert websocket.closed.is_set()


def test_event_model_families_map_to_common_events() -> None:
    legacy_message: dict[str, JsonValue] = {
        "id": "message",
        "channel_id": "channel",
        "guild_id": "guild",
        "content": "hello",
        "timestamp": "2026-08-17T00:00:00Z",
        "author": {"id": "user"},
        "attachments": [],
        "mentions": [],
    }
    guild_member: dict[str, JsonValue] = {
        "guild_id": "guild",
        "joined_at": "2026-08-17T00:00:00Z",
        "nick": "member",
        "op_user_id": "operator",
        "roles": [],
        "user": {"id": "user"},
    }
    message_delete = {"message": legacy_message, "op_user": {"id": "operator"}}
    payloads: dict[str, dict[str, JsonValue]] = {
        "FRIEND_ADD": {
            "timestamp": 1,
            "openid": "user",
            "scene": 1000,
        },
        "FRIEND_DEL": {"timestamp": 1, "openid": "user"},
        "C2C_MSG_RECEIVE": {"timestamp": 1, "openid": "user"},
        "GROUP_ADD_ROBOT": {
            "timestamp": 1,
            "group_openid": "group",
            "op_member_openid": "member",
        },
        "GROUP_DEL_ROBOT": {
            "timestamp": 1,
            "group_openid": "group",
            "op_member_openid": "member",
        },
        "GROUP_MEMBER_ADD": {
            "timestamp": 1,
            "group_openid": "group",
            "member_openid": "member",
            "user_openid": "user",
        },
        "GROUP_MEMBER_REMOVE": {
            "timestamp": 1,
            "group_openid": "group",
            "member_openid": "member",
        },
        "GUILD_MEMBER_ADD": guild_member,
        "GUILD_MEMBER_UPDATE": guild_member,
        "GUILD_MEMBER_REMOVE": guild_member,
        "SUBSCRIBE_MESSAGE_STATUS": {
            "result": [
                {
                    "template_id": 1,
                    "custom_template_id": "template",
                    "op": 1,
                    "subscribe_id": "subscribe",
                    "subscribe_ts": 1,
                    "update_ts": 2,
                }
            ]
        },
        "GUILD_CREATE": {"id": "guild"},
        "CHANNEL_CREATE": {"id": "channel", "guild_id": "guild"},
        "AT_MESSAGE_CREATE": legacy_message,
        "DIRECT_MESSAGE_CREATE": legacy_message,
        "MESSAGE_DELETE": message_delete,
        "PUBLIC_MESSAGE_DELETE": message_delete,
        "DIRECT_MESSAGE_DELETE": message_delete,
        "INTERACTION_CREATE": {
            "id": "interaction",
            "type": 11,
            "data": {"type": 11, "resolved": {}},
            "version": 1,
        },
    }
    expected_types: dict[str, type[Event]] = {
        "FRIEND_ADD": FriendIncreaseNoticeEvent,
        "FRIEND_DEL": FriendDecreaseNoticeEvent,
        "C2C_MSG_RECEIVE": NoticeEvent,
        "GROUP_ADD_ROBOT": GroupMemberIncreaseNoticeEvent,
        "GROUP_DEL_ROBOT": GroupMemberDecreaseNoticeEvent,
        "GROUP_MEMBER_ADD": NoticeEvent,
        "GROUP_MEMBER_REMOVE": NoticeEvent,
        "GUILD_MEMBER_ADD": GuildMemberIncreaseNoticeEvent,
        "GUILD_MEMBER_UPDATE": NoticeEvent,
        "GUILD_MEMBER_REMOVE": GuildMemberDecreaseNoticeEvent,
        "SUBSCRIBE_MESSAGE_STATUS": NoticeEvent,
        "GUILD_CREATE": NoticeEvent,
        "CHANNEL_CREATE": NoticeEvent,
        "AT_MESSAGE_CREATE": ChannelMessageEvent,
        "DIRECT_MESSAGE_CREATE": PrivateMessageEvent,
        "MESSAGE_DELETE": ChannelMessageDeleteNoticeEvent,
        "PUBLIC_MESSAGE_DELETE": ChannelMessageDeleteNoticeEvent,
        "DIRECT_MESSAGE_DELETE": PrivateMessageDeleteNoticeEvent,
        "INTERACTION_CREATE": NoticeEvent,
    }
    assert payloads.keys() == expected_types.keys()
    gateway = support.gateway(support.Pool())
    for event_type, payload in payloads.items():
        event = gateway._event_from_dispatch(
            QQDispatch.model_validate({
                "id": "event",
                "op": 0,
                "s": 1,
                "t": event_type,
                "d": payload,
            })
        )
        expected_type = expected_types[event_type]
        assert type(event) is expected_type, event_type
        if expected_type is NoticeEvent:
            assert event.detail_type == f"qq.{event_type.lower()}", event_type
        if isinstance(
            event, GroupMemberIncreaseNoticeEvent | GroupMemberDecreaseNoticeEvent
        ):
            assert (event.user_id, event.group_id, event.operator_id) == (
                "app",
                "group",
                "member",
            )
            assert event.sub_type == (
                "invite" if event_type == "GROUP_ADD_ROBOT" else "kick"
            )
        if isinstance(
            event, GuildMemberIncreaseNoticeEvent | GuildMemberDecreaseNoticeEvent
        ):
            assert (event.user_id, event.guild_id, event.operator_id) == (
                "user",
                "guild",
                "operator",
            )
        if isinstance(event, ChannelMessageDeleteNoticeEvent):
            assert (
                event.user_id,
                event.guild_id,
                event.channel_id,
                event.message_id,
                event.operator_id,
            ) == ("user", "guild", "channel", "message", "operator")
        if isinstance(event, PrivateMessageDeleteNoticeEvent):
            assert (event.user_id, event.message_id) == ("user", "message")
        if isinstance(event, NoticeEvent):
            assert event.model_extra is not None
            assert event.model_extra["qq_event_type"] == event_type
            assert event.model_extra["qq_data"] == payload
            assert event.model_extra["qq_raw"] is False
        else:
            assert event.model_extra is not None
            qq_data = event.model_extra["qq_data"]
            assert isinstance(qq_data, dict)
            assert qq_data["id"] == "message"
            assert event.model_extra["qq_raw"] is False


def test_authorization_interaction_omits_data_type() -> None:
    interaction = qq_gateway_module.QQInteraction.model_validate({
        "id": "interaction",
        "type": 18,
        "data": {
            "resolved": {
                "authorize_data": {"opt_scene": "setting", "scope": "c2c_push"}
            }
        },
        "version": 1,
    })

    assert interaction.data.type is None


@pytest.mark.parametrize(("outer", "inner"), [(11, None), (11, 12), (18, 11)])
def test_interaction_data_type_matches_outer_type(
    outer: int,
    inner: int | None,
) -> None:
    data: dict[str, JsonValue] = {"resolved": {}}
    if inner is not None:
        data["type"] = inner

    with pytest.raises(ValidationError):
        qq_gateway_module.QQInteraction.model_validate({
            "id": "interaction",
            "type": outer,
            "data": data,
            "version": 1,
        })


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            "pong",
            {
                "content": "pong",
                "msg_id": "incoming-message",
                "msg_seq": 1,
                "msg_type": 0,
            },
        ),
        (
            [{"type": "image", "data": {"file_id": "uploaded-image"}}],
            {
                "media": {"file_info": "uploaded-image"},
                "msg_id": "incoming-message",
                "msg_seq": 1,
                "msg_type": 7,
            },
        ),
    ],
    ids=["text", "image"],
)
async def test_common_reply_sends_the_incoming_message_id(
    reply: MsgInput,
    expected: dict[str, object],
) -> None:
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {"id": "sent", "timestamp": "2026-08-17T00:00:01Z"},
    )
    gateway = support.gateway(pool, online=True)
    event = PrivateMessageEvent.model_validate({
        "id": "event",
        "self": {"platform": "qq", "user_id": "app"},
        "time": 1.0,
        "sub_type": "",
        "message_id": "incoming-message",
        "message": [{"type": "text", "data": {"text": "ping"}}],
        "alt_message": "ping",
        "user_id": "user",
        "qq_scene": "c2c",
    })

    response = await gateway.connection_for(event.self_).execute_message_action(
        event,
        reply,
    )

    assert isinstance(response, QQSentMessage)
    assert pool.requests[1][:2] == (
        HTTPMethod.POST,
        "https://qq.example/v2/users/user/messages",
    )
    assert pool.requests[1][2]["json"] == expected


async def test_direct_message_reply_preserves_the_dm_target() -> None:
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {"id": "sent", "timestamp": "2026-08-17T00:00:01Z"},
    )
    gateway = support.gateway(pool, online=True)
    event = gateway._event_from_dispatch(
        QQDispatch.model_validate({
            "id": "event",
            "op": 0,
            "s": 1,
            "t": "DIRECT_MESSAGE_CREATE",
            "d": {
                "id": "incoming-message",
                "channel_id": "channel",
                "guild_id": "guild",
                "content": "ping",
                "timestamp": "2026-08-17T00:00:00Z",
                "author": {"id": "user"},
            },
        })
    )
    assert isinstance(event, PrivateMessageEvent)

    await gateway.connection_for(event.self_).execute_message_action(event, "pong")

    assert pool.requests[1][:2] == (
        HTTPMethod.POST,
        "https://qq.example/dms/guild/messages",
    )


async def test_message_actions_require_an_online_gateway() -> None:
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {"file_uuid": "file", "file_info": "uploaded", "ttl": 60},
    )
    gateway = support.gateway(pool)
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="app"))

    with pytest.raises(ConnectionError, match="not connected"):
        await connection.action(
            Action.SEND_MESSAGE,
            detail_type="private",
            user_id="user",
            message=[{"type": "image", "data": {"file_id": "https://qq/image"}}],
        )
    for action in (action for action in QQAction if action.name.startswith("SEND_")):
        with pytest.raises(ConnectionError, match="not connected"):
            await connection.action(action)
    for action in (QQAction.UPLOAD_C2C_FILE, QQAction.UPLOAD_GROUP_FILE):
        with pytest.raises(ConnectionError, match="not connected"):
            await connection.action(action, srv_send_msg=True)
    assert not pool.requests

    await connection.action(
        QQAction.UPLOAD_C2C_FILE,
        user_openid="user",
        file_type=1,
        url="https://qq.example/image",
        srv_send_msg=False,
    )
    assert len(pool.requests) == 2


async def test_actions_reject_wrong_or_foreign_bot_connections() -> None:
    gateway = support.gateway(support.Pool())
    wrong_self = gateway.connection_for(BotSelf(platform="qq", user_id="wrong"))
    foreign = support.gateway(support.Pool()).connection_for(
        BotSelf(platform="qq", user_id="app")
    )

    for connection in (wrong_self, foreign):
        with pytest.raises(ValueError, match="wrong BotSelf"):
            await gateway.request_action(
                connection,
                Action.GET_STATUS,
                ActionParamModel(),
            )


@pytest.mark.parametrize(
    ("target", "path"),
    [
        (
            {"detail_type": "private", "user_id": "user"},
            "/v2/users/user/messages",
        ),
        (
            {"detail_type": "group", "group_id": "group"},
            "/v2/groups/group/messages",
        ),
        (
            {
                "detail_type": "channel",
                "guild_id": "guild",
                "channel_id": "channel",
            },
            "/channels/channel/messages",
        ),
        (
            {
                "detail_type": "private",
                "qq_scene": "dm",
                "user_id": "user",
                "guild_id": "guild",
                "channel_id": "channel",
            },
            "/dms/guild/messages",
        ),
    ],
    ids=[
        "c2c-default",
        "group-default",
        "channel-default",
        "dm",
    ],
)
async def test_common_send_message_maps_all_qq_scenes(
    target: dict[str, str],
    path: str,
) -> None:
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {"id": "sent", "timestamp": "2026-08-17T00:00:01Z"},
    )
    connection = support.gateway(pool, online=True).connection_for(
        BotSelf(platform="qq", user_id="app")
    )

    await connection.action(Action.SEND_MESSAGE, **target, message="hello")

    assert pool.requests[1][:2] == (
        HTTPMethod.POST,
        f"https://qq.example{path}",
    )
    assert pool.requests[1][2]["json"] == {
        "content": "hello",
        **({"msg_type": 0} if path.startswith("/v2/") else {}),
    }


@pytest.mark.parametrize(
    ("target", "path"),
    [
        (
            {"detail_type": "private", "user_id": "user"},
            "/v2/users/user/messages",
        ),
        (
            {"detail_type": "group", "group_id": "group"},
            "/v2/groups/group/messages",
        ),
    ],
    ids=["c2c", "group"],
)
async def test_common_media_message_preserves_caption(
    target: dict[str, str],
    path: str,
) -> None:
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {"id": "sent", "timestamp": "2026-08-17T00:00:01Z"},
    )
    connection = support.gateway(pool, online=True).connection_for(
        BotSelf(platform="qq", user_id="app")
    )

    await connection.action(
        Action.SEND_MESSAGE,
        **target,
        message=[
            {"type": "text", "data": {"text": "caption"}},
            {"type": "image", "data": {"file_id": "uploaded-image"}},
        ],
    )

    assert pool.requests[1][:2] == (
        HTTPMethod.POST,
        f"https://qq.example{path}",
    )
    assert pool.requests[1][2]["json"] == {
        "content": "caption",
        "media": {"file_info": "uploaded-image"},
        "msg_type": 7,
    }


@pytest.mark.parametrize(
    ("event_type", "event_data", "resource_path"),
    [
        (
            "C2C_MESSAGE_CREATE",
            {
                "author": {"user_openid": "user"},
                "message_scene": {"source": "c2c"},
            },
            "/v2/users/user",
        ),
        (
            "GROUP_AT_MESSAGE_CREATE",
            {
                "author": {"member_openid": "member"},
                "group_openid": "group",
                "message_scene": {"source": "group"},
            },
            "/v2/groups/group",
        ),
    ],
    ids=["c2c", "group"],
)
async def test_common_media_reply_uploads_inbound_attachment_url(
    event_type: str,
    event_data: dict[str, object],
    resource_path: str,
) -> None:
    attachment_url = "//qq.example/image.png"
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {"file_uuid": "file", "file_info": "uploaded-image", "ttl": 60},
        {"id": "sent", "timestamp": "2026-08-17T00:00:01Z"},
    )
    gateway = support.gateway(pool, online=True)
    event = gateway._event_from_dispatch(
        QQDispatch.model_validate({
            "id": "event",
            "op": 0,
            "s": 1,
            "t": event_type,
            "d": {
                "id": "incoming-message",
                "content": "",
                "timestamp": "2026-08-17T00:00:00Z",
                "message_type": 0,
                "attachments": [{"url": attachment_url, "content_type": "image/png"}],
                **event_data,
            },
        })
    )
    assert isinstance(event, PrivateMessageEvent | GroupMessageEvent)

    await gateway.connection_for(event.self_).execute_message_action(
        event,
        event.message,
    )

    assert [request[1] for request in pool.requests[1:]] == [
        f"https://qq.example{resource_path}/files",
        f"https://qq.example{resource_path}/messages",
    ]
    assert pool.requests[1][2]["json"] == {
        "file_type": 1,
        "srv_send_msg": False,
        "url": f"https:{attachment_url}",
    }
    assert pool.requests[2][2]["json"] == {
        "media": {"file_info": "uploaded-image"},
        "msg_id": "incoming-message",
        "msg_seq": 1,
        "msg_type": 7,
    }


@pytest.mark.parametrize(
    ("action", "qq_action", "params", "expected"),
    [
        (
            Action.GET_GROUP_INFO,
            QQAction.GET_GROUP_INFO,
            {"group_id": "group"},
            {"group_openid": "group"},
        ),
        (
            Action.GET_CHANNEL_INFO,
            QQAction.GET_CHANNEL,
            {"guild_id": "guild", "channel_id": "channel"},
            {"channel_id": "channel"},
        ),
        (
            Action.GET_CHANNEL_LIST,
            QQAction.LIST_GUILD_CHANNELS,
            {"guild_id": "guild", "joined_only": True},
            {"guild_id": "guild"},
        ),
        (
            Action.SET_CHANNEL_NAME,
            QQAction.UPDATE_CHANNEL,
            {
                "guild_id": "guild",
                "channel_id": "channel",
                "channel_name": "renamed",
            },
            {"channel_id": "channel", "name": "renamed"},
        ),
    ],
)
async def test_common_actions_translate_onebot_parameters(
    monkeypatch: pytest.MonkeyPatch,
    action: Action,
    qq_action: QQAction,
    params: dict[str, ActionParamInput],
    expected: dict[str, object],
) -> None:
    gateway = support.gateway(support.Pool())
    request = AsyncMock(return_value=QQNoContent())
    monkeypatch.setattr(gateway, "request_qq", request)

    await gateway.connection_for(BotSelf(platform="qq", user_id="app")).action(
        action,
        **params,
    )

    request.assert_awaited_once_with(qq_action, **expected)


async def test_channel_rejects_non_image_media() -> None:
    pool = support.Pool()
    gateway = support.gateway(pool, online=True)

    with pytest.raises(ValueError, match="only support image"):
        await gateway.connection_for(BotSelf(platform="qq", user_id="app")).action(
            Action.SEND_MESSAGE,
            detail_type="channel",
            guild_id="guild",
            channel_id="channel",
            msg_id="incoming-message",
            message=[{"type": "voice", "data": {"file_id": "voice"}}],
        )

    assert not pool.requests


async def test_clean_close_reconnects_and_fatal_close_clears_session() -> None:
    clean = ScriptedWebSocket(StopAsyncIteration())
    fatal_native = AsyncMock()
    fatal_native.recv.side_effect = (
        dumpb({"op": 10, "d": {"heartbeat_interval": 60_000}}).decode(),
        ConnectionClosedError(Close(4014, "fatal"), None),
    )
    fatal = WebsocketsConnection(fatal_native)
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {"url": "wss://qq.example"},
    )
    connect = AsyncMock(return_value=fatal)

    bot = Bot()
    gateway = QQGateway(
        bot,
        app_id="app",
        client_secret=support.CREDENTIAL,
        http_pool=cast(AsyncPoolManager, pool),
        websocket_connector=connect,
    )

    async with timeout(1):
        with pytest.raises(ConnectionError, match="closed normally"):
            await gateway._serve_websocket(clean, "token")

    gateway._session_id = "online-session"
    gateway._seq = 0
    gateway._online = True
    async with timeout(1), bot:
        await gateway._run_gateway()

    assert gateway._session_id is None
    assert not gateway._online
    assert clean.closed.is_set()
    fatal_native.close.assert_awaited_once()


async def test_group_pagination_uses_query_parameters_and_parses_items() -> None:
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {
            "list": [
                {
                    "join_request_id": "request",
                    "member_openid": "member",
                    "username": "Member",
                    "apply_at": "2026-08-17T00:00:00Z",
                    "apply_source": "self_apply",
                }
            ],
            "next_cursor": "next",
        },
        {
            "strategies": [
                {
                    "strategy_id": "strategy",
                    "group_openids": [],
                    "group_ids": ["10****499"],
                    "whitelist_user_count": 0,
                    "is_enable": "on",
                    "expire_at": "2027-08-05T15:30:16+08:00",
                    "created_at": "2026-08-05T15:30:16+08:00",
                    "updated_at": "2026-08-05T15:45:28+08:00",
                }
            ],
            "next_cursor": "",
        },
        {"strategies": []},
    )
    client = support.client(pool)

    requests = await client.request_qq(
        QQAction.LIST_GROUP_JOIN_REQUESTS,
        group_openid="group",
        cursor="join-cursor",
        limit=10,
    )
    strategies = await client.request_qq(
        QQAction.LIST_GROUP_APPROVAL_STRATEGIES,
        cursor="strategy-cursor",
        limit=20,
    )
    empty_strategy_page = await client.request_qq(
        QQAction.LIST_GROUP_APPROVAL_STRATEGIES
    )

    assert isinstance(requests, QQJoinRequestList)
    assert requests.list[0].member_openid == "member"
    assert isinstance(strategies, QQStrategyList)
    assert strategies.strategies[0].group_ids == ["10****499"]
    assert isinstance(empty_strategy_page, QQStrategyList)
    assert [request[:2] for request in pool.requests[1:]] == [
        (
            HTTPMethod.GET,
            "https://qq.example/v2/groups/group/join_request_list?cursor=join-cursor&limit=10",
        ),
        (
            HTTPMethod.GET,
            "https://qq.example/v2/groups/join_approval_strategy?cursor=strategy-cursor&limit=20",
        ),
        (
            HTTPMethod.GET,
            "https://qq.example/v2/groups/join_approval_strategy",
        ),
    ]
    assert [request[2]["json"] for request in pool.requests[1:]] == [None, None, None]


async def test_rest_reports_business_and_token_errors() -> None:
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {
            "code": 0,
            "err_code": 10004,
            "message": "business failure",
            "trace_id": "body-trace",
        },
    )
    client = support.client(pool)

    with pytest.raises(QQAPIError) as business_error:
        await client.request_qq(
            QQAction.SEND_CHANNEL_MESSAGE,
            channel_id="channel",
            content="message",
        )
    assert type(business_error.value) is QQAPIError
    assert (
        business_error.value.status,
        business_error.value.code,
        business_error.value.trace_id,
    ) == (200, 10004, "body-trace")

    token_client = support.client(
        support.Pool({"code": 100007, "message": "appid invalid"})
    )
    with pytest.raises(QQAccessTokenError) as token_error:
        await token_client.access_token()
    assert (token_error.value.status, token_error.value.code) == (200, 100007)


@pytest.mark.parametrize(
    ("code", "retries"),
    [(100001, 1), (10004, 0), (100007, 0), (100016, 0)],
)
async def test_gateway_retries_only_retryable_token_errors(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    retries: int,
) -> None:
    gateway = support.gateway(support.Pool({"code": code, "message": "token error"}))
    pause = AsyncMock(
        side_effect=lambda _: setattr(gateway, "_closing", True),
    )
    monkeypatch.setattr(gateway.bot, "wait_until_running", AsyncMock())
    monkeypatch.setattr(qq_gateway_module, "sleep", pause)

    await gateway._run_gateway()

    assert pause.await_count == retries


async def test_rest_maps_created_accepted_and_empty_successes() -> None:
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        _response(
            201,
            {"code": 304023, "message": "created"},
            headers={"X-Trace-ID": "created-trace"},
        ),
        _response(
            202,
            {"code": 304023, "message": "accepted"},
            headers={"X-Tps-Trace-ID": "accepted-trace"},
        ),
        _response(204),
    )
    client = support.client(pool)

    results = [
        await client.request_qq(
            QQAction.SEND_GROUP_MESSAGE,
            group_openid="group",
            msg_type=0,
            content="proactive",
        )
        for _ in range(2)
    ]
    no_content = await client.request_qq(
        QQAction.SEND_CHANNEL_MESSAGE,
        channel_id="channel",
        content="message",
    )

    assert all(isinstance(result, QQAsyncResult) for result in results)
    assert [
        (result.status, result.err_code, result.trace_id)
        for result in cast(list[QQAsyncResult], results)
    ] == [
        (201, 304023, "created-trace"),
        (202, 304023, "accepted-trace"),
    ]
    assert isinstance(no_content, QQNoContent)


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (401, {"message": "unauthorized"}),
        (200, {"code": 11244, "message": "token expired"}),
    ],
    ids=["http-401", "business-11244"],
)
async def test_rest_refreshes_an_expired_token_at_most_once(
    status: int,
    payload: JsonValue,
) -> None:
    pool = support.Pool(
        {"access_token": "stale", "expires_in": 7200},
        _response(status, payload),
        {"access_token": "fresh", "expires_in": 7200},
        [],
    )
    client = support.client(pool)

    assert isinstance(
        await client.request_qq(QQAction.LIST_BOT_GUILDS),
        QQGuildList,
    )

    assert [
        cast(dict[str, str], pool.requests[index][2]["headers"])["Authorization"]
        for index in (1, 3)
    ] == [
        "QQBot stale",
        "QQBot fresh",
    ]
    client.invalidate_token("stale")
    assert await client.access_token() == "fresh"
    assert len(pool.requests) == 4


async def test_access_token_is_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = support.Pool()
    started = AsyncEvent()
    joined = AsyncEvent()
    release = AsyncEvent()

    async def request_token(*args: object, **kwargs: object) -> AsyncHTTPResponse:
        _ = args, kwargs
        started.set()
        await release.wait()
        return _response(200, {"access_token": "shared", "expires_in": 7200})

    request = AsyncMock(side_effect=request_token)
    monkeypatch.setattr(pool, "request", request)
    client = support.client(pool)

    async def join_request() -> str:
        joined.set()
        return await client.access_token()

    async with timeout(1), TaskGroup() as tasks:
        first = tasks.create_task(client.access_token())
        await started.wait()
        second = tasks.create_task(join_request())
        await joined.wait()
        release.set()

    assert [first.result(), second.result()] == ["shared", "shared"]
    request.assert_awaited_once()


async def test_group_join_request_maps_and_native_approval_is_routed() -> None:
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {},
    )
    gateway = support.gateway(pool)
    dispatch = QQDispatch.model_validate({
        "id": "event",
        "op": 0,
        "s": 1,
        "t": "GROUP_JOIN_REQUEST",
        "d": {
            "group_openid": "group",
            "join_request_id": "request",
            "member_openid": "member",
            "username": "Member",
            "apply_at": "2026-08-17T00:00:00Z",
            "apply_source": "self_apply",
            "verify_info": {
                "method": "verify_message",
                "verify_message": "let me in",
            },
        },
    })
    event = gateway._event_from_dispatch(dispatch)

    assert isinstance(event, GroupRequestEvent)
    assert (
        event.group_id,
        event.user_id,
        event.flag,
        event.sub_type,
        event.comment,
    ) == ("group", "member", "request", "add", "let me in")
    connection = gateway.connection_for(event.self_)
    response = await connection.action(
        QQAction.APPROVE_GROUP_JOIN_REQUEST,
        group_openid=event.group_id,
        member_openid=event.user_id,
        join_request_id=event.flag,
        op="decline",
        reject_reason="declined",
    )

    assert isinstance(response, QQNoContent)
    assert pool.requests[1][:2] == (
        HTTPMethod.POST,
        "https://qq.example/v2/groups/group/approval_join_request/member",
    )
    assert pool.requests[1][2]["json"] == {
        "join_request_id": "request",
        "op": "decline",
        "reject_reason": "declined",
    }

    assert isinstance(dispatch.d, qq_gateway_module.QQGroupJoinRequest)
    invited = QQDispatch.model_validate({
        **dispatch.model_dump(mode="json"),
        "d": {
            **dispatch.d.model_dump(mode="json"),
            "apply_source": "invited",
            "verify_info": {
                "method": "admin_review_qa",
                "review_qa_list": [{"question": "Q", "answer": "A"}],
            },
        },
    })
    invite_event = gateway._event_from_dispatch(invited)
    assert isinstance(invite_event, GroupRequestEvent)
    assert (invite_event.sub_type, invite_event.comment) == ("invite", "Q: A")

    auto_approved = QQDispatch.model_validate({
        **dispatch.model_dump(mode="json"),
        "d": {
            **dispatch.d.model_dump(mode="json"),
            "auto_approved": {"strategy_id": "strategy"},
        },
    })
    notice = gateway._event_from_dispatch(auto_approved)
    assert isinstance(notice, NoticeEvent)
    assert not isinstance(notice, GroupRequestEvent)


async def test_recall_is_qq_specific_and_closed_connections_are_rejected() -> None:
    pool = support.Pool(
        {"access_token": "token", "expires_in": 7200},
        {},
    )
    gateway = support.gateway(pool)
    connection = gateway.connection_for(BotSelf(platform="qq", user_id="app"))

    supported = (await connection.action(Action.GET_SUPPORTED_ACTIONS)).model_dump()
    assert Action.DELETE_MESSAGE.value not in supported
    assert QQAction.RECALL_DM_MESSAGE.value in supported
    with pytest.raises(LookupError, match="does not support"):
        await connection.action(Action.DELETE_MESSAGE, message_id="message")
    response = await connection.action(
        QQAction.RECALL_DM_MESSAGE,
        message_id="message",
        guild_id="guild",
        hidetip=True,
    )

    assert isinstance(response, QQNoContent)
    assert pool.requests[1][:2] == (
        HTTPMethod.DELETE,
        "https://qq.example/dms/guild/messages/message?hidetip=true",
    )
    assert pool.requests[1][2]["json"] is None

    await gateway.close()
    with pytest.raises(RuntimeError, match="closed"):
        await connection.action(QQAction.GET_GATEWAY)
    assert len(pool.requests) == 2


async def test_websocket_hello_timeout_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = support.gateway(support.Pool())
    stalled = ScriptedWebSocket()
    monkeypatch.setattr(qq_gateway_module, "_HELLO_TIMEOUT", 0)

    async with timeout(1):
        with pytest.raises(TimeoutError):
            await gateway._serve_websocket(stalled, "token")
    assert stalled.closed.is_set()


@pytest.mark.parametrize(
    ("data", "reset_session"),
    [(True, False), (False, True)],
)
async def test_websocket_invalid_session_follows_resume_flag(
    data: bool,
    reset_session: bool,
) -> None:
    websocket = ScriptedWebSocket(
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {"op": 9, "d": data},
    )

    async with timeout(1):
        with pytest.raises(ConnectionError) as caught:
            await support.gateway(support.Pool())._serve_websocket(
                websocket,
                "token",
            )

    assert vars(caught.value)["reset_session"] is reset_session
    assert websocket.closed.is_set()


async def test_websocket_invalid_session_requires_a_boolean() -> None:
    websocket = ScriptedWebSocket(
        {"op": 10, "d": {"heartbeat_interval": 60_000}},
        {"op": 9, "d": 1},
    )

    with pytest.raises(ValidationError, match="valid boolean"):
        await support.gateway(support.Pool())._serve_websocket(
            websocket,
            "token",
        )

    assert websocket.closed.is_set()


@pytest.mark.parametrize(
    ("code", "policy"),
    [
        (4004, (True, False, None)),
        (4006, (False, True, None)),
        (4008, (False, False, 60.0)),
        (4009, (False, False, None)),
        (4900, (False, True, None)),
        (4913, (False, True, None)),
        (4999, (False, True, None)),
    ],
)
async def test_websocket_close_code_recovery_policy(
    code: int,
    policy: tuple[bool, bool, float | None],
) -> None:
    native = AsyncMock()
    closed = ConnectionClosedError(Close(code, "closed"), None)
    native.recv.side_effect = closed

    with pytest.raises(ConnectionError) as caught:
        await support.gateway(support.Pool())._serve_websocket(
            WebsocketsConnection(native),
            "token",
        )

    error = caught.value
    assert (
        getattr(error, "reset_token", False),
        getattr(error, "reset_session", False),
        getattr(error, "delay", None),
    ) == policy
    assert error.__cause__ is not None
    assert error.__cause__.__cause__ is closed
    native.close.assert_awaited_once()


async def test_websocket_rejects_a_missed_heartbeat_ack() -> None:
    websocket = ScriptedWebSocket({"op": 10, "d": {"heartbeat_interval": 1}})

    with pytest.raises(ConnectionError) as caught:
        async with timeout(1):
            await support.gateway(support.Pool())._serve_websocket(
                websocket,
                "token",
            )

    assert str(caught.value.__cause__) == "QQ Gateway heartbeat was not acknowledged"
    assert websocket.sent.qsize() == 2
    assert websocket.closed.is_set()


def test_boundary_models_and_message_conversion_follow_qq_wire_types() -> None:
    credential = "client-secret"
    for model in (
        QQAccessTokenRequest(appId="app", clientSecret=credential),
        qq_gateway_module.QQIdentifyData(
            token=credential,
            intents=0,
            shard=(0, 1),
        ),
        qq_gateway_module.QQResumeData(
            token=credential,
            session_id="session",
            seq=0,
        ),
    ):
        assert credential not in repr(model)

    with pytest.raises(ValidationError):
        qq_gateway_module.QQJoinVerification.model_validate({
            "method": "admin_review_qa",
            "review_qa_list": None,
        })
    with pytest.raises(ValidationError, match="file_data"):
        QQFileUploadFields.model_validate({
            "file_type": 1,
            "file_data": "YQ==",
            "srv_send_msg": False,
        })

    for model, payload in (
        (QQGatewayInfo, {"url": "https://qq.example"}),
        (QQFileUploadFields, {"file_type": 1, "url": "https://"}),
        (
            QQAudioControlRequest,
            {"channel_id": "channel", "status": 0, "audio_url": "https://"},
        ),
        (
            QQMenuLinkItem,
            {"name": "link", "type": "link", "link": "http://qq.example"},
        ),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)

    role_page = QQRoleMemberListParams(
        guild_id="guild",
        role_id="role",
        start_index="next",
    )
    groups = QQStrategyGroups(group_ids=["123456789"])
    assert role_page.start_index == "next"
    assert groups.group_ids == ["123456789"]
    recurring = {
        "task_id": "task",
        "weekdays": list(range(1, 8)),
        "start_time": "00:00",
        "end_time": "23:59",
        "enabled": True,
    }
    assert QQRecurringRestriction.model_validate(recurring).weekdays == list(
        range(1, 8)
    )
    with pytest.raises(ValidationError):
        QQRecurringRestriction.model_validate({**recurring, "weekdays": [1] * 8})
    create_strategy = QQ_ROUTES[
        QQAction.CREATE_GROUP_APPROVAL_STRATEGY
    ].request.model_validate({"group_ids": ["123456789"]})
    update_strategy = QQ_ROUTES[
        QQAction.UPDATE_GROUP_APPROVAL_STRATEGY
    ].request.model_validate({
        "strategy_id": "strategy",
        "group_action": {"op": "add", "group_ids": ["123456789"]},
    })
    assert create_strategy.model_dump(mode="json", exclude_none=True)["group_ids"] == [
        "123456789"
    ]
    assert update_strategy.model_dump(mode="json", exclude_none=True)[
        "group_action"
    ] == {
        "op": "add",
        "group_ids": ["123456789"],
    }
    for invalid_group_id in (123456789, "x", "-1", str(2**64)):
        with pytest.raises(ValidationError):
            QQStrategyGroups.model_validate({"group_ids": [invalid_group_id]})

    for stream in (
        {"input_state": 10, "index": 0},
        {"input_state": 1, "index": 0, "stream_msg_id": "unexpected"},
        {"input_state": 1, "index": 1},
    ):
        with pytest.raises(ValidationError):
            QQStreamMessageRequest.model_validate({
                "user_openid": "user",
                "input_mode": "replace",
                "content_type": "markdown",
                "content_raw": "stream",
                "msg_id": "message",
                "msg_seq": 0,
                **stream,
            })

    with pytest.raises(ValidationError, match="rejection fields"):
        QQ_ROUTES[QQAction.APPROVE_GROUP_JOIN_REQUEST].request.model_validate({
            "group_openid": "group",
            "member_openid": "member",
            "op": "approve",
            "reject_reason": "not allowed for approval",
        })
    with pytest.raises(ValidationError, match="Chinese counts as two"):
        QQMenuLinkItem(
            name="测试测试测试",
            type="link",
            link="https://qq.example",
        )
    QQMenuLinkItem(
        name="éééééééééé",
        type="link",
        link="https://qq.example",
    )

    gateway = support.gateway(support.Pool())
    voice = gateway._event_from_dispatch(
        QQDispatch.model_validate({
            "id": "event",
            "op": 0,
            "s": 1,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": "message",
                "author": {"user_openid": "user"},
                "content": "",
                "timestamp": "2026-08-17T00:00:00Z",
                "message_type": 3,
                "message_scene": {"source": "c2c"},
                "attachments": [
                    {"url": "https://qq.example/voice", "content_type": "voice"}
                ],
            },
        })
    )
    assert isinstance(voice, PrivateMessageEvent)
    assert voice.message[0].type is MsgSegmentType.VOICE
