from asyncio import (
    Event,
    Lock,
    timeout,
)
from base64 import b64encode
from collections.abc import Mapping
from contextlib import suppress
from hashlib import sha256
from http import HTTPStatus
from pathlib import PurePosixPath
from typing import Annotated, Literal, Never, Self
from urllib.parse import quote

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    RootModel,
    SecretStr,
    StrictBool,
    StrictBytes,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
    model_validator,
)
from urllib3_future import AsyncHTTPResponse, AsyncPoolManager
from urllib3_future.exceptions import HTTPError
from urllib3_future.filepost import encode_multipart_formdata

from bot.json import dumpb, loads
from bot.protocol.base import Model

from .base import run_while_open, validate_https_base_url

TELEGRAM_API_BASE_URL = "https://api.telegram.org"
TELEGRAM_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

TELEGRAM_METHODS = frozenset(
    """
    getUpdates setWebhook deleteWebhook getWebhookInfo getMe logOut close sendMessage
    forwardMessage forwardMessages copyMessage copyMessages sendPhoto sendLivePhoto
    sendAudio sendDocument sendVideo sendAnimation sendVoice sendVideoNote
    sendPaidMedia sendMediaGroup sendLocation sendVenue sendContact sendPoll
    sendChecklist sendDice sendMessageDraft sendChatAction setMessageReaction
    getUserProfilePhotos getUserProfileAudios setUserEmojiStatus getFile banChatMember
    unbanChatMember restrictChatMember promoteChatMember
    setChatAdministratorCustomTitle setChatMemberTag banChatSenderChat
    unbanChatSenderChat setChatPermissions exportChatInviteLink createChatInviteLink
    editChatInviteLink createChatSubscriptionInviteLink editChatSubscriptionInviteLink
    revokeChatInviteLink approveChatJoinRequest declineChatJoinRequest
    answerChatJoinRequestQuery sendChatJoinRequestWebApp setChatPhoto deleteChatPhoto
    setChatTitle setChatDescription pinChatMessage unpinChatMessage
    unpinAllChatMessages leaveChat getChat getChatAdministrators getChatMemberCount
    getChatMember getUserPersonalChatMessages setChatStickerSet deleteChatStickerSet
    getForumTopicIconStickers createForumTopic editForumTopic closeForumTopic
    reopenForumTopic deleteForumTopic unpinAllForumTopicMessages editGeneralForumTopic
    closeGeneralForumTopic reopenGeneralForumTopic hideGeneralForumTopic
    unhideGeneralForumTopic unpinAllGeneralForumTopicMessages answerCallbackQuery
    answerGuestQuery getUserChatBoosts getBusinessConnection getManagedBotToken
    replaceManagedBotToken getManagedBotAccessSettings setManagedBotAccessSettings
    setMyCommands deleteMyCommands getMyCommands setMyName getMyName setMyDescription
    getMyDescription setMyShortDescription getMyShortDescription setMyProfilePhoto
    removeMyProfilePhoto setChatMenuButton getChatMenuButton
    setMyDefaultAdministratorRights getMyDefaultAdministratorRights getAvailableGifts
    sendGift giftPremiumSubscription verifyUser verifyChat removeUserVerification
    removeChatVerification readBusinessMessage deleteBusinessMessages
    setBusinessAccountName setBusinessAccountUsername setBusinessAccountBio
    setBusinessAccountProfilePhoto removeBusinessAccountProfilePhoto
    setBusinessAccountGiftSettings getBusinessAccountStarBalance
    transferBusinessAccountStars getBusinessAccountGifts getUserGifts getChatGifts
    convertGiftToStars upgradeGift transferGift postStory repostStory editStory
    deleteStory answerWebAppQuery savePreparedInlineMessage savePreparedKeyboardButton
    editMessageText editMessageCaption editMessageMedia editMessageLiveLocation
    stopMessageLiveLocation editMessageChecklist editMessageReplyMarkup stopPoll
    editEphemeralMessageText editEphemeralMessageMedia editEphemeralMessageCaption
    editEphemeralMessageReplyMarkup approveSuggestedPost declineSuggestedPost
    deleteMessage deleteMessages deleteEphemeralMessage deleteMessageReaction
    deleteAllMessageReactions sendSticker getStickerSet getCustomEmojiStickers
    uploadStickerFile createNewStickerSet addStickerToSet setStickerPositionInSet
    deleteStickerFromSet replaceStickerInSet setStickerEmojiList setStickerKeywords
    setStickerMaskPosition setStickerSetTitle setStickerSetThumbnail
    setCustomEmojiStickerSetThumbnail deleteStickerSet sendRichMessage
    sendRichMessageDraft answerInlineQuery sendInvoice createInvoiceLink
    answerShippingQuery answerPreCheckoutQuery getMyStarBalance getStarTransactions
    refundStarPayment editUserStarSubscription setPassportDataErrors sendGame
    setGameScore getGameHighScores
    """.split()  # ruff: ignore[split-static-string] - compact official manifest
)

TELEGRAM_UPDATE_TYPES = tuple(
    """
    message edited_message channel_post edited_channel_post business_connection
    business_message edited_business_message deleted_business_messages guest_message
    message_reaction message_reaction_count inline_query chosen_inline_result
    callback_query shipping_query pre_checkout_query purchased_paid_media poll
    poll_answer my_chat_member chat_member chat_join_request chat_boost
    removed_chat_boost managed_bot subscription
    """.split()  # ruff: ignore[split-static-string] - compact official manifest
)

TELEGRAM_SERVICE_MESSAGE_TYPES = tuple(
    """
    new_chat_members left_chat_member chat_owner_left chat_owner_changed
    new_chat_title new_chat_photo delete_chat_photo group_chat_created
    supergroup_chat_created channel_chat_created message_auto_delete_timer_changed
    migrate_to_chat_id migrate_from_chat_id pinned_message successful_payment
    refunded_payment users_shared chat_shared gift unique_gift gift_upgrade_sent
    connected_website write_access_allowed proximity_alert_triggered boost_added
    chat_background_set checklist_tasks_done checklist_tasks_added
    community_chat_added community_chat_removed direct_message_price_changed
    forum_topic_created forum_topic_edited forum_topic_closed forum_topic_reopened
    general_forum_topic_hidden general_forum_topic_unhidden giveaway_created
    giveaway_completed managed_bot_created paid_message_price_changed
    poll_option_added poll_option_deleted suggested_post_approved
    suggested_post_approval_failed suggested_post_declined suggested_post_paid
    suggested_post_refunded video_chat_scheduled video_chat_started video_chat_ended
    video_chat_participants_invited web_app_data
    """.split()  # ruff: ignore[split-static-string] - compact official manifest
)

_MAX_INT32 = 2**31 - 1
_MAX_INT64 = 2**63 - 1
_MAX_TELEGRAM_USER_ID = 0xFF_FFFF_FFFF
_MIN_TELEGRAM_CHAT_ID = -4_000_000_000_000


def _nonzero_id(value: int) -> int:
    if value == 0:
        msg = "Telegram chat IDs cannot be zero"
        raise ValueError(msg)
    return value


type TelegramChatID = Annotated[
    StrictInt,
    Field(ge=_MIN_TELEGRAM_CHAT_ID, le=_MAX_TELEGRAM_USER_ID),
    AfterValidator(_nonzero_id),
]
type TelegramUserID = Annotated[
    StrictInt,
    Field(gt=0, le=_MAX_TELEGRAM_USER_ID),
]
type TelegramUpdateOffset = Annotated[
    StrictInt,
    Field(ge=-_MAX_INT32 - 1, le=_MAX_INT32),
]
type TelegramUpdateID = Annotated[StrictInt, Field(gt=0, le=_MAX_INT32)]
type NonNegativeInt = Annotated[StrictInt, Field(ge=0, le=_MAX_INT64)]
type PositiveInt = Annotated[StrictInt, Field(gt=0, le=_MAX_INT64)]
type PositiveSeconds = Annotated[
    StrictInt | StrictFloat,
    Field(gt=0, allow_inf_nan=False),
]
type Latitude = Annotated[
    StrictInt | StrictFloat,
    Field(ge=-90, le=90, allow_inf_nan=False),
]
type Longitude = Annotated[
    StrictInt | StrictFloat,
    Field(ge=-180, le=180, allow_inf_nan=False),
]
type Heading = Annotated[StrictInt, Field(ge=1, le=360)]
type ProximityAlertRadius = Annotated[StrictInt, Field(ge=1, le=100_000)]
type TelegramFileData = Annotated[
    StrictBytes,
    PlainSerializer(
        lambda value: b64encode(value).decode(),
        return_type=str,
        when_used="json",
    ),
]


class TelegramUpload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    data: StrictBytes = Field(repr=False)
    filename: Annotated[
        StrictStr,
        Field(min_length=1, max_length=255, pattern=r"^[^/\\\r\n]+$"),
    ]
    content_type: Annotated[
        StrictStr,
        Field(pattern=r"^[^\s/;]+/[^\s;]+$"),
    ] = "application/octet-stream"


class TelegramDownloadedFile(Model):
    name: Annotated[StrictStr, Field(min_length=1)]
    data: TelegramFileData = Field(repr=False)
    sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class TelegramFileTooLargeError(ValueError):
    pass


type TelegramParams = dict[
    Annotated[StrictStr, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")],
    JsonValue,
]
type TelegramObject = dict[StrictStr, JsonValue]
type TelegramFiles = dict[
    Annotated[StrictStr, Field(min_length=1, pattern=r"^[A-Za-z0-9_]+$")],
    StrictBytes | TelegramUpload,
]

_STRICT_CONFIG = ConfigDict(
    allow_inf_nan=False,
    hide_input_in_errors=True,
)
_PARAMS_ADAPTER = TypeAdapter(TelegramParams, config=_STRICT_CONFIG)
_FILES_ADAPTER = TypeAdapter(TelegramFiles, config=_STRICT_CONFIG)
_REQUEST_TIMEOUT_ADAPTER = TypeAdapter(PositiveSeconds, config=_STRICT_CONFIG)
_OFFSET_ADAPTER = TypeAdapter(TelegramUpdateOffset | None, config=_STRICT_CONFIG)
_POLL_TIMEOUT_ADAPTER = TypeAdapter(NonNegativeInt, config=_STRICT_CONFIG)
_NON_NEGATIVE_INT_ADAPTER = TypeAdapter(NonNegativeInt, config=_STRICT_CONFIG)
_POSITIVE_INT_ADAPTER = TypeAdapter(PositiveInt, config=_STRICT_CONFIG)
_METHODS_BY_CASE = {method.casefold(): method for method in TELEGRAM_METHODS}
_RAW_UPDATES_ADAPTER = TypeAdapter(list[TelegramObject], config=_STRICT_CONFIG)


class TelegramUser(Model):
    id: TelegramUserID
    is_bot: StrictBool
    first_name: StrictStr
    last_name: StrictStr | None = None
    username: StrictStr | None = None
    language_code: StrictStr | None = None
    is_premium: Literal[True] | None = None
    added_to_attachment_menu: Literal[True] | None = None
    can_join_groups: StrictBool | None = None
    can_read_all_group_messages: StrictBool | None = None
    supports_guest_queries: StrictBool | None = None
    supports_inline_queries: StrictBool | None = None
    can_connect_to_business: StrictBool | None = None
    has_main_web_app: StrictBool | None = None
    has_topics_enabled: StrictBool | None = None
    allows_users_to_create_topics: StrictBool | None = None
    can_manage_bots: StrictBool | None = None
    supports_join_request_queries: StrictBool | None = None


class TelegramChat(Model):
    id: TelegramChatID
    type: Literal["private", "group", "supergroup", "channel"]
    title: StrictStr | None = None
    username: StrictStr | None = None
    first_name: StrictStr | None = None
    last_name: StrictStr | None = None
    is_forum: Literal[True] | None = None
    is_direct_messages: Literal[True] | None = None

    @model_validator(mode="after")
    def id_matches_type(self) -> Self:
        if self.is_direct_messages and self.type != "supergroup":
            msg = "Telegram direct-message chats must be supergroups"
            raise ValueError(msg)
        ranges = {
            "private": ((1, _MAX_TELEGRAM_USER_ID),),
            "group": ((-999_999_999_999, -1),),
            "supergroup": (
                (-1_997_852_516_352, -1_000_000_000_001),
                (_MIN_TELEGRAM_CHAT_ID, -2_002_147_483_649),
            ),
            "channel": (
                (-1_997_852_516_352, -1_000_000_000_001),
                (_MIN_TELEGRAM_CHAT_ID, -2_002_147_483_649),
            ),
        }[self.type]
        if not any(start <= self.id <= end for start, end in ranges):
            msg = f"Telegram {self.type} chat ID is outside its official range"
            raise ValueError(msg)
        return self


class TelegramDirectMessagesTopic(Model):
    topic_id: PositiveInt
    user: TelegramUser | None = None


class TelegramFile(Model):
    file_id: StrictStr
    file_unique_id: StrictStr
    file_size: NonNegativeInt | None = None
    width: NonNegativeInt | None = None
    height: NonNegativeInt | None = None
    duration: NonNegativeInt | None = None
    file_name: StrictStr | None = None
    mime_type: StrictStr | None = None
    file_path: StrictStr | None = None


class TelegramLocation(Model):
    latitude: Latitude
    longitude: Longitude
    horizontal_accuracy: (
        Annotated[
            StrictInt | StrictFloat,
            Field(ge=0, le=1500, allow_inf_nan=False),
        ]
        | None
    ) = None
    live_period: NonNegativeInt | None = None
    heading: Heading | None = None
    proximity_alert_radius: ProximityAlertRadius | None = None

    @model_validator(mode="after")
    def live_fields_require_live_period(self) -> Self:
        if self.live_period is None and (
            self.heading is not None or self.proximity_alert_radius is not None
        ):
            msg = "Telegram live-location fields require live_period"
            raise ValueError(msg)
        return self


class TelegramVenue(Model):
    location: TelegramLocation
    title: StrictStr
    address: StrictStr
    foursquare_id: StrictStr | None = None
    foursquare_type: StrictStr | None = None
    google_place_id: StrictStr | None = None
    google_place_type: StrictStr | None = None

    @model_validator(mode="after")
    def location_is_not_live(self) -> Self:
        if any(
            value is not None
            for value in (
                self.location.live_period,
                self.location.heading,
                self.location.proximity_alert_radius,
            )
        ):
            msg = "Telegram venue locations cannot be live"
            raise ValueError(msg)
        return self


class TelegramMessageEntity(Model):
    type: StrictStr
    offset: NonNegativeInt
    length: PositiveInt
    url: StrictStr | None = None
    user: TelegramUser | None = None
    language: StrictStr | None = None
    custom_emoji_id: StrictStr | None = None
    unix_time: StrictInt | None = None

    @model_validator(mode="after")
    def text_mention_user(self) -> Self:
        if self.type == "text_mention" and self.user is None:
            msg = "Telegram text_mention entity requires user"
            raise ValueError(msg)
        return self


class TelegramMessage(Model):
    message_id: NonNegativeInt
    message_thread_id: PositiveInt | None = None
    direct_messages_topic: TelegramDirectMessagesTopic | None = None
    from_: TelegramUser | None = Field(alias="from", default=None)
    sender_chat: TelegramChat | None = None
    sender_business_bot: TelegramUser | None = None
    receiver_user: TelegramUser | None = None
    ephemeral_message_id: NonNegativeInt | None = None
    date: PositiveInt
    guest_query_id: StrictStr | None = None
    business_connection_id: StrictStr | None = None
    chat: TelegramChat
    reply_to_message: TelegramMessage | None = None
    via_bot: TelegramUser | None = None
    edit_date: PositiveInt | None = None
    text: StrictStr | None = None
    entities: list[TelegramMessageEntity] | None = None
    animation: TelegramFile | None = None
    audio: TelegramFile | None = None
    document: TelegramFile | None = None
    photo: list[TelegramFile] | None = None
    sticker: TelegramFile | None = None
    video: TelegramFile | None = None
    video_note: TelegramFile | None = None
    voice: TelegramFile | None = None
    caption: StrictStr | None = None
    caption_entities: list[TelegramMessageEntity] | None = None
    venue: TelegramVenue | None = None
    location: TelegramLocation | None = None
    new_chat_members: list[TelegramUser] | None = None
    left_chat_member: TelegramUser | None = None

    @property
    def service_type(self) -> str | None:
        return next(
            (
                name
                for name in TELEGRAM_SERVICE_MESSAGE_TYPES
                if getattr(self, name, None) is not None
            ),
            None,
        )


class TelegramInaccessibleMessage(Model):
    chat: TelegramChat
    message_id: NonNegativeInt
    date: Literal[0]


class TelegramCallbackQuery(Model):
    id: StrictStr
    from_: TelegramUser = Field(alias="from")
    message: TelegramMessage | TelegramInaccessibleMessage | None = None
    inline_message_id: StrictStr | None = None
    chat_instance: StrictStr
    data: StrictStr | None = None
    game_short_name: StrictStr | None = None

    @model_validator(mode="after")
    def one_payload(self) -> Self:
        if (self.data is None) == (self.game_short_name is None):
            msg = "Telegram callback query requires exactly one payload"
            raise ValueError(msg)
        if (self.message is None) == (self.inline_message_id is None):
            msg = "Telegram callback query requires exactly one message reference"
            raise ValueError(msg)
        return self


class TelegramPollAnswer(Model):
    poll_id: StrictStr
    voter_chat: TelegramChat | None = None
    user: TelegramUser | None = None
    option_ids: list[NonNegativeInt]
    option_persistent_ids: list[StrictStr]

    @model_validator(mode="after")
    def exactly_one_voter(self) -> Self:
        if (self.voter_chat is None) == (self.user is None):
            msg = "Telegram poll answer requires exactly one voter"
            raise ValueError(msg)
        return self


class TelegramChatMemberOwner(Model):
    status: Literal["creator"]
    user: TelegramUser
    is_anonymous: StrictBool
    custom_title: StrictStr | None = None


class TelegramChatMemberAdministrator(Model):
    status: Literal["administrator"]
    user: TelegramUser
    can_be_edited: StrictBool
    is_anonymous: StrictBool
    can_manage_chat: StrictBool
    can_delete_messages: StrictBool
    can_manage_video_chats: StrictBool
    can_restrict_members: StrictBool
    can_promote_members: StrictBool
    can_change_info: StrictBool
    can_invite_users: StrictBool
    can_post_stories: StrictBool
    can_edit_stories: StrictBool
    can_delete_stories: StrictBool
    can_post_messages: StrictBool | None = None
    can_edit_messages: StrictBool | None = None
    can_pin_messages: StrictBool | None = None
    can_manage_topics: StrictBool | None = None
    can_manage_direct_messages: StrictBool | None = None
    can_manage_tags: StrictBool | None = None
    custom_title: StrictStr | None = None


class TelegramChatMemberMember(Model):
    status: Literal["member"]
    user: TelegramUser
    until_date: NonNegativeInt | None = None
    tag: StrictStr | None = None


class TelegramChatMemberRestricted(Model):
    status: Literal["restricted"]
    user: TelegramUser
    is_member: StrictBool
    can_send_messages: StrictBool
    can_send_audios: StrictBool
    can_send_documents: StrictBool
    can_send_photos: StrictBool
    can_send_videos: StrictBool
    can_send_video_notes: StrictBool
    can_send_voice_notes: StrictBool
    can_send_polls: StrictBool
    can_send_other_messages: StrictBool
    can_add_web_page_previews: StrictBool
    can_change_info: StrictBool
    can_invite_users: StrictBool
    can_pin_messages: StrictBool
    can_manage_topics: StrictBool
    can_react_to_messages: StrictBool
    can_edit_tag: StrictBool
    until_date: NonNegativeInt
    tag: StrictStr | None = None


class TelegramChatMemberLeft(Model):
    status: Literal["left"]
    user: TelegramUser


class TelegramChatMemberBanned(Model):
    status: Literal["kicked"]
    user: TelegramUser
    until_date: NonNegativeInt


type TelegramChatMember = Annotated[
    TelegramChatMemberOwner
    | TelegramChatMemberAdministrator
    | TelegramChatMemberMember
    | TelegramChatMemberRestricted
    | TelegramChatMemberLeft
    | TelegramChatMemberBanned,
    Field(discriminator="status"),
]


class TelegramChatMemberUpdated(Model):
    chat: TelegramChat
    from_: TelegramUser = Field(alias="from")
    date: PositiveInt
    old_chat_member: TelegramChatMember
    new_chat_member: TelegramChatMember
    invite_link: JsonValue = None
    via_join_request: StrictBool | None = None
    via_chat_folder_invite_link: StrictBool | None = None


class TelegramChatJoinRequest(Model):
    chat: TelegramChat
    from_: TelegramUser = Field(alias="from")
    user_chat_id: TelegramUserID
    date: PositiveInt
    bio: StrictStr | None = None
    invite_link: JsonValue = None
    query_id: StrictStr | None = None


class TelegramUpdate(Model):
    update_id: TelegramUpdateID
    message: TelegramMessage | None = None
    edited_message: TelegramMessage | None = None
    channel_post: TelegramMessage | None = None
    edited_channel_post: TelegramMessage | None = None
    business_connection: TelegramObject | None = None
    business_message: TelegramMessage | None = None
    edited_business_message: TelegramMessage | None = None
    deleted_business_messages: TelegramObject | None = None
    guest_message: TelegramMessage | None = None
    message_reaction: TelegramObject | None = None
    message_reaction_count: TelegramObject | None = None
    inline_query: TelegramObject | None = None
    chosen_inline_result: TelegramObject | None = None
    callback_query: TelegramCallbackQuery | None = None
    shipping_query: TelegramObject | None = None
    pre_checkout_query: TelegramObject | None = None
    purchased_paid_media: TelegramObject | None = None
    poll: TelegramObject | None = None
    poll_answer: TelegramPollAnswer | None = None
    my_chat_member: TelegramChatMemberUpdated | None = None
    chat_member: TelegramChatMemberUpdated | None = None
    chat_join_request: TelegramChatJoinRequest | None = None
    chat_boost: TelegramObject | None = None
    removed_chat_boost: TelegramObject | None = None
    managed_bot: TelegramObject | None = None
    subscription: TelegramObject | None = None

    @model_validator(mode="after")
    def at_most_one_payload(self) -> Self:
        if (
            sum(getattr(self, name) is not None for name in TELEGRAM_UPDATE_TYPES)
            + len(self.model_extra or {})
            > 1
        ):
            msg = "Telegram update accepts at most one payload"
            raise ValueError(msg)
        return self

    @property
    def payload(self) -> tuple[str, BaseModel | JsonValue] | None:
        for name in TELEGRAM_UPDATE_TYPES:
            value = getattr(self, name)
            if value is not None:
                return name, value
        return next(iter((self.model_extra or {}).items()), None)

    @property
    def raw(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json", exclude_none=True)


class TelegramWebhookInfo(Model):
    url: StrictStr
    has_custom_certificate: StrictBool
    pending_update_count: NonNegativeInt
    ip_address: StrictStr | None = None
    last_error_date: PositiveInt | None = None
    last_error_message: StrictStr | None = None
    last_synchronization_error_date: PositiveInt | None = None
    max_connections: PositiveInt | None = None
    allowed_updates: list[StrictStr] | None = None


class TelegramResponseParameters(Model):
    migrate_to_chat_id: TelegramChatID | None = None
    retry_after: NonNegativeInt | None = None


class TelegramEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: StrictBool
    result: JsonValue = None
    description: StrictStr | None = None
    error_code: StrictInt | None = None
    parameters: TelegramResponseParameters | None = None

    @model_validator(mode="after")
    def valid_variant(self) -> Self:
        if self.ok:
            if "result" not in self.model_fields_set:
                msg = "successful Telegram response requires result"
                raise ValueError(msg)
            if self.model_fields_set & {"description", "error_code", "parameters"}:
                msg = "successful Telegram response cannot contain error fields"
                raise ValueError(msg)
        else:
            if self.description is None or self.error_code is None:
                msg = "failed Telegram response requires error_code and description"
                raise ValueError(msg)
            if "result" in self.model_fields_set:
                msg = "failed Telegram response cannot contain result"
                raise ValueError(msg)
        return self


class TelegramResult(RootModel[JsonValue]):
    def __repr_args__(  # ruff: ignore[bad-dunder-method-name] - Pydantic's repr/str hook
        self,
    ) -> list[tuple[str | None, object]]:
        return []


class TelegramAPIError(RuntimeError):
    def __init__(
        self,
        status: int,
        error_code: int,
        description: str,
        parameters: TelegramResponseParameters | None = None,
    ) -> None:
        self.status = status
        self.error_code = error_code
        self.description = description
        self.parameters = parameters
        super().__init__(
            f"Telegram API request failed with code {error_code}: {description}"
        )


class TelegramRestClient:
    def __init__(
        self,
        token: SecretStr | str,
        *,
        base_url: str = TELEGRAM_API_BASE_URL,
        http_pool: AsyncPoolManager | None = None,
        request_timeout: float = 30.0,
        max_rate_limit_retries: int = 2,
        max_retry_after: int = 30,
    ) -> None:
        value = token.get_secret_value() if isinstance(token, SecretStr) else token
        if (
            not isinstance(value, str)
            or not value
            or any(
                character.isspace()
                or character in "/\\?%#"
                or not character.isprintable()
                for character in value
            )
        ):
            msg = "invalid Telegram bot token"
            raise ValueError(msg)
        request_timeout = _REQUEST_TIMEOUT_ADAPTER.validate_python(request_timeout)
        max_rate_limit_retries = _NON_NEGATIVE_INT_ADAPTER.validate_python(
            max_rate_limit_retries
        )
        max_retry_after = _NON_NEGATIVE_INT_ADAPTER.validate_python(max_retry_after)
        self.token = SecretStr(value)
        self.base_url = validate_https_base_url(base_url, "Telegram")
        self.http_pool = http_pool if http_pool is not None else AsyncPoolManager()
        self._owns_http_pool = http_pool is None
        self._pool_closed = False
        self.request_timeout = float(request_timeout)
        self.max_rate_limit_retries = max_rate_limit_retries
        self.max_retry_after = max_retry_after
        self._lifecycle_lock = Lock()
        self._closed = False
        self._closed_event = Event()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if not self._closed:
                return
            if self._owns_http_pool:
                if not self._pool_closed:
                    await self.http_pool.clear()
                self.http_pool = AsyncPoolManager()
                self._pool_closed = False
            self._closed_event = Event()
            self._closed = False

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._closed and (not self._owns_http_pool or self._pool_closed):
                return
            self._closed = True
            self._closed_event.set()
            if self._owns_http_pool:
                await self.http_pool.clear()
                self._pool_closed = True

    async def call(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        files: Mapping[str, bytes | TelegramUpload] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> TelegramResult:
        result = await self.call_json(
            method,
            params,
            files,
            request_timeout=request_timeout,
        )
        return TelegramResult(result)

    async def call_json(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        files: Mapping[str, bytes | TelegramUpload] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JsonValue:
        closed_event = self._closed_event
        self._ensure_open(closed_event)
        canonical = _METHODS_BY_CASE.get(method.casefold())
        if canonical is None:
            msg = f"unsupported Telegram method: {method}"
            raise LookupError(msg)
        try:
            validated_params = _PARAMS_ADAPTER.validate_python(
                {} if params is None else params
            )
            validated_files = _FILES_ADAPTER.validate_python(
                {} if files is None else files
            )
            timeout_seconds = (
                self.request_timeout
                if request_timeout is None
                else float(_REQUEST_TIMEOUT_ADAPTER.validate_python(request_timeout))
            )
        except ValueError as exc:
            msg = "invalid Telegram method parameters"
            raise ValueError(msg) from exc
        retries = 0
        while True:
            self._ensure_open(closed_event)
            envelope, status = await run_while_open(
                self._request(
                    canonical,
                    validated_params,
                    validated_files,
                    timeout_seconds,
                ),
                closed_event,
                self._ensure_open,
            )
            if envelope.ok and HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
                return envelope.result
            error_code = envelope.error_code or status
            description = envelope.description or "HTTP request failed"
            retry_after = (
                envelope.parameters.retry_after
                if envelope.parameters is not None
                else None
            )
            if (
                retry_after is not None
                and retry_after <= self.max_retry_after
                and retries < self.max_rate_limit_retries
            ):
                retries += 1
                with suppress(TimeoutError):
                    async with timeout(retry_after):
                        await closed_event.wait()
                self._ensure_open(closed_event)
                continue
            raise TelegramAPIError(
                status,
                error_code,
                description,
                envelope.parameters,
            )

    async def get_me(self) -> TelegramUser:
        return TelegramUser.model_validate(await self.call_json("getMe"))

    async def get_webhook_info(self) -> TelegramWebhookInfo:
        return TelegramWebhookInfo.model_validate(
            await self.call_json("getWebhookInfo")
        )

    async def get_updates(
        self,
        *,
        offset: int | None,
        poll_timeout: int,
    ) -> list[TelegramUpdate]:
        offset = _OFFSET_ADAPTER.validate_python(offset)
        poll_timeout = _POLL_TIMEOUT_ADAPTER.validate_python(poll_timeout)
        params: dict[str, JsonValue] = {
            "timeout": poll_timeout,
            "limit": 100,
            "allowed_updates": list(TELEGRAM_UPDATE_TYPES),
        }
        if offset is not None:
            params["offset"] = offset
        result = await self.call_json(
            "getUpdates",
            params,
            request_timeout=max(self.request_timeout, poll_timeout + 10),
        )
        raw_updates = _RAW_UPDATES_ADAPTER.validate_python(result)
        return [TelegramUpdate.model_validate(update) for update in raw_updates]

    async def get_file(self, file_id: str) -> TelegramFile:
        return TelegramFile.model_validate(
            await self.call_json("getFile", {"file_id": file_id})
        )

    async def download_file(
        self,
        file_id: str,
        *,
        max_bytes: int = TELEGRAM_MAX_DOWNLOAD_BYTES,
    ) -> TelegramDownloadedFile:
        closed_event = self._closed_event
        self._ensure_open(closed_event)
        max_bytes = _POSITIVE_INT_ADAPTER.validate_python(max_bytes)
        file = await self.get_file(file_id)
        if file.file_path is None:
            msg = "Telegram getFile response has no file_path"
            raise RuntimeError(msg)
        path = _download_path(file.file_path)
        if file.file_size is not None and file.file_size > max_bytes:
            _raise_file_too_large(max_bytes)

        url = (
            f"{self.base_url}/file/bot{self.token.get_secret_value()}/"
            f"{quote(path, safe='/')}"
        )
        self._ensure_open(closed_event)
        data, checksum = await run_while_open(
            _download_response(
                self.http_pool,
                url,
                max_bytes=max_bytes,
                request_timeout=self.request_timeout,
            ),
            closed_event,
            self._ensure_open,
        )

        return TelegramDownloadedFile(
            name=PurePosixPath(path).name,
            data=data,
            sha256=checksum,
        )

    def _ensure_open(self, closed_event: Event) -> None:
        if (
            self._closed
            or closed_event is not self._closed_event
            or closed_event.is_set()
        ):
            msg = "Telegram REST client is closed"
            raise RuntimeError(msg)

    async def _request(
        self,
        method: str,
        params: TelegramParams,
        files: TelegramFiles,
        request_timeout: float,
    ) -> tuple[TelegramEnvelope, int]:
        url = f"{self.base_url}/bot{self.token.get_secret_value()}/{method}"
        if files:
            fields: list[tuple[str, str | bytes | tuple[str, str | bytes, str]]] = [
                (name, _form_value(value)) for name, value in params.items()
            ]
            fields.extend(
                (
                    name,
                    (name, file, "application/octet-stream")
                    if isinstance(file, bytes)
                    else (file.filename, file.data, file.content_type),
                )
                for name, file in files.items()
            )
            body, content_type = encode_multipart_formdata(fields)
        try:  # ruff: ignore[too-many-statements-in-try-clause] - one timeout owns the full response read
            async with timeout(request_timeout):
                if files:
                    response = await self.http_pool.request(
                        "POST",
                        url,
                        body=body,
                        headers={"Content-Type": content_type},
                        retries=False,
                        timeout=request_timeout,
                    )
                else:
                    response = await self.http_pool.request(
                        "POST",
                        url,
                        json=params,
                        retries=False,
                        timeout=request_timeout,
                    )
                data = await response.data
        except HTTPError, TimeoutError:
            msg = f"Telegram API request failed for {method}"
            raise ConnectionError(msg) from None
        try:
            payload = loads(data)
            envelope = TelegramEnvelope.model_validate(payload)
        except ValueError:
            msg = f"Telegram API returned an invalid response for {method}"
            if response.status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                raise ConnectionError(msg) from None
            raise RuntimeError(msg) from None
        return envelope, response.status


def _form_value(value: JsonValue) -> str:
    return value if isinstance(value, str) else dumpb(value).decode()


def _download_path(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or value.startswith(("/", "\\"))
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
    ):
        msg = "Telegram file_path must be a relative path"
        raise ValueError(msg)
    return value


async def _download_response(
    http_pool: AsyncPoolManager,
    url: str,
    *,
    max_bytes: int,
    request_timeout: float,
) -> tuple[bytes, str]:
    response: AsyncHTTPResponse | None = None
    try:
        async with timeout(request_timeout):
            response = await http_pool.request(
                "GET",
                url,
                headers={"Accept-Encoding": "identity"},
                retries=False,
                timeout=request_timeout,
                preload_content=False,
            )
            _validate_download_response(response, max_bytes)
            return await _read_download(response, max_bytes)
    except TelegramFileTooLargeError:
        raise
    except HTTPError, TimeoutError:
        msg = "Telegram file download failed"
        raise ConnectionError(msg) from None
    finally:
        if response is not None:
            with suppress(Exception):
                await response.close()


def _validate_download_response(
    response: AsyncHTTPResponse,
    max_bytes: int,
) -> None:
    if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
        msg = "Telegram file download failed"
        raise ConnectionError(msg)
    content_length = response.headers.get("Content-Length")
    if content_length is None:
        return
    try:
        declared_size = int(content_length)
    except ValueError:
        declared_size = -1
    if declared_size < 0:
        msg = "Telegram file download returned invalid metadata"
        raise ConnectionError(msg) from None
    if declared_size > max_bytes:
        _raise_file_too_large(max_bytes)


async def _read_download(
    response: AsyncHTTPResponse,
    max_bytes: int,
) -> tuple[bytes, str]:
    chunks: list[bytes] = []
    size = 0
    digest = sha256()
    async for chunk in response.stream(64 * 1024, decode_content=False):
        size += len(chunk)
        if size > max_bytes:
            _raise_file_too_large(max_bytes)
        digest.update(chunk)
        chunks.append(chunk)
    return b"".join(chunks), digest.hexdigest()


def _raise_file_too_large(max_bytes: int) -> Never:
    msg = f"Telegram file exceeds the {max_bytes}-byte download limit"
    raise TelegramFileTooLargeError(msg)
