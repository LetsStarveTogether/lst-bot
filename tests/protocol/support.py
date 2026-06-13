from __future__ import annotations

from pydantic import JsonValue


def bot_self() -> dict[str, str]:
    return {"platform": "qq", "user_id": "10000"}


def private_msg_payload() -> dict[str, JsonValue]:
    return {
        "id": "evt-private",
        "self": bot_self(),
        "time": 1632847927.599013,
        "type": "message",
        "detail_type": "private",
        "sub_type": "",
        "message_id": "6283",
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "alt_message": "hello",
        "user_id": "42",
    }


def action_request_payload() -> dict[str, JsonValue]:
    return {
        "action": "send_message",
        "params": {
            "detail_type": "private",
            "user_id": "42",
            "message": "hello",
        },
        "echo": "echo-1",
        "self": bot_self(),
    }
