"""Message bus — async pub/sub hub + unified message dataclasses.

Inspired by deer-flow message_bus.py. All IM platform messages are normalized
into InboundMessage/OutboundMessage and routed through the MessageBus.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class MessageType(str, Enum):
    CHAT = "chat"
    COMMAND = "command"


@dataclass
class InboundMessage:
    """Normalized inbound message from any IM channel."""

    channel_name: str           # "web", "slack", "line", "teams"
    chat_id: str                # platform conversation ID
    user_id: str                # platform user ID
    text: str                   # message content
    msg_type: MessageType = MessageType.CHAT
    thread_ts: str | None = None    # platform thread ID (for threaded replies)
    topic_id: str | None = None     # maps to workspace session
    session_id: str | None = None   # explicit session ID (web channel)
    files: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())


@dataclass
class ResolvedAttachment:
    """A file resolved and ready for upload back to the IM platform."""

    filename: str
    content: bytes
    mime_type: str = "application/octet-stream"
    size: int = 0
    is_image: bool = False


@dataclass
class OutboundMessage:
    """Normalized outbound message to an IM channel."""

    channel_name: str           # target channel for routing
    chat_id: str                # target conversation
    text: str                   # response text
    workspace_id: str = ""
    session_id: str = ""
    is_final: bool = True
    attachments: list[ResolvedAttachment] = field(default_factory=list)
    thread_ts: str | None = None    # platform thread ID
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())


# Type alias for outbound callbacks
OutboundCallback = Callable[[OutboundMessage], Coroutine[Any, Any, None]]


class MessageBus:
    """Async pub/sub hub decoupling channel adapters from the dispatcher.

    - Inbound: channels publish → asyncio.Queue → manager consumes
    - Outbound: manager publishes → fan out to registered callbacks
    """

    def __init__(self, max_queue_size: int = 1000):
        self._inbound_queue: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=max_queue_size)
        self._outbound_callbacks: list[OutboundCallback] = []

    # ── Inbound ───────────────────────────────────────────────────

    async def publish_inbound(self, msg: InboundMessage) -> None:
        await self._inbound_queue.put(msg)
        logger.debug(
            "Inbound published: channel=%s chat_id=%s user=%s",
            msg.channel_name, msg.chat_id, msg.user_id,
        )

    async def get_inbound(self) -> InboundMessage:
        return await self._inbound_queue.get()

    @property
    def inbound_pending(self) -> int:
        return self._inbound_queue.qsize()

    # ── Outbound ──────────────────────────────────────────────────

    def subscribe_outbound(self, callback: OutboundCallback) -> None:
        self._outbound_callbacks.append(callback)

    def unsubscribe_outbound(self, callback: OutboundCallback) -> None:
        self._outbound_callbacks = [cb for cb in self._outbound_callbacks if cb is not callback]

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        logger.debug(
            "Outbound published: channel=%s chat_id=%s final=%s",
            msg.channel_name, msg.chat_id, msg.is_final,
        )
        for callback in self._outbound_callbacks:
            try:
                await callback(msg)
            except Exception:
                logger.exception("Outbound callback error for channel=%s", msg.channel_name)
