"""Web channel adapter — handles HTTP-based chat from frontend.

This adapter bridges the existing Gateway HTTP endpoints to the channel
abstraction layer. Unlike Slack/LINE which use webhooks/websockets,
the web channel is request-response based — the frontend POSTs a message
and gets a response directly.

For the web channel, the MessageBus flow is:
  Frontend POST → WebChannel.handle_request() → bus.publish_inbound()
  → ChannelManager._handle_chat() → Agent Pod → bus.publish_outbound()
  → WebChannel collects response via asyncio.Future → return to frontend
"""

import asyncio
import logging
from typing import Any

from poc.gateway.channels.base import Channel
from poc.gateway.channels.message_bus import (
    MessageBus,
    MessageType,
    OutboundMessage,
)

logger = logging.getLogger(__name__)


class WebChannel(Channel):
    """HTTP request-response channel for web frontend."""

    def __init__(self, name: str, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name, bus, config)
        # Pending response futures: chat_id → Future[OutboundMessage]
        self._pending: dict[str, asyncio.Future[OutboundMessage]] = {}

    async def start(self) -> None:
        self.bus.subscribe_outbound(self._on_outbound)
        self._running = True
        logger.info("WebChannel started")

    async def stop(self) -> None:
        self.bus.unsubscribe_outbound(self._on_outbound)
        # Cancel all pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self._running = False
        logger.info("WebChannel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Resolve the pending future for this chat_id."""
        fut = self._pending.get(msg.chat_id)
        if fut and not fut.done():
            fut.set_result(msg)
        else:
            logger.warning("No pending request for chat_id=%s", msg.chat_id)

    async def handle_chat_request(
        self,
        user_id: str,
        message: str,
        session_id: str,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Handle a synchronous chat request from the frontend.

        Creates an InboundMessage, publishes it to the bus, and waits
        for the ChannelManager to produce an OutboundMessage.
        """
        # Use session_id as chat_id for web channel (1:1 mapping)
        chat_id = session_id

        # Create a future to wait for the response
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[OutboundMessage] = loop.create_future()
        self._pending[chat_id] = fut

        try:
            # Publish inbound message
            inbound = self._make_inbound(
                chat_id=chat_id,
                user_id=user_id,
                text=message,
                msg_type=MessageType.COMMAND if message.startswith("/") else MessageType.CHAT,
                session_id=session_id,
            )
            await self.bus.publish_inbound(inbound)

            # Wait for response
            outbound = await asyncio.wait_for(fut, timeout=timeout)
            return {
                "content": outbound.text,
                "session_id": session_id,
                "workspace_id": outbound.workspace_id,
                "tool_calls": [],
            }
        except asyncio.TimeoutError:
            logger.error("Chat request timeout for user=%s session=%s", user_id, session_id)
            return {
                "content": "請求超時，請稍後再試。",
                "session_id": session_id,
                "workspace_id": "",
                "tool_calls": [],
            }
        finally:
            self._pending.pop(chat_id, None)

    async def handle_mcp_request(
        self,
        user_id: str,
        workspace_id: str,
        method: str,
        params: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Handle MCP file operations — bypass the message bus, call agent directly.

        MCP file ops (list_files, read_file, write_file) don't need the
        channel abstraction since they're direct file operations.
        Returns the raw agent response.
        """
        from poc.gateway.channels.service import get_channel_service

        svc = get_channel_service()
        if not svc:
            return {"error": {"code": -1, "message": "Channel service not initialized"}}

        endpoint = svc.manager.get_route_table().get(workspace_id)
        if not endpoint:
            return {"error": {"code": -1, "message": f"No route for workspace {workspace_id}"}}

        import httpx
        agent_url = f"http://{endpoint}/mcp/execute"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                agent_url,
                json={
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params,
                    "id": request_id,
                },
            )
            return resp.json()
