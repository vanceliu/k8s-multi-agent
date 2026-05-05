"""Factory functions for creating supervisor and sub-agent workflows.

Decouples agent construction from the runtime lifecycle so that
each agent's wiring (model, tools, prompt) is defined in one place.
"""

import logging
from typing import Any

from poc.agent.agents.prompts import (
    build_code_agent_prompt,
    build_research_agent_prompt,
    build_supervisor_prompt,
)

logger = logging.getLogger(__name__)


def create_research_agent(model: Any, tools: list) -> Any:
    """Create the research sub-agent (web search, rate-limited)."""
    from langgraph.prebuilt import create_react_agent

    prompt = build_research_agent_prompt()
    logger.debug("[research_agent] prompt:\n%s", prompt)

    return create_react_agent(
        model=model,
        tools=tools,
        name="research_agent",
        prompt=prompt,
    )


def create_code_agent(model: Any, tools: list, session_id: str) -> Any:
    """Create the code sub-agent (shell + python REPL, sandboxed)."""
    from langgraph.prebuilt import create_react_agent

    prompt = build_code_agent_prompt(session_id)
    logger.info("[code_agent] prompt:\n%s", prompt)

    return create_react_agent(
        model=model,
        tools=tools,
        name="code_agent",
        prompt=prompt,
    )


def create_supervisor_workflow(
    model: Any,
    sub_model: Any,
    research_tools: list,
    code_tools: list,
    common_tools: list,
    workspace_id: str,
    session_id: str,
    checkpointer: Any = None,
) -> Any:
    """Create the full supervisor + sub-agent workflow and compile it.

    Args:
        model: LLM for the supervisor (high-capability model).
        sub_model: LLM for sub-agents (lightweight, fast model).

    Returns a compiled LangGraph application ready for invoke/astream.
    """
    from langgraph_supervisor import create_supervisor

    supervisor_prompt = build_supervisor_prompt(workspace_id, session_id)
    logger.debug("[supervisor] prompt:\n%s", supervisor_prompt)

    research_agent = create_research_agent(sub_model, research_tools)
    code_agent = create_code_agent(sub_model, code_tools + common_tools, session_id)

    workflow = create_supervisor(
        agents=[research_agent, code_agent],
        model=model,
        prompt=supervisor_prompt,
        output_mode="full_history",
        add_handoff_messages=True,
    )

    app = workflow.compile(checkpointer=checkpointer)

    logger.debug(
        "Supervisor workflow created for session %s (research_agent + code_agent)",
        session_id,
    )
    return app
