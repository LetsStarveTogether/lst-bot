from collections.abc import Mapping, Sequence
from logging import getLogger

from bot import (
    ActionResponse,
    ApiStatus,
    Cmd,
    Connection,
    EventRouter,
    Injected,
    MessageEvent,
    Msg,
)
from bot.protocol.msg import ReplySegment
from pydantic_ai import Agent

logger = getLogger(__name__)
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
            "fetch replied message failed: %s (%s: %s)",
            message_id,
            type(exc).__name__,
            exc,
        )
        return ""

    match response:
        case ActionResponse(status=ApiStatus.OK, data={"message": message}):
            return message_payload_text(message)
        case _:
            return ""


@router.on_cmd("问")
async def ask_dst_question(
    cmd: Injected[Cmd],
    event: Injected[MessageEvent],
    conn: Injected[Connection],
    agent: Injected[Agent],
) -> Msg:
    reply_text = await replied_message_text(conn, event)
    parts = []
    if reply_text:
        parts.append(f"被回复的消息：\n{reply_text}")
    if cmd.arg:
        parts.append(f"用户问题：\n{cmd.arg}")
    question = "\n\n".join(parts)
    if not question:
        answer = f"用法：{cmd.raw} 《饥荒联机版》相关问题"
    else:
        answer = (await agent.run(question)).output

    return Msg.reply(event.message_id, answer, user_id=event.user_id)
