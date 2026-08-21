from collections.abc import Mapping, Sequence

from bot import (
    Cmd,
    Connection,
    EventRouter,
    Injected,
    MessageEvent,
    Reply,
    ReturnAction,
)
from bot.protocol.msg import ReplySegment
from logbook import Logger

from .agent import DstQuestionAgent

GET_MESSAGE_ACTION = "get_msg"

logger = Logger(__name__)
router = EventRouter()


def reply_message_id(event: MessageEvent) -> str:
    for segment in event.message:
        if isinstance(segment, ReplySegment):
            return segment.data.message_id
    return ""


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

    message_id = reply_message_id(event)
    if not message_id:
        return ""

    try:
        response = await conn.action(GET_MESSAGE_ACTION, message_id=message_id)
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
    agent: Injected[DstQuestionAgent],
    r: Injected[Reply],
) -> ReturnAction:
    question = await build_question(conn, event, cmd.arg)
    if not question:
        return r(f"用法：{cmd.raw} 《饥荒联机版》相关问题")

    return r(await agent.answer(question))
