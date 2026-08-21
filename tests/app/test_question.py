import pytest
from bot import ActionResponse, Bot, Msg
from bot.testing import RecordingGateway, private_message_event, recording_gateway
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from lst_bot.question import (
    build_question,
    message_payload_text,
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

    assert question == "被回复的消息：\nold question\n\n用户问题：\nnew question"
    action = gateway.actions[0].model_dump(mode="json", by_alias=True)
    assert action["action"] == "get_msg"
    assert action["params"] == {"message_id": "source-message"}

    embedded = event.model_copy(update={"reply_alt_message": "embedded question"})
    assert await build_question(gateway.connection, embedded, "new question") == (
        "被回复的消息：\nembedded question\n\n用户问题：\nnew question"
    )
    empty = event.model_copy(update={"reply_alt_message": ""})
    assert await build_question(gateway.connection, empty, "") == ""
    assert len(gateway.actions) == 1


async def test_question_command_sends_reply_from_injected_agent() -> None:
    bot = Bot()
    bot.container.add_instance(Agent(TestModel(custom_output_text="答案")))
    bot.add_router(router)
    gateway = recording_gateway(bot)

    async with bot:
        await bot.dispatch(
            gateway.connection,
            private_message_event("/问 巨鹿什么时候来？"),
        )

    action = gateway.actions[0].model_dump(mode="json", by_alias=True)
    assert action["action"] == "send_message"
    assert action["params"]["message"][0]["type"] == "reply"
    assert action["params"]["message"][1]["data"]["text"] == "答案"
