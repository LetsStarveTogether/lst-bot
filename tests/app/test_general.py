from __future__ import annotations

from datetime import date
from unittest.mock import Mock

from bot import Bot
from bot.testing import RecordingGateway, private_message_event, recording_gateway
from hitokoto import HitokotoClient
from klei import (
    KleiClient,
    Platform,
    Version,
    VersionType,
)
from support import room_data

from lst_bot.general import report, router
from lst_bot.settings import Settings


def version(number: int, version_type: VersionType) -> Version:
    return Version(
        number=number,
        type=version_type,
        date=date(2026, 8, number),
    )


async def test_hitokoto_command_dispatches_with_injected_client() -> None:
    bot = Bot()
    client = Mock(spec_set=HitokotoClient)
    client.get_hitokoto.return_value = "今日一言"
    bot.container.add_instance(client, provides=HitokotoClient)
    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        results = await bot.dispatch(gateway.connection, private_message_event("/一言"))

    client.get_hitokoto.assert_awaited_once_with(use_cache=True)
    assert results[0].values == ["今日一言"]


async def test_versions_command_dispatches_with_injected_client() -> None:
    bot = Bot()
    client = Mock(spec_set=KleiClient)
    client.get_latest_versions.return_value = [
        version(7, VersionType.RELEASE),
        version(9, VersionType.RELEASE),
        version(8, VersionType.TEST),
    ]
    bot.container.add_instance(client, provides=KleiClient)
    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            private_message_event("/最新版本"),
        )

    client.get_latest_versions.assert_awaited_once_with()
    assert results[0].values == [
        (
            "发布版本：9\n发布类型：Release\n发布日期：2026-08-09\n\n\n"
            "发布版本：8\n发布类型：Test\n发布日期：2026-08-08"
        ),
    ]


async def test_search_player_command_filters_active_rooms() -> None:
    bot = Bot()
    client = Mock(spec_set=KleiClient)
    client.get_lobby_data.return_value = [room_data(row_id="1")]
    client.get_room_data.return_value = [
        room_data(row_id="1", name="Alpha", players="Wilson, Wendy"),
        room_data(row_id="2", name="Beta", players="WX-78"),
    ]
    bot.container.add_instance(client, provides=KleiClient)
    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            private_message_event("/搜索玩家 Wendy"),
        )

    client.get_lobby_data.assert_awaited_once_with(platforms=(Platform.Steam,))
    client.get_room_data.assert_awaited_once()
    result = results[0].values[0]
    assert isinstance(result, str)
    assert result.startswith("🔍️ 1/2\n")
    assert "Alpha" in result
    assert "Beta" not in result


async def test_report_uses_injected_settings_for_room_and_message_targets() -> None:
    bot = Bot()
    hitokoto = Mock(spec_set=HitokotoClient)
    hitokoto.get_hitokoto.return_value = "今日一言"
    klei = Mock(spec_set=KleiClient)
    klei.get_lobby_data.return_value = [
        room_data(row_id="1", host="wanted", connected=1),
        room_data(row_id="2", host="other", connected=1),
    ]
    klei.get_room_data.return_value = [room_data(row_id="1")]
    gateway = RecordingGateway(bot)
    settings = Settings(_env_file=None, klei_host_id="wanted", report_group_id="group")

    await report(hitokoto, klei, gateway.connection, settings)

    room_data_call = klei.get_room_data.await_args
    assert room_data_call is not None
    room_refs = list(room_data_call.args[0])
    assert room_refs == [("1", room_data().region)]
    action = gateway.actions[0].root.model_dump(mode="json", by_alias=True)
    assert action["params"]["group_id"] == "group"
    assert action["params"]["message"][0]["data"]["text"].startswith("今日一言")
