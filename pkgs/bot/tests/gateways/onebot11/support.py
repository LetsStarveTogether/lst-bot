from bot import Retcode
from pydantic import JsonValue


def private_msg_payload(message: JsonValue = "hello") -> dict[str, JsonValue]:
    return {
        "time": 1632847927,
        "self_id": 10000,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 12,
        "user_id": 42,
        "message": message,
        "raw_message": str(message) if isinstance(message, str) else "",
        "font": 0,
        "sender": {
            "user_id": 42,
            "nickname": "tester",
            "sex": "unknown",
            "age": 18,
        },
    }


def friend_request_payload() -> dict[str, JsonValue]:
    return {
        "time": 1632847927,
        "self_id": 10000,
        "post_type": "request",
        "request_type": "friend",
        "sub_type": "",
        "user_id": 42,
        "comment": "hello",
        "flag": "friend-flag",
    }


def group_request_payload() -> dict[str, JsonValue]:
    return {
        **friend_request_payload(),
        "request_type": "group",
        "sub_type": "add",
        "group_id": 20000,
        "comment": "join",
        "flag": "group-flag",
    }


def action_response_payload(
    data: JsonValue,
    *,
    echo: str | None = None,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "status": "ok",
        "retcode": Retcode.OK,
        "data": data,
    }
    if echo is not None:
        payload["echo"] = echo
    return payload
