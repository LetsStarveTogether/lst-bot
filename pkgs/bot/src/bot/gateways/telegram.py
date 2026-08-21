from asyncio import (
    CancelledError,
    Lock,
    QueueFull,
    Task,
    create_task,
    current_task,
    sleep,
    wait,
)
from asyncio import (
    Event as AsyncEvent,
)
from collections.abc import Mapping
from contextlib import suppress
from html import escape
from importlib.metadata import version
from logging import getLogger
from time import time
from typing import Annotated, Self, cast, override

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    model_validator,
)
from urllib3_future import AsyncPoolManager

from bot.core import Bot
from bot.protocol.actions import ActionParamInput, ActionParamModel
from bot.protocol.common import BotSelf, BotStatus, Status, Version
from bot.protocol.enums import Action, MsgSegmentType
from bot.protocol.events import (
    Event,
    GroupMessageEvent,
    GroupRequestEvent,
    MessageEvent,
    NoticeEvent,
    PrivateMessageEvent,
)
from bot.protocol.msg import (
    LocationSegment,
    MentionAllSegment,
    MentionSegment,
    Msg,
    MsgInput,
    ReplySegment,
    TextSegment,
)
from bot.protocol.returns import ReturnAction

from .base import Connection, Gateway, await_cleanup
from .telegram_api import (
    TELEGRAM_API_BASE_URL,
    TELEGRAM_METHODS,
    PositiveInt,
    TelegramAPIError,
    TelegramChatJoinRequest,
    TelegramLocation,
    TelegramMessage,
    TelegramMessageEntity,
    TelegramObject,
    TelegramRestClient,
    TelegramResult,
    TelegramTopicID,
    TelegramUpdate,
    TelegramUpload,
    TelegramUser,
    TelegramUserID,
)

logger = getLogger(__name__)

_RETRY_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)
_MAX_TEXT_LENGTH = 4096
_MAX_MEDIA_CAPTION_LENGTH = 1024
_MEDIA_METHODS: dict[str | MsgSegmentType, tuple[str, str]] = {
    MsgSegmentType.IMAGE: ("sendPhoto", "photo"),
    MsgSegmentType.VOICE: ("sendVoice", "voice"),
    MsgSegmentType.AUDIO: ("sendAudio", "audio"),
    MsgSegmentType.VIDEO: ("sendVideo", "video"),
    MsgSegmentType.FILE: ("sendDocument", "document"),
    "telegram.animation": ("sendAnimation", "animation"),
    "telegram.sticker": ("sendSticker", "sticker"),
    "telegram.video_note": ("sendVideoNote", "video_note"),
}
_POLL_TIMEOUT_ADAPTER = TypeAdapter(Annotated[StrictInt, Field(ge=0)])
_MESSAGE_ID_ADAPTER = TypeAdapter(Annotated[StrictInt, Field(ge=0)])
_USER_ID_ADAPTER = TypeAdapter(TelegramUserID)
_NATIVE_ACTIONS = TELEGRAM_METHODS - {"getUpdates", "setWebhook"}
_GROUP_METHODS = {
    Action.GET_GROUP_INFO: "getChat",
    Action.GET_GROUP_MEMBER_INFO: "getChatMember",
    Action.SET_GROUP_NAME: "setChatTitle",
    Action.LEAVE_GROUP: "leaveChat",
}

_COMMON_ACTIONS = (
    Action.GET_SUPPORTED_ACTIONS,
    Action.GET_STATUS,
    Action.GET_VERSION,
    Action.GET_SELF_INFO,
    Action.SEND_MESSAGE,
    Action.DELETE_MESSAGE,
    Action.GET_FILE,
    *_GROUP_METHODS,
)


class _TelegramMessageOptions(BaseModel):
    model_config = ConfigDict(
        allow_inf_nan=False,
        extra="forbid",
        hide_input_in_errors=True,
    )

    business_connection_id: StrictStr | None = None
    message_thread_id: PositiveInt | None = None
    direct_messages_topic_id: TelegramTopicID | None = None
    receiver_user_id: TelegramUserID | None = None
    callback_query_id: StrictStr | None = None
    disable_notification: StrictBool | None = None
    protect_content: StrictBool | None = None
    allow_paid_broadcast: StrictBool | None = None
    message_effect_id: StrictStr | None = None
    suggested_post_parameters: TelegramObject | None = None
    reply_parameters: TelegramObject | None = None
    reply_markup: TelegramObject | None = None
    parse_mode: StrictStr | None = None
    entities: list[TelegramMessageEntity] | None = None

    @model_validator(mode="after")
    def one_formatting_mode(self) -> Self:
        if self.parse_mode is not None and self.entities is not None:
            msg = "Telegram parse_mode and entities are mutually exclusive"
            raise ValueError(msg)
        return self


class TelegramConnection(Connection):
    @staticmethod
    @override
    def _message_action_params(  # ruff: ignore[complex-structure] - protocol contexts are independent flat checks
        event: MessageEvent,
        msg: MsgInput,
    ) -> dict[str, ActionParamInput]:
        params = Connection._message_action_params(  # ruff: ignore[private-member-access] - shared target mapping
            event, msg
        )
        guest_query_id = getattr(event, "telegram_guest_query_id", None)
        if isinstance(guest_query_id, str):
            params["telegram_guest_query_id"] = guest_query_id
            return params
        thread_id = getattr(event, "telegram_message_thread_id", None)
        chat_id = getattr(event, "telegram_chat_id", None)
        if isinstance(chat_id, int) and not isinstance(chat_id, bool):
            for target in ("group_id", "channel_id", "user_id"):
                if target in params:
                    params[target] = str(chat_id)
            params["telegram_chat_id"] = chat_id
        if isinstance(thread_id, int) and not isinstance(thread_id, bool):
            params["message_thread_id"] = thread_id
        business_connection_id = getattr(event, "telegram_business_connection_id", None)
        if isinstance(business_connection_id, str):
            params["business_connection_id"] = business_connection_id
        direct_topic_id = getattr(event, "telegram_direct_messages_topic_id", None)
        if isinstance(direct_topic_id, int) and not isinstance(direct_topic_id, bool):
            params.pop("message_thread_id", None)
            params["direct_messages_topic_id"] = direct_topic_id
        elif getattr(event, "telegram_is_direct_messages", None) is True:
            msg = "Telegram direct-message replies require direct_messages_topic_id"
            raise ValueError(msg)
        ephemeral_message_id = getattr(event, "telegram_ephemeral_message_id", None)
        if ephemeral_message_id is not None and getattr(
            event, "telegram_chat_type", None
        ) not in {"group", "supergroup"}:
            msg = "Telegram ephemeral replies require a group or supergroup"
            raise ValueError(msg)
        if isinstance(ephemeral_message_id, int) and not isinstance(
            ephemeral_message_id, bool
        ):
            params["receiver_user_id"] = _user_id(event.user_id)
            params["reply_parameters"] = {"ephemeral_message_id": ephemeral_message_id}
        elif ephemeral_message_id is not None:
            msg = "Telegram ephemeral replies require a valid ephemeral_message_id"
            raise ValueError(msg)
        return params


class TelegramGateway(Gateway, TelegramRestClient):
    def __init__(
        self,
        bot: Bot,
        *,
        token: SecretStr | str,
        base_url: str = TELEGRAM_API_BASE_URL,
        http_pool: AsyncPoolManager | None = None,
        poll_timeout: int = 30,
    ) -> None:
        Gateway.__init__(self, bot)
        TelegramRestClient.__init__(
            self,
            token,
            base_url=base_url,
            http_pool=http_pool,
        )
        self.poll_timeout = _POLL_TIMEOUT_ADAPTER.validate_python(poll_timeout)
        self._gateway_lock = Lock()
        self._startup_task: Task[None] | None = None
        self._startup_waiters = 0
        self._close_task: Task[None] | None = None
        self._task: Task[None] | None = None
        self._closing = False
        self._polling_reserved = False
        self._webhook_calls = 0
        self._webhook_idle = AsyncEvent()
        self._webhook_idle.set()
        self._online = False
        self._offset: int | None = None
        self._self: BotSelf | None = None
        self._me: TelegramUser | None = None

    @override
    async def start(  # ruff: ignore[complex-structure] - shared startup keeps paired rollback failures
        self,
    ) -> None:
        while True:
            async with self._gateway_lock:
                cleanup = self._close_task
                if cleanup is None:
                    if self._task is not None:
                        if not self._task.done():
                            return
                        self._task.result()
                    startup = self._startup_task
                    if startup is None or startup.done():
                        self._task = None
                        self._closing = False
                        self._polling_reserved = True
                        startup = create_task(
                            self._start_gateway(),
                            name="telegram-gateway-start",
                        )
                        self._startup_task = startup
                    self._startup_waiters += 1
                    break
            await wait((cleanup,))
            await self._await_close_task(cleanup)

        startup_error: BaseException | None = None
        try:
            await wait((startup,))
            startup.result()
        except BaseException as exc:
            startup_error = exc
        async with self._gateway_lock:
            cleanup = self._release_startup_waiter(startup)

        cleanup_error: BaseException | None = None
        if cleanup is not None:
            try:
                await self._await_close_task(cleanup)
            except BaseException as exc:
                cleanup_error = exc

        if startup_error is not None and cleanup_error is not None:
            msg = "Telegram startup and cleanup failed"
            raise BaseExceptionGroup(msg, [startup_error, cleanup_error]) from None
        if startup_error is not None:
            raise startup_error
        if cleanup_error is not None:
            raise cleanup_error

    def _release_startup_waiter(self, startup: Task[None]) -> Task[None] | None:
        self._startup_waiters -= 1
        if not startup.done():
            if not self._startup_waiters and self._startup_task is startup:
                return self._ensure_close_task()
            return None

        failed = startup.cancelled() or startup.exception() is not None
        if self._startup_task is startup:
            self._startup_task = None
            if failed:
                return self._ensure_close_task()
        return self._close_task if failed else None

    async def _start_gateway(self) -> None:
        await TelegramRestClient.start(self)
        await self._webhook_idle.wait()
        me = await self._identify()
        async with self._gateway_lock:
            if self._closing:
                raise CancelledError
            self._me = me
            self._self = BotSelf(platform="telegram", user_id=str(me.id))
            self._online = True
            self._task = create_task(
                self._run_poller(),
                name="telegram-gateway",
            )

    async def _identify(self) -> TelegramUser:
        me = await self.get_me()
        if not me.is_bot:
            msg = "Telegram getMe returned a non-bot user"
            raise RuntimeError(msg)
        if (await self.get_webhook_info()).url:
            msg = "Telegram webhook is configured; getUpdates is unavailable"
            raise RuntimeError(msg)
        return me

    @override
    async def close(self) -> None:
        async with self._gateway_lock:
            cleanup = self._ensure_close_task()
        await self._await_close_task(cleanup)

    async def _await_close_task(self, cleanup: Task[None]) -> None:
        try:
            await await_cleanup(cleanup)
        finally:
            async with self._gateway_lock:
                if self._close_task is cleanup and cleanup.done():
                    self._close_task = None

    def _ensure_close_task(self) -> Task[None]:
        cleanup = self._close_task
        if cleanup is None or cleanup.done():
            cleanup = create_task(
                self._close_gateway(),
                name="telegram-gateway-close",
            )
            self._close_task = cleanup
        return cleanup

    async def _close_gateway(self) -> None:
        async with self._gateway_lock:
            self._closing = True
            self._online = False
            self._polling_reserved = True
            tasks = tuple(
                task for task in (self._startup_task, self._task) if task is not None
            )
            for task in tasks:
                task.cancel()
        try:
            for task in tasks:
                with suppress(CancelledError, Exception):
                    await task
        finally:
            try:
                await TelegramRestClient.close(self)
            finally:
                async with self._gateway_lock:
                    if self._startup_task in tasks:
                        self._startup_task = None
                    if self._task in tasks:
                        self._task = None
                    self._polling_reserved = False

    @override
    def connection_for(self, self_: BotSelf) -> TelegramConnection:
        return TelegramConnection(self, self_)

    @override
    async def call_json(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        files: Mapping[str, bytes | TelegramUpload] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        canonical = method.casefold()
        webhook_call = False
        if canonical in {"getupdates", "setwebhook"}:
            async with self._gateway_lock:
                if (
                    canonical == "getupdates"
                    and self._polling_reserved
                    and current_task() is not self._task
                ):
                    msg = "Telegram getUpdates is reserved for the running gateway"
                    raise RuntimeError(msg)
                if canonical == "setwebhook":
                    if self._polling_reserved:
                        msg = "Telegram setWebhook is unavailable while polling"
                        raise RuntimeError(msg)
                    self._webhook_calls += 1
                    self._webhook_idle.clear()
                    webhook_call = True
        try:
            return await super().call_json(
                method,
                params,
                files,
                request_timeout=request_timeout,
            )
        finally:
            if webhook_call:
                async with self._gateway_lock:
                    self._webhook_calls -= 1
                    if not self._webhook_calls:
                        self._webhook_idle.set()

    @override
    async def request_action(  # ruff: ignore[complex-structure, too-many-branches, too-many-statements] - protocol action router is intentionally flat
        self,
        connection: Connection,
        action: str,
        params: ActionParamModel,
    ) -> BaseModel:
        if self._closing or self._closed:
            msg = "Telegram gateway is closed"
            raise RuntimeError(msg)
        if (
            connection.gateway is not self
            or self._self is None
            or connection.self_ != self._self
        ):
            msg = "Telegram action targets an unknown bot self"
            raise LookupError(msg)

        data = params.model_dump(mode="python", exclude_none=True)
        try:
            common_action = Action(action)
        except ValueError:
            common_action = None
        if common_action == Action.SEND_MESSAGE:
            return await self._send_message(data)
        if common_action == Action.GET_SUPPORTED_ACTIONS:
            return RootModel[list[StrictStr]]([
                *(item.value for item in _COMMON_ACTIONS),
                *sorted(_NATIVE_ACTIONS),
            ])
        if common_action == Action.GET_STATUS:
            return Status(
                good=self._task is not None and not self._task.done(),
                bots=[BotStatus(self_=self._self, online=self._online)],
            )
        if common_action == Action.GET_VERSION:
            return Version(
                impl="lst-bot.telegram",
                version=version("bot"),
                onebot_version="12",
            )
        if common_action == Action.GET_SELF_INFO:
            if self._me is None:
                msg = "Telegram gateway has not started"
                raise RuntimeError(msg)
            return self._me
        if common_action == Action.DELETE_MESSAGE:
            message_id = _message_id(data.pop("message_id"))
            ephemeral_message_id = data.pop("ephemeral_message_id", None)
            receiver_user_id = data.pop("receiver_user_id", None)
            chat_id = _pop_chat_id(data)
            if (ephemeral_message_id is None) != (receiver_user_id is None):
                msg = (
                    "Telegram ephemeral deletion requires both "
                    "ephemeral_message_id and receiver_user_id"
                )
                raise ValueError(msg)
            if ephemeral_message_id is not None and receiver_user_id is not None:
                return await self.call(
                    "deleteEphemeralMessage",
                    {
                        "chat_id": chat_id,
                        "receiver_user_id": _user_id(receiver_user_id),
                        "ephemeral_message_id": _message_id(ephemeral_message_id),
                        **data,
                    },
                )
            if message_id == 0:
                msg = (
                    "Telegram message_id 0 cannot be deleted without "
                    "ephemeral_message_id and receiver_user_id"
                )
                raise ValueError(msg)
            return await self.call(
                "deleteMessage",
                {"chat_id": chat_id, "message_id": message_id, **data},
            )

        if common_action == Action.GET_FILE:
            file_type = data.pop("type")
            if file_type != "data":
                msg = "Telegram get_file supports only the data result type"
                raise TypeError(msg)
            return await self.download_file(cast(str, data.pop("file_id")))

        method = _GROUP_METHODS.get(common_action)
        if method is not None:
            member_id = (
                data.pop("user_id")
                if common_action == Action.GET_GROUP_MEMBER_INFO
                else None
            )
            chat_id = _pop_chat_id(data)
            if member_id is not None:
                data["user_id"] = _user_id(member_id)
            elif common_action == Action.SET_GROUP_NAME:
                data["title"] = data.pop("group_name")
            return await self.call(method, {"chat_id": chat_id, **data})

        if action.casefold() in map(str.casefold, _NATIVE_ACTIONS):
            files = cast(Mapping[str, bytes | TelegramUpload], data.pop("files", {}))
            return await self.call(action, data, files)

        if common_action is None:
            msg = f"unsupported Telegram action: {action}"
            raise LookupError(msg)
        msg = f"Telegram does not support common action {common_action.value}"
        raise LookupError(msg)

    @override
    async def execute_return_action(
        self,
        connection: Connection,
        event: Event | None,
        action: ReturnAction,
    ) -> BaseModel:
        if action.kind != "request":
            return await super().execute_return_action(connection, event, action)
        if (
            not isinstance(event, GroupRequestEvent)
            or (event.model_extra or {}).get("telegram_event_type")
            != "chat_join_request"
        ):
            msg = "Telegram request responses require a chat join request"
            raise TypeError(msg)
        if action.approve is None:
            msg = "Telegram request response requires approve"
            raise TypeError(msg)
        if action.reason or action.remark:
            msg = "Telegram chat join request responses do not support text"
            raise TypeError(msg)
        query_id = (event.model_extra or {}).get("telegram_chat_join_request_query_id")
        if isinstance(query_id, str):
            return await connection.action(
                "answerChatJoinRequestQuery",
                chat_join_request_query_id=query_id,
                result="approve" if action.approve else "decline",
            )
        return await connection.action(
            "approveChatJoinRequest" if action.approve else "declineChatJoinRequest",
            chat_id=event.group_id,
            user_id=int(event.user_id),
        )

    async def _run_poller(self) -> None:
        await self.bot.wait_until_running()
        retries = 0
        try:
            while not self._closing:
                try:
                    updates = await self.get_updates(
                        offset=self._offset,
                        poll_timeout=self.poll_timeout,
                    )
                    self._accept_updates(updates)
                except (ConnectionError, QueueFull, TelegramAPIError) as exc:
                    self._online = False
                    if isinstance(exc, TelegramAPIError) and (
                        exc.error_code in {401, 403} or exc.status in {401, 403}
                    ):
                        logger.warning(
                            "Telegram polling stopped after permanent API error %s",
                            exc.error_code,
                        )
                        return
                    delay = _RETRY_DELAYS[min(retries, len(_RETRY_DELAYS) - 1)]
                    if (
                        isinstance(exc, TelegramAPIError)
                        and exc.parameters is not None
                        and exc.parameters.retry_after is not None
                    ):
                        delay = max(delay, float(exc.parameters.retry_after))
                    retries += 1
                    logger.warning(
                        "Telegram polling failed; retrying in %ss: %s",
                        delay,
                        type(exc).__name__,
                    )
                    await sleep(delay)
                else:
                    retries = 0
                    self._online = True
        finally:
            self._online = False

    def _accept_updates(self, updates: list[TelegramUpdate]) -> None:
        for update in updates:
            event = self._event_from_update(update)
            self.enqueue_event(event)
            self._offset = update.update_id + 1

    def _event_from_update(self, update: TelegramUpdate) -> Event:
        event_type, payload = update.payload or ("raw_update", None)
        notice_type = event_type
        if isinstance(payload, TelegramMessage):
            if payload.service_type is None:
                return self._message_event(update, event_type, payload)
            notice_type = payload.service_type
        if isinstance(payload, TelegramChatJoinRequest):
            return GroupRequestEvent.model_validate({
                **self._event_fields(update, event_type, float(payload.date)),
                "sub_type": "add",
                "user_id": str(payload.from_.id),
                "group_id": str(payload.chat.id),
                "comment": payload.bio or "",
                "flag": payload.query_id or str(payload.user_chat_id),
                "telegram_chat_join_request_query_id": payload.query_id,
            })
        return NoticeEvent.model_validate({
            **self._event_fields(update, event_type, _payload_time(payload)),
            "detail_type": f"telegram.{notice_type}",
            "sub_type": "",
        })

    def _message_event(
        self,
        update: TelegramUpdate,
        event_type: str,
        message: TelegramMessage,
    ) -> MessageEvent:
        sender = message.from_ or message.sender_chat or message.chat
        reply = message.reply_to_message
        fields = {
            **self._event_fields(update, event_type, float(message.date)),
            "sub_type": event_type,
            "user_id": str(sender.id),
            "message_id": str(message.message_id),
            "message": _telegram_message(message),
            "alt_message": message.text or message.caption or "",
            **(
                {"reply_alt_message": reply.text or reply.caption or ""}
                if reply is not None
                else {}
            ),
            "telegram_chat_id": message.chat.id,
            "telegram_chat_type": message.chat.type,
            "telegram_is_direct_messages": message.chat.is_direct_messages,
            "telegram_message_thread_id": message.message_thread_id,
            "telegram_business_connection_id": message.business_connection_id,
            "telegram_direct_messages_topic_id": (
                message.direct_messages_topic.topic_id
                if message.direct_messages_topic is not None
                else None
            ),
            "telegram_ephemeral_message_id": message.ephemeral_message_id,
            "telegram_receiver_user_id": (
                message.receiver_user.id if message.receiver_user is not None else None
            ),
            "telegram_guest_query_id": message.guest_query_id,
        }
        if message.chat.type == "private":
            return PrivateMessageEvent.model_validate(fields)
        # Telegram channels are flat chats; do not invent a guild ID.
        return GroupMessageEvent.model_validate({
            **fields,
            "group_id": str(message.chat.id),
        })

    def _event_fields(
        self,
        update: TelegramUpdate,
        event_type: str,
        timestamp: float,
    ) -> dict[str, object]:
        if self._self is None:
            msg = "Telegram gateway has not identified its bot"
            raise RuntimeError(msg)
        return {
            "id": f"telegram:{update.update_id}",
            "time": timestamp,
            "self_": self._self,
            "telegram_update_id": update.update_id,
            "telegram_event_type": event_type,
            "telegram_raw": update.raw,
        }

    async def _send_message(self, params: dict[str, object]) -> BaseModel:
        message = Msg.model_validate(params.pop("message"))
        guest_query_id = params.pop("telegram_guest_query_id", None)
        if guest_query_id is not None:
            for key in (
                "telegram_chat_id",
                "chat_id",
                "group_id",
                "channel_id",
                "user_id",
                "guild_id",
                "detail_type",
            ):
                params.pop(key, None)
            if params:
                msg = (
                    "Telegram guest replies do not accept send-message options; "
                    "use answerGuestQuery"
                )
                raise ValueError(msg)
            return await self._answer_guest_message(guest_query_id, message)
        chat_id = _pop_chat_id(params)
        calls = _message_calls(chat_id, message, params)
        results = [
            (await self.call(method, call_params)).root for method, call_params in calls
        ]
        if len(results) == 1:
            return TelegramResult(results[0])
        return RootModel[list[JsonValue]](results)

    async def _answer_guest_message(
        self,
        guest_query_id: object,
        message: Msg,
    ) -> TelegramResult:
        if not isinstance(guest_query_id, str) or not guest_query_id:
            msg = "Telegram guest reply requires guest_query_id"
            raise ValueError(msg)
        if not all(isinstance(segment, TextSegment) for segment in message):
            msg = (
                "Telegram guest replies support only text; use "
                "answerGuestQuery for other results"
            )
            raise TypeError(msg)
        text = "".join(cast(TextSegment, segment).data.text for segment in message)
        if not 1 <= len(text) <= _MAX_TEXT_LENGTH:
            msg = "Telegram guest reply text must contain 1 to 4096 characters"
            raise ValueError(msg)
        return await self.call(
            "answerGuestQuery",
            {
                "guest_query_id": guest_query_id,
                "result": {
                    "type": "article",
                    "id": "reply",
                    "title": "回复",
                    "input_message_content": {"message_text": text},
                },
            },
        )


def _payload_time(payload: object) -> float:
    message = (
        payload.get("message")
        if isinstance(payload, Mapping)
        else getattr(payload, "message", None)
    )
    for value, field in (
        (payload, "date"),
        (payload, "remove_date"),
        (message, "date"),
    ):
        timestamp = (
            value.get(field)
            if isinstance(value, Mapping)
            else getattr(value, field, None)
        )
        if (
            isinstance(timestamp, int)
            and not isinstance(timestamp, bool)
            and timestamp > 0
        ):
            return float(timestamp)
    return time()


def _telegram_message(message: TelegramMessage) -> Msg:
    segments: list[dict[str, object]] = []
    if message.reply_to_message is not None:
        segments.append({
            "type": MsgSegmentType.REPLY,
            "data": {"message_id": str(message.reply_to_message.message_id)},
        })
    text = message.text or message.caption
    if text:
        segments.append({"type": MsgSegmentType.TEXT, "data": {"text": text}})
    if message.photo:
        segments.append({
            "type": MsgSegmentType.IMAGE,
            "data": {"file_id": message.photo[-1].file_id},
        })
    media = (
        (message.audio, MsgSegmentType.AUDIO),
        (
            message.document if message.animation is None else None,
            MsgSegmentType.FILE,
        ),
        (message.video, MsgSegmentType.VIDEO),
        (message.voice, MsgSegmentType.VOICE),
        (message.animation, "telegram.animation"),
        (message.sticker, "telegram.sticker"),
        (message.video_note, "telegram.video_note"),
    )
    segments.extend(
        {"type": segment_type, "data": {"file_id": file.file_id}}
        for file, segment_type in media
        if file is not None
    )
    venue = message.venue
    location = venue.location if venue is not None else message.location
    if location is not None:
        segments.append({
            "type": MsgSegmentType.LOCATION,
            "data": {
                "latitude": location.latitude,
                "longitude": location.longitude,
                "title": venue.title if venue is not None else "",
                "content": venue.address if venue is not None else "",
            },
        })
    if not segments:
        segments.append({
            "type": "telegram.message",
            "data": {"raw": message.model_dump(mode="json")},
        })
    return Msg.model_validate(segments)


def _message_calls(  # ruff: ignore[complex-structure, too-many-branches, too-many-locals, too-many-statements] - segment conversion is clearest as one flat pass
    chat_id: object,
    message: Msg,
    extra: Mapping[str, object],
) -> list[tuple[str, dict[str, object]]]:
    text_parts: list[str] = []
    resources: list[tuple[str, dict[str, object]]] = []
    reply: int | None = None
    html = False
    text_length = 0
    for segment in message:
        if isinstance(segment, TextSegment):
            text_length += len(segment.data.text)
            text_parts.append(escape(segment.data.text) if html else segment.data.text)
        elif isinstance(segment, MentionSegment):
            if not html:
                text_parts = [escape(part) for part in text_parts]
                html = True
            user_id = _user_id(segment.data.user_id)
            text_length += len(str(user_id))
            text_parts.append(f'<a href="tg://user?id={user_id}">{user_id}</a>')
        elif isinstance(segment, MentionAllSegment):
            msg = "Telegram does not support mention-all"
            raise TypeError(msg)
        elif segment.type in _MEDIA_METHODS:
            method, field = _MEDIA_METHODS[segment.type]
            resources.append(
                (method, {field: cast(object, segment.data).file_id})  # ty: ignore[unresolved-attribute]
            )
        elif isinstance(segment, LocationSegment):
            location = TelegramLocation(
                latitude=segment.data.latitude,
                longitude=segment.data.longitude,
            )
            if segment.data.title or segment.data.content:
                resources.append((
                    "sendVenue",
                    {
                        "latitude": location.latitude,
                        "longitude": location.longitude,
                        "title": segment.data.title,
                        "address": segment.data.content,
                    },
                ))
            else:
                resources.append((
                    "sendLocation",
                    {
                        "latitude": location.latitude,
                        "longitude": location.longitude,
                    },
                ))
        elif isinstance(segment, ReplySegment):
            if reply is not None:
                msg = "Telegram messages accept at most one reply segment"
                raise ValueError(msg)
            reply = _message_id(segment.data.message_id)
        else:
            msg = f"Telegram does not support message segment {segment.type!s}"
            raise TypeError(msg)

    options = _TelegramMessageOptions.model_validate(extra).model_dump(
        exclude_none=True
    )
    parse_mode = cast(str | None, options.pop("parse_mode", None))
    entities = cast(list[dict[str, object]] | None, options.pop("entities", None))
    text = "".join(text_parts)
    if not text and (parse_mode is not None or entities is not None):
        msg = "Telegram formatting options require message text"
        raise ValueError(msg)
    if text_length > _MAX_TEXT_LENGTH and (html or parse_mode is None):
        msg = "Telegram message text exceeds 4096 characters"
        raise ValueError(msg)
    if html:
        if entities is not None or parse_mode not in {None, "HTML"}:
            msg = "Telegram mention segments require HTML parse mode"
            raise ValueError(msg)
        parse_mode = "HTML"
    common = {"chat_id": chat_id, **options}
    keep_reply_context = False
    if reply is not None:
        if "reply_parameters" in common:
            msg = "Telegram reply is specified twice"
            raise ValueError(msg)
        common["reply_parameters"] = {"message_id": reply}
    elif isinstance(common.get("reply_parameters"), Mapping):
        keep_reply_context = (
            cast(Mapping[str, object], common["reply_parameters"]).get(
                "ephemeral_message_id"
            )
            is not None
        )

    calls: list[tuple[str, dict[str, object]]] = []
    caption_used = bool(
        text
        and text_length <= _MAX_MEDIA_CAPTION_LENGTH
        and resources
        and resources[0][0]
        not in {"sendSticker", "sendVideoNote", "sendLocation", "sendVenue"}
    )
    if text and not caption_used:
        params = {**common, "text": text}
        if parse_mode is not None:
            params["parse_mode"] = parse_mode
        if entities is not None:
            params["entities"] = entities
        calls.append(("sendMessage", params))
        if not keep_reply_context:
            common.pop("reply_parameters", None)

    for index, (method, resource) in enumerate(resources):
        params = {**common, **resource}
        if index and not keep_reply_context:
            params.pop("reply_parameters", None)
        if caption_used and index == 0:
            params["caption"] = text
            if parse_mode is not None:
                params["parse_mode"] = parse_mode
            if entities is not None:
                params["caption_entities"] = entities
        calls.append((method, params))
    if not calls:
        msg = "Telegram message must contain text or a supported resource"
        raise ValueError(msg)
    return calls


def _message_id(value: object) -> int:
    return _validate_id(value, _MESSAGE_ID_ADAPTER)


def _user_id(value: object) -> int:
    return _validate_id(value, _USER_ID_ADAPTER)


def _validate_id(value: object, adapter: TypeAdapter[int]) -> int:
    if isinstance(value, str):
        with suppress(ValueError):
            value = int(value)
    return adapter.validate_python(value)


def _pop_chat_id(params: dict[str, object]) -> object:
    values: list[object] = []
    for key in (
        "telegram_chat_id",
        "chat_id",
        "group_id",
        "channel_id",
        "user_id",
    ):
        candidate = params.pop(key, None)
        if candidate is not None:
            values.append(candidate)
    params.pop("guild_id", None)
    params.pop("detail_type", None)
    if not values:
        msg = "Telegram action requires a chat target"
        raise ValueError(msg)
    if len({str(value) for value in values}) != 1:
        msg = "Telegram action has conflicting chat targets"
        raise ValueError(msg)
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, int | str) or not value:
        msg = "Telegram action requires a valid chat target"
        raise ValueError(msg)
    return value
