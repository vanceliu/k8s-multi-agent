"""Agent definitions — prompts and factory functions for supervisor + sub-agents."""

from poc.agent.agents.factory import create_code_agent, create_research_agent, create_supervisor_workflow
from poc.agent.agents.prompts import (
    build_code_agent_prompt,
    build_research_agent_prompt,
    build_supervisor_prompt,
)

__all__ = [
    "build_code_agent_prompt",
    "build_research_agent_prompt",
    "build_supervisor_prompt",
    "create_code_agent",
    "create_research_agent",
    "create_supervisor_workflow",
]
