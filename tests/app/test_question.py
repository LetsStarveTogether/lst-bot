from unittest.mock import Mock

import pytest
from bot import ActionResponse, Bot, Msg, ReturnAction
from bot.testing import RecordingGateway, private_message_event, recording_gateway

from lst_bot.agent import DstQuestionAgent
from lst_bot.question import (
    build_question,
    message_payload_text,
    reply_message_id,
    router,
)


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
        (b"bytes", ""),
        (None, ""),
    ],
    ids=("string", "mapping", "sequence", "invalid-data", "bytes", "none"),
)
def test_message_payload_text(payload: object, expected: str) -> None:
    assert message_payload_text(payload) == expected


async def test_build_question_combines_reply_and_command_text() -> None:
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

    question = await build_question(gateway.connection, event, "new question")

    assert reply_message_id(event) == "source-message"
    assert question == "被回复的消息：\nold question\n\n用户问题：\nnew question"
    action = gateway.actions[0].root.model_dump(mode="json", by_alias=True)
    assert action["action"] == "get_msg"
    assert action["params"] == {"message_id": "source-message"}


async def test_question_command_dispatches_with_injected_agent_and_reply() -> None:
    bot = Bot()
    agent = Mock(spec_set=DstQuestionAgent)
    agent.answer.return_value = "答案"
    bot.container.add_instance(agent, provides=DstQuestionAgent)
    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        results = await bot.dispatch(
            gateway.connection,
            private_message_event("/问 巨鹿什么时候来？"),
        )

    agent.answer.assert_awaited_once_with("用户问题：\n巨鹿什么时候来？")
    value = results[0].values[0]
    assert isinstance(value, ReturnAction)
    assert value.kind == "message"
    action = gateway.actions[0].root.model_dump(mode="json", by_alias=True)
    assert action["action"] == "send_message"
    assert action["params"]["message"][0]["type"] == "reply"
    assert action["params"]["message"][1]["data"]["text"] == "答案"
