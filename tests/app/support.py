from klei import LobbyData, RoomData


def lobby_data(**overrides: object) -> LobbyData:
    values: dict[str, object] = {
        "row_id": "1",
        "host": "host",
        "connected": 2,
        "region": "ap-east-1",
        **overrides,
    }
    return LobbyData.model_validate(values, by_name=True)


def room_data(**overrides: object) -> RoomData:
    values: dict[str, object] = {
        "name": "Room",
        "connected": 2,
        "maxconnections": 6,
        "password": False,
        "serverpaused": False,
        "season": "autumn",
        "data": "day=12",
        **overrides,
    }
    return RoomData.model_validate(values, by_name=True)
