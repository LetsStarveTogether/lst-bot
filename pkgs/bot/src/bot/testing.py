from __future__ import annotations

from asyncio import Event, Queue
from typing import override

import orjson
from pydantic import JsonValue

from bot import (
    ActionCall,
    ActionResponse,
    Bot,
    BotSelf,
    Connection,
    Gateway,
)
from bot.protocol.actions import ActionParamModel


class RecordingGateway(Gateway):
    def __init__(
        self,
        bot: Bot,
        *,
        responses: dict[str, ActionResponse] | None = None,
    ) -> None:
        super().__init__(bot)
        self.actions: list[ActionCall] = []
        self.responses = responses or {}

    @property
    def connection(self) -> Connection:
        return self.connection_for(BotSelf(platform="test", user_id="bot"))

    @override
    async def request_action(
        self,
        connection: Connection,
        action: str,
        params: ActionParamModel,
    ) -> ActionResponse:
        _ = connection
        self.actions.append(
            ActionCall.model_validate({"action": action, "params": params})
        )
        return self.responses.get(action, ActionResponse.ok({"status": "ok"}))


class ScriptedWebSocket:
    def __init__(self, *incoming: JsonValue | BaseException) -> None:
        self.incoming: Queue[str | BaseException] = Queue()
        self.sent: Queue[str] = Queue()
        self.receiving = Event()
        self.send_allowed = Event()
        self.send_allowed.set()
        self.closed = Event()
        for item in incoming:
            self.feed(item)

    def feed(self, item: JsonValue | BaseException) -> None:
        value = item if isinstance(item, BaseException) else orjson.dumps(item).decode()
        self.incoming.put_nowait(value)

    def finish(self) -> None:
        self.feed(StopAsyncIteration())

    async def receive_text(self) -> str:
        self.receiving.set()
        value = await self.incoming.get()
        self.receiving.clear()
        if isinstance(value, BaseException):
            raise value
        return value

    async def send_text(self, payload: str) -> None:
        await self.send_allowed.wait()
        await self.sent.put(payload)

    async def close(self) -> None:
        self.closed.set()


__all__ = ["RecordingGateway", "ScriptedWebSocket"]
