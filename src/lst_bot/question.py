from collections.abc import Mapping, Sequence

from bot import (
    Cmd,
    Connection,
    EventRouter,
    Injected,
    MessageEvent,
    Msg,
)
from bot.protocol.msg import ReplySegment
from logbook import Logger
from pydantic_ai import Agent

logger = Logger(__name__)
router = EventRouter()


def message_payload_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    segments = (value,) if isinstance(value, Mapping) else value
    if not isinstance(segments, Sequence):
        return ""
    parts: list[str] = []
    for segment in segments:
        match segment:
            case {"type": "text", "data": {"text": str(text)}}:
                parts.append(text)
    return "".join(parts).strip()


async def replied_message_text(conn: Connection, event: MessageEvent) -> str:
    extra = event.model_extra or {}
    if "reply_alt_message" in extra:
        value = extra["reply_alt_message"]
        return value.strip() if isinstance(value, str) else ""

    message_id = next(
        (
            segment.data.message_id
            for segment in event.message
            if isinstance(segment, ReplySegment)
        ),
        "",
    )
    if not message_id:
        return ""

    try:
        response = await conn.action("get_msg", message_id=message_id)
    except Exception as exc:
        logger.warning(
            "fetch replied message failed: {message_id} ({error})",
            message_id=message_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return ""

    data = getattr(response, "data", None)
    if not isinstance(data, Mapping):
        return ""

    return message_payload_text(data.get("message")) or message_payload_text(
        data.get("raw_message")
    )


async def build_question(
    conn: Connection,
    event: MessageEvent,
    question: str,
) -> str:
    reply_text = await replied_message_text(conn, event)
    parts = []
    if reply_text:
        parts.append(f"被回复的消息：\n{reply_text}")
    if question:
        parts.append(f"用户问题：\n{question}")
    return "\n\n".join(parts)


@router.on_cmd("问")
async def ask_dst_question(
    cmd: Injected[Cmd],
    event: Injected[MessageEvent],
    conn: Injected[Connection],
    agent: Injected[Agent],
) -> Msg:
    question = await build_question(conn, event, cmd.arg)
    if not question:
        answer = f"用法：{cmd.raw} 《饥荒联机版》相关问题"
    else:
        answer = (await agent.run(question)).output

    return Msg.reply(event.message_id, answer, user_id=event.user_id)
