"""IM Channel abstraction layer.

Inspired by bytedance/deer-flow channels architecture.
"""

from poc.gateway.channels.base import Channel
from poc.gateway.channels.message_bus import InboundMessage, MessageBus, OutboundMessage

__all__ = ["Channel", "InboundMessage", "MessageBus", "OutboundMessage"]
