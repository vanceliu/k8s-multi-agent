"""Deep Agents Runtime — Supervisor + Sub-Agent architecture.

Uses langgraph-supervisor to orchestrate specialized sub-agents:
  - Supervisor: understands user intent, delegates to sub-agents, synthesizes results
  - research_agent: web search (rate-limited duckduckgo_search)
  - code_agent: shell + python REPL execution

Preserves:
  - AsyncPostgresSaver checkpointer (conversations persist across Pod restarts)
  - SkillsInjectionMiddleware (dynamic skills on every model call)
  - LocalBackend (PVC file operations)
  - Session-scoped tool isolation
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from poc.agent.agents.factory import create_supervisor_workflow
from poc.agent.backends.local import LocalBackend
from poc.agent.config import AgentConfig
from poc.agent.tools import get_code_tools, get_common_tools, get_research_tools, get_workspace_tools

logger = logging.getLogger(__name__)


class LLMRequestLogger(BaseCallbackHandler):
    """Logs every LLM request: model messages and response."""

    def on_llm_start(self, serialized, prompts, **kwargs):
        run_name = kwargs.get("name", serialized.get("id", ["unknown"])[-1])
        for i, prompt in enumerate(prompts):
            logger.debug("[LLM_REQUEST] %s prompt[%d]: %s", run_name, i, prompt[:500])

    def on_chat_model_start(self, serialized, messages, **kwargs):
        run_name = kwargs.get("name", serialized.get("id", ["unknown"])[-1])
        for batch_idx, msg_list in enumerate(messages):
            for msg in msg_list:
                role = getattr(msg, "type", "unknown")
                content = getattr(msg, "content", "")
                tool_calls = getattr(msg, "tool_calls", [])
                preview = str(content)[:300] if content else ""
                if tool_calls:
                    tc_summary = [f"{tc.get('name', '?')}({str(tc.get('args', ''))[:100]})" for tc in tool_calls]
                    logger.debug("[LLM_REQUEST] %s msg[%s]: tool_calls=%s", run_name, role, tc_summary)
                elif preview:
                    logger.debug("[LLM_REQUEST] %s msg[%s]: %s", run_name, role, preview)


class DeepAgentsRuntime:
    """Agent Runtime based on Supervisor + Sub-Agent pattern."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.workspace_id = config.workspace_id
        self.orchestrator_url = config.orchestrator_url
        self.pod_name = config.pod_name

        self._session_agents: dict[str, Any] = {}  # session_id → supervisor workflow
        self.backend: LocalBackend | None = None
        self._model = None       # supervisor LLM
        self._sub_model = None   # sub-agent LLM (lightweight)
        self._checkpointer = None
        self._pg_checkpointer = None
        self._pg_conn = None

        self._shutting_down = False
        self._active_requests = 0
        self._drain_event = asyncio.Event()

    async def initialize(self) -> None:
        """Initialize all runtime components."""
        logger.info("Initializing Deep Agents runtime for workspace %s", self.workspace_id)

        # 1. Initialize workspace directories on PVC
        ws = Path(self.config.workspace_path)
        ws.mkdir(parents=True, exist_ok=True)
        for subdir in ("data", "memories", "skills", "sessions"):
            (ws / subdir).mkdir(exist_ok=True)

        init_marker = ws / ".initialized"
        if not init_marker.exists():
            init_marker.touch()
            logger.info("Workspace initialized at %s", self.config.workspace_path)
        else:
            logger.info("Workspace already initialized at %s", self.config.workspace_path)

        # 2. Initialize LocalBackend (PVC, no session context at init time)
        self.backend = LocalBackend(self.config.workspace_path)

        # 3. Initialize shared LLM model + checkpointer (heavy, done once)
        await self._init_model_and_checkpointer()

        logger.info(
            "Deep Agents runtime initialized (workspace=%s, supervisor_model=%s, sub_agent_model=%s)",
            self.workspace_id, self.config.model_name, self.config.sub_agent_model_name,
        )

    async def _init_model_and_checkpointer(self) -> None:
        """Initialize LLM models and checkpointer (shared across all sessions)."""
        try:
            from langchain.chat_models import init_chat_model

            # Supervisor model (high-capability)
            model_kwargs = {}
            if self.config.model_provider == "openai" and self.config.openai_api_base:
                model_kwargs["base_url"] = self.config.openai_api_base

            self._model = init_chat_model(
                model=self.config.model_name,
                model_provider=self.config.model_provider,
                temperature=self.config.temperature,
                callbacks=[LLMRequestLogger()],
                **model_kwargs,
            )
            logger.info("Supervisor LLM initialized (model=%s)", self.config.model_name)

            # Sub-agent model (lightweight, fast)
            sub_model_kwargs = {}
            if self.config.sub_agent_model_provider == "openai" and self.config.sub_agent_api_base:
                sub_model_kwargs["base_url"] = self.config.sub_agent_api_base

            self._sub_model = init_chat_model(
                model=self.config.sub_agent_model_name,
                model_provider=self.config.sub_agent_model_provider,
                temperature=self.config.sub_agent_temperature,
                callbacks=[LLMRequestLogger()],
                **sub_model_kwargs,
            )
            logger.info("Sub-agent LLM initialized (model=%s)", self.config.sub_agent_model_name)

        except ImportError as e:
            logger.warning("LangChain not available (%s), using stub agent", e)
            return
        except Exception as e:
            logger.warning("Failed to create LLM model (%s), using stub agent", e)
            return

        self._checkpointer = await self._build_checkpointer()

    def _get_or_create_agent(self, session_id: str):
        """Return cached supervisor workflow for session, or create one.

        Architecture:
          Supervisor → research_agent (duckduckgo_search)
                     → code_agent (terminal + python_repl)

        Falls back to single flat agent if langgraph-supervisor is unavailable.
        """
        if session_id in self._session_agents:
            return self._session_agents[session_id]

        if self._model is None:
            return None

        # Collect tools for sub-agents
        research_tools = get_research_tools()
        code_tools = get_code_tools(self.config.workspace_path, session_id)
        common_tools = get_common_tools(self.workspace_id, self.orchestrator_url)

        try:
            return self._create_supervisor_agent(session_id, research_tools, code_tools, common_tools)
        except Exception as e:
            logger.warning("Supervisor creation failed (%s), falling back to flat agent", e)
            return self._create_flat_agent(session_id)

    def _create_supervisor_agent(self, session_id, research_tools, code_tools, common_tools):
        """Create supervisor + sub-agent workflow (delegated to agents.factory)."""
        app = create_supervisor_workflow(
            model=self._model,
            sub_model=self._sub_model,
            research_tools=research_tools,
            code_tools=code_tools,
            common_tools=common_tools,
            workspace_id=self.workspace_id,
            session_id=session_id,
            checkpointer=self._checkpointer,
        )
        self._session_agents[session_id] = app
        return app

    def _create_flat_agent(self, session_id):
        """Fallback: single agent with all tools (no supervisor)."""
        try:
            from langchain.agents import create_agent
            from poc.agent.middleware import SkillsInjectionMiddleware

            tools = get_workspace_tools(
                workspace_id=self.workspace_id,
                orchestrator_url=self.orchestrator_url,
                workspace_path=self.config.workspace_path,
                session_id=session_id,
            )
            self._session_tools[session_id] = tools

            agent = create_agent(
                model=self._model,
                tools=tools,
                system_prompt=self._build_static_system_prompt(),
                checkpointer=self._checkpointer,
                middleware=[SkillsInjectionMiddleware(self)],
            )

            self._session_agents[session_id] = agent
            logger.info("Flat agent created for session %s (fallback)", session_id)
            return agent
        except Exception as e:
            logger.warning("Flat agent creation also failed: %s", e)
            return None

    async def _build_checkpointer(self):
        """Build checkpointer: PostgreSQL if available, else in-memory."""
        if self.config.database_url:
            try:
                from psycopg import AsyncConnection
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                conn = await AsyncConnection.connect(
                    self.config.database_url,
                    autocommit=True,
                    prepare_threshold=0,
                )
                checkpointer = AsyncPostgresSaver(conn)
                await checkpointer.setup()
                self._pg_checkpointer = checkpointer
                self._pg_conn = conn
                logger.info("PostgreSQL checkpointer initialized (conversations persist across Pod restarts)")
                return checkpointer
            except ImportError:
                logger.warning("langgraph-checkpoint-postgres not installed, falling back to MemorySaver")
            except Exception as e:
                logger.warning("PostgreSQL checkpointer failed (%s), falling back to MemorySaver", e)

        from langgraph.checkpoint.memory import MemorySaver
        logger.info("Using in-memory checkpointer (conversations lost on Pod restart)")
        return MemorySaver()

    def _load_skills_metadata(self) -> list[dict[str, str]]:
        """Load skill metadata (name + description) from three sources.

        Priority (highest first, same-name skill uses highest priority):
          1. Shared workspace skills — from mounted shared PVCs /shared/*/skills/
          2. Bundled skills — shipped with agent image (poc/agent/skills/)
          3. Workspace skills — per-workspace, from workspace PVC skills/
        """
        sources: list[tuple[str, Path, str]] = []

        # 1. Shared workspace skills
        shared_base = Path(self.config.shared_base_path)
        if shared_base.is_dir():
            for shared_ws in sorted(shared_base.iterdir()):
                skills_dir = shared_ws / "skills"
                if skills_dir.is_dir():
                    sources.append((f"shared:{shared_ws.name}", skills_dir, "shared"))

        # 2. Bundled skills
        sources.append(("bundled", Path(__file__).parent / "skills", "bundled"))

        # 3. Workspace (local) skills
        sources.append(("workspace", Path(self.config.workspace_path) / "skills", "workspace"))

        seen_names: set[str] = set()
        skills: list[dict[str, str]] = []

        for source_label, sdir, source_type in sources:
            if not sdir.is_dir():
                continue
            for skill_md in sorted(sdir.rglob("SKILL.md")):
                try:
                    content = skill_md.read_text(encoding="utf-8")
                    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
                    if not match:
                        continue
                    frontmatter = match.group(1)
                    name = ""
                    description = ""
                    for line in frontmatter.split("\n"):
                        if line.startswith("name:"):
                            name = line.split(":", 1)[1].strip()
                        elif line.startswith("description:"):
                            desc_val = line.split(":", 1)[1].strip()
                            if desc_val.startswith(">"):
                                desc_lines = []
                                fm_lines = frontmatter.split("\n")
                                idx = fm_lines.index(line)
                                for next_line in fm_lines[idx + 1:]:
                                    if next_line.startswith("  ") or next_line.startswith("\t"):
                                        desc_lines.append(next_line.strip())
                                    else:
                                        break
                                description = " ".join(desc_lines)
                            else:
                                description = desc_val.strip("'\"")

                    if not name:
                        continue

                    if name in seen_names:
                        logger.debug("Skill '%s' from %s skipped (overridden by higher priority)", name, source_label)
                        continue

                    seen_names.add(name)
                    rel_path = str(skill_md.parent.relative_to(sdir))
                    skills.append({
                        "name": name,
                        "description": description,
                        "path": str(skill_md),
                        "source": source_label,
                    })
                    logger.info("Skill loaded: %s [%s] (%s)", name, source_label, rel_path)
                except Exception:
                    logger.warning("Failed to parse skill: %s", skill_md)

        return skills

    def _build_system_prompt(self) -> list[tuple[str, str]]:
        """Build layered system prompt (for flat agent fallback only)."""
        messages: list[tuple[str, str]] = []
        messages.append(("system", f"""你是一個專屬於工作區 {self.workspace_id} 的 AI 助理。

你運行在一個 Kubernetes Pod 中，工作區資料保存於本地 PVC 掛載。

## 能力
- **檔案操作**：可直接讀寫工作區內的檔案
- **規劃**：可將複雜任務拆解為步驟
- **長期記憶**：可將重要資訊寫入 /memories/ 以跨 session 保存
- **網路搜尋**：使用 duckduckgo_search 工具查詢即時資訊
- **Shell 執行**：可在容器內執行 shell 命令
- **Python 執行**：可執行 Python 程式碼進行資料分析與計算

## 工具使用規則
- 需要查詢網路資訊時，必須使用 duckduckgo_search 工具，不要用 shell curl 替代
- 工具呼叫失敗時，應嘗試使用其他可用工具完成任務，而非直接用訓練知識回答
- Shell 工具僅用於檔案操作和腳本執行，不要用來呼叫外部 API
- **禁止安裝套件**：不要使用 pip install 或任何套件管理工具。只能使用容器內已預裝的套件（包含 matplotlib, pandas, numpy, openpyxl 等）。如果需要的套件不存在，請改用其他已有的方式完成任務
- **圖表優先用 Mermaid**：繪製圖表時，絕大部分情況都以 Mermaid 語法輸出（用 ```mermaid 程式碼區塊），包含流程圖、架構圖、時序圖、甘特圖、圓餅圖、長條圖等。僅在圖表非常複雜（如大量數據點的散佈圖、多軸統計圖、地理分布圖等 Mermaid 無法表達的場景）才使用 matplotlib

## 工作區結構
- data/              — 使用者共用資料（永久，所有 session 可讀寫）
- memories/          — 跨 session 記憶（永久，所有 session 可讀寫）
- skills/            — 技能定義（永久）
- sessions/<sid>/    — 各 session 專屬工作目錄（僅該 session 可寫入）

## 注意事項
- 所有檔案操作透過本地 PVC，Pod 回收後資料保留
- Session 刪除時，對應的 sessions/<sid>/ 資料夾會一併清除"""))

        return messages

    def _build_static_system_prompt(self) -> str:
        """Combine layered system messages into a single prompt string (flat agent fallback)."""
        layers = self._build_system_prompt()
        return "\n\n---\n\n".join(content for _, content in layers)

    def _ensure_session_dir(self, session_id: str) -> None:
        """Create session-scoped directory if it doesn't exist."""
        session_dir = Path(self.config.workspace_path) / "sessions" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

    def _session_backend(self, session_id: str) -> LocalBackend:
        """Return a session-aware backend with write permission scoping."""
        return LocalBackend(self.config.workspace_path, session_id=session_id)

    def _session_context_message(self, session_id: str) -> str:
        """Return a session context reminder injected before each user message.

        Includes dynamic skills metadata so supervisor can trigger skills
        via code_agent (no middleware needed in supervisor pattern).
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y年%m月%d日")
        time_str = now.strftime("%H:%M UTC")

        context = (
            f"[Session Context]\n"
            f"今天是 {date_str}，目前時間 {time_str}。\n"
            f"你目前的 session_id 是 {session_id}。\n"
            f"你的工作目錄是 sessions/{session_id}/。\n\n"
            f"## 存取規則\n"
            f"- 所有檔案產出必須寫入 sessions/{session_id}/ 內\n"
            f"- 使用 Shell 或 Python 工具時，檔案路徑請使用相對路徑 sessions/{session_id}/\n"
            f"- 你可以讀取其他 session 的資料夾，但絕對不可寫入\n"
            f"- data/ 和 memories/ 為共用目錄，所有 session 皆可讀寫\n"
            f"- 禁止在工作區根目錄直接建立檔案"
        )

        # Append skills metadata
        skills = self._load_skills_metadata()
        if skills:
            skill_lines = []
            for s in skills:
                skill_lines.append(
                    f"- **{s['name']}** [{s['source']}]：{s['description']}\n"
                    f"  路徑：`{s['path']}`"
                )
            skill_list = "\n".join(skill_lines)
            context += f"""

## 可用 Skills

以下 skill 可供你使用。當使用者的請求符合 skill 的觸發條件時，
分派給 code_agent，讓它用 terminal 工具讀取對應的 SKILL.md 完整內容（`cat <路徑>`），
然後按照其中的指示執行。

{skill_list}

### Skill 使用規則
- 判斷是否觸發 skill 時，以 description 為依據
- 觸發後讓 code_agent 先讀取完整 SKILL.md，再按步驟執行
- 如果 skill 目錄下有 scripts/ 或 references/，按需讀取"""

        return context

    async def invoke(self, message: str, session_id: str) -> dict[str, Any]:
        """Process a user message (synchronous)."""
        self._ensure_session_dir(session_id)
        self._active_requests += 1
        try:
            agent = self._get_or_create_agent(session_id)
            if agent is not None:
                result = await agent.ainvoke(
                    {"messages": [
                        ("system", self._session_context_message(session_id)),
                        ("user", message),
                    ]},
                    config={
                        "configurable": {"thread_id": session_id},
                        "recursion_limit": self.config.recursion_limit,
                    },
                )
                ai_message = result["messages"][-1]
                return {
                    "content": ai_message.content,
                    "session_id": session_id,
                    "tool_calls": [
                        {"name": tc["name"], "args": tc.get("args", {})}
                        for tc in getattr(ai_message, "tool_calls", [])
                    ],
                }
            else:
                return {
                    "content": f"[Stub Agent] workspace={self.workspace_id}, message={message}",
                    "session_id": session_id,
                    "tool_calls": [],
                }
        finally:
            self._active_requests -= 1
            if self._shutting_down and self._active_requests == 0:
                self._drain_event.set()

    async def invoke_stream(self, message: str, session_id: str):
        """Process a user message (streaming via astream_events).

        Yields structured event dicts for the SSE handler:
          - {"type": "content", "content": "token"}
          - {"type": "tool_call", "name": "...", "args": {...}}
          - {"type": "tool_result", "tool_name": "...", "content": "..."}
          - {"type": "thinking"}  (keepalive during LLM reasoning)
          - {"type": "error", "error": "..."}
        """
        self._ensure_session_dir(session_id)
        self._active_requests += 1
        try:
            agent = self._get_or_create_agent(session_id)
            if agent is not None:
                async for event in agent.astream_events(
                    {"messages": [
                        ("system", self._session_context_message(session_id)),
                        ("user", message),
                    ]},
                    config={
                        "configurable": {"thread_id": session_id},
                        "recursion_limit": self.config.recursion_limit,
                    },
                    version="v2",
                ):
                    kind = event.get("event", "")
                    tags = event.get("tags", [])
                    checkpoint_ns = event.get("metadata", {}).get("checkpoint_ns", "")
                    ns_prefix = checkpoint_ns.split(":")[0] if checkpoint_ns else ""
                    is_sub_agent = ns_prefix in ("research_agent", "code_agent")

                    # LLM token streaming — only emit supervisor's content tokens
                    if kind == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk", None)
                        if chunk:
                            content = getattr(chunk, "content", "")
                            if content and not is_sub_agent:
                                yield {"type": "content", "content": content}
                            elif not content and not is_sub_agent:
                                yield {"type": "thinking"}

                    # Tool start — emit tool_call events
                    elif kind == "on_tool_start":
                        tool_name = event.get("name", "")
                        tool_input = event.get("data", {}).get("input", {})
                        # Skip internal handoff tools
                        if not tool_name.startswith("transfer_"):
                            yield {"type": "tool_call", "name": tool_name, "args": tool_input}

                    # Tool end — emit tool_result events
                    elif kind == "on_tool_end":
                        tool_name = event.get("name", "")
                        output = event.get("data", {}).get("output", "")
                        if not tool_name.startswith("transfer_"):
                            # Extract content from ToolMessage or string
                            if hasattr(output, "content"):
                                content = output.content
                            else:
                                content = str(output) if output else ""
                            yield {"type": "tool_result", "tool_name": tool_name, "content": content}

            else:
                yield {
                    "type": "content",
                    "content": f"[Stub Agent] workspace={self.workspace_id}, message={message}",
                }
        except Exception as e:
            err_name = type(e).__name__
            if "Recursion" in err_name:
                err_msg = "Agent 處理步驟已達上限，已基於目前結果回覆。"
            else:
                err_msg = f"Agent 處理發生錯誤：{err_name}"
            logger.warning("Stream error: %s: %s", err_name, e)
            yield {"type": "error", "error": err_msg}
        finally:
            self._active_requests -= 1
            if self._shutting_down and self._active_requests == 0:
                self._drain_event.set()

    async def shutdown(self) -> None:
        """Graceful shutdown — save state, release resources."""
        logger.info("Shutting down Deep Agents runtime")
        self._shutting_down = True

        # 1. Wait for in-flight requests (max 10s)
        if self._active_requests > 0:
            logger.info("Waiting for %d active requests to drain", self._active_requests)
            try:
                await asyncio.wait_for(self._drain_event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("Drain timeout, %d requests still active", self._active_requests)

        # 2. Save shutdown state to PVC
        if self.backend:
            try:
                state = json.dumps({
                    "workspace_id": self.workspace_id,
                    "pod_name": self.pod_name,
                    "shutdown_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "sigterm",
                })
                await self.backend.write_file("last_shutdown.json", state)
                logger.info("Shutdown state saved to PVC")
            except Exception:
                logger.exception("Failed to save shutdown state")

        # 3. Close PostgreSQL checkpointer
        if self._pg_conn:
            try:
                await self._pg_conn.close()
                logger.info("PostgreSQL checkpointer connection closed")
            except Exception:
                logger.exception("Failed to close PostgreSQL connection")

        # 4. Close backend
        if self.backend:
            await self.backend.close()

        logger.info("Deep Agents runtime shutdown complete")
