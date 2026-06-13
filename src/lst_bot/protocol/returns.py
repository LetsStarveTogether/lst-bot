from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from .actions import ActionCall, ActionParamInput
from .common import BotSelf
from .msg import Msg, MsgInput

type ReturnActionKind = Literal["message", "call", "request"]


@dataclass(frozen=True, slots=True)
class ReturnAction:
    kind: ReturnActionKind
    msg: Msg | None = None
    action_call: ActionCall | None = None
    self_: BotSelf | None = None
    approve: bool | None = None
    reason: str = ""
    remark: str = ""

    def __str__(self) -> str:
        if self.kind == "message" and self.msg is not None:
            text = " ".join(self.msg.text.split())
            if text:
                return f'message "{text}"'
            count = len(self.msg)
            return f"message {count} segments" if count else "message -"

        if self.kind == "call" and self.action_call is not None:
            text = f"call:{self.action_call}"
            if self.self_ is not None:
                text = f"{text} @ {self.self_}"
            return text

        if self.kind == "request":
            decision = "approve" if self.approve else "reject"
            reason = self.reason or self.remark
            if not reason:
                return f"request:{decision}"
            reason = " ".join(reason.split())
            return f"request:{decision} {reason}"

        return self.kind

    @classmethod
    def message(cls, message: MsgInput) -> ReturnAction:
        return cls(kind="message", msg=Msg.from_input(message))

    @classmethod
    def call(
        cls,
        action: str,
        params: Mapping[str, ActionParamInput] | BaseModel | None = None,
        *,
        self_: BotSelf | None = None,
    ) -> ReturnAction:
        payload: Mapping[str, ActionParamInput] | BaseModel
        payload = params if isinstance(params, BaseModel) else dict(params or {})
        return cls(
            kind="call",
            action_call=ActionCall.model_validate({
                "action": action,
                "params": payload,
            }),
            self_=self_,
        )

    @classmethod
    def from_call(
        cls,
        action_call: ActionCall,
        *,
        self_: BotSelf | None = None,
    ) -> ReturnAction:
        return cls(kind="call", action_call=action_call, self_=self_)

    @classmethod
    def request(
        cls,
        approve: bool,
        *,
        reason: str = "",
        remark: str = "",
    ) -> ReturnAction:
        return cls(
            kind="request",
            approve=approve,
            reason=reason,
            remark=remark,
        )


__all__ = ["ReturnAction", "ReturnActionKind"]
