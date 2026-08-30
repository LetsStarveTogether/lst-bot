from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from bot import ActionResponse, ApiStatus, Bot, Cmd, MessageEvent, Msg
from bot_test_support import RecordingGateway, private_message_event
from pydantic_ai import Agent

from lst_bot.question import ask_dst_question, message_payload_text


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("  plain text  ", "plain text"),
        ({"type": "text", "data": {"text": "single"}}, "single"),
        (
            [
                {"type": "image", "data": {"file": "platform-native-id"}},
                {"type": "text", "data": {"text": "multi"}},
                {"type": "text", "data": {"text": " segment"}},
            ],
            "multi segment",
        ),
        ({"type": "text", "data": "invalid"}, ""),
        (None, ""),
    ],
    ids=("string", "mapping", "sequence", "invalid-data", "none"),
)
def test_message_payload_text(payload: object, expected: str) -> None:
    assert message_payload_text(payload) == expected


async def test_question_handler_builds_agent_prompt_from_reply() -> None:
    bot = Bot()
    gateway = RecordingGateway(
        bot,
        responses={
            "get_msg": ActionResponse.ok({
                "message": [{"type": "text", "data": {"text": "old question"}}],
            }),
        },
    )
    event = private_message_event("问 new question").model_copy(
        update={"message": Msg.reply("source-message", "问 new question")},
    )
    agent = Mock(spec_set=Agent)
    agent.run.return_value = SimpleNamespace(output="答案")

    async def ask(
        current_event: MessageEvent = event,
        arg: str = "new question",
    ) -> Msg:
        agent.reset_mock()
        return await ask_dst_question(
            Cmd(raw="/问", arg=arg), current_event, gateway.connection, agent
        )

    reply = await ask()
    assert reply == Msg.reply(event.message_id, "答案", user_id=event.user_id)
    agent.run.assert_awaited_once_with(
        "被回复的消息：\nold question\n\n用户问题：\nnew question"
    )
    action = gateway.actions[0].model_dump(mode="json")
    assert action["action"] == "get_msg"
    assert action["params"] == {"message_id": "source-message"}

    embedded = event.model_copy(update={"reply_alt_message": "embedded question"})
    await ask(embedded)
    agent.run.assert_awaited_once_with(
        "被回复的消息：\nembedded question\n\n用户问题：\nnew question"
    )

    empty = event.model_copy(update={"reply_alt_message": ""})
    reply = await ask(empty, "")
    assert reply == Msg.reply(
        event.message_id,
        "用法：/问 《饥荒联机版》相关问题",
        user_id=event.user_id,
    )
    agent.run.assert_not_awaited()
    assert len(gateway.actions) == 1

    for response in (
        ActionResponse.ok({"raw_message": "legacy"}),
        ActionResponse(status=ApiStatus.FAILED, retcode=2, data=None, message="x"),
        ActionResponse(
            status=ApiStatus.ASYNC,
            retcode=1,
            data={"message": [{"type": "text", "data": {"text": "pending"}}]},
            message="queued",
        ),
    ):
        gateway.responses["get_msg"] = response
        await ask()
        agent.run.assert_awaited_once_with("用户问题：\nnew question")
