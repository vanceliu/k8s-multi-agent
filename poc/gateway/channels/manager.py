"""Channel manager — central dispatcher bridging IM channels to Agent Pods.

Inspired by deer-flow manager.py. Consumes InboundMessages from the MessageBus,
ensures workspace via Orchestrator, forwards to Agent Pod, and publishes
OutboundMessages back through the bus.
"""

import asyncio
import logging
import uuid
from typing import Any

import httpx

from poc.gateway.channels.message_bus import (
    InboundMessage,
    MessageBus,
    MessageType,
    OutboundMessage,
)
from poc.gateway.channels.store import ChannelStore

logger = logging.getLogger(__name__)

# Known slash commands
COMMANDS = {"/new", "/status", "/help"}


class ChannelManager:
    """Central dispatcher: IM messages → ensure workspace → Agent Pod → IM response."""

    def __init__(
        self,
        bus: MessageBus,
        store: ChannelStore,
        orchestrator_url: str,
        max_concurrency: int = 5,
    ):
        self.bus = bus
        self.store = store
        self.orchestrator_url = orchestrator_url
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._dispatch_task: asyncio.Task | None = None
        self._running = False

        # In-memory route table: workspace_id → service_endpoint
        self._route_table: dict[str, str] = {}
        # Per-workspace busy lock to prevent concurrent processing
        self._workspace_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        logger.info("ChannelManager started")

    async def stop(self) -> None:
        self._running = False
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        logger.info("ChannelManager stopped")

    async def _dispatch_loop(self) -> None:
        """Continuously consume inbound messages and dispatch them."""
        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.get_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception:
                logger.exception("Dispatch loop error")
                continue

            asyncio.create_task(self._bounded_handle(msg))

    async def _bounded_handle(self, msg: InboundMessage) -> None:
        async with self._semaphore:
            try:
                await self._handle_message(msg)
            except Exception:
                logger.exception(
                    "Failed to handle message from %s:%s",
                    msg.channel_name, msg.chat_id,
                )
                # Send error response back to channel
                await self.bus.publish_outbound(OutboundMessage(
                    channel_name=msg.channel_name,
                    chat_id=msg.chat_id,
                    text="抱歉，處理訊息時發生錯誤，請稍後再試。",
                    thread_ts=msg.thread_ts,
                ))

    async def _handle_message(self, msg: InboundMessage) -> None:
        if msg.msg_type == MessageType.COMMAND:
            await self._handle_command(msg)
        else:
            await self._handle_chat(msg)

    # ── Command handling ──────────────────────────────────────────

    async def _handle_command(self, msg: InboundMessage) -> None:
        cmd = msg.text.strip().split()[0].lower()
        args = msg.text.strip().split()[1:]

        if cmd == "/new":
            self.store.remove(msg.channel_name, msg.chat_id, msg.topic_id)
            text = "已重置對話，下次訊息將建立新的 session。"

        elif cmd == "/status":
            mapping = self.store.get(msg.channel_name, msg.chat_id, msg.topic_id)
            if mapping:
                workspace_id = mapping["workspace_id"]
                try:
                    ws_info = await self._get_workspace_info(workspace_id)
                    text = (
                        f"工作區：{workspace_id}\n"
                        f"狀態：{ws_info.get('status', 'unknown')}\n"
                        f"Sessions：{len(ws_info.get('sessions', []))}"
                    )
                except Exception:
                    text = f"工作區：{workspace_id}（無法取得詳細狀態）"
            else:
                text = "目前沒有活躍的工作區，發送訊息即可自動建立。"

        elif cmd == "/help":
            text = (
                "可用指令：\n"
                "/new — 重置對話，建立新 session\n"
                "/status — 查看工作區狀態\n"
                "/help — 顯示此說明"
            )
        else:
            text = f"未知指令：{cmd}。輸入 /help 查看可用指令。"

        await self.bus.publish_outbound(OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            text=text,
            thread_ts=msg.thread_ts,
        ))

    # ── Chat handling ─────────────────────────────────────────────

    async def _handle_chat(self, msg: InboundMessage) -> None:
        # 1. Resolve or create workspace session mapping
        mapping = self.store.get(msg.channel_name, msg.chat_id, msg.topic_id)

        if not mapping:
            # Ensure workspace via Orchestrator
            session_id = msg.session_id or str(uuid.uuid4())
            workspace = await self._ensure_workspace(msg.user_id, session_id)
            workspace_id = workspace["workspace_id"]

            self.store.put(
                channel_name=msg.channel_name,
                chat_id=msg.chat_id,
                user_id=msg.user_id,
                workspace_id=workspace_id,
                session_id=session_id,
                topic_id=msg.topic_id,
            )
            mapping = self.store.get(msg.channel_name, msg.chat_id, msg.topic_id)

        workspace_id = mapping["workspace_id"]
        session_id = mapping["session_id"]

        # 2. Per-workspace lock to prevent concurrent agent calls
        if workspace_id not in self._workspace_locks:
            self._workspace_locks[workspace_id] = asyncio.Lock()

        async with self._workspace_locks[workspace_id]:
            # 3. Ensure workspace is ready (may have been reaped)
            endpoint = self._route_table.get(workspace_id)
            if not endpoint:
                workspace = await self._ensure_workspace(msg.user_id, session_id)
                endpoint = workspace.get("service_endpoint", "")
                self._route_table[workspace_id] = endpoint

            # 4. Mark activity
            await self._mark_activity(msg.user_id, session_id)

            # 5. Call Agent Pod
            try:
                response = await self._call_agent(endpoint, session_id, msg.text)
            except httpx.ConnectError:
                # Agent Pod may have been reaped, re-ensure
                logger.warning("Agent unreachable for %s, re-ensuring workspace", workspace_id)
                workspace = await self._ensure_workspace(msg.user_id, session_id)
                endpoint = workspace.get("service_endpoint", "")
                self._route_table[workspace_id] = endpoint
                response = await self._call_agent(endpoint, session_id, msg.text)

        # 6. Publish response
        await self.bus.publish_outbound(OutboundMessage(
            channel_name=msg.channel_name,
            chat_id=msg.chat_id,
            text=response.get("content", ""),
            workspace_id=workspace_id,
            session_id=session_id,
            thread_ts=msg.thread_ts,
        ))

    # ── Orchestrator / Agent calls ────────────────────────────────

    async def _ensure_workspace(self, user_id: str, session_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.orchestrator_url}/api/v1/orchestrator/ensure",
                json={"user_id": user_id, "session_id": session_id},
            )
            resp.raise_for_status()
            data = resp.json()
            # Cache route
            workspace_id = data["workspace_id"]
            self._route_table[workspace_id] = data.get("service_endpoint", "")
            return data

    async def _mark_activity(self, user_id: str, session_id: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self.orchestrator_url}/api/v1/orchestrator/mark-activity",
                    json={"user_id": user_id, "session_id": session_id},
                )
        except Exception:
            logger.warning("Failed to mark activity for user=%s session=%s", user_id, session_id)

    async def _call_agent(self, endpoint: str, session_id: str, message: str) -> dict[str, Any]:
        agent_url = f"http://{endpoint}/api/v1/chat"
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                agent_url,
                json={"message": message, "session_id": session_id},
            )
            resp.raise_for_status()
            return resp.json()

    async def _get_workspace_info(self, workspace_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self.orchestrator_url}/api/v1/orchestrator/workspaces/{workspace_id}",
            )
            resp.raise_for_status()
            return resp.json()

    # ── Public API for Gateway routes ─────────────────────────────

    def get_route_table(self) -> dict[str, str]:
        """Expose route table for Gateway's direct proxy routes."""
        return self._route_table

    def set_route(self, workspace_id: str, endpoint: str) -> None:
        self._route_table[workspace_id] = endpoint
