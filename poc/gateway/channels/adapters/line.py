"""LINE Messaging API channel adapter.

Handles webhook events from LINE platform, signature verification,
user binding flow (verification code), unbind flow, and push messaging.
"""

import asyncio
import hashlib
import hmac
import base64
import json
import logging
import time
from typing import Any

import httpx

from poc.gateway.channels.base import Channel
from poc.gateway.channels.message_bus import (
    MessageBus,
    MessageType,
    OutboundMessage,
)

logger = logging.getLogger(__name__)

LINE_API_BASE = "https://api.line.me/v2/bot"


class LINEChannel(Channel):
    """LINE Messaging API adapter.

    Supports:
    - Webhook event handling (message, follow, unfollow)
    - Signature verification (HMAC-SHA256)
    - Push messaging (for outbound/notifications)
    - Binding flow (verification code matching)
    - Unbind flow (two-step confirmation)
    """

    def __init__(self, name: str, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name, bus, config)
        self.channel_secret: str = config.get("channel_secret", "")
        self.access_token: str = config.get("channel_access_token", "")
        # Pending unbind confirmations: {line_uid: requested_at_timestamp}
        self._pending_unbinds: dict[str, float] = {}
        # Rate limit tracking: {line_uid: [timestamps]}
        self._bind_attempts: dict[str, list[float]] = {}

    async def start(self) -> None:
        self.bus.subscribe_outbound(self._on_outbound)
        self._running = True
        logger.info("LINEChannel started")

    async def stop(self) -> None:
        self.bus.unsubscribe_outbound(self._on_outbound)
        self._pending_unbinds.clear()
        self._running = False
        logger.info("LINEChannel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Push message to LINE user (chat_id = LINE user ID)."""
        await self._push_message(msg.chat_id, msg.text)

    # ── Webhook handling ─────────────────────────────────────────

    def verify_signature(self, body: bytes, signature: str) -> bool:
        """Verify LINE webhook signature (HMAC-SHA256)."""
        if not self.channel_secret:
            logger.error("LINE channel_secret not configured")
            return False
        digest = hmac.new(
            self.channel_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(signature, expected)

    async def handle_webhook(self, body: bytes, signature: str) -> None:
        """Process LINE webhook events."""
        if not self.verify_signature(body, signature):
            raise ValueError("Invalid LINE webhook signature")

        payload = json.loads(body)
        events = payload.get("events", [])

        for event in events:
            event_type = event.get("type")
            try:
                if event_type == "message":
                    await self._handle_message_event(event)
                elif event_type == "follow":
                    await self._handle_follow_event(event)
                elif event_type == "unfollow":
                    await self._handle_unfollow_event(event)
                else:
                    logger.debug("Ignoring LINE event type: %s", event_type)
            except Exception:
                logger.exception("Error handling LINE event: %s", event_type)

    # ── Event handlers ───────────────────────────────────────────

    async def _handle_message_event(self, event: dict) -> None:
        """Handle incoming text message."""
        message = event.get("message", {})
        if message.get("type") != "text":
            return

        source = event.get("source", {})
        line_uid = source.get("userId", "")
        text = message.get("text", "").strip()
        reply_token = event.get("replyToken", "")

        if not line_uid or not text:
            return

        # Check if this is an unbind confirmation
        if await self._check_unbind_confirmation(line_uid, text, reply_token):
            return

        # Check if user has an active binding
        from poc.gateway.channels.adapters._line_binding import resolve_binding
        binding = await resolve_binding(platform="line", platform_uid=line_uid)

        if binding and binding.get("status") == "active":
            # Bound user — route message to ChannelManager
            user_id = binding["user_id"]

            if text.startswith("/"):
                if text.lower().startswith("/unbind"):
                    await self._initiate_unbind(line_uid, reply_token)
                    return
                msg_type = MessageType.COMMAND
            else:
                msg_type = MessageType.CHAT

            # Immediately reply "processing" so user knows we received it
            await self._reply(reply_token, "收到，處理中⋯⋯")

            inbound = self._make_inbound(
                chat_id=line_uid,
                user_id=user_id,
                text=text,
                msg_type=msg_type,
                metadata={"reply_token": reply_token},
            )
            await self.bus.publish_inbound(inbound)

        elif self._is_verification_code(text):
            # Attempt binding with verification code
            await self._handle_bind_attempt(line_uid, text, reply_token)

        else:
            # Not bound, not a verification code — send Flex Message with LIFF bind button
            await self._reply_bind_prompt(reply_token, line_uid)

    async def _handle_follow_event(self, event: dict) -> None:
        """Handle user adding the official account as friend."""
        source = event.get("source", {})
        line_uid = source.get("userId", "")
        if not line_uid:
            return

        from poc.gateway.channels.adapters._line_binding import (
            resolve_binding,
            reactivate_binding,
        )
        binding = await resolve_binding(platform="line", platform_uid=line_uid)

        if binding and binding.get("status") == "inactive":
            await reactivate_binding(platform="line", platform_uid=line_uid)
            await self._push_message(line_uid, "歡迎回來！已恢復通知功能。")
            logger.info("LINE binding reactivated for uid=%s", line_uid)
        else:
            # Not bound — send Flex Message with LIFF bind button
            await self._push_bind_prompt(line_uid)

    async def _handle_unfollow_event(self, event: dict) -> None:
        """Handle user blocking or deleting the official account."""
        source = event.get("source", {})
        line_uid = source.get("userId", "")
        if not line_uid:
            return

        from poc.gateway.channels.adapters._line_binding import deactivate_binding
        await deactivate_binding(platform="line", platform_uid=line_uid)
        logger.info("LINE binding deactivated (unfollow) for uid=%s", line_uid)

    # ── Binding flow ─────────────────────────────────────────────

    def _is_verification_code(self, text: str) -> bool:
        """Check if text looks like a 6-digit verification code."""
        return len(text) == 6 and text.isdigit()

    def _check_rate_limit(self, line_uid: str) -> bool:
        """Check if line_uid has exceeded bind attempt rate limit (5 per 5 min)."""
        now = time.time()
        window = 300  # 5 minutes
        max_attempts = 5

        attempts = self._bind_attempts.get(line_uid, [])
        # Clean old attempts
        attempts = [t for t in attempts if now - t < window]
        self._bind_attempts[line_uid] = attempts

        return len(attempts) >= max_attempts

    async def _handle_bind_attempt(self, line_uid: str, code: str, reply_token: str) -> None:
        """Attempt to bind using verification code."""
        if self._check_rate_limit(line_uid):
            await self._reply(reply_token, "嘗試次數過多，請 5 分鐘後再試。")
            return

        # Record attempt
        self._bind_attempts.setdefault(line_uid, []).append(time.time())

        from poc.gateway.channels.adapters._line_binding import (
            verify_and_bind,
            get_line_profile,
        )

        # Get LINE profile for display_name
        profile = await get_line_profile(line_uid, self.access_token)
        display_name = profile.get("displayName", "") if profile else ""

        result = await verify_and_bind(
            platform="line",
            platform_uid=line_uid,
            code=code,
            display_name=display_name,
        )

        if result.get("success"):
            await self._reply(
                reply_token,
                f"綁定成功！已連結帳號 {result.get('username', '')}。\n"
                "現在可以直接在 LINE 上跟 Agent 對話了。",
            )
            logger.info("LINE binding created: uid=%s -> user=%s", line_uid, result.get("user_id"))
        else:
            error = result.get("error", "驗證碼錯誤或已過期")
            await self._reply(reply_token, f"綁定失敗：{error}\n請在 Web 端重新取得驗證碼。")

    # ── Unbind flow ──────────────────────────────────────────────

    async def _initiate_unbind(self, line_uid: str, reply_token: str) -> None:
        """Start unbind confirmation flow."""
        self._pending_unbinds[line_uid] = time.time()
        await self._reply(
            reply_token,
            "確定要解除 LINE 綁定嗎？\n"
            "解綁後將無法在 LINE 上使用工作區及接收排程通知。\n"
            "請在 60 秒內回覆「確認解綁」以完成操作。",
        )

    async def _check_unbind_confirmation(self, line_uid: str, text: str, reply_token: str) -> bool:
        """Check if message is an unbind confirmation. Returns True if handled."""
        if line_uid not in self._pending_unbinds:
            return False

        requested_at = self._pending_unbinds[line_uid]
        elapsed = time.time() - requested_at

        if elapsed > 60:
            # Expired
            del self._pending_unbinds[line_uid]
            return False

        if text == "確認解綁":
            del self._pending_unbinds[line_uid]
            from poc.gateway.channels.adapters._line_binding import unbind
            await unbind(platform="line", platform_uid=line_uid)
            await self._reply(reply_token, "已解除綁定。如需重新綁定，請至 Web 端操作。")
            logger.info("LINE binding removed (user unbind) for uid=%s", line_uid)
            return True
        else:
            # Any other message cancels the unbind flow
            del self._pending_unbinds[line_uid]
            return False

    # ── LINE API calls ───────────────────────────────────────────

    async def _push_message(self, to: str, text: str) -> bool:
        """Send push message to a LINE user."""
        if not self.access_token:
            logger.error("LINE access_token not configured")
            return False

        # Split long messages (LINE limit: 5000 chars per message)
        messages = []
        while text:
            chunk = text[:5000]
            messages.append({"type": "text", "text": chunk})
            text = text[5000:]

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{LINE_API_BASE}/message/push",
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                    json={"to": to, "messages": messages[:5]},  # LINE max 5 messages per push
                )
                if resp.status_code != 200:
                    logger.warning(
                        "LINE push failed: status=%d body=%s",
                        resp.status_code, resp.text,
                    )
                    return False
                return True
        except Exception:
            logger.exception("LINE push error to=%s", to)
            return False

    async def _reply(self, reply_token: str, text: str) -> bool:
        """Reply to a webhook event using reply token (free, no quota cost)."""
        if not self.access_token or not reply_token:
            return await self._push_message("", text) if not reply_token else False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{LINE_API_BASE}/message/reply",
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "replyToken": reply_token,
                        "messages": [{"type": "text", "text": text[:5000]}],
                    },
                )
                if resp.status_code != 200:
                    logger.warning(
                        "LINE reply failed: status=%d body=%s",
                        resp.status_code, resp.text,
                    )
                    return False
                return True
        except Exception:
            logger.exception("LINE reply error")
            return False

    # ── Flex Message: bind prompt ────────────────────────────────

    def _build_bind_flex(self) -> dict:
        """Build Flex Message JSON for binding prompt."""
        liff_url = "https://liff.line.me/2010184377-zdxRpWV3"
        return {
            "type": "flex",
            "altText": "請先綁定帳號",
            "contents": {
                "type": "bubble",
                "size": "kilo",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "text",
                            "text": "帳號綁定",
                            "weight": "bold",
                            "size": "lg",
                            "align": "center",
                        },
                        {
                            "type": "text",
                            "text": "請先綁定系統帳號，才能開始使用 AI 助理。",
                            "size": "sm",
                            "color": "#666666",
                            "margin": "md",
                            "wrap": True,
                        },
                    ],
                },
                "footer": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "button",
                            "action": {
                                "type": "uri",
                                "label": "開始綁定",
                                "uri": liff_url,
                            },
                            "style": "primary",
                            "color": "#06C755",
                        }
                    ],
                },
            },
        }

    async def _reply_bind_prompt(self, reply_token: str, line_uid: str) -> bool:
        """Reply with Flex Message bind prompt (uses reply token)."""
        if not self.access_token or not reply_token:
            return await self._push_bind_prompt(line_uid)

        flex_msg = self._build_bind_flex()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{LINE_API_BASE}/message/reply",
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                    json={"replyToken": reply_token, "messages": [flex_msg]},
                )
                if resp.status_code != 200:
                    logger.warning("LINE reply bind prompt failed: %s", resp.text)
                    return False
                return True
        except Exception:
            logger.exception("LINE reply bind prompt error")
            return False

    async def _push_bind_prompt(self, to: str) -> bool:
        """Push Flex Message bind prompt (uses push quota)."""
        if not self.access_token:
            return False

        flex_msg = self._build_bind_flex()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{LINE_API_BASE}/message/push",
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                    json={"to": to, "messages": [flex_msg]},
                )
                if resp.status_code != 200:
                    logger.warning("LINE push bind prompt failed: %s", resp.text)
                    return False
                return True
        except Exception:
            logger.exception("LINE push bind prompt error")
            return False
