"""Unit tests for Context Compaction module."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from poc.agent.compaction.counter import TokenCounter
from poc.agent.compaction.compactor import ContextCompactor


# ─── TokenCounter Tests ──────────────────────────────────────────────────


class TestTokenCounter:
    def test_estimate_messages_simple(self):
        messages = [
            HumanMessage(content="Hello world"),
            AIMessage(content="Hi there, how can I help?"),
        ]
        estimate = TokenCounter.estimate_messages(messages)
        assert estimate > 0
        # "Hello world" = 11 chars, "Hi there, how can I help?" = 25 chars
        # Total = 36 chars / 3.5 ≈ 10 tokens
        assert 8 <= estimate <= 12

    def test_estimate_messages_chinese(self):
        messages = [
            HumanMessage(content="你好，請幫我寫一個 Python 程式"),
        ]
        estimate = TokenCounter.estimate_messages(messages)
        assert estimate > 0

    def test_estimate_messages_with_tool_calls(self):
        msg = AIMessage(content="Let me search for that.")
        msg.tool_calls = [{"name": "duckduckgo_search", "args": {"query": "python async"}}]
        messages = [msg]
        estimate = TokenCounter.estimate_messages(messages)
        # Should account for both content and tool call args
        assert estimate > TokenCounter.estimate_text("Let me search for that.")

    def test_estimate_messages_list_content(self):
        messages = [
            HumanMessage(content=[{"text": "block one"}, {"text": "block two"}]),
        ]
        estimate = TokenCounter.estimate_messages(messages)
        assert estimate > 0

    def test_estimate_messages_empty(self):
        assert TokenCounter.estimate_messages([]) == 0

    def test_estimate_text(self):
        text = "a" * 350  # 350 chars / 3.5 = 100 tokens
        assert TokenCounter.estimate_text(text) == 100

    def test_get_context_window_known_models(self):
        assert TokenCounter.get_context_window("glm-5.1") == 128_000
        assert TokenCounter.get_context_window("deepseek-chat") == 64_000
        assert TokenCounter.get_context_window("claude-sonnet-4-20250514") == 200_000
        assert TokenCounter.get_context_window("minimax-01") == 1_000_000

    def test_get_context_window_unknown_model(self):
        assert TokenCounter.get_context_window("unknown-model-xyz") == 128_000


# ─── ContextCompactor Tests ──────────────────────────────────────────────


def _make_conversation(turns: int) -> list:
    """Generate a fake conversation with N turns (user + assistant pairs)."""
    messages = []
    for i in range(turns):
        messages.append(HumanMessage(content=f"使用者訊息 {i}: " + "x" * 200))
        messages.append(AIMessage(content=f"助理回覆 {i}: " + "y" * 300))
    return messages


def _make_long_conversation(token_target: int) -> list:
    """Generate a conversation that exceeds the given token target."""
    messages = []
    i = 0
    while TokenCounter.estimate_messages(messages) < token_target:
        messages.append(HumanMessage(content=f"問題 {i}: " + "問" * 500))
        messages.append(AIMessage(content=f"回答 {i}: " + "答" * 800))
        i += 1
    return messages


class TestContextCompactorShouldCompact:
    def setup_method(self):
        self.model = AsyncMock()
        self.compactor = ContextCompactor(
            model=self.model,
            model_name="glm-5.1",
            threshold_ratio=0.75,
            target_ratio=0.40,
            preserve_recent_turns=6,
            min_messages=20,
        )

    def test_should_not_compact_few_messages(self):
        messages = _make_conversation(5)  # 10 messages < 20 min
        assert self.compactor.should_compact(messages) is False

    def test_should_not_compact_below_threshold(self):
        messages = _make_conversation(12)  # 24 messages > 20, but tokens low
        assert self.compactor.should_compact(messages) is False

    def test_should_compact_above_threshold(self):
        # 128K * 0.75 = 96K tokens threshold
        messages = _make_long_conversation(97_000)
        assert self.compactor.should_compact(messages) is True


class TestContextCompactorSplitIndex:
    def setup_method(self):
        self.model = AsyncMock()
        self.compactor = ContextCompactor(
            model=self.model,
            model_name="glm-5.1",
            preserve_recent_turns=3,
            min_messages=4,
        )

    def test_preserves_recent_turns(self):
        messages = _make_conversation(10)  # 20 messages
        split_idx = self.compactor._find_split_index(messages)
        # Should preserve last 3 turns = 6 messages
        preserved = messages[split_idx:]
        human_count = sum(1 for m in preserved if isinstance(m, HumanMessage))
        assert human_count >= 3

    def test_does_not_split_on_tool_message(self):
        messages = [
            HumanMessage(content="msg 1"),
            AIMessage(content="reply 1"),
            HumanMessage(content="msg 2"),
            AIMessage(content="calling tool"),
            ToolMessage(content="tool result", tool_call_id="tc1"),
            HumanMessage(content="msg 3"),
            AIMessage(content="reply 3"),
            HumanMessage(content="msg 4"),
            AIMessage(content="reply 4"),
        ]
        self.compactor._preserve_recent_turns = 2
        split_idx = self.compactor._find_split_index(messages)
        # Should not split right at the ToolMessage
        if split_idx > 0:
            assert not isinstance(messages[split_idx], ToolMessage)

    def test_returns_zero_for_short_conversation(self):
        messages = _make_conversation(1)  # 2 messages
        split_idx = self.compactor._find_split_index(messages)
        assert split_idx == 0


class TestContextCompactorMemoryFlush:
    @pytest.mark.asyncio
    async def test_memory_flush_extracts_and_saves(self):
        model = AsyncMock()
        model.ainvoke.return_value = AIMessage(
            content=json.dumps([
                {"category": "fact", "content": "使用者是資料工程師"},
                {"category": "decision", "content": "選擇用 PostgreSQL 而非 MySQL"},
            ])
        )

        memory_service = AsyncMock()
        memory_service.store = AsyncMock(return_value="test.md")

        compactor = ContextCompactor(
            model=model,
            model_name="glm-5.1",
            memory_service=memory_service,
            memory_flush_enabled=True,
        )

        messages = [
            HumanMessage(content="我是資料工程師，我們決定用 PostgreSQL"),
            AIMessage(content="好的，PostgreSQL 是個好選擇"),
        ]

        count = await compactor._memory_flush(messages)
        assert count == 2
        assert memory_service.store.call_count == 2

    @pytest.mark.asyncio
    async def test_memory_flush_handles_empty_response(self):
        model = AsyncMock()
        model.ainvoke.return_value = AIMessage(content="[]")

        compactor = ContextCompactor(
            model=model,
            model_name="glm-5.1",
            memory_service=AsyncMock(),
            memory_flush_enabled=True,
        )

        messages = [HumanMessage(content="今天天氣不錯")]
        count = await compactor._memory_flush(messages)
        assert count == 0

    @pytest.mark.asyncio
    async def test_memory_flush_handles_error(self):
        model = AsyncMock()
        model.ainvoke.side_effect = Exception("LLM error")

        compactor = ContextCompactor(
            model=model,
            model_name="glm-5.1",
            memory_service=AsyncMock(),
            memory_flush_enabled=True,
        )

        messages = [HumanMessage(content="test")]
        count = await compactor._memory_flush(messages)
        assert count == 0


class TestContextCompactorSummarize:
    @pytest.mark.asyncio
    async def test_summarize_returns_content(self):
        model = AsyncMock()
        model.ainvoke.return_value = AIMessage(
            content="- 討論了資料庫選型\n- 決定使用 PostgreSQL\n- 下一步建立 schema"
        )

        compactor = ContextCompactor(model=model, model_name="glm-5.1")
        messages = _make_conversation(5)

        summary = await compactor._summarize(messages)
        assert "PostgreSQL" in summary
        assert len(summary) > 0

    @pytest.mark.asyncio
    async def test_summarize_fallback_on_error(self):
        model = AsyncMock()
        model.ainvoke.side_effect = Exception("timeout")

        compactor = ContextCompactor(model=model, model_name="glm-5.1")
        messages = _make_conversation(5)

        summary = await compactor._summarize(messages)
        assert "使用者討論的主題" in summary


class TestContextCompactorCompact:
    @pytest.mark.asyncio
    async def test_full_compaction_flow(self):
        model = AsyncMock()
        # First call: memory flush
        model.ainvoke.side_effect = [
            AIMessage(content='[{"category": "fact", "content": "test fact"}]'),
            AIMessage(content="- 摘要：討論了測試相關議題"),
        ]

        memory_service = AsyncMock()
        memory_service.store = AsyncMock(return_value="test.md")

        compactor = ContextCompactor(
            model=model,
            model_name="glm-5.1",
            memory_service=memory_service,
            memory_flush_enabled=True,
            preserve_recent_turns=2,
            min_messages=4,
        )

        messages = _make_conversation(8)  # 16 messages
        result = await compactor.compact("sess-test", messages)

        # Result should have: 1 summary SystemMessage + preserved recent messages
        assert isinstance(result[0], SystemMessage)
        assert "摘要" in result[0].content
        assert len(result) < len(messages)

    @pytest.mark.asyncio
    async def test_compaction_without_memory_flush(self):
        model = AsyncMock()
        model.ainvoke.return_value = AIMessage(content="- 摘要內容")

        compactor = ContextCompactor(
            model=model,
            model_name="glm-5.1",
            memory_service=None,
            memory_flush_enabled=False,
            preserve_recent_turns=2,
            min_messages=4,
        )

        messages = _make_conversation(8)
        result = await compactor.compact("sess-test", messages)

        assert isinstance(result[0], SystemMessage)
        # Only one LLM call (summarize, no flush)
        assert model.ainvoke.call_count == 1

    @pytest.mark.asyncio
    async def test_compaction_concurrency_lock(self):
        model = AsyncMock()

        async def slow_invoke(*args, **kwargs):
            await asyncio.sleep(0.1)
            return AIMessage(content="- summary")

        model.ainvoke.side_effect = slow_invoke

        compactor = ContextCompactor(
            model=model,
            model_name="glm-5.1",
            memory_flush_enabled=False,
            preserve_recent_turns=2,
            min_messages=4,
        )

        messages = _make_conversation(8)

        # Run two compactions concurrently on same session
        results = await asyncio.gather(
            compactor.compact("sess-1", messages),
            compactor.compact("sess-1", messages),
        )

        # Both should succeed (sequential due to lock)
        assert len(results) == 2
        assert all(isinstance(r[0], SystemMessage) for r in results)


class TestContextCompactorHelpers:
    def test_parse_json_response_plain(self):
        content = '[{"category": "fact", "content": "test"}]'
        result = ContextCompactor._parse_json_response(content)
        assert len(result) == 1
        assert result[0]["category"] == "fact"

    def test_parse_json_response_markdown_block(self):
        content = '```json\n[{"category": "decision", "content": "use postgres"}]\n```'
        result = ContextCompactor._parse_json_response(content)
        assert len(result) == 1
        assert result[0]["category"] == "decision"

    def test_parse_json_response_with_surrounding_text(self):
        content = 'Here are the results:\n[{"category": "todo", "content": "fix bug"}]\nDone.'
        result = ContextCompactor._parse_json_response(content)
        assert len(result) == 1

    def test_parse_json_response_invalid(self):
        content = "This is not JSON at all"
        result = ContextCompactor._parse_json_response(content)
        assert result == []

    def test_format_messages_truncates_long_content(self):
        compactor = ContextCompactor(model=AsyncMock(), model_name="glm-5.1")
        messages = [HumanMessage(content="x" * 5000)]
        formatted = compactor._format_messages_for_prompt(messages)
        assert "截斷" in formatted
        assert len(formatted) < 5000

    def test_get_role_label(self):
        assert ContextCompactor._get_role_label(HumanMessage(content="")) == "使用者"
        assert ContextCompactor._get_role_label(AIMessage(content="")) == "助理"
        assert ContextCompactor._get_role_label(SystemMessage(content="")) == "系統"

    def test_fallback_summary(self):
        messages = [
            HumanMessage(content="第一個問題"),
            AIMessage(content="回答"),
            HumanMessage(content="第二個問題"),
        ]
        summary = ContextCompactor._fallback_summary(messages)
        assert "第一個問題" in summary
        assert "第二個問題" in summary
