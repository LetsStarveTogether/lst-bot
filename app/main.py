from __future__ import annotations

import re
from operator import attrgetter

import uvloop
from agent import DstQuestionAgent
from logbook import Logger
from settings import settings
from urllib3_future import AsyncProxyManager

from lst_bot import (
    Bot,
    Cmd,
    Connection,
    Event,
    Injected,
    Permission,
    Reply,
    ReturnAction,
)
from lst_bot.clients.hitokoto import HitokotoClient
from lst_bot.clients.klei import (
    KleiClient,
    LobbyData,
    Platform,
    RoomData,
    Season,
    VersionType,
)
from lst_bot.clients.lst import LstClient
from lst_bot.gateways.onebot11 import ForwardWebSocket, OneBot11Gateway, WebSocketAction

DAY_PATTERN = re.compile(r"day=(\d+)")

logger = Logger(__name__)


bot = Bot(admin_ids=settings.bot_admin)
gateway = OneBot11Gateway(
    bot,
    ingress=[ForwardWebSocket(settings.onebot_ws_url)],
    action=WebSocketAction(),
    access_token=settings.onebot_access_token,
)
bot.add_gateway(gateway)
bot.container.add_instance(LstClient())
bot.container.add_instance(
    HitokotoClient(
        http_pool=AsyncProxyManager(settings.http_proxy),
    )
)
bot.container.add_instance(
    KleiClient(
        access_token=settings.klei_access_token,
        http_pool=AsyncProxyManager(settings.http_proxy),
    ),
)
bot.container.add_instance(
    DstQuestionAgent(
        gemini_api_key=settings.gemini_api_key,
        dosu_mcp_endpoint=settings.dosu_mcp_endpoint,
        dosu_api_key=settings.dosu_api_key,
        http_proxy=settings.http_proxy,
    ),
)


def format_lobby_data(data: LobbyData, *, verbose: bool = False) -> str:
    mark = ("🟧" if data.serverpaused else "🟢") if data.connected > 0 else "🟨"

    if data.password:
        mark += "🔒"

    player_count = f"{data.connected}/{data.maxconnections}"
    season = {
        Season.AUTUMN: "秋",
        Season.WINTER: "冬",
        Season.SPRING: "春",
        Season.SUMMER: "夏",
    }.get(data.season, "")
    day = ""
    if (
        isinstance(data, RoomData)
        and data.data
        and (match := DAY_PATTERN.search(data.data))
    ):
        day = match[1]

    value = f"{mark:3}{player_count:7}{season + day:7}{data.name}"
    if verbose:
        value += f" {data.addr}:{data.port}"
    return value


def parse_room_ids(value: str) -> list[int]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        msg = "room ids are required"
        raise ValueError(msg)

    room_ids: list[int] = []
    for item in items:
        if "-" not in item:
            room_ids.append(int(item))
            continue

        start_value, end_value = item.split("-", maxsplit=1)
        start = int(start_value)
        end = int(end_value)
        if start > end:
            msg = f"invalid room id range: {item}"
            raise ValueError(msg)
        room_ids.extend(range(start, end + 1))

    return room_ids


async def get_host_rooms(
    kc: KleiClient,
    *,
    connected_only: bool = False,
) -> list[RoomData]:
    lobbies = await kc.get_lobby_data(platforms=(Platform.Steam,))
    rooms = (
        (data.row_id, data.region)
        for data in lobbies
        if data.host == settings.klei_host_id
        and (not connected_only or data.connected > 0)
    )
    return await kc.get_room_data(rooms)


async def get_active_rooms(kc: KleiClient) -> list[RoomData]:
    lobbies = await kc.get_lobby_data(platforms=(Platform.Steam,))
    rooms = ((data.row_id, data.region) for data in lobbies if data.connected > 0)
    return await kc.get_room_data(rooms)


@bot.on_event()
def log_event(event: Injected[Event]) -> None:
    if __debug__:
        logger.trace(
            "receive event : {event}",
            event=event,
        )


@bot.on_cmd("一言")
async def hitokoto(hc: Injected[HitokotoClient]) -> str:
    return str(await hc.get_hitokoto(use_cache=True))


@bot.on_cmd("问")
async def ask_dst_question(
    cmd: Injected[Cmd],
    agent: Injected[DstQuestionAgent],
    r: Injected[Reply],
) -> ReturnAction:
    question = cmd.arg.strip()
    if not question:
        return r("用法：{cmd.raw} 《饥荒联机版》相关问题")

    return r(await agent.answer(question))


@bot.on_cmd("最新版本")
async def versions(kc: Injected[KleiClient]) -> str:
    versions = await kc.get_latest_versions()
    messages = []
    for version_type in VersionType:
        version = max(
            (item for item in versions if item.type is version_type),
            key=attrgetter("number"),
        )
        messages.append(
            f"发布版本：{version.number}\n"
            f"发布类型：{version.type}\n"
            f"发布日期：{version.date}",
        )
    return "\n\n\n".join(messages)


@bot.on_cmd("搜索玩家")
async def search_player(cmd: Injected[Cmd], kc: Injected[KleiClient]) -> str:
    target_name = cmd.arg.strip()
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


@bot.on_cmd("房间列表")
async def rooms(kc: Injected[KleiClient]) -> str:
    room_data_list = await get_host_rooms(kc)
    if not room_data_list:
        return "❌ 未搜索到相关大厅信息"

    room_data_list.sort(key=attrgetter("name"))
    return "\n".join(format_lobby_data(room) for room in room_data_list)


@bot.on_cmd("房间存档", permission=Permission.admin())
def save_room(cmd: Injected[Cmd], lc: Injected[LstClient]) -> str:
    try:
        room_ids = parse_room_ids(cmd.arg)
    except ValueError:
        return f"用法：{cmd.raw} 1,2,4-6"

    lc.save_rooms(room_ids)
    return f"已存档 {room_ids}"


@bot.on_cmd("房间回档", permission=Permission.admin())
def rollback_room(cmd: Injected[Cmd], lc: Injected[LstClient]) -> str:
    try:
        room_ids_text, days_text = cmd.arg.split()
        room_ids = parse_room_ids(room_ids_text)
        days = int(days_text)
    except ValueError:
        return f"用法：{cmd.raw} 1,2,4-6"

    lc.rollback_rooms(room_ids, days)
    return f"已回档 {days} 天 {room_ids}"


@bot.on_cmd("房间重启", permission=Permission.admin())
def restart_room(cmd: Injected[Cmd], lc: Injected[LstClient]) -> str:
    try:
        room_ids = parse_room_ids(cmd.arg)
    except ValueError:
        return f"用法：{cmd.raw} 1,2,4-6"

    try:
        lc.restart_rooms(room_ids)
    except Exception as exc:
        logger.exception(
            "restart DST rooms failed: {rooms} ({error})",
            rooms=",".join(str(item) for item in room_ids),
            error=f"{type(exc).__name__}: {exc}",
        )
        return f"重启失败：{exc} {room_ids}"
    return f"已重启 {room_ids}"


@bot.on_cmd("房间重置", permission=Permission.admin())
def regenerate_room(cmd: Injected[Cmd], lc: Injected[LstClient]) -> str:
    try:
        room_ids = parse_room_ids(cmd.arg)
    except ValueError:
        return f"用法：{cmd.raw} 1,2,4-6"

    lc.regenerate_rooms(room_ids)
    return f"已重置 {room_ids}"


@bot.on_cron("0 0,8-23 * * *")
async def report(
    hc: Injected[HitokotoClient], kc: Injected[KleiClient], conn: Injected[Connection]
) -> None:
    hitokoto = str(await hc.get_hitokoto(use_cache=True))
    rooms = await get_host_rooms(kc, connected_only=True)
    rooms.sort(key=attrgetter("connected"), reverse=True)

    lobby_text = "\n".join(format_lobby_data(room) for room in rooms)
    message = "\n\n".join(filter(None, (hitokoto, lobby_text)))
    await conn.send_msg(message, group_id=settings.report_group_id)


if __name__ == "__main__":
    uvloop.run(bot.run())
