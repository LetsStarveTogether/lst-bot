from dataclasses import dataclass
from typing import Literal

from .msg import Msg, MsgInput

type ReturnActionKind = Literal["message", "request"]


@dataclass(frozen=True, slots=True)
class ReturnAction:
    kind: ReturnActionKind
    msg: Msg | None = None
    approve: bool | None = None
    reason: str = ""
    remark: str = ""

    @classmethod
    def message(cls, message: MsgInput) -> ReturnAction:
        return cls(kind="message", msg=Msg.from_input(message))

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
