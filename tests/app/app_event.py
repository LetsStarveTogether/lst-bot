from __future__ import annotations

from bot import EventPayload, PrivateMessageEvent


def message_event(
    text: str,
    *,
    user_id: str = "user",
    event_id: str = "event",
) -> PrivateMessageEvent:
    event = EventPayload.model_validate({
        "id": event_id,
        "self": {"platform": "test", "user_id": "bot"},
        "time": 1.0,
        "type": "message",
        "detail_type": "private",
        "sub_type": "",
        "message_id": f"{event_id}-message",
        "message": [{"type": "text", "data": {"text": text}}],
        "alt_message": text,
        "user_id": user_id,
    }).root
    if not isinstance(event, PrivateMessageEvent):
        msg = "message event helper must create a private message event"
        raise TypeError(msg)
    return event


__all__ = ["message_event"]
