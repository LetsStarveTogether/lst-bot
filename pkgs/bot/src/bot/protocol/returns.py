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
        return cls(
            kind="call",
            action_call=ActionCall.model_validate({
                "action": action,
                "params": {} if params is None else params,
            }),
            self_=self_,
        )

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
