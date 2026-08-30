from bot import BotSelf
from pydantic import JsonValue

SELF = BotSelf(platform="qq", user_id="10000")


def private_message_payload() -> dict[str, JsonValue]:
    return {
        "id": "evt-private",
        "self": SELF.model_dump(mode="json"),
        "time": 1.0,
        "type": "message",
        "detail_type": "private",
        "sub_type": "",
        "message_id": "message-1",
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "alt_message": "hello",
        "user_id": "42",
    }


def connect_payload() -> dict[str, JsonValue]:
    return {
        "id": "evt-connect",
        "time": 1.0,
        "type": "meta",
        "detail_type": "connect",
        "sub_type": "",
        "version": {
            "impl": "test",
            "version": "1.0.0",
            "onebot_version": "12",
        },
    }


def status_payload(*selfs: BotSelf) -> dict[str, JsonValue]:
    return {
        "id": "evt-status",
        "time": 1.0,
        "type": "meta",
        "detail_type": "status_update",
        "sub_type": "",
        "status": {
            "good": True,
            "bots": [
                {"self": self_.model_dump(mode="json"), "online": True}
                for self_ in selfs
            ],
        },
    }
