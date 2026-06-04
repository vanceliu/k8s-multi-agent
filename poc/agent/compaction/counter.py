"""Token estimation and context window lookup."""

from langchain_core.messages import BaseMessage

# Context window sizes per model (tokens)
_CONTEXT_WINDOWS: dict[str, int] = {
    "glm-5.1": 128_000,
    "glm-5-turbo": 128_000,
    "glm-4-plus": 128_000,
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-opus-4-20250514": 200_000,
    "minimax-01": 1_000_000,
    "MiniMax-M2.7": 1_000_000,
}

_DEFAULT_CONTEXT_WINDOW = 128_000

# Average chars per token for mixed Chinese/English text
_CHARS_PER_TOKEN = 3.5


class TokenCounter:
    """Estimates token count from messages without requiring a specific tokenizer."""

    @staticmethod
    def estimate_messages(messages: list[BaseMessage]) -> int:
        """Estimate total token count for a list of messages."""
        total_chars = 0
        for msg in messages:
            content = msg.content
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total_chars += len(block.get("text", ""))
                    elif isinstance(block, str):
                        total_chars += len(block)
            # Account for tool_calls content
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    total_chars += len(str(tc.get("args", "")))
                    total_chars += len(tc.get("name", ""))
        return int(total_chars / _CHARS_PER_TOKEN)

    @staticmethod
    def estimate_text(text: str) -> int:
        """Estimate token count for a single text string."""
        return int(len(text) / _CHARS_PER_TOKEN)

    @staticmethod
    def get_context_window(model_name: str) -> int:
        """Return context window size for a given model."""
        return _CONTEXT_WINDOWS.get(model_name, _DEFAULT_CONTEXT_WINDOW)
