from typing import Any

from klei import RoomData, Season


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
        "region": "ap-east-1",
        "season": Season.AUTUMN,
        "data": "day=12",
        **overrides,
    }
    return RoomData.model_construct(**values)
