"""Custom tools for Deep Agent — sandboxed shell + python REPL + orchestrator notification.

All tools are sandboxed to workspace_path:
  - Shell: cwd forced to session directory, escape patterns rejected
  - Python REPL: os.chdir to session directory + patched open() block access outside workspace
  - Session isolation: cwd = sessions/{session_id}/ (hard), workspace boundary enforced

Tools are split into groups for supervisor sub-agents:
  - research_tools: duckduckgo_search (rate-limited)
  - code_tools: terminal + python_repl
  - common_tools: notify_orchestrator_activity
"""

import asyncio
import logging
import os
import re
import subprocess
from typing import Annotated

import httpx
from langchain_core.tools import BaseTool, tool

logger = logging.getLogger(__name__)

# ── Patterns that attempt to escape workspace ──────────────────────
_ESCAPE_PATTERNS = [
    re.compile(r"\bcd\s+/"),           # cd /anything
    re.compile(r"\bcd\s+~"),           # cd ~
    re.compile(r"\bcd\s+(\.\./){2}"),  # cd ../../ (2+ levels up)
    re.compile(r"(?:>|>>)\s*/"),       # redirect write to absolute path
    re.compile(r"\brm\s+-[rf]*\s+/"),  # rm on absolute path
    re.compile(r"\bmv\s+.+\s+/"),      # mv target to absolute path
    re.compile(r"\bcp\s+.+\s+/"),      # cp target to absolute path
    re.compile(r"\bln\s+.+\s+/"),      # symlink to absolute path
]


class SandboxedShellTool(BaseTool):
    """Shell tool sandboxed to session directory within workspace."""

    name: str = "terminal"
    description: str = "在工作區內執行 shell 命令。"
    workspace_path: str = "/tmp"
    session_cwd: str = "/tmp"

    def _validate_command(self, command: str) -> str | None:
        """Return error message if command tries to escape workspace, else None."""
        for pattern in _ESCAPE_PATTERNS:
            if pattern.search(command):
                return (
                    f"命令被拒絕：偵測到可能離開工作區的操作。"
                    f"所有命令必須在 {self.workspace_path} 內執行。"
                )
        return None

    def _make_env(self) -> dict[str, str]:
        return {**os.environ, "HOME": self.workspace_path}

    def _run(self, command: str) -> str:
        if err := self._validate_command(command):
            return err
        try:
            result = subprocess.run(
                ["bash", "-c", command],
                cwd=self.session_cwd,
                capture_output=True,
                text=True,
                timeout=120,
                env=self._make_env(),
            )
            output = result.stdout + result.stderr
            return output.strip() if output.strip() else "(no output)"
        except subprocess.TimeoutExpired:
            return "命令執行超時（120 秒）"
        except Exception as e:
            return f"執行失敗：{e}"

    async def _arun(self, command: str) -> str:
        if err := self._validate_command(command):
            return err
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", command,
                cwd=self.session_cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._make_env(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = (stdout or b"").decode() + (stderr or b"").decode()
            return output.strip() if output.strip() else "(no output)"
        except asyncio.TimeoutError:
            return "命令執行超時（120 秒）"
        except Exception as e:
            return f"執行失敗：{e}"


class SandboxedPythonREPLTool(BaseTool):
    """Python REPL sandboxed to session directory within workspace.

    Injects os.chdir(session_cwd) and patches open() to block
    file access outside workspace.
    """

    name: str = "python_repl"
    description: str = "執行 Python 程式碼。適合資料分析、計算、檔案處理等任務。"
    workspace_path: str = "/tmp"
    session_cwd: str = "/tmp"

    def _build_sandbox_prefix(self) -> str:
        """Python code injected before user code to enforce sandbox."""
        return (
            f"import os as _os; _os.chdir({self.session_cwd!r})\n"
            f"from pathlib import Path as _OrigPath\n"
            f"_ws_root = _OrigPath({self.workspace_path!r}).resolve()\n"
            f"_orig_open = open\n"
            f"def open(file, mode='r', *a, **kw):\n"
            f"    p = _OrigPath(file).resolve()\n"
            f"    if not str(p).startswith(str(_ws_root)):\n"
            f"        raise PermissionError(f'Access denied: {{file}} is outside workspace')\n"
            f"    return _orig_open(file, mode, *a, **kw)\n"
        )

    def _run(self, command: str) -> str:
        full_code = self._build_sandbox_prefix() + "\n" + command
        try:
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                exec(full_code, {"__builtins__": __builtins__})
            output = buf.getvalue()
            return output.strip() if output.strip() else "(no output)"
        except Exception as e:
            return f"執行失敗：{e}"

    async def _arun(self, command: str) -> str:
        return self._run(command)


# ── Tool factories for sub-agents ─────────────────────────────────

def _session_cwd(workspace_path: str, session_id: str | None) -> str:
    if session_id and workspace_path:
        return os.path.join(workspace_path, "sessions", session_id)
    return workspace_path


def get_common_tools(workspace_id: str, orchestrator_url: str) -> list:
    """Tools shared across all agents: orchestrator notification."""
    @tool
    async def notify_orchestrator_activity(
        session_id: Annotated[str, "目前的 session ID"],
    ) -> str:
        """通知 Orchestrator 更新 session 活動時間戳。
        在執行耗時任務後呼叫，避免被誤判為閒置。"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{orchestrator_url}/api/v1/orchestrator/mark-activity",
                    json={"user_id": workspace_id, "session_id": session_id},
                )
            return "Activity reported successfully"
        except Exception as e:
            return f"Failed to report activity: {e}"

    return [notify_orchestrator_activity]


class LoggingSearchTool(BaseTool):
    """DuckDuckGo search wrapper that logs every query and enforces a hard call limit per turn."""

    name: str = "duckduckgo_search"
    description: str = (
        "搜尋網路上的即時資訊。適合查詢天氣、新聞、技術文件等最新資料。"
        "輸入搜尋關鍵字，回傳相關結果摘要。"
    )
    max_calls: int = 5
    _inner: object = None
    _call_count: int = 0

    def __init__(self, max_calls: int = 5, **kwargs):
        super().__init__(**kwargs)
        from langchain_community.tools import DuckDuckGoSearchRun
        self._inner = DuckDuckGoSearchRun()
        self._call_count = 0
        self.max_calls = max_calls

    def _run(self, query: str) -> str:
        self._call_count += 1
        if self._call_count > self.max_calls:
            logger.info("[duckduckgo_search] BLOCKED (call %d > max %d), query: %s",
                        self._call_count, self.max_calls, query)
            return "搜尋次數已達上限，請立即根據已有的搜尋結果整理摘要並回傳。"
        logger.info("[duckduckgo_search] query (%d/%d): %s",
                    self._call_count, self.max_calls, query)
        try:
            result = self._inner.run(query)
            logger.info("[duckduckgo_search] result length: %d chars", len(result))
            return result
        except Exception as e:
            logger.warning("[duckduckgo_search] search failed: %s", e)
            return f"搜尋失敗：{e}。請根據已有資訊回傳結果。"

    async def _arun(self, query: str) -> str:
        return self._run(query)


def get_research_tools() -> list:
    """Tools for research sub-agent: web search."""
    try:
        search_tool = LoggingSearchTool()
        logger.info("LoggingSearchTool (DuckDuckGo) loaded")
        return [search_tool]
    except ImportError:
        logger.warning("DuckDuckGoSearchRun not available")
        return []


def get_code_tools(workspace_path: str, session_id: str | None = None) -> list:
    """Tools for code sub-agent: shell + python REPL."""
    tools = []
    cwd = _session_cwd(workspace_path, session_id)

    if workspace_path:
        shell_tool = SandboxedShellTool(
            workspace_path=workspace_path,
            session_cwd=cwd,
            description=(
                f"在工作區內執行 shell 命令。工作目錄：{cwd}。"
                "可用於執行腳本、檔案操作（ls, cat, echo, cp, mv 等）。"
                "所有命令都被限制在工作區目錄內。"
                "存取共用目錄請使用 ../../data/ 或 ../../memories/。"
            ),
        )
        tools.append(shell_tool)
        logger.info("SandboxedShellTool loaded (cwd=%s)", cwd)

        python_tool = SandboxedPythonREPLTool(
            workspace_path=workspace_path,
            session_cwd=cwd,
            description=(
                f"執行 Python 程式碼。工作目錄：{cwd}。"
                "適合資料分析、計算、檔案處理等任務。"
                "已預裝 matplotlib, pandas, numpy, openpyxl。"
                "所有檔案操作限制在工作區內。"
            ),
        )
        tools.append(python_tool)
        logger.info("SandboxedPythonREPLTool loaded (cwd=%s)", cwd)

    return tools


def get_workspace_tools(
    workspace_id: str,
    orchestrator_url: str,
    workspace_path: str = "",
    session_id: str | None = None,
) -> list:
    """Return ALL tools (flat list). Used as fallback if supervisor is unavailable."""
    tools = get_common_tools(workspace_id, orchestrator_url)
    tools.extend(get_code_tools(workspace_path, session_id))
    tools.extend(get_research_tools())
    return tools