from __future__ import annotations

from asyncio import CancelledError, create_task, sleep, wait_for
from asyncio import Event as AsyncEvent

import pytest
from pydantic import BaseModel

from lst_bot import ActionResponse, Bot, BotSelf, Gateway
from lst_bot.gateways.base import WebSocketActionManager
from lst_bot.protocol.base import Model


class RecordingActionWebSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False

    async def receive_bytes(self) -> bytes:
        msg = "not used"
        raise RuntimeError(msg)

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    async def wait_sent(self) -> str:
        for _ in range(20):
            if self.sent:
                return self.sent[0].decode()
            await sleep(0)
        msg = "websocket did not receive an action payload"
        raise AssertionError(msg)


async def test_gateway_server_payloads_run_concurrently() -> None:
    gateway = Gateway(Bot())
    started = AsyncEvent()

    async def slow(_payload: BaseModel) -> None:
        started.set()
        await AsyncEvent().wait()

    def fast(payload: BaseModel) -> BaseModel:
        return payload

    await gateway.start()
    slow_task = create_task(gateway._run_on_server_task(Model(), slow))
    await started.wait()

    payload = Model()
    result = await wait_for(gateway._run_on_server_task(payload, fast), timeout=0.2)
    await gateway.close()

    assert result is payload
    with pytest.raises(CancelledError):
        await slow_task


async def test_gateway_close_cancels_pending_server_payloads() -> None:
    gateway = Gateway(Bot())
    started = AsyncEvent()

    async def slow(_payload: BaseModel) -> None:
        started.set()
        await AsyncEvent().wait()

    await gateway.start()
    task = create_task(gateway._run_on_server_task(Model(), slow))
    await started.wait()

    await gateway.close()

    with pytest.raises(CancelledError):
        await task


async def test_websocket_action_manager_fails_old_pending_and_recovers() -> None:
    manager = WebSocketActionManager(timeout=1)
    self_ = BotSelf(platform="test", user_id="bot")
    old_websocket = RecordingActionWebSocket()
    old_session = manager.register(old_websocket)
    manager.bind_self(old_session, self_)

    old_task = create_task(manager.request(self_, lambda echo: echo.encode()))
    await old_websocket.wait_sent()
    manager.unregister(old_session)

    with pytest.raises(ConnectionError):
        await old_task

    new_websocket = RecordingActionWebSocket()
    new_session = manager.register(new_websocket)
    manager.bind_self(new_session, self_)
    new_task = create_task(manager.request(self_, lambda echo: echo.encode()))
    echo = await new_websocket.wait_sent()

    assert manager.receive(ActionResponse.ok({"ok": True}, echo=echo)) is True
    response = await wait_for(new_task, timeout=0.2)
    assert response.data == {"ok": True}
