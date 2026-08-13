from __future__ import annotations

import re
from operator import attrgetter

from bot import Cmd, EventRouter, Injected, Permission
from klei import KleiClient, LobbyData, Platform, RoomData, Season
from logbook import Logger
from lst import LstClient

from .settings import Settings

DAY_PATTERN = re.compile(r"day=(\d+)")

logger = Logger(__name__)
router = EventRouter()


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
    host_id: str,
    *,
    connected_only: bool = False,
) -> list[RoomData]:
    lobbies = await kc.get_lobby_data(platforms=(Platform.Steam,))
    rooms = (
        (data.row_id, data.region)
        for data in lobbies
        if data.host == host_id and (not connected_only or data.connected > 0)
    )
    return await kc.get_room_data(rooms)


async def get_active_rooms(kc: KleiClient) -> list[RoomData]:
    lobbies = await kc.get_lobby_data(platforms=(Platform.Steam,))
    rooms = ((data.row_id, data.region) for data in lobbies if data.connected > 0)
    return await kc.get_room_data(rooms)


@router.on_cmd("房间列表")
async def rooms(
    kc: Injected[KleiClient],
    settings: Injected[Settings],
) -> str:
    room_data_list = await get_host_rooms(kc, settings.klei_host_id)
    if not room_data_list:
        return "❌ 未搜索到相关大厅信息"

    room_data_list.sort(key=attrgetter("name"))
    return "\n".join(format_lobby_data(room) for room in room_data_list)


@router.on_cmd("房间存档", permission=Permission.admin())
def save_room(cmd: Injected[Cmd], lc: Injected[LstClient]) -> str:
    try:
        room_ids = parse_room_ids(cmd.arg)
    except ValueError:
        return f"用法：{cmd.raw} 1,2,4-6"

    lc.save_rooms(room_ids)
    return f"已存档 {room_ids}"


@router.on_cmd("房间回档", permission=Permission.admin())
def rollback_room(cmd: Injected[Cmd], lc: Injected[LstClient]) -> str:
    try:
        room_ids_text, days_text = cmd.arg.split()
        room_ids = parse_room_ids(room_ids_text)
        days = int(days_text)
    except ValueError:
        return f"用法：{cmd.raw} 1,2,4-6"

    lc.rollback_rooms(room_ids, days)
    return f"已回档 {days} 天 {room_ids}"


@router.on_cmd("房间重启", permission=Permission.admin())
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


@router.on_cmd("房间重置", permission=Permission.admin())
def regenerate_room(cmd: Injected[Cmd], lc: Injected[LstClient]) -> str:
    try:
        room_ids = parse_room_ids(cmd.arg)
    except ValueError:
        return f"用法：{cmd.raw} 1,2,4-6"

    lc.regenerate_rooms(room_ids)
    return f"已重置 {room_ids}"


__all__ = [
    "format_lobby_data",
    "get_active_rooms",
    "get_host_rooms",
    "parse_room_ids",
    "router",
]
