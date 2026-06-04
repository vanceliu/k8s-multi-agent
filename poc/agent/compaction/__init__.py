"""Context Compaction — automatic conversation history compression.

Prevents context window overflow by summarizing old messages
while preserving recent conversation and flushing important
information to the Memory System before compression.
"""

from poc.agent.compaction.counter import TokenCounter
from poc.agent.compaction.compactor import ContextCompactor

__all__ = ["TokenCounter", "ContextCompactor"]