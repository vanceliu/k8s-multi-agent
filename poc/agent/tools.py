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


# ── CWA (Central Weather Administration) Weather Tool ────────────

# 台灣 22 縣市 → 鄉鎮預報 dataset ID 對照表
# 每個縣市有兩個 dataset：3天逐3小時預報 / 1週逐12小時預報
# F-D0047-089 / F-D0047-091 為全台彙整端點
_CWA_TOWNSHIP_DATASETS: dict[str, tuple[str, str]] = {
    # 縣市名稱: (3天 dataset_id, 1週 dataset_id)
    "宜蘭縣": ("F-D0047-001", "F-D0047-003"),
    "桃園市": ("F-D0047-005", "F-D0047-007"),
    "新竹縣": ("F-D0047-009", "F-D0047-011"),
    "苗栗縣": ("F-D0047-013", "F-D0047-015"),
    "彰化縣": ("F-D0047-017", "F-D0047-019"),
    "南投縣": ("F-D0047-021", "F-D0047-023"),
    "雲林縣": ("F-D0047-025", "F-D0047-027"),
    "嘉義縣": ("F-D0047-029", "F-D0047-031"),
    "屏東縣": ("F-D0047-033", "F-D0047-035"),
    "臺東縣": ("F-D0047-037", "F-D0047-039"),
    "花蓮縣": ("F-D0047-041", "F-D0047-043"),
    "澎湖縣": ("F-D0047-045", "F-D0047-047"),
    "基隆市": ("F-D0047-049", "F-D0047-051"),
    "新竹市": ("F-D0047-053", "F-D0047-055"),
    "嘉義市": ("F-D0047-057", "F-D0047-059"),
    "臺北市": ("F-D0047-061", "F-D0047-063"),
    "高雄市": ("F-D0047-065", "F-D0047-067"),
    "新北市": ("F-D0047-069", "F-D0047-071"),
    "臺中市": ("F-D0047-073", "F-D0047-075"),
    "臺南市": ("F-D0047-077", "F-D0047-079"),
    "連江縣": ("F-D0047-081", "F-D0047-083"),
    "金門縣": ("F-D0047-085", "F-D0047-087"),
}

# 台灣縣市名稱集合（用於驗證）
_TW_LOCATIONS = set(_CWA_TOWNSHIP_DATASETS.keys())

# 查詢類型定義
_CWA_QUERY_TYPES = {"forecast_36h", "forecast_3d", "forecast_7d"}


class CWAWeatherTool(BaseTool):
    """查詢台灣中央氣象署開放資料 API 的天氣工具。"""

    name: str = "taiwan_weather"
    description: str = (
        "查詢台灣天氣預報（中央氣象署 CWA 開放資料 API）。\n"
        "\n"
        "【查詢類型】\n"
        "  forecast_36h — 今明 36 小時天氣預報（縣市級，天氣現象/降雨機率/溫度）\n"
        "  forecast_3d  — 未來 3 天逐 3 小時天氣預報（鄉鎮級，需指定縣市）\n"
        "  forecast_7d  — 未來 1 週逐 12 小時天氣預報（鄉鎮級，需指定縣市）\n"
        "\n"
        "【輸入格式】\n"
        "  '<查詢類型> [縣市名稱]'\n"
        "  - forecast_36h 可不帶縣市（回傳全台），也可指定縣市篩選\n"
        "  - forecast_3d 和 forecast_7d 必須指定縣市名稱\n"
        "  - 若只輸入縣市名稱（無查詢類型），預設使用 forecast_36h\n"
        "\n"
        "【可用縣市】（共 22 個，必須使用正式名稱）\n"
        "  臺北市、新北市、桃園市、臺中市、臺南市、高雄市、\n"
        "  基隆市、新竹市、嘉義市、新竹縣、苗栗縣、彰化縣、\n"
        "  南投縣、雲林縣、嘉義縣、屏東縣、宜蘭縣、花蓮縣、\n"
        "  臺東縣、澎湖縣、金門縣、連江縣\n"
        "  注意：請用「臺」不要用「台」（例如「臺北市」而非「台北市」）\n"
        "\n"
        "【範例】\n"
        "  'forecast_36h'          → 全台 36 小時預報\n"
        "  'forecast_36h 臺北市'   → 臺北市 36 小時預報\n"
        "  'forecast_3d 桃園市'    → 桃園市未來 3 天逐 3 小時鄉鎮預報\n"
        "  'forecast_7d 高雄市'    → 高雄市未來 1 週鄉鎮預報\n"
        "  '臺北市'               → 等同 forecast_36h 臺北市\n"
    )
    api_base: str = ""
    api_key: str = ""

    def _normalize_location(self, name: str) -> str:
        """將「台」正規化為「臺」。"""
        return name.replace("台北", "臺北").replace("台中", "臺中") \
                    .replace("台南", "臺南").replace("台東", "臺東")

    def _parse_query(self, query: str) -> tuple[str, str | None]:
        """Parse query into (query_type, location_name)."""
        parts = query.strip().split(maxsplit=1)
        query_type = parts[0] if parts else "forecast_36h"
        location = parts[1] if len(parts) > 1 else None

        # 如果第一個 token 不是已知查詢類型，當作地名處理
        if query_type not in _CWA_QUERY_TYPES:
            location = query.strip()
            query_type = "forecast_36h"

        if location:
            location = self._normalize_location(location)

        return query_type, location

    def _resolve_dataset_id(self, query_type: str, location: str | None) -> tuple[str, str | None]:
        """Resolve query_type + location to a CWA dataset ID.

        Returns (dataset_id, error_message). error_message is None on success.
        """
        if query_type == "forecast_36h":
            return "F-C0032-001", None

        # forecast_3d / forecast_7d 需要縣市名稱
        if not location:
            return "", (
                f"查詢類型 '{query_type}' 需要指定縣市名稱。\n"
                f"範例：'{query_type} 臺北市'\n"
                f"可用縣市：{', '.join(sorted(_TW_LOCATIONS))}"
            )

        # 從縣市名稱中提取（支援「臺北市中山區」→「臺北市」）
        county = None
        for name in _TW_LOCATIONS:
            if location.startswith(name):
                county = name
                break
        if not county and location in _TW_LOCATIONS:
            county = location

        if not county:
            return "", (
                f"找不到縣市 '{location}'。\n"
                f"可用縣市：{', '.join(sorted(_TW_LOCATIONS))}\n"
                f"注意：請用「臺」不要用「台」"
            )

        ds_3d, ds_7d = _CWA_TOWNSHIP_DATASETS[county]
        if query_type == "forecast_3d":
            return ds_3d, None
        else:  # forecast_7d
            return ds_7d, None

    def _format_forecast_36h(self, records: dict, location_filter: str | None) -> str:
        """Format F-C0032-001 (36h county-level forecast) response."""
        locations = records.get("location", [])
        if location_filter:
            locations = [loc for loc in locations if loc.get("locationName") == location_filter]
        if not locations:
            return f"找不到 '{location_filter}' 的天氣資料。可用縣市：{', '.join(sorted(_TW_LOCATIONS))}"

        lines = [f"📋 {records.get('datasetDescription', '36小時天氣預報')}"]
        for loc in locations[:5]:
            lines.append(f"\n🏙️ {loc['locationName']}")
            for elem in loc.get("weatherElement", []):
                elem_name = elem.get("elementName", "")
                for t in elem.get("time", []):
                    start = t.get("startTime", "")
                    end = t.get("endTime", "")
                    param = t.get("parameter", {})
                    value = param.get("parameterName", "")
                    unit = param.get("parameterUnit", "")
                    lines.append(f"  {elem_name}: {value}{unit} ({start} ~ {end})")
        return "\n".join(lines)

    def _format_township_forecast(self, records: dict, location_filter: str | None) -> str:
        """Format F-D0047-XXX (township-level forecast) response.

        Response structure: records.Locations[].Location[].WeatherElement[].Time[]
        """
        loc_groups = records.get("Locations", [])
        if not loc_groups:
            return "無預報資料。"

        loc_group = loc_groups[0]
        desc = loc_group.get("DatasetDescription", "鄉鎮天氣預報")
        county_name = loc_group.get("LocationsName", "")
        townships = loc_group.get("Location", [])

        # 如果 location_filter 包含鄉鎮名稱（如「中山區」），嘗試篩選
        township_filter = None
        if location_filter and location_filter != county_name:
            # 去掉縣市前綴取鄉鎮名
            township_filter = location_filter.replace(county_name, "").strip()

        if township_filter:
            townships = [t for t in townships if township_filter in t.get("LocationName", "")]

        if not townships:
            all_names = [t.get("LocationName", "") for t in loc_group.get("Location", [])]
            return (
                f"找不到 '{township_filter or location_filter}' 的鄉鎮資料。\n"
                f"{county_name} 可用鄉鎮：{', '.join(all_names[:20])}"
            )

        # 選取重要天氣因子顯示
        key_elements = {"天氣現象", "溫度", "最高溫度", "最低溫度", "平均溫度",
                        "3小時降雨機率", "12小時降雨機率", "相對濕度", "平均相對濕度",
                        "風速", "天氣預報綜合描述"}

        lines = [f"📋 {desc}（{county_name}）"]
        for tw in townships[:5]:
            lines.append(f"\n📍 {tw['LocationName']}")
            for elem in tw.get("WeatherElement", []):
                elem_name = elem.get("ElementName", "")
                if elem_name not in key_elements:
                    continue
                times = elem.get("Time", [])
                for t in times[:6]:  # 最多顯示 6 個時段
                    # 3天預報用 DataTime，1週預報用 StartTime~EndTime
                    data_time = t.get("DataTime", "")
                    start_time = t.get("StartTime", "")
                    end_time = t.get("EndTime", "")
                    time_str = data_time if data_time else f"{start_time} ~ {end_time}"

                    values = t.get("ElementValue", [])
                    if values and isinstance(values, list):
                        # ElementValue 是 list of dict，取第一個的值
                        val_dict = values[0] if values else {}
                        val_parts = [f"{v}" for v in val_dict.values() if v and v != "-99"]
                        val_str = ", ".join(val_parts)
                    else:
                        val_str = str(values)
                    lines.append(f"  {elem_name}: {val_str} ({time_str})")
        return "\n".join(lines)

    def _call_api(self, dataset_id: str, location: str | None,
                  param_key: str = "locationName") -> dict:
        """Synchronous API call to CWA."""
        params: dict[str, str] = {"Authorization": self.api_key}
        if location:
            params[param_key] = location

        url = f"{self.api_base}/{dataset_id}"
        with httpx.Client(timeout=15.0, verify=False) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    async def _async_call_api(self, dataset_id: str, location: str | None,
                              param_key: str = "locationName") -> dict:
        """Async API call to CWA."""
        params: dict[str, str] = {"Authorization": self.api_key}
        if location:
            params[param_key] = location

        url = f"{self.api_base}/{dataset_id}"
        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    def _format_response(self, data: dict, query_type: str, location: str | None) -> str:
        """Route to appropriate formatter."""
        if data.get("success") != "true":
            return f"API 回傳錯誤：{data}"

        records = data.get("records", {})
        if query_type == "forecast_36h":
            return self._format_forecast_36h(records, location)
        else:
            return self._format_township_forecast(records, location)

    def _execute(self, query: str) -> str:
        """Core logic shared by _run and _arun."""
        if not self.api_key:
            return "CWA API Key 未設定。請設定環境變數 CWA_API_KEY。"

        query_type, location = self._parse_query(query)
        dataset_id, err = self._resolve_dataset_id(query_type, location)
        if err:
            return err

        return query_type, dataset_id, location

    def _run(self, query: str) -> str:
        result = self._execute(query)
        if isinstance(result, str):
            return result
        query_type, dataset_id, location = result

        # forecast_36h 用 locationName 篩選縣市；鄉鎮預報不需要（dataset 已對應縣市）
        api_location = location if query_type == "forecast_36h" else None

        try:
            data = self._call_api(dataset_id, api_location)
            return self._format_response(data, query_type, location)
        except Exception as e:
            return f"查詢氣象資料失敗：{e}"

    async def _arun(self, query: str) -> str:
        result = self._execute(query)
        if isinstance(result, str):
            return result
        query_type, dataset_id, location = result

        api_location = location if query_type == "forecast_36h" else None

        try:
            data = await self._async_call_api(dataset_id, api_location)
            return self._format_response(data, query_type, location)
        except Exception as e:
            return f"查詢氣象資料失敗：{e}"


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
            logger.debug("[duckduckgo_search] BLOCKED (call %d > max %d), query: %s",
                        self._call_count, self.max_calls, query)
            return "搜尋次數已達上限，請立即根據已有的搜尋結果整理摘要並回傳。"
        logger.debug("[duckduckgo_search] query (%d/%d): %s",
                    self._call_count, self.max_calls, query)
        try:
            result = self._inner.run(query)
            logger.debug("[duckduckgo_search] result length: %d chars", len(result))
            return result
        except Exception as e:
            logger.warning("[duckduckgo_search] search failed: %s", e)
            return f"搜尋失敗：{e}。請根據已有資訊回傳結果。"

    async def _arun(self, query: str) -> str:
        return self._run(query)


def get_research_tools() -> list:
    """Tools for research sub-agent: web search + Taiwan weather."""
    tools = []

    # DuckDuckGo search
    try:
        search_tool = LoggingSearchTool()
        tools.append(search_tool)
        logger.info("LoggingSearchTool (DuckDuckGo) loaded")
    except ImportError:
        logger.warning("DuckDuckGoSearchRun not available")

    # CWA Taiwan weather
    from poc.utils.config import CWA_API_BASE, CWA_API_KEY
    if CWA_API_KEY:
        weather_tool = CWAWeatherTool(api_base=CWA_API_BASE, api_key=CWA_API_KEY)
        tools.append(weather_tool)
        logger.info("CWAWeatherTool loaded")
    else:
        logger.warning("CWA_API_KEY not set, CWAWeatherTool disabled")

    return tools


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