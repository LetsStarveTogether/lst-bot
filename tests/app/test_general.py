from datetime import date
from unittest.mock import Mock

from bot import Bot, Cmd
from bot.testing import RecordingGateway
from hitokoto import HitokotoClient
from klei import (
    KleiClient,
    Platform,
    Version,
    VersionType,
)
from support import room_data

from lst_bot.general import hitokoto, report, search_player, versions
from lst_bot.settings import Settings


def version(number: int, version_type: VersionType) -> Version:
    return Version(
        number=number,
        type=version_type,
        date=date(2026, 8, number),
    )


async def test_hitokoto_uses_cached_client() -> None:
    client = Mock(spec_set=HitokotoClient)
    client.get_hitokoto.return_value = "今日一言"

    assert await hitokoto(client) == "今日一言"
    client.get_hitokoto.assert_awaited_once_with(use_cache=True)


async def test_versions_selects_latest_available_channels() -> None:
    client = Mock(spec_set=KleiClient)
    client.get_latest_versions.return_value = [
        version(7, VersionType.RELEASE),
        version(9, VersionType.RELEASE),
        version(8, VersionType.TEST),
    ]

    assert await versions(client) == (
        "发布版本：9\n发布类型：Release\n发布日期：2026-08-09\n\n\n"
        "发布版本：8\n发布类型：Test\n发布日期：2026-08-08"
    )
    client.get_latest_versions.return_value = [version(7, VersionType.RELEASE)]
    assert await versions(client) == (
        "发布版本：7\n发布类型：Release\n发布日期：2026-08-07"
    )
    client.get_latest_versions.return_value = []
    assert await versions(client) == "❌ 未搜索到版本信息"


async def test_search_player_filters_active_rooms() -> None:
    client = Mock(spec_set=KleiClient)
    client.get_lobby_data.return_value = [
        room_data(row_id="1"),
        room_data(row_id="2", connected=0),
        room_data(row_id="3"),
    ]
    client.get_room_data.return_value = [
        room_data(row_id="1", name="Alpha", players="Wilson, Wendy"),
        room_data(row_id="3", name="Beta", players="WX-78"),
    ]
    reply = await search_player(
        Cmd(name="搜索玩家", raw="/搜索玩家", arg="Wendy"),
        client,
    )

    client.get_lobby_data.assert_awaited_once_with(platforms=(Platform.Steam,))
    room_data_call = client.get_room_data.await_args
    assert room_data_call is not None
    assert list(room_data_call.args[0]) == [
        ("1", room_data().region),
        ("3", room_data().region),
    ]
    assert reply.startswith("🔍️ 1/2\n")
    assert "Alpha" in reply
    assert "Beta" not in reply


async def test_report_uses_injected_settings_for_room_and_message_targets() -> None:
    bot = Bot()
    hitokoto = Mock(spec_set=HitokotoClient)
    hitokoto.get_hitokoto.return_value = "今日一言"
    klei = Mock(spec_set=KleiClient)
    klei.get_lobby_data.return_value = [
        room_data(row_id="1", host="wanted", connected=1),
        room_data(row_id="2", host="other", connected=1),
        room_data(row_id="3", host="wanted", connected=0),
    ]
    klei.get_room_data.return_value = [room_data(row_id="1")]
    gateway = RecordingGateway(bot)
    settings = Settings(_env_file=None, klei_host_id="wanted", report_group_id="group")

    await report(hitokoto, klei, gateway.connection, settings)

    room_data_call = klei.get_room_data.await_args
    assert room_data_call is not None
    room_refs = list(room_data_call.args[0])
    assert room_refs == [("1", room_data().region)]
    action = gateway.actions[0].model_dump(mode="json")
    assert action["params"]["group_id"] == "group"
    assert action["params"]["message"][0]["data"]["text"].startswith("今日一言")
