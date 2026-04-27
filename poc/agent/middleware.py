"""Agent middleware — dynamic prompt injection for skills.

Implements SkillsInjectionMiddleware that injects the latest skills
metadata into every model call, so newly added/modified/removed skills
take effect immediately without recreating the agent.

Requires langchain >= 1.0.0.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from langchain.agents.middleware import AgentMiddleware

if TYPE_CHECKING:
    from poc.agent.runtime import DeepAgentsRuntime

logger = logging.getLogger(__name__)


class SkillsInjectionMiddleware(AgentMiddleware):
    """Dynamically inject skills metadata before each model call.

    On every LLM invocation, reads the latest skills from all three
    sources (shared > bundled > workspace) and prepends a system
    message with the skill catalogue. This ensures:
      - New skills are available immediately after file creation
      - Removed skills disappear on the next model call
      - No agent restart or cache invalidation needed
    """

    def __init__(self, runtime: DeepAgentsRuntime) -> None:
        self._runtime = runtime

    def _build_skills_system_message(self) -> tuple[str | None, int]:
        """Build skills metadata system message and count.

        Returns (message, count) where message is None if no skills.
        """
        skills = self._runtime._load_skills_metadata()
        if not skills:
            return None, 0

        skill_lines = []
        for s in skills:
            skill_lines.append(
                f"- **{s['name']}** [{s['source']}]：{s['description']}\n"
                f"  路徑：`{s['path']}`"
            )
        skill_list = "\n".join(skill_lines)

        return f"""## 可用 Skills

以下 skill 可供你使用。當使用者的請求符合 skill 的觸發條件時，
用 terminal 工具讀取對應的 SKILL.md 完整內容（`cat <路徑>`），
然後按照其中的指示執行。

{skill_list}

### Skill 使用規則
- 判斷是否觸發 skill 時，以 description 為依據
- 觸發後先讀取完整 SKILL.md，再按步驟執行
- 如果 skill 目錄下有 scripts/ 或 references/，按需讀取""", len(skills)

    async def awrap_model_call(self, request: Any, handler: Callable) -> Any:
        """Inject skills metadata into model request before each LLM call.

        Modifies request.messages to prepend a system message with the
        current skills catalogue. The handler then proceeds with the
        augmented messages.
        """
        skills_msg, skills_count = self._build_skills_system_message()
        if skills_msg:
            # request.messages is a list of BaseMessage objects
            # Insert skills system message after existing system messages
            # but before user/assistant messages
            from langchain_core.messages import SystemMessage

            skills_system = SystemMessage(content=skills_msg)

            messages = list(request.messages) if hasattr(request, 'messages') else []
            if messages:
                # Find insertion point: after last system message
                insert_idx = 0
                for i, msg in enumerate(messages):
                    if hasattr(msg, 'type') and msg.type == 'system':
                        insert_idx = i + 1
                    elif isinstance(msg, SystemMessage):
                        insert_idx = i + 1

                messages.insert(insert_idx, skills_system)
                request.messages = messages
                logger.debug("Skills metadata injected (%d skills)", skills_count)

        return await handler(request)

    def wrap_model_call(self, request: Any, handler: Callable) -> Any:
        """Sync version — inject skills metadata before each LLM call."""
        skills_msg, _ = self._build_skills_system_message()
        if skills_msg:
            from langchain_core.messages import SystemMessage

            skills_system = SystemMessage(content=skills_msg)

            messages = list(request.messages) if hasattr(request, 'messages') else []
            if messages:
                insert_idx = 0
                for i, msg in enumerate(messages):
                    if hasattr(msg, 'type') and msg.type == 'system':
                        insert_idx = i + 1
                    elif isinstance(msg, SystemMessage):
                        insert_idx = i + 1

                messages.insert(insert_idx, skills_system)
                request.messages = messages

        return handler(request)
