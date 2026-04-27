"""Abstract base class for IM channel adapters.

Inspired by deer-flow base.py. Every platform adapter must extend Channel
and implement start(), stop(), send().
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

from poc.gateway.channels.message_bus import (
    InboundMessage,
    MessageBus,
    MessageType,
    OutboundMessage,
)

logger = logging.getLogger(__name__)


class Channel(ABC):
    """Base class for all IM channel adapters."""

    def __init__(self, name: str, bus: MessageBus, config: dict[str, Any]) -> None:
        self.name = name
        self.bus = bus
        self.config = config
        self._running = False

    @abstractmethod
    async def start(self) -> None:
        """Begin listening for messages from the external platform."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the channel."""
        ...

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Send a response back to the platform."""
        ...

    async def send_file(self, msg: OutboundMessage, attachment: Any) -> bool:
        """Optional: upload a file to the platform. Returns True if supported."""
        return False

    def _make_inbound(
        self,
        chat_id: str,
        user_id: str,
        text: str,
        msg_type: MessageType = MessageType.CHAT,
        thread_ts: str | None = None,
        topic_id: str | None = None,
        session_id: str | None = None,
        files: list[dict] | None = None,
        metadata: dict | None = None,
    ) -> InboundMessage:
        """Convenience factory — pre-fills channel_name."""
        return InboundMessage(
            channel_name=self.name,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            msg_type=msg_type,
            thread_ts=thread_ts,
            topic_id=topic_id,
            session_id=session_id,
            files=files or [],
            metadata=metadata or {},
        )

    async def _on_outbound(self, msg: OutboundMessage) -> None:
        """Outbound callback registered with the bus. Filters by channel_name."""
        if msg.channel_name != self.name:
            return

        try:
            await self.send(msg)
        except Exception:
            logger.exception("Failed to send message on channel %s", self.name)
            return

        # Send attachments
        for attachment in msg.attachments:
            try:
                await self.send_file(msg, attachment)
            except Exception:
                logger.exception("Failed to send file on channel %s", self.name)

    @property
    def is_running(self) -> bool:
        return self._running
