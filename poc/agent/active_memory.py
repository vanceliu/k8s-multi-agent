"""Active Memory Hook — pre-conversation memory injection with resilience.

Wraps MemoryService.recall() with:
  - Timeout (default 3s)
  - Circuit breaker (skip after N consecutive failures)
  - Cache (avoid redundant searches within TTL)
"""

import asyncio
import logging
import time
from typing import Optional

from poc.agent.memory_service import MemoryService

logger = logging.getLogger(__name__)


class ActiveMemoryHook:
    """Resilient wrapper around MemoryService.recall() for pre-conversation injection."""

    def __init__(
        self,
        memory_service: MemoryService,
        timeout_ms: int = 3000,
        cache_ttl_ms: int = 30000,
        max_results: int = 3,
        cb_max_failures: int = 3,
        cb_cooldown_ms: int = 60000,
    ):
        self._memory_service = memory_service
        self._timeout_s = timeout_ms / 1000.0
        self._cache_ttl_s = cache_ttl_ms / 1000.0
        self._max_results = max_results
        self._cb_max_failures = cb_max_failures
        self._cb_cooldown_s = cb_cooldown_ms / 1000.0

        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_opened_at: Optional[float] = None

        # Simple cache: query_hash → (result, timestamp)
        self._cache: dict[str, tuple[Optional[str], float]] = {}

    async def recall(self, message: str, recent_messages: Optional[list[str]] = None) -> Optional[str]:
        """Attempt to recall relevant memories with full resilience protection.

        Returns formatted context string or None.
        """
        if self._is_circuit_open():
            logger.debug("Active memory circuit breaker open, skipping recall")
            return None

        cache_key = self._cache_key(message)
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached if cached != "" else None

        try:
            result = await asyncio.wait_for(
                self._memory_service.recall(
                    user_message=message,
                    recent_messages=recent_messages,
                    max_results=self._max_results,
                ),
                timeout=self._timeout_s,
            )
            self._reset_failures()
            self._set_cache(cache_key, result)
            return result

        except asyncio.TimeoutError:
            logger.warning("Active memory recall timed out (%.1fs)", self._timeout_s)
            self._record_failure()
            return None

        except Exception as e:
            logger.warning("Active memory recall failed: %s", e)
            self._record_failure()
            return None

    def _is_circuit_open(self) -> bool:
        """Check if circuit breaker is open (too many recent failures)."""
        if self._consecutive_failures < self._cb_max_failures:
            return False

        if self._circuit_opened_at is None:
            return False

        elapsed = time.monotonic() - self._circuit_opened_at
        if elapsed >= self._cb_cooldown_s:
            # Cooldown expired, allow one attempt (half-open)
            self._consecutive_failures = 0
            self._circuit_opened_at = None
            logger.info("Active memory circuit breaker reset (cooldown expired)")
            return False

        return True

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._cb_max_failures:
            self._circuit_opened_at = time.monotonic()
            logger.warning(
                "Active memory circuit breaker opened (%d consecutive failures, cooldown %.0fs)",
                self._consecutive_failures,
                self._cb_cooldown_s,
            )

    def _reset_failures(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _cache_key(self, message: str) -> str:
        return message[:200].strip().lower()

    def _get_cache(self, key: str) -> Optional[str]:
        """Return cached result if within TTL, else None."""
        if key not in self._cache:
            return None
        result, ts = self._cache[key]
        if time.monotonic() - ts > self._cache_ttl_s:
            del self._cache[key]
            return None
        # Return empty string to distinguish "searched but found nothing" from "not cached"
        return result if result is not None else ""

    def _set_cache(self, key: str, result: Optional[str]) -> None:
        self._cache[key] = (result, time.monotonic())
        # Evict old entries if cache grows too large
        if len(self._cache) > 100:
            cutoff = time.monotonic() - self._cache_ttl_s
            self._cache = {k: v for k, v in self._cache.items() if v[1] > cutoff}
