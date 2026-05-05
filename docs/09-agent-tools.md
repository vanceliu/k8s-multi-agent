# 09 Agent 工具（Tools）

本文件描述 Agent 容器內可用的工具定義、分組策略、沙箱機制，以及外部 API 整合工具。

> **與 06-agent-container.md 的關係**：06 聚焦 Agent 容器的整體架構（Runtime、HTTP 層、記憶、S3Backend）；本文件聚焦工具層的設計與實作細節。

---

## 1. 工具架構概覽

```
Supervisor (langgraph-supervisor)
├── research_agent
│   ├── duckduckgo_search    — 網路搜尋（rate-limited, max 5 calls/turn）
│   └── taiwan_weather       — 台灣氣象查詢（CWA 開放資料 API）
├── code_agent
│   ├── terminal             — Sandboxed Shell（cwd 強制 session 目錄）
│   └── python_repl          — Sandboxed Python REPL（workspace 邊界限制）
└── common (所有 agent 共用)
    └── notify_orchestrator_activity — 通知 Orchestrator 更新活動時間戳
```

### 1.1 工具分組策略

| 分組 | 函式 | 用途 | 分配給 |
|------|------|------|--------|
| `get_research_tools()` | 搜尋 + 氣象 | 資訊蒐集 | research_agent |
| `get_code_tools(workspace_path, session_id)` | Shell + Python | 程式執行 | code_agent |
| `get_common_tools(workspace_id, orchestrator_url)` | 活動通知 | 系統維運 | 所有 agent |
| `get_workspace_tools(...)` | 全部合併 | Flat agent fallback | 單一 agent |

---

## 2. Research Tools

### 2.1 DuckDuckGo Search（LoggingSearchTool）

**Tool name**: `duckduckgo_search`

**描述**：搜尋網路上的即時資訊。適合查詢天氣、新聞、技術文件等最新資料。

**特性**：
- 基於 `langchain-community` 的 `DuckDuckGoSearchRun`
- Rate-limited：每輪對話最多 5 次呼叫，超過後回傳「搜尋次數已達上限」
- 每次呼叫都有 debug log（query + result length）
- 搜尋失敗時 graceful fallback

**輸入**：搜尋關鍵字字串

**輸出**：搜尋結果摘要文字

---

### 2.2 台灣氣象查詢（CWAWeatherTool）

**Tool name**: `taiwan_weather`

**描述**：查詢台灣天氣資訊（中央氣象署 CWA 開放資料 API）

**API 來源**：[中央氣象署開放資料平台](https://opendata.cwa.gov.tw)

**設定**（`poc/utils/config.py`）：
```python
CWA_API_BASE = os.getenv("CWA_API_BASE", "https://opendata.cwa.gov.tw/api/v1/rest/datastore")
CWA_API_KEY = os.getenv("CWA_API_KEY", "")
```

**環境變數傳遞路徑**：
```
utils/config.py → orchestrator/k8s_client.py（建立 Agent Pod 時注入）→ Agent Pod 內 tools.py 讀取
```

若 `CWA_API_KEY` 未設定，工具不會載入（graceful skip）。

**支援的資料集**：

| 查詢類型 | 資料集 ID | 說明 |
|---------|-----------|------|
| `forecast_36h` | F-C0032-001 | 今明 36 小時天氣預報（全台縣市） |
| `forecast_7d` | F-C0032-005 | 一週天氣預報（全台縣市） |
| `observation` | O-A0003-001 | 即時氣象觀測（溫度、濕度、風速等） |
| `rain` | O-A0002-001 | 即時雨量觀測 |

**輸入格式**：`<查詢類型> [縣市名稱]`

**使用範例**：
- `taiwan_weather("forecast_36h 臺北市")` — 臺北市 36 小時預報
- `taiwan_weather("forecast_7d 高雄市")` — 高雄市一週預報
- `taiwan_weather("observation")` — 全台即時觀測
- `taiwan_weather("rain 臺中市")` — 臺中市即時雨量
- `taiwan_weather("臺南市")` — 預設查 36 小時預報（第一個 token 非已知 key 時）

**支援縣市**：臺北市、新北市、桃園市、臺中市、臺南市、高雄市、基隆市、新竹市、嘉義市、新竹縣、苗栗縣、彰化縣、南投縣、雲林縣、嘉義縣、屏東縣、宜蘭縣、花蓮縣、臺東縣、澎湖縣、金門縣、連江縣

**輸出格式**：格式化的天氣資訊文字（含 emoji 標記），例如：
```
📋 一般天氣預報-今明 36 小時天氣預報

🏙️ 臺北市
  Wx: 晴時多雲 (2026-04-28 06:00:00 ~ 2026-04-28 18:00:00)
  MinT: 22°C (2026-04-28 06:00:00 ~ 2026-04-28 18:00:00)
  MaxT: 30°C (2026-04-28 06:00:00 ~ 2026-04-28 18:00:00)
```

**實作細節**：
- 同步版 `_run` 和非同步版 `_arun` 都使用 `httpx`（無 `requests` 依賴）
- Authorization 透過 query parameter 傳遞（CWA API 規格）
- 預報類回應走 `_format_forecast()`，觀測類走 `_format_observation()`
- 輸入解析由 `_parse_query()` 處理（注意：不可命名為 `_parse_input`，會與 `BaseTool` 內部方法衝突）
- 最多顯示 5 個縣市（預報）或 10 個測站（觀測），避免回應過長
- SSL 驗證已停用（`verify=False`）：CWA 伺服器憑證缺少 Subject Key Identifier，Python 3.13+ 會拒絕連線

---

## 3. Code Tools

### 3.1 Sandboxed Shell（SandboxedShellTool）

**Tool name**: `terminal`

**描述**：在工作區內執行 shell 命令

**沙箱機制**：
- `cwd` 強制為 `sessions/{session_id}/`
- `HOME` 環境變數設為 workspace_path
- Escape pattern 攔截（拒絕 `cd /`、`cd ~`、`rm -rf /`、redirect 到絕對路徑等）
- 執行超時 120 秒

**攔截的 escape patterns**：
```python
cd /anything        # 切換到絕對路徑
cd ~                # 切換到 home
cd ../../           # 向上兩層以上
> /path             # redirect 寫入絕對路徑
rm -rf /path        # 刪除絕對路徑
mv ... /path        # 移動到絕對路徑
cp ... /path        # 複製到絕對路徑
ln ... /path        # symlink 到絕對路徑
```

### 3.2 Sandboxed Python REPL（SandboxedPythonREPLTool）

**Tool name**: `python_repl`

**描述**：執行 Python 程式碼。適合資料分析、計算、檔案處理等任務。

**沙箱機制**：
- `os.chdir(session_cwd)` 注入在使用者程式碼之前
- `open()` 函式被 patch：任何存取 workspace 外的路徑都會拋出 `PermissionError`
- 預裝套件：matplotlib, pandas, numpy, openpyxl

**安全邊界**：
```python
# 注入的 sandbox prefix（每次執行前自動加入）
import os; os.chdir(session_cwd)
from pathlib import Path
_ws_root = Path(workspace_path).resolve()
# patch open() to block access outside workspace
```

---

## 4. Common Tools

### 4.1 Notify Orchestrator Activity

**Tool name**: `notify_orchestrator_activity`

**描述**：通知 Orchestrator 更新 session 活動時間戳。在執行耗時任務後呼叫，避免被誤判為閒置。

**呼叫目標**：`POST {orchestrator_url}/api/v1/orchestrator/mark-activity`

**輸入**：`session_id`（目前的 session ID）

---

## 5. Production 工具設計（規劃中）

Production 版本將 S3Backend 取代 PVC 本地操作，工具改為：

| Tool | 說明 |
|------|------|
| `notify_orchestrator_activity` | 同 POC |
| `list_workspace_files` | 列出 S3 prefix 下的檔案 |
| `read_workspace_file` | 從 S3 讀取檔案 |
| `write_workspace_file` | 寫入檔案到 S3 |

```python
# Production 版本工具定義（app/agent/tools.py）
def get_workspace_tools(s3_backend, user_id, orchestrator_url) -> list:
    return [
        notify_orchestrator_activity,
        list_workspace_files,   # S3Backend.ls()
        read_workspace_file,    # S3Backend.read_file()
        write_workspace_file,   # S3Backend.write_file()
    ]
```

> POC 版本使用 `SandboxedShellTool` 和 `SandboxedPythonREPLTool` 取代 S3 檔案工具，
> 提供 workspace 邊界硬限制。Production 版本應在 S3Backend 層面實作同等的 session 隔離。

---

## 6. 新增工具指南

要新增一個 Agent tool：

1. **定義 Tool class**：在 `poc/agent/tools.py` 繼承 `BaseTool`，實作 `_run` + `_arun`
2. **設定 config**：若需要 API Key 或外部設定，加到 `poc/utils/config.py`
3. **注入環境變數**：在 `poc/orchestrator/k8s_client.py` 的 Pod env 列表加入對應 `V1EnvVar`
4. **整合到分組函式**：加到 `get_research_tools()` 或 `get_code_tools()` 或 `get_common_tools()`
5. **更新文件**：本文件 + `docs/02-api-design.md` + `docs/07-deployment.md`（環境變數）
