from unittest.mock import Mock, call

import pytest
from bot import Bot, BotSelf, Cmd, GroupMessageEvent
from bot_test_support import private_message_event, recording_gateway
from klei import KleiClient, RoomData
from lst import LstClient
from support import lobby_data, room_data

from lst_bot.rooms import (
    control_room,
    format_lobby_data,
    parse_room_ids,
    rooms,
    router,
)
from lst_bot.settings import Settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", ["1"]),
        ("000, 020,100", ["000", "020", "100"]),
        ("1,3,1,5", ["1", "3", "5"]),
    ],
    ids=("single", "list", "stable-deduplication"),
)
def test_parse_room_ids(value: str, expected: list[str]) -> None:
    assert parse_room_ids(value) == expected


@pytest.mark.parametrize(
    "value",
    ["  , ", "1-3", "../1", "-1", "1,,2"],
    ids=("empty", "range", "path", "negative", "empty-item"),
)
def test_parse_room_ids_rejects_invalid_input(value: str) -> None:
    with pytest.raises(ValueError, match="room id"):
        parse_room_ids(value)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (room_data(), "🟢  2/6    秋12    Room"),
        (
            room_data(serverpaused=True, password=True),
            "🟧🔒 2/6    秋12    Room",
        ),
        (room_data(connected=0), "🟨  0/6    秋12    Room"),
    ],
    ids=("active", "paused-password", "empty"),
)
def test_format_lobby_data(data: RoomData, expected: str) -> None:
    assert format_lobby_data(data) == expected


async def test_rooms_command_uses_settings_and_klei_dependency() -> None:
    settings = Settings(_env_file=None, klei_host_id="wanted")
    client = Mock(spec_set=KleiClient)
    client.get_lobby_data.return_value = [
        lobby_data(row_id="1", host="wanted"),
        lobby_data(row_id="2", host="other"),
    ]
    client.get_room_data.return_value = [room_data(name="Alpha")]
    reply = await rooms(client, settings)

    client.get_lobby_data.assert_awaited_once_with()
    room_data_call = client.get_room_data.await_args
    assert room_data_call is not None
    room_refs = list(room_data_call.args[0])
    assert room_refs == [("1", "ap-east-1")]
    assert "Alpha" in reply


@pytest.mark.parametrize(
    ("operation", "arg", "expected_call"),
    [
        (
            "存档",
            "000,020,100",
            call.send_console_command(["000", "020", "100"], "c_save()"),
        ),
        (
            "回档",
            "000, 020, 100 2",
            call.send_console_command(["000", "020", "100"], "c_rollback(2)"),
        ),
        (
            "重启",
            "000,020,100",
            call.restart_rooms(["000", "020", "100"]),
        ),
        (
            "重置",
            "000,020,100",
            call.send_console_command(["000", "020", "100"], "c_regenerateworld()"),
        ),
    ],
    ids=("save", "rollback", "restart", "regenerate"),
)
def test_room_commands_dispatch_to_lst(
    operation: str,
    arg: str,
    expected_call: object,
) -> None:
    client = Mock(spec_set=LstClient)
    reply = control_room(operation, Cmd(raw=f"/房间{operation}", arg=arg), client)

    assert client.method_calls == [expected_call]
    assert reply == f"已发送{operation}请求：000,020,100"


@pytest.mark.parametrize("operation", ["存档", "回档", "重启", "重置"])
def test_room_commands_report_failure_without_internal_details(operation: str) -> None:
    client = Mock(spec_set=LstClient)
    client.send_console_command.side_effect = OSError("internal details")
    client.restart_rooms.side_effect = RuntimeError("internal details")
    arg = "020 2" if operation == "回档" else "020"
    assert control_room(operation, Cmd(raw="", arg=arg), client) == (
        f"{operation}未全部完成：020，请检查房间状态"
    )


def test_rollback_room_rejects_negative_snapshot_counts() -> None:
    client = Mock(spec_set=LstClient)

    assert control_room("回档", Cmd(raw="/房间回档", arg="020 0"), client) == (
        "已发送回档请求：020"
    )
    client.send_console_command.assert_called_once_with(["020"], "c_rollback(0)")
    client.reset_mock()

    assert control_room("回档", Cmd(raw="/房间回档", arg="020 -1"), client) == (
        "用法：/房间回档 000,020,100 2"
    )
    assert client.method_calls == []


@pytest.mark.parametrize(
    "message",
    [
        "/房间存档 1",
        "/房间回档 1 1",
        "/房间重启 1",
        "/房间重置 1",
    ],
)
async def test_room_admin_commands_require_configured_admin(message: str) -> None:
    client = Mock(spec_set=LstClient)
    bot = Bot(
        admin_ids={"test": {"configured-admin"}, "qq": {"configured-admin"}},
        dependencies={LstClient: client},
    )
    bot.add_router(router)
    gateway = recording_gateway(bot)

    group_admin = private_message_event(
        message,
        user_id="group-admin",
        event_id="group-admin",
    ).model_dump(mode="json")
    group_admin |= {
        "detail_type": "group",
        "group_id": "group",
        "sender": {"user_id": "group-admin", "role": "admin"},
    }
    other_platform = private_message_event(
        message,
        user_id="configured-admin",
        event_id="other-platform",
    ).model_copy(update={"self_": BotSelf(platform="other", user_id="bot")})
    anonymous_admin = GroupMessageEvent.model_validate(
        group_admin
        | {
            "id": "anonymous-admin",
            "self": {"platform": "qq", "user_id": "bot"},
            "user_id": "configured-admin",
            "sub_type": "anonymous",
            "sender": {"user_id": "configured-admin", "role": "owner"},
        }
    )

    async with bot:
        for event in (
            GroupMessageEvent.model_validate(group_admin),
            other_platform,
            anonymous_admin,
        ):
            await bot.dispatch(gateway.connection_for(event.self_), event)
        assert client.method_calls == []
        assert gateway.actions == []

        await bot.dispatch(
            gateway.connection,
            private_message_event(
                message,
                user_id="configured-admin",
                event_id="configured-admin",
            ),
        )

    assert len(client.method_calls) == 1
    assert len(gateway.actions) == 1
