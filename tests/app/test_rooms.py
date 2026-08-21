from collections.abc import Callable
from unittest.mock import Mock

import pytest
from bot import Bot, Cmd
from bot.testing import private_message_event, recording_gateway
from klei import KleiClient, Platform, RoomData
from lst import LstClient
from support import lobby_data, room_data

from lst_bot.rooms import (
    format_lobby_data,
    parse_room_ids,
    regenerate_room,
    restart_room,
    rollback_room,
    rooms,
    router,
    save_room,
)
from lst_bot.settings import Settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", [1]),
        ("1, 3,5", [1, 3, 5]),
        ("1,3,1,5", [1, 3, 5]),
    ],
    ids=("single", "list", "stable-deduplication"),
)
def test_parse_room_ids(value: str, expected: list[int]) -> None:
    assert parse_room_ids(value) == expected


@pytest.mark.parametrize(
    "value",
    ["  , ", "1-3", "0", "-1", "1,,2"],
    ids=("empty", "range", "zero", "negative", "empty-item"),
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

    client.get_lobby_data.assert_awaited_once_with(platforms=(Platform.Steam,))
    room_data_call = client.get_room_data.await_args
    assert room_data_call is not None
    room_refs = list(room_data_call.args[0])
    assert room_refs == [("1", "ap-east-1")]
    assert "Alpha" in reply


@pytest.mark.parametrize(
    ("handler", "arg", "method_name", "expected_args", "expected_reply"),
    [
        (
            save_room,
            "1,3,4",
            "send_console_command",
            ([1, 3, 4], "c_save()"),
            "已存档 [1, 3, 4]",
        ),
        (
            rollback_room,
            "1,3,4 2",
            "send_console_command",
            ([1, 3, 4], "c_rollback(2)"),
            "已回档 2 天 [1, 3, 4]",
        ),
        (
            restart_room,
            "1,3,4",
            "restart_rooms",
            ([1, 3, 4],),
            "已重启 [1, 3, 4]",
        ),
        (
            regenerate_room,
            "1,3,4",
            "send_console_command",
            ([1, 3, 4], "c_regenerateworld()"),
            "已重置 [1, 3, 4]",
        ),
    ],
    ids=("save", "rollback", "restart", "regenerate"),
)
def test_room_commands_dispatch_to_lst(
    handler: Callable[[Cmd, LstClient], str],
    arg: str,
    method_name: str,
    expected_args: tuple[object, ...],
    expected_reply: str,
) -> None:
    client = Mock(spec_set=LstClient)
    reply = handler(Cmd(raw="", arg=arg), client)

    method = getattr(client, method_name)
    method.assert_called_once_with(*expected_args)
    assert reply == expected_reply


def test_restart_room_hides_internal_error() -> None:
    client = Mock(spec_set=LstClient)
    client.restart_rooms.side_effect = RuntimeError("internal details")
    assert restart_room(Cmd(raw="", arg="1"), client) == "重启失败：[1]"


async def test_room_admin_command_rejects_non_admin() -> None:
    bot = Bot()
    client = Mock(spec_set=LstClient)
    bot.container.add_instance(client, provides=LstClient)
    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        await bot.dispatch(
            gateway.connection,
            private_message_event("/房间存档 1", user_id="member"),
        )

    client.send_console_command.assert_not_called()
    assert gateway.actions == []
