from asyncio import Event as AsyncEvent
from typing import override

from bot import Bot, BotSelf
from pydantic import JsonValue

SELF = BotSelf(platform="qq", user_id="10000")


class ObservableReadinessBot(Bot):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = AsyncEvent()

    @override
    async def wait_until_running(self) -> None:
        self.waiting.set()
        await super().wait_until_running()


def private_message_payload(message: str = "hello") -> dict[str, JsonValue]:
    return {
        "id": "evt-private",
        "self": SELF.model_dump(mode="json"),
        "time": 1.0,
        "type": "message",
        "detail_type": "private",
        "sub_type": "",
        "message_id": "message-1",
        "message": [{"type": "text", "data": {"text": message}}],
        "alt_message": message,
        "user_id": "42",
    }


def connect_payload(*, impl: str = "test") -> dict[str, JsonValue]:
    return {
        "id": "evt-connect",
        "time": 1.0,
        "type": "meta",
        "detail_type": "connect",
        "sub_type": "",
        "version": {
            "impl": impl,
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
