from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from app_event import message_event
from bot import Bot
from bot.testing import RecordingGateway
from hitokoto import HitokotoClient
from klei import (
    KleiClient,
    Platform,
    Region,
    RoomData,
    Season,
    Version,
    VersionType,
)

from lst_bot.general import format_versions, report, router
from lst_bot.settings import Settings


def version(number: int, version_type: VersionType) -> Version:
    return Version(
        number=number,
        type=version_type,
        date=date(2026, 8, number),
        url=f"https://example.test/{number}",
    )


def room_data(**overrides: object) -> RoomData:
    values: dict[str, Any] = {
        "row_id": "1",
        "name": "Room",
        "addr": "127.0.0.1",
        "port": 10999,
        "host": "host",
        "connected": 2,
        "maxconnections": 6,
        "password": False,
        "serverpaused": False,
        "region": Region.AP_EAST,
        "season": Season.AUTUMN,
        "data": "day=12",
        "players": None,
        **overrides,
    }
    return RoomData.model_construct(**values)


@pytest.mark.parametrize(
    ("values", "expected_numbers"),
    [
        (
            [version(1, VersionType.RELEASE), version(2, VersionType.TEST)],
            (1, 2),
        ),
        (
            [
                version(1, VersionType.RELEASE),
                version(3, VersionType.RELEASE),
                version(2, VersionType.TEST),
            ],
            (3, 2),
        ),
    ],
    ids=("one-per-channel", "latest-per-channel"),
)
def test_format_versions_selects_latest_per_channel(
    values: list[Version],
    expected_numbers: tuple[int, int],
) -> None:
    text = format_versions(values)

    assert [f"发布版本：{number}" in text for number in expected_numbers] == [
        True,
        True,
    ]
    assert text.index("发布类型：Release") < text.index("发布类型：Test")


async def test_hitokoto_command_dispatches_with_injected_client() -> None:
    bot = Bot()
    client = Mock(spec_set=HitokotoClient)
    client.get_hitokoto = AsyncMock(return_value="今日一言")
    bot.container.add_instance(client, provides=HitokotoClient)
    bot.add_router(router)
    gateway = RecordingGateway(bot)
    bot.add_gateway(gateway)

    async with bot:
        results = await bot.dispatch(gateway.connection, message_event("/一言"))

    client.get_hitokoto.assert_awaited_once_with(use_cache=True)
    assert results[0].values == ["今日一言"]
    action = gateway.actions[0].root.model_dump(mode="json", by_alias=True)
    assert action["action"] == "send_message"
    assert action["params"]["message"][0]["data"]["text"] == "今日一言"


async def test_versions_command_dispatches_with_injected_client() -> None:
    bot = Bot()
    client = Mock(spec_set=KleiClient)
    client.get_latest_versions = AsyncMock(
        return_value=[version(7, VersionType.RELEASE), version(8, VersionType.TEST)],
    )
    bot.container.add_instance(client, provides=KleiClient)
    bot.add_router(router)
    gateway = RecordingGateway(bot)
    bot.add_gateway(gateway)

    async with bot:
        results = await bot.dispatch(gateway.connection, message_event("/最新版本"))

    client.get_latest_versions.assert_awaited_once_with()
    expected = format_versions(client.get_latest_versions.return_value)
    assert results[0].values == [expected]
    assert gateway.actions[0].root.action == "send_message"


async def test_search_player_command_filters_active_rooms() -> None:
    bot = Bot()
    client = Mock(spec_set=KleiClient)
    client.get_lobby_data = AsyncMock(return_value=[room_data(row_id="1")])
    client.get_room_data = AsyncMock(
        return_value=[
            room_data(row_id="1", name="Alpha", players="Wilson, Wendy"),
            room_data(row_id="2", name="Beta", players="WX-78"),
        ],
    )
    bot.container.add_instance(client, provides=KleiClient)
    bot.add_router(router)
    gateway = RecordingGateway(bot)
    bot.add_gateway(gateway)

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            message_event("/搜索玩家 Wendy"),
        )

    client.get_lobby_data.assert_awaited_once_with(platforms=(Platform.Steam,))
    client.get_room_data.assert_awaited_once()
    result = results[0].values[0]
    assert isinstance(result, str)
    assert result.startswith("🔍️ 1/2\n")
    assert "Alpha" in result
    assert "Beta" not in result
    assert gateway.actions[0].root.action == "send_message"


async def test_report_uses_injected_settings_for_room_and_message_targets() -> None:
    bot = Bot()
    hitokoto = Mock(spec_set=HitokotoClient)
    hitokoto.get_hitokoto = AsyncMock(return_value="今日一言")
    klei = Mock(spec_set=KleiClient)
    klei.get_lobby_data = AsyncMock(
        return_value=[
            room_data(row_id="1", host="wanted", connected=1),
            room_data(row_id="2", host="other", connected=1),
        ],
    )
    klei.get_room_data = AsyncMock(return_value=[room_data(row_id="1")])
    gateway = RecordingGateway(bot)
    settings = Settings(
        _env_file=None,
        onebot_self_id="bot",
        klei_host_id="wanted",
        report_group_id="group",
    )

    await report(hitokoto, klei, gateway.connection, settings)

    room_data_call = klei.get_room_data.await_args
    assert room_data_call is not None
    room_refs = list(room_data_call.args[0])
    assert room_refs == [("1", room_data().region)]
    action = gateway.actions[0].root.model_dump(mode="json", by_alias=True)
    assert action["params"]["group_id"] == "group"
    assert action["params"]["message"][0]["data"]["text"].startswith("今日一言")
