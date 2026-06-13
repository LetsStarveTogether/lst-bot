from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from inspect import isawaitable

from diwire import Injected, ResolverProtocol

from lst_bot.core.di import InjectionContext, call_with_injection
from lst_bot.protocol.events import UserEvent


@dataclass(slots=True, match_args=False)
class Rule:
    checker: Callable
    raw: bool = False

    def __and__(self, other: Rule | Callable | None) -> Rule:
        if other is None:
            return self
        other_rule = other if isinstance(other, Rule) else Rule(other)

        async def check(context: InjectionContext, resolver: ResolverProtocol) -> bool:
            return await self(context, resolver) and await other_rule(context, resolver)

        return Rule(check, raw=True)

    def __or__(self, other: Rule | Callable | None) -> Rule:
        if other is None:
            return self
        other_rule = other if isinstance(other, Rule) else Rule(other)

        async def check(context: InjectionContext, resolver: ResolverProtocol) -> bool:
            return await self(context, resolver) or await other_rule(context, resolver)

        return Rule(check, raw=True)

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


@dataclass(slots=True, match_args=False)
class Permission:
    checker: Callable
    raw: bool = False

    @classmethod
    def bot_admin(cls) -> Permission:
        def check(
            event: Injected[UserEvent],
            context: Injected[InjectionContext],
        ) -> bool:
            return event.user_id in context.bot.admin_ids

        return cls(check)

    @classmethod
    def admin(cls) -> Permission:
        def check(
            event: Injected[UserEvent],
            context: Injected[InjectionContext],
        ) -> bool:
            if event.user_id in context.bot.admin_ids:
                return True

            sender = (event.model_extra or {}).get("sender")
            return isinstance(sender, Mapping) and sender.get("role") in {
                "admin",
                "owner",
            }

        return cls(check)

    def __and__(
        self,
        other: Permission | Callable | None,
    ) -> Permission:
        if other is None:
            return self
        other_permission = other if isinstance(other, Permission) else Permission(other)

        async def check(context: InjectionContext, resolver: ResolverProtocol) -> bool:
            return await self(context, resolver) and await other_permission(
                context,
                resolver,
            )

        return Permission(check, raw=True)

    def __or__(
        self,
        other: Permission | Callable | None,
    ) -> Permission:
        if other is None:
            return self
        other_permission = other if isinstance(other, Permission) else Permission(other)

        async def check(context: InjectionContext, resolver: ResolverProtocol) -> bool:
            return await self(context, resolver) or await other_permission(
                context,
                resolver,
            )

        return Permission(check, raw=True)

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


__all__ = ["Permission", "Rule"]
