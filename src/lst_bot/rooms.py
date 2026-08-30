from logging import getLogger
from operator import attrgetter
from re import search

from bot import Bot, Cmd, EventRouter, Injected, UserEvent
from klei import KleiClient, RoomData
from lst import LstClient

from .settings import Settings

logger = getLogger(__name__)
router = EventRouter()


def _configured_admin(
    event: Injected[UserEvent],
    bot: Injected[Bot],
) -> bool:
    return event.user_id in bot.admin_ids.get(event.self_.platform, ())


def format_lobby_data(data: RoomData) -> str:
    mark = ("🟧" if data.serverpaused else "🟢") if data.connected > 0 else "🟨"

    if data.password:
        mark += "🔒"

    player_count = f"{data.connected}/{data.maxconnections}"
    season = {
        "autumn": "秋",
        "winter": "冬",
        "spring": "春",
        "summer": "夏",
    }.get(data.season, "")
    day = ""
    if data.data and (match := search(r"day=(\d+)", data.data)):
        day = match[1]

    return f"{mark:3}{player_count:7}{season + day:7}{data.name}"


def parse_room_ids(value: str) -> list[int]:
    try:
        room_ids = list(dict.fromkeys(map(int, value.split(","))))
    except ValueError:
        room_ids = []
    if not room_ids or min(room_ids) <= 0:
        msg = "room ids must be comma-separated positive integers"
        raise ValueError(msg)
    return room_ids


async def get_host_rooms(
    kc: KleiClient,
    host_id: str,
    *,
    connected_only: bool = False,
) -> list[RoomData]:
    lobbies = await kc.get_lobby_data()
    rooms = (
        (data.row_id, data.region)
        for data in lobbies
        if data.host == host_id and (not connected_only or data.connected > 0)
    )
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


@router.on_cmd("房间存档", _configured_admin)
def save_room(cmd: Injected[Cmd], lc: Injected[LstClient]) -> str:
    try:
        room_ids = parse_room_ids(cmd.arg)
    except ValueError:
        return f"用法：{cmd.raw} 1,2,4"

    lc.send_console_command(room_ids, "c_save()")
    return f"已存档 {room_ids}"


@router.on_cmd("房间回档", _configured_admin)
def rollback_room(cmd: Injected[Cmd], lc: Injected[LstClient]) -> str:
    usage = f"用法：{cmd.raw} 1,2,4 2"
    try:
        room_ids_text, snapshots_text = cmd.arg.split()
        room_ids = parse_room_ids(room_ids_text)
        snapshots = int(snapshots_text)
    except ValueError:
        return usage
    if snapshots < 0:
        return usage

    lc.send_console_command(room_ids, f"c_rollback({snapshots})")
    return f"已回档 {snapshots} 个存档点 {room_ids}"


@router.on_cmd("房间重启", _configured_admin)
def restart_room(cmd: Injected[Cmd], lc: Injected[LstClient]) -> str:
    try:
        room_ids = parse_room_ids(cmd.arg)
    except ValueError:
        return f"用法：{cmd.raw} 1,2,4"

    try:
        lc.restart_rooms(room_ids)
    except Exception:
        logger.exception(
            "restart DST rooms failed: %s",
            ",".join(map(str, room_ids)),
        )
        return f"重启失败：{room_ids}"
    return f"已重启 {room_ids}"


@router.on_cmd("房间重置", _configured_admin)
def regenerate_room(cmd: Injected[Cmd], lc: Injected[LstClient]) -> str:
    try:
        room_ids = parse_room_ids(cmd.arg)
    except ValueError:
        return f"用法：{cmd.raw} 1,2,4"

    lc.send_console_command(room_ids, "c_regenerateworld()")
    return f"已重置 {room_ids}"
