"""Context Compactor — orchestrates memory flush + summarization."""

import asyncio
import json
import logging
from typing import Any, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from poc.agent.compaction.counter import TokenCounter
from poc.agent.compaction.prompts import MEMORY_FLUSH_PROMPT, SUMMARIZE_PROMPT

logger = logging.getLogger(__name__)


class ContextCompactor:
    """Manages conversation history compaction with memory flush."""

    def __init__(
        self,
        model: Any,
        model_name: str,
        memory_service: Optional[Any] = None,
        threshold_ratio: float = 0.75,
        target_ratio: float = 0.40,
        preserve_recent_turns: int = 6,
        min_messages: int = 20,
        memory_flush_enabled: bool = True,
    ):
        self._model = model
        self._model_name = model_name
        self._memory_service = memory_service
        self._threshold_ratio = threshold_ratio
        self._target_ratio = target_ratio
        self._preserve_recent_turns = preserve_recent_turns
        self._min_messages = min_messages
        self._memory_flush_enabled = memory_flush_enabled
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def context_window(self) -> int:
        return TokenCounter.get_context_window(self._model_name)

    @property
    def threshold_tokens(self) -> int:
        return int(self.context_window * self._threshold_ratio)

    @property
    def target_tokens(self) -> int:
        return int(self.context_window * self._target_ratio)

    def should_compact(self, messages: list[BaseMessage]) -> bool:
        """Check if compaction is needed based on token count and message count."""
        if len(messages) < self._min_messages:
            return False
        token_count = TokenCounter.estimate_messages(messages)
        return token_count > self.threshold_tokens

    def get_token_estimate(self, messages: list[BaseMessage]) -> int:
        """Return current token estimate for messages."""
        return TokenCounter.estimate_messages(messages)

    async def compact(
        self,
        session_id: str,
        messages: list[BaseMessage],
        on_event: Optional[Any] = None,
    ) -> list[BaseMessage]:
        """Execute compaction: memory flush + summarize + rebuild messages.

        Returns the compacted message list to be used as the new conversation state.

        on_event: optional async callable(event_dict) invoked for progress events:
          - {"type": "compaction_start", "before_tokens": int, "message_count": int}
          - {"type": "compaction_memory_flush", "stored": int}
          - {"type": "compaction_complete", "before_tokens": int, "after_tokens": int,
             "message_count": int, "memories_flushed": int}
        """
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await self._do_compact(messages, on_event)

    async def _do_compact(
        self,
        messages: list[BaseMessage],
        on_event: Optional[Any] = None,
    ) -> list[BaseMessage]:
        """Internal compaction logic."""
        # Determine split point: preserve recent N turns
        split_idx = self._find_split_index(messages)
        if split_idx <= 0:
            logger.warning("No messages to compact (split_idx=%d)", split_idx)
            return messages

        messages_to_compact = messages[:split_idx]
        messages_to_keep = messages[split_idx:]

        before_tokens = TokenCounter.estimate_messages(messages)
        logger.info(
            "Compacting: %d messages to summarize, %d to keep (tokens before: %d)",
            len(messages_to_compact),
            len(messages_to_keep),
            before_tokens,
        )

        if on_event:
            await self._emit(on_event, {
                "type": "compaction_start",
                "before_tokens": before_tokens,
                "message_count": len(messages_to_compact),
            })

        # Phase 1 & 2: run memory flush and summarize in parallel
        flush_task = None
        if self._memory_flush_enabled and self._memory_service:
            flush_task = asyncio.create_task(
                self._memory_flush(messages_to_compact)
            )

        summary = await self._summarize(messages_to_compact)

        flush_count = 0
        if flush_task:
            flush_count = await flush_task
            if on_event:
                await self._emit(on_event, {
                    "type": "compaction_memory_flush",
                    "stored": flush_count,
                })

        # Phase 3: rebuild message list
        compacted = self._rebuild_messages(summary, messages_to_keep, len(messages_to_compact))

        after_tokens = TokenCounter.estimate_messages(compacted)
        logger.info(
            "Compaction complete: tokens %d → %d, memories flushed: %d",
            before_tokens,
            after_tokens,
            flush_count,
        )

        if on_event:
            await self._emit(on_event, {
                "type": "compaction_complete",
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "message_count": len(messages_to_compact),
                "memories_flushed": flush_count,
            })

        return compacted

    @staticmethod
    async def _emit(on_event: Any, event: dict) -> None:
        """Safely invoke an event callback, swallowing errors."""
        try:
            result = on_event(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.warning("Compaction event callback failed: %s", e)

    def _find_split_index(self, messages: list[BaseMessage]) -> int:
        """Find the index where we split: everything before is compacted.

        Preserves the last N turns (user+assistant pairs) intact.
        We also ensure the compacted portion ends at a complete turn boundary.
        """
        # Count turns from the end
        turns_found = 0
        idx = len(messages) - 1

        while idx >= 0 and turns_found < self._preserve_recent_turns:
            msg = messages[idx]
            if isinstance(msg, HumanMessage):
                turns_found += 1
            idx -= 1

        # idx+1 is the start of the preserved section
        split_idx = idx + 1

        # Ensure we're not splitting in the middle of a tool call sequence
        while split_idx > 0 and isinstance(messages[split_idx], ToolMessage):
            split_idx -= 1

        # Check if compacting would actually help reach target
        if split_idx < 2:
            return 0

        return split_idx

    async def _memory_flush(self, messages: list[BaseMessage]) -> int:
        """Extract important information from messages and save to memory."""
        try:
            messages_text = self._format_messages_for_prompt(messages)
            prompt = MEMORY_FLUSH_PROMPT.format(messages_text=messages_text)

            response = await self._model.ainvoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)

            # Parse JSON from response
            extracted = self._parse_json_response(content)
            if not extracted:
                return 0

            stored = 0
            for item in extracted:
                category = item.get("category", "fact")
                memory_content = item.get("content", "")
                if not memory_content:
                    continue

                try:
                    await self._memory_service.store(
                        name=f"[compaction] {memory_content[:50]}",
                        type="core",
                        tags=[category, "compaction_flush"],
                        content=memory_content,
                    )
                    stored += 1
                except Exception as e:
                    logger.warning("Failed to save flushed memory: %s", e)

            logger.info("Memory flush: extracted %d items, stored %d", len(extracted), stored)
            return stored

        except Exception as e:
            logger.warning("Memory flush failed: %s", e)
            return 0

    async def _summarize(self, messages: list[BaseMessage]) -> str:
        """Generate a summary of the messages to compact."""
        messages_text = self._format_messages_for_prompt(messages)
        prompt = SUMMARIZE_PROMPT.format(messages_text=messages_text)

        try:
            response = await self._model.ainvoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)
            return content.strip()
        except Exception as e:
            logger.error("Summarization failed: %s", e)
            # Fallback: create a minimal summary from message types
            return self._fallback_summary(messages)

    def _rebuild_messages(
        self,
        summary: str,
        recent_messages: list[BaseMessage],
        compacted_count: int,
    ) -> list[BaseMessage]:
        """Rebuild the message list with summary + recent messages."""
        summary_msg = SystemMessage(
            content=(
                f"[對話摘要 — 以下為先前 {compacted_count} 則訊息的壓縮摘要]\n\n"
                f"{summary}\n\n"
                f"[摘要結束 — 以下為近期完整對話]"
            )
        )
        return [summary_msg] + recent_messages

    def _format_messages_for_prompt(self, messages: list[BaseMessage]) -> str:
        """Format messages into readable text for LLM prompts."""
        lines = []
        for msg in messages:
            role = self._get_role_label(msg)
            content = msg.content
            if isinstance(content, list):
                content = " ".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                )
            if not content:
                # For tool calls, show the tool name
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    tc_names = [tc.get("name", "?") for tc in tool_calls]
                    content = f"[呼叫工具: {', '.join(tc_names)}]"
                else:
                    continue

            # Truncate very long messages (tool results)
            if len(content) > 2000:
                content = content[:1000] + "\n...[截斷]...\n" + content[-500:]

            lines.append(f"**{role}**: {content}")

        return "\n\n".join(lines)

    @staticmethod
    def _get_role_label(msg: BaseMessage) -> str:
        if isinstance(msg, HumanMessage):
            return "使用者"
        elif isinstance(msg, AIMessage):
            return "助理"
        elif isinstance(msg, SystemMessage):
            return "系統"
        elif isinstance(msg, ToolMessage):
            tool_name = getattr(msg, "name", "tool")
            return f"工具結果({tool_name})"
        return "未知"

    @staticmethod
    def _parse_json_response(content: str) -> list[dict]:
        """Parse JSON array from LLM response, handling markdown code blocks."""
        content = content.strip()
        # Strip markdown code block if present
        if content.startswith("```"):
            lines = content.split("\n")
            lines = lines[1:]  # remove opening ```json
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines)

        try:
            result = json.loads(content)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # Try to find JSON array in the response
        import re
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        logger.warning("Failed to parse memory flush response as JSON")
        return []

    @staticmethod
    def _fallback_summary(messages: list[BaseMessage]) -> str:
        """Create a minimal summary when LLM summarization fails."""
        human_msgs = [m for m in messages if isinstance(m, HumanMessage)]
        if not human_msgs:
            return "（先前對話已壓縮，無法生成摘要）"

        topics = []
        for msg in human_msgs[:10]:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            topics.append(f"- {content[:100]}")

        return "使用者討論的主題：\n" + "\n".join(topics)
