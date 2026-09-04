from functools import partial
from logging import getLogger
from operator import attrgetter
from re import search

from bot import Cmd, EventRouter, Injected, configured_admin_permission
from klei import KleiClient, RoomData
from lst import LstClient, validate_room_ids

from .settings import Settings

logger = getLogger(__name__)
router = EventRouter()


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


def parse_room_ids(value: str) -> list[str]:
    return validate_room_ids(item.strip() for item in value.split(","))


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


def control_room(
    operation: str,
    cmd: Injected[Cmd],
    lc: Injected[LstClient],
) -> str:
    usage = f"用法：{cmd.raw} 000,020,100"
    command = {
        "存档": "c_save()",
        "回档": None,
        "重启": None,
        "重置": "c_regenerateworld()",
    }[operation]
    room_ids_text = cmd.arg
    if operation == "回档":
        usage += " 2"
        try:
            room_ids_text, snapshots_text = cmd.arg.rsplit(maxsplit=1)
            snapshots = int(snapshots_text)
        except ValueError:
            return usage
        if snapshots < 0:
            return usage
        command = f"c_rollback({snapshots})"
    try:
        room_ids = parse_room_ids(room_ids_text)
    except ValueError:
        return usage

    targets = ",".join(room_ids)
    try:
        if command is None:
            lc.restart_rooms(room_ids)
        else:
            lc.send_console_command(room_ids, command)
    except Exception:
        logger.exception("DST room %s failed: %s", operation, targets)
        return f"{operation}未全部完成：{targets}，请检查房间状态"
    return f"已发送{operation}请求：{targets}"


for operation in ("存档", "回档", "重启", "重置"):
    router.on_cmd(f"房间{operation}", configured_admin_permission)(
        partial(control_room, operation),
    )
