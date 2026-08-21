import pytest
from bot import ActionResponse, ApiStatus, Bot, Cmd, Msg, Retcode
from bot.testing import RecordingGateway, private_message_event
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from lst_bot.question import (
    ask_dst_question,
    build_question,
    message_payload_text,
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
        (None, ""),
    ],
    ids=("string", "mapping", "sequence", "invalid-data", "none"),
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
    action = gateway.actions[0].model_dump(mode="json")
    assert action["action"] == "get_msg"
    assert action["params"] == {"message_id": "source-message"}

    embedded = event.model_copy(update={"reply_alt_message": "embedded question"})
    assert await build_question(gateway.connection, embedded, "new question") == (
        "被回复的消息：\nembedded question\n\n用户问题：\nnew question"
    )
    empty = event.model_copy(update={"reply_alt_message": ""})
    assert await build_question(gateway.connection, empty, "") == ""
    assert len(gateway.actions) == 1

    gateway.responses["get_msg"] = ActionResponse.ok({"raw_message": "legacy"})
    assert await build_question(gateway.connection, event, "new question") == (
        "用户问题：\nnew question"
    )

    gateway.responses["get_msg"] = ActionResponse.failed(
        Retcode.INTERNAL_HANDLER_ERROR,
        "internal details",
    )
    assert await build_question(gateway.connection, event, "new question") == (
        "用户问题：\nnew question"
    )

    gateway.responses["get_msg"] = ActionResponse(
        status=ApiStatus.ASYNC,
        retcode=1,
        data={"message": [{"type": "text", "data": {"text": "pending"}}]},
        message="queued",
    )
    assert await build_question(gateway.connection, event, "new question") == (
        "用户问题：\nnew question"
    )


async def test_question_handler_replies_with_agent_output() -> None:
    bot = Bot()
    gateway = RecordingGateway(bot)
    event = private_message_event("/问 巨鹿什么时候来？")

    reply = await ask_dst_question(
        Cmd(raw="/问", arg="巨鹿什么时候来？"),
        event,
        gateway.connection,
        Agent(TestModel(custom_output_text="答案")),
    )

    assert reply == Msg.reply(event.message_id, "答案", user_id=event.user_id)
