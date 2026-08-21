from collections.abc import Callable, Mapping
from dataclasses import dataclass
from inspect import isawaitable
from typing import Self

from diwire import Injected, ResolverProtocol

from bot.core.di import InjectionContext, call_with_injection, inject
from bot.protocol.events import UserEvent


def _is_bot_admin(event: UserEvent, context: InjectionContext) -> bool:
    return event.user_id in context.bot.admin_ids.get(event.self_.platform, ())


@dataclass(slots=True, match_args=False)
class Rule:
    checker: Callable
    raw: bool = False

    def __post_init__(self) -> None:
        if not self.raw:
            self.checker = inject(self.checker)

    def __and__(self, other: Rule | Callable | None) -> Self:
        if other is None:
            return self
        rule_type = type(self)
        other_rule = other if isinstance(other, Rule) else rule_type(other)

        async def check(context: InjectionContext, resolver: ResolverProtocol) -> bool:
            return await self(context, resolver) and await other_rule(context, resolver)

        return rule_type(check, raw=True)

    def __or__(self, other: Rule | Callable | None) -> Self:
        if other is None:
            return self
        rule_type = type(self)
        other_rule = other if isinstance(other, Rule) else rule_type(other)

        async def check(context: InjectionContext, resolver: ResolverProtocol) -> bool:
            return await self(context, resolver) or await other_rule(context, resolver)

        return rule_type(check, raw=True)

    async def __call__(
        self,
        context: InjectionContext,
        resolver: ResolverProtocol,
    ) -> bool:
        if self.raw:
            value = self.checker(context, resolver)
            if isawaitable(value):
                return bool(await value)
            return bool(value)
        return bool(await call_with_injection(self.checker, context, resolver))


class Permission(Rule):
    __slots__ = ()

    @classmethod
    def bot_admin(cls) -> Permission:
        def check(
            event: Injected[UserEvent],
            context: Injected[InjectionContext],
        ) -> bool:
            return _is_bot_admin(event, context)

        return cls(check)

    @classmethod
    def admin(cls) -> Permission:
        def check(
            event: Injected[UserEvent],
            context: Injected[InjectionContext],
        ) -> bool:
            if _is_bot_admin(event, context):
                return True

            sender = (event.model_extra or {}).get("sender")
            role = sender.get("role") if isinstance(sender, Mapping) else None
            return isinstance(role, str) and role in {"admin", "owner"}

        return cls(check)
