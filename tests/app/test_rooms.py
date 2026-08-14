from __future__ import annotations

from unittest.mock import Mock

import pytest
from bot import Bot
from bot.testing import private_message_event, recording_gateway
from klei import KleiClient, Platform, Region, RoomData
from lst import LstClient
from support import room_data

from lst_bot.rooms import format_lobby_data, parse_room_ids, router
from lst_bot.settings import Settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", [1]),
        ("1, 3,5", [1, 3, 5]),
        ("1,3-5,8", [1, 3, 4, 5, 8]),
    ],
    ids=("single", "list", "list-and-range"),
)
def test_parse_room_ids(value: str, expected: list[int]) -> None:
    assert parse_room_ids(value) == expected


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("", "room ids are required"),
        ("  , ", "room ids are required"),
        ("3-1", "invalid room id range: 3-1"),
        ("invalid", "invalid literal for int"),
        ("1-2-3", "invalid literal for int"),
    ],
    ids=("empty", "separators-only", "descending", "non-number", "extra-dash"),
)
def test_parse_room_ids_rejects_invalid_input(value: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        parse_room_ids(value)


@pytest.mark.parametrize(
    ("data", "verbose", "expected"),
    [
        (room_data(), False, "🟢  2/6    秋12    Room"),
        (
            room_data(serverpaused=True, password=True),
            False,
            "🟧🔒 2/6    秋12    Room",
        ),
        (room_data(connected=0), False, "🟨  0/6    秋12    Room"),
        (room_data(), True, "🟢  2/6    秋12    Room 127.0.0.1:10999"),
    ],
    ids=("active", "paused-password", "empty", "verbose"),
)
def test_format_lobby_data(data: RoomData, verbose: bool, expected: str) -> None:
    assert format_lobby_data(data, verbose=verbose) == expected


async def test_rooms_command_uses_settings_and_klei_dependency() -> None:
    bot = Bot()
    settings = Settings(
        _env_file=None,
        onebot_self_id="bot",
        klei_host_id="wanted",
    )
    client = Mock(spec_set=KleiClient)
    client.get_lobby_data.return_value = [
        room_data(row_id="1", host="wanted"),
        room_data(row_id="2", host="other"),
    ]
    client.get_room_data.return_value = [room_data(name="Alpha")]
    bot.container.add_instance(settings, provides=Settings)
    bot.container.add_instance(client, provides=KleiClient)
    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            private_message_event("/房间列表"),
        )

    client.get_lobby_data.assert_awaited_once_with(platforms=(Platform.Steam,))
    room_data_call = client.get_room_data.await_args
    assert room_data_call is not None
    room_refs = list(room_data_call.args[0])
    assert room_refs == [("1", Region.AP_EAST)]
    result = results[0].values[0]
    assert isinstance(result, str)
    assert "Alpha" in result


@pytest.mark.parametrize(
    ("command", "method_name", "expected_args", "expected_reply"),
    [
        ("/房间存档 1,3-4", "save_rooms", ([1, 3, 4],), "已存档 [1, 3, 4]"),
        (
            "/房间回档 1,3-4 2",
            "rollback_rooms",
            ([1, 3, 4], 2),
            "已回档 2 天 [1, 3, 4]",
        ),
        ("/房间重启 1,3-4", "restart_rooms", ([1, 3, 4],), "已重启 [1, 3, 4]"),
        ("/房间重置 1,3-4", "regenerate_rooms", ([1, 3, 4],), "已重置 [1, 3, 4]"),
    ],
    ids=("save", "rollback", "restart", "regenerate"),
)
async def test_admin_room_commands_dispatch_to_lst(
    command: str,
    method_name: str,
    expected_args: tuple[object, ...],
    expected_reply: str,
) -> None:
    bot = Bot(admin_ids={"admin"})
    client = Mock(spec_set=LstClient)
    bot.container.add_instance(client, provides=LstClient)
    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            private_message_event(command, user_id="admin"),
        )

    method = getattr(client, method_name)
    method.assert_called_once_with(*expected_args)
    assert results[0].values == [expected_reply]


async def test_room_admin_command_rejects_non_admin() -> None:
    bot = Bot()
    client = Mock(spec_set=LstClient)
    bot.container.add_instance(client, provides=LstClient)
    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            private_message_event("/房间存档 1", user_id="member"),
        )

    assert results == []
    client.save_rooms.assert_not_called()
    assert gateway.actions == []
