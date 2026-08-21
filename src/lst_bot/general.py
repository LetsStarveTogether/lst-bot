from operator import attrgetter

from bot import Cmd, Connection, EventRouter, Injected
from hitokoto import HitokotoClient
from klei import KleiClient, VersionType

from .rooms import format_lobby_data, get_active_rooms, get_host_rooms
from .settings import Settings

router = EventRouter()


@router.on_cmd("一言")
async def hitokoto(hc: Injected[HitokotoClient]) -> str:
    return str(await hc.get_hitokoto(use_cache=True))


@router.on_cmd("最新版本")
async def versions(kc: Injected[KleiClient]) -> str:
    versions = await kc.get_latest_versions()
    messages = []
    for version_type in VersionType:
        version = max(
            (item for item in versions if item.type is version_type),
            key=attrgetter("number"),
            default=None,
        )
        if version is None:
            continue
        messages.append(
            f"发布版本：{version.number}\n"
            f"发布类型：{version.type}\n"
            f"发布日期：{version.date}",
        )
    return "\n\n\n".join(messages) or "❌ 未搜索到版本信息"


@router.on_cmd("搜索玩家")
async def search_player(cmd: Injected[Cmd], kc: Injected[KleiClient]) -> str:
    target_name = cmd.arg
    if not target_name:
        return f"用法：{cmd.raw} 玩家名"

    room_data_list = await get_active_rooms(kc)
    results = [
        room for room in room_data_list if room.players and target_name in room.players
    ]
    if not results:
        return f"🟥 {len(results)}/{len(room_data_list)}"

    rooms_text = "\n".join(format_lobby_data(room, verbose=True) for room in results)
    return f"🔍️ {len(results)}/{len(room_data_list)}\n{rooms_text}"


async def report(
    hc: Injected[HitokotoClient],
    kc: Injected[KleiClient],
    conn: Injected[Connection],
    settings: Injected[Settings],
) -> None:
    hitokoto = str(await hc.get_hitokoto(use_cache=True))
    rooms = await get_host_rooms(
        kc,
        settings.klei_host_id,
        connected_only=True,
    )
    rooms.sort(key=attrgetter("connected"), reverse=True)

    lobby_text = "\n".join(format_lobby_data(room) for room in rooms)
    message = "\n\n".join(filter(None, (hitokoto, lobby_text)))
    await conn.send_msg(message, group_id=settings.report_group_id)
