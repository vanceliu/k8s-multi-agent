"""Model factory — per-model LLM initialization + output normalization.

Strategy pattern: each model family gets its own creation method and normalizer.
To add a new model, add a _create_xxx method + elif branch in create().
"""

import re
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Content Normalizers
# ---------------------------------------------------------------------------

class ContentNormalizer(ABC):
    """Base class for model-specific output normalization."""

    @abstractmethod
    def normalize(self, raw_content: str) -> str:
        """Process a streaming chunk, return cleaned content for the user."""
        ...

    def normalize_complete(self, full_content: str) -> str:
        """Process a complete (non-streaming) response. Override if needed."""
        return self.normalize(full_content)


class PassthroughNormalizer(ContentNormalizer):
    """No transformation — used for models with clean output (e.g. GLM)."""

    def normalize(self, raw_content: str) -> str:
        return raw_content

    def normalize_complete(self, full_content: str) -> str:
        return full_content


class ThinkingStripNormalizer(ContentNormalizer):
    """Strip <think>...</think> blocks from streaming output.

    Used for models that emit chain-of-thought in content (MiniMax, DeepSeek).
    Handles partial streaming: tracks whether we're inside a <think> block.
    """

    _OPEN_TAG = "<think>"
    _CLOSE_TAG = "</think>"

    def __init__(self):
        self._inside_think = False
        self._buffer = ""

    def normalize(self, raw_content: str) -> str:
        result = []
        self._buffer += raw_content

        while self._buffer:
            if self._inside_think:
                end_idx = self._buffer.find(self._CLOSE_TAG)
                if end_idx == -1:
                    # Check if buffer ends with a partial </think> prefix
                    # e.g. "</thi" could become "</think>" with more data
                    held = self._partial_suffix(self._buffer, self._CLOSE_TAG)
                    self._buffer = held
                    break
                self._buffer = self._buffer[end_idx + len(self._CLOSE_TAG):]
                self._inside_think = False
            else:
                start_idx = self._buffer.find(self._OPEN_TAG)
                if start_idx == -1:
                    # Hold back any trailing chars that could be a partial <think>
                    held = self._partial_suffix(self._buffer, self._OPEN_TAG)
                    safe = self._buffer[:len(self._buffer) - len(held)] if held else self._buffer
                    if safe:
                        result.append(safe)
                    self._buffer = held
                    break
                result.append(self._buffer[:start_idx])
                self._buffer = self._buffer[start_idx + len(self._OPEN_TAG):]
                self._inside_think = True

        return "".join(result)

    @staticmethod
    def _partial_suffix(text: str, tag: str) -> str:
        """Return the longest suffix of text that is a prefix of tag."""
        for i in range(1, len(tag)):
            if text.endswith(tag[:i]):
                return tag[:i]
        return ""

    _STRIP_RE = re.compile(r"<think>[\s\S]*?</think>")

    def normalize_complete(self, full_content: str) -> str:
        """Stateless strip for complete (non-streaming) responses."""
        return self._STRIP_RE.sub("", full_content).strip()


# ---------------------------------------------------------------------------
# Model Factory
# ---------------------------------------------------------------------------

class ModelFactory:
    """Creates LLM instances with model-specific configuration and normalizers."""

    def create(self, model_name: str, provider: str, temperature: float,
               api_base: str | None = None, api_key: str | None = None,
               callbacks: list | None = None):
        """Create a (model, normalizer) tuple based on model name.

        Returns:
            tuple: (ChatModel instance, ContentNormalizer instance)
        """
        name_lower = model_name.lower()

        if "minimax" in name_lower:
            return self._create_minimax(model_name, provider, temperature, api_base, api_key, callbacks)
        elif "deepseek" in name_lower:
            return self._create_deepseek(model_name, provider, temperature, api_base, api_key, callbacks)
        elif "glm" in name_lower:
            return self._create_glm(model_name, provider, temperature, api_base, api_key, callbacks)
        elif "claude" in name_lower:
            return self._create_claude(model_name, provider, temperature, api_base, api_key, callbacks)
        elif "gpt" in name_lower or "o1" in name_lower or "o3" in name_lower:
            return self._create_openai(model_name, provider, temperature, api_base, api_key, callbacks)
        else:
            return self._create_default(model_name, provider, temperature, api_base, api_key, callbacks)

    def _init_model(self, model_name: str, provider: str, temperature: float,
                    api_base: str | None, api_key: str | None,
                    callbacks: list | None, **extra_kwargs):
        """Shared init_chat_model call."""
        from langchain.chat_models import init_chat_model

        model_kwargs = {}
        if provider == "openai" and api_base:
            model_kwargs["base_url"] = api_base
        if api_key:
            model_kwargs["api_key"] = api_key
        model_kwargs.update(extra_kwargs)

        return init_chat_model(
            model=model_name,
            model_provider=provider,
            temperature=temperature,
            callbacks=callbacks or [],
            **model_kwargs,
        )

    # ------------------------------------------------------------------
    # Per-model creation methods
    # ------------------------------------------------------------------

    def _create_minimax(self, model_name, provider, temperature, api_base, api_key, callbacks):
        """MiniMax models — strip <think> tags from output."""
        logger.info("Creating MiniMax model: %s", model_name)
        model = self._init_model(model_name, provider, temperature, api_base, api_key, callbacks)
        normalizer = ThinkingStripNormalizer()
        return model, normalizer

    def _create_deepseek(self, model_name, provider, temperature, api_base, api_key, callbacks):
        """DeepSeek models — also emit <think> blocks that need stripping."""
        logger.info("Creating DeepSeek model: %s", model_name)
        model = self._init_model(model_name, provider, temperature, api_base, api_key, callbacks)
        normalizer = ThinkingStripNormalizer()
        return model, normalizer

    def _create_glm(self, model_name, provider, temperature, api_base, api_key, callbacks):
        """GLM (Zhipu) models — clean output, no post-processing needed."""
        logger.info("Creating GLM model: %s", model_name)
        model = self._init_model(model_name, provider, temperature, api_base, api_key, callbacks)
        normalizer = PassthroughNormalizer()
        return model, normalizer

    def _create_claude(self, model_name, provider, temperature, api_base, api_key, callbacks):
        """Anthropic Claude models — provider switch to 'anthropic'."""
        logger.info("Creating Claude model: %s", model_name)
        actual_provider = "anthropic" if provider == "openai" else provider
        model = self._init_model(model_name, actual_provider, temperature, api_base, api_key, callbacks)
        normalizer = PassthroughNormalizer()
        return model, normalizer

    def _create_openai(self, model_name, provider, temperature, api_base, api_key, callbacks):
        """OpenAI GPT/o-series models — clean output."""
        logger.info("Creating OpenAI model: %s", model_name)
        model = self._init_model(model_name, provider, temperature, api_base, api_key, callbacks)
        normalizer = PassthroughNormalizer()
        return model, normalizer

    def _create_default(self, model_name, provider, temperature, api_base, api_key, callbacks):
        """Fallback for unknown models — passthrough normalizer."""
        logger.info("Creating model (default handler): %s", model_name)
        model = self._init_model(model_name, provider, temperature, api_base, api_key, callbacks)
        normalizer = PassthroughNormalizer()
        return model, normalizer


# ---------------------------------------------------------------------------
# Post-model hook for LangGraph (cleans content before checkpoint storage)
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>")


def build_post_model_hook(normalizer: ContentNormalizer):
    """Build a post_model_hook for create_supervisor / create_react_agent.

    Returns None if the normalizer is PassthroughNormalizer (no-op).
    Otherwise returns a callable that strips model artifacts from the
    last AI message before it's persisted to the checkpointer.
    """
    if isinstance(normalizer, PassthroughNormalizer):
        return None

    def post_model_hook(state: dict) -> dict:
        messages = state.get("messages", [])
        if not messages:
            return state
        last_msg = messages[-1]
        content = getattr(last_msg, "content", "")
        if content and isinstance(content, str):
            cleaned = normalizer.normalize_complete(content)
            if cleaned != content:
                last_msg.content = cleaned
        return state

    return post_model_hook
