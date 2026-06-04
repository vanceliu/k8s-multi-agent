# 16 - Claw Code 架構比較與學習方向

## 概述

本文件比較 Claw Code（Rust 實作的 CLI Agent Harness）與本專案（dbt-openclaw）的設計差異，並整理可借鏡的設計模式與具體行動建議。

Claw Code 專案：https://github.com/vanceliu/claw-code（fork from ultraworkers/claw-code）

## Claw Code 簡介

Claw Code 是一個以 Rust 實作的 CLI Agent Harness，核心理念是「Human direction, agent execution」— 人類提供高層意圖，Agent 負責規劃、編碼、測試與部署。

### 三層架構

| 元件 | 角色 |
|------|------|
| **OmX** (oh-my-codex) | Workflow 層 — 將短指令轉換為結構化執行（planning + execution + verification） |
| **clawhip** | Event/Notification 路由 — 監控 git commits、tmux sessions、GitHub issues/PRs、agent lifecycle |
| **OmO** (oh-my-openagent) | Multi-Agent 協調 — Architect/Executor/Reviewer 角色分工、衝突解決 |

### 技術棧

- **語言**：Rust 95% + Python 4%（audit helpers）
- **架構**：Cargo workspace（9 個 crate）
- **CLI**：Interactive REPL + One-shot prompt
- **Provider**：Anthropic / OpenAI / xAI / DashScope（多 provider 自動路由）
- **協定**：MCP (Model Context Protocol) + ACP (Agent Communication Protocol)
- **容器**：Docker/Podman 支援

### Crate 結構

```
rust/crates/
├── api/                # Provider clients + streaming + auth
├── commands/           # Slash command registry + help rendering
├── compat-harness/     # TS manifest extraction
├── mock-anthropic-service/  # 確定性 mock 服務（parity testing）
├── plugins/            # Plugin metadata + install/enable/disable
├── runtime/            # ConversationRuntime + config + session + MCP lifecycle
├── rusty-claude-cli/   # Main CLI binary (claw)
├── telemetry/          # Session tracing + usage telemetry
└── tools/              # Built-in tools + skill resolution + agent surfaces
```

---

## 專案定位差異

| 面向 | dbt-openclaw | Claw Code |
|------|-------------|-----------|
| **定位** | K8s 雲端 Agent 工作區管理系統 | 本地 CLI Agent Harness |
| **運行環境** | K8s Pod（雲端） | 本地終端機（桌面） |
| **語言** | Python (FastAPI) | Rust (actix-web 風格) |
| **使用者介面** | HTTP API + SSE + IM Channel | Interactive REPL + CLI |
| **多租戶** | workspace_members ACL + Pod 隔離 | 單使用者本地 session |
| **Agent 架構** | LangGraph Supervisor + Sub-Agent | ConversationRuntime + Tool system |
| **Provider** | ModelFactory（MiniMax/DeepSeek/GLM/Claude/OpenAI） | 多 Provider 自動路由（Anthropic/OpenAI/xAI/DashScope） |
| **部署** | K8s 容器化五元件 | cargo build 本地二進位 |

---

## 可學習的設計模式

### 1. Multi-Provider 自動路由

**Claw Code 做法**：

根據 model name prefix 自動選擇 provider，無需使用者手動指定：

- `claude-*` → Anthropic
- `grok-*` → xAI
- `gpt-*` / `openai/*` → OpenAI-compatible
- `qwen-*` / `kimi-*` → DashScope
- Unknown → fallback 依據已設定的 credentials

支援 model alias（`opus` → `claude-opus-4-6`），使用者可自定義 alias。

**對本專案的啟示**：

- 目前 ModelFactory 用 strategy pattern 手動分派，但缺乏 model alias 和自動路由
- 使用者需要知道完整 model name

**建議**：

```yaml
# config.yaml 新增
model_aliases:
  fast: "deepseek-chat"
  smart: "claude-sonnet-4-6"
  local: "ollama/llama3"

model_routing:
  prefixes:
    claude: anthropic
    deepseek: deepseek
    glm: zhipu
    gpt: openai
  fallback: deepseek  # 預設 provider
```

### 2. Permission System（權限分級）

**Claw Code 做法**：

三級權限模式控制 Agent 可執行的操作：

| Mode | 能力 |
|------|------|
| `read-only` | 只能讀取檔案、搜尋 |
| `workspace-write` | 可讀寫工作區檔案 |
| `danger-full-access` | 完全存取（含 shell 執行） |

搭配 `--allowedTools` 白名單精細控制。

**對本專案的啟示**：

- 目前 workspace_members 有 owner/admin/member/readonly 角色
- 但角色到工具的映射是隱含的，沒有明確的 permission mode 定義
- 缺乏 per-tool 白名單機制

**建議**：定義明確的 permission mode → tool mapping：

```python
PERMISSION_MODES = {
    "readonly": {
        "allowed_tools": ["search", "weather", "file_list"],
        "denied_tools": ["shell", "python_repl", "file_write", "file_delete"]
    },
    "member": {
        "allowed_tools": ["search", "weather", "file_list", "file_write", "file_upload"],
        "denied_tools": ["shell", "python_repl", "file_delete"]
    },
    "admin": {
        "allowed_tools": "*",  # all tools
        "denied_tools": []
    }
}
```

### 3. Doctor 健康檢查命令

**Claw Code 做法**：

`claw doctor` 一鍵檢查所有依賴狀態：

- API key 是否有效
- Model 是否可存取
- Tool 配置是否正確
- MCP server 是否可連線

支援 `--output-format json` 機器可讀輸出。

**對本專案的啟示**：

- 目前各元件有 `/health` endpoint，但缺乏整合性的健康檢查
- 部署後需要手動逐一確認各服務狀態

**建議**：在 Admin Service 加入 `/api/v1/admin/doctor` endpoint：

```python
@router.get("/doctor")
async def doctor_check():
    """整合性健康檢查"""
    return {
        "orchestrator": await check_orchestrator(),
        "storage_service": await check_storage(),
        "database": await check_database(),
        "kubernetes": await check_k8s_api(),
        "llm_provider": await check_llm_connectivity(),
        "overall": "healthy" | "degraded" | "unhealthy"
    }
```

### 4. Mock Parity Harness（確定性測試）

**Claw Code 做法**：

內建確定性 Anthropic-compatible mock 服務，用於端對端 parity 測試：

- `mock-anthropic-service` — 本地 mock，回傳預定義回應
- 覆蓋場景：streaming_text、read_file_roundtrip、write_file_allowed/denied、bash_permission_prompt 等
- `mock_parity_scenarios.json` — 場景清單 manifest

**對本專案的啟示**：

- 目前 E2E 測試依賴真實 LLM API（不穩定、有成本）
- Agent 行為測試難以重現

**建議**：建立 mock LLM service 用於 CI/CD：

```python
# poc/tests/mock_llm_service.py
class MockLLMService:
    """確定性 LLM mock，用於 Agent 行為測試"""

    SCENARIOS = {
        "simple_chat": {"response": "Hello! How can I help?"},
        "tool_call_search": {"tool_calls": [{"name": "search", "args": {...}}]},
        "tool_call_denied": {"response": "I cannot execute that tool."},
        "context_overflow": {"trigger_compaction": True},
    }
```

### 5. Session Persistence + Resume

**Claw Code 做法**：

- Session 自動持久化到 `.claw/sessions/`
- `--resume latest` 恢復最近 session
- `/session` 命令管理 session 列表
- `/export` 匯出 session 紀錄

**對本專案的啟示**：

- 目前用 AsyncPostgresSaver (LangGraph checkpointer) 持久化對話
- 但缺乏 session 匯出/匯入功能
- 使用者無法將對話紀錄帶走或分享

**建議**：加入 session export endpoint：

```python
@router.get("/sessions/{session_id}/export")
async def export_session(session_id: str, format: str = "json"):
    """匯出 session 對話紀錄（json / markdown / html）"""
    messages = await get_session_messages(session_id)
    if format == "markdown":
        return format_as_markdown(messages)
    return messages
```

### 6. Config Resolution Order（多層配置合併）

**Claw Code 做法**：

五層配置，後者覆蓋前者：

1. `~/.claw.json`（全域預設）
2. `~/.config/claw/settings.json`（使用者設定）
3. `<repo>/.claw.json`（專案預設）
4. `<repo>/.claw/settings.json`（專案設定）
5. `<repo>/.claw/settings.local.json`（本地覆蓋，不進 git）

**對本專案的啟示**：

- 目前是 `config.yaml` + env vars 兩層
- 缺乏 per-workspace 配置覆蓋能力
- 所有 workspace 共用同一套 Agent 配置

**建議**：加入 workspace-level 配置覆蓋：

```
配置優先級（高 → 低）：
1. 環境變數（K8s ConfigMap/Secret）
2. workspace PVC 內的 .agent/config.yaml（per-workspace 覆蓋）
3. poc/config.yaml（全域預設）
```

### 7. Slash Commands + Tab Completion

**Claw Code 做法**：

豐富的 slash command 系統，分類清晰：

- **Session**：`/help`, `/status`, `/cost`, `/resume`
- **Workspace**：`/compact`, `/clear`, `/config`, `/memory`
- **Discovery**：`/mcp`, `/agents`, `/skills`, `/doctor`, `/tasks`
- **Automation**：`/review`, `/security-review`, `/cron`
- **Plugin**：`/plugin list|install|enable|disable`

特殊命令：
- `/ultraplan <task>` — 多步驟推理規劃
- `/teleport <file>` — 跳轉到檔案/符號
- `/bughunter [path]` — 掃描 bug 和 anti-pattern

**對本專案的啟示**：

- 目前 Agent 只有自然語言對話，缺乏結構化命令
- Skills 需要 Supervisor LLM 判斷觸發，有延遲和成本

**建議**：在 Agent HTTP layer 加入 command 解析：

```python
COMMANDS = {
    "/help": show_help,
    "/status": show_workspace_status,
    "/skills": list_skills,
    "/compact": trigger_compaction,
    "/export": export_session,
    "/weather": quick_weather,  # 直接呼叫，不經 Supervisor
}

async def process_message(message: str, session_id: str):
    # 先檢查是否為 slash command
    if message.startswith("/"):
        cmd, *args = message.split(maxsplit=1)
        if cmd in COMMANDS:
            return await COMMANDS[cmd](*args, session_id=session_id)
    # 否則走正常 Supervisor 流程
    return await supervisor.invoke(message, session_id=session_id)
```

### 8. Event/Notification Router（clawhip）

**Claw Code 做法**：

`clawhip` 作為獨立的事件路由層：

- 監控 git commits、GitHub issues/PRs
- 監控 agent lifecycle events
- 將通知路由到適當的 channel
- **關鍵設計**：通知路由在 coding agent 的 context window 之外，保持 agent 專注

**對本專案的啟示**：

- 目前 Agent 的 context window 包含所有互動（包括通知類訊息）
- Scheduler Service 的通知推播尚未設計路由機制
- 缺乏 agent lifecycle event 的統一監控

**建議**：

1. 將通知類訊息（排程結果、系統事件）與對話訊息分離，不佔用 Agent context
2. 建立 event bus 統一收集 lifecycle events（Pod 啟動/停止、session 建立/結束、compaction 觸發）
3. clawhip 的「context window hygiene」理念值得採納 — 非對話類事件不應進入 LLM context

> **與 docs/10 的關聯**：Scheduler Service 的 `deliver_to` 投遞機制（§6）已設計為獨立於 Agent context 的推播路徑（Worker → Channel Layer → 使用者），排程結果不會進入使用者的即時對話 thread（`skip_memory=True` + 獨立 session `sched-exec-{task_id}`）。clawhip 的理念在排程場景已由 docs/10 §11（排程與即時對話的併發處理）落實。

### 9. Multi-Agent 角色分工（OmO）

**Claw Code 做法**：

OmO (oh-my-openagent) 定義三個角色：

| 角色 | 職責 |
|------|------|
| **Architect** | 系統設計、任務分解、架構決策 |
| **Executor** | 編碼實作、測試執行 |
| **Reviewer** | 程式碼審查、品質把關 |

當多個 agent 意見不一致時，系統提供結構化的衝突解決機制。

**對本專案的啟示**：

- 目前 Supervisor + research_agent + code_agent 是功能分工
- 缺乏 review/verification 角色
- 沒有 agent 間衝突解決機制

**建議**：考慮加入 reviewer 角色（中長期）：

```python
# 三角色架構
agents = {
    "research_agent": "搜尋資料、收集資訊",
    "code_agent": "編碼實作、檔案操作",
    "review_agent": "驗證結果、品質檢查（optional, 複雜任務時啟用）"
}
```

### 10. Verification Map 模式

**Claw Code 做法**：

每個功能模組都有對應的 verification map（`g002-security-verification-map.md` 等），定義：

- 功能的驗證條件
- 測試場景清單
- 通過/失敗標準
- Release readiness gate

**對本專案的啟示**：

- 目前 E2E 測試是 shell script，缺乏結構化的驗證矩陣
- 新功能上線沒有明確的 readiness checklist

**建議**：為每個核心功能建立 verification map：

```markdown
# Storage Service Verification Map

## 驗證場景
- [ ] PVC 建立成功（personal + group）
- [ ] 檔案上傳/下載（Pod 在線）
- [ ] 檔案操作（Pod 離線，K8s Job）
- [ ] 權限檢查（readonly 不可寫入）
- [ ] 容量限制（超過 quota 回傳 413）

## Release Gate
- 所有場景通過 ✅
- 效能基準：檔案操作 < 3s
- 安全掃描：無 path traversal
```

---

## dbt-openclaw 的優勢（無需改變）

| 面向 | 說明 |
|------|------|
| **雲端原生** | K8s Pod 隔離 + PVC 持久化，適合企業多租戶場景；Claw Code 是單使用者本地工具 |
| **IM Channel 整合** | MessageBus + ChannelManager 支援多平台 IM；Claw Code 只有 CLI REPL |
| **Workspace 生命週期** | 動態建立/回收/修復；Claw Code 無此需求 |
| **Multi-Workspace** | personal + group workspace + 權限控制；Claw Code 是單一工作目錄 |
| **Storage 雙模式** | Pod 在線 proxy / 離線 K8s Job；Claw Code 只有本地檔案系統 |
| **Context Compaction** | 已實作完整的 memory flush + summarize；Claw Code 有 `/compact` 但細節不明 |

---

## 行動建議（優先級排序）

### 短期（可立即開始）

1. **Slash Command 系統** — Agent 加入 `/` 命令解析，常用操作直接執行不經 LLM
2. **Doctor Endpoint** — Admin Service 加入整合性健康檢查 API
3. **Model Alias** — config.yaml 加入 model alias 支援，簡化使用者配置

### 中期（Phase 2-3）

4. **Permission Mode Mapping** — 定義 workspace role → tool permission 的明確映射
5. **Mock LLM Service** — 建立確定性 mock 用於 CI/CD Agent 行為測試
6. **Session Export** — 加入對話紀錄匯出功能（json/markdown）
7. **Workspace Config Override** — 支援 per-workspace 配置覆蓋

### 長期（Phase 4+）

8. **Event Router** — 建立獨立的事件路由層，通知不佔用 Agent context window
9. **Verification Maps** — 為每個核心功能建立結構化驗證矩陣
10. **Review Agent** — 評估加入 reviewer 角色用於複雜任務品質把關

---

## 設計哲學借鏡

Claw Code 的 PHILOSOPHY.md 提出幾個值得思考的觀點：

> **「The bottleneck shifted」** — 當 Agent 可以在數小時內重建 codebase，稀缺資源變成了「architectural clarity」、「task decomposition」、「judgment」和「taste」。

對本專案的啟示：

1. **Context Window Hygiene** — 保持 Agent 的 context 乾淨，非對話類事件（通知、系統日誌）不應進入 LLM context。這與我們的 Context Compaction 設計互補。

2. **Structured Convergence** — 當多個 agent 意見不一致時，需要結構化的解決機制而非簡單的 fallback。目前 Supervisor 是單點決策，未來可考慮加入 disagreement resolution。

3. **Human direction, agent execution** — 系統設計應讓使用者專注於「做什麼」和「為什麼」，Agent 負責「怎麼做」。這與我們的 Skill 系統理念一致，但可以更進一步 — 例如 `/ultraplan` 式的多步驟規劃命令。

---

## 總結

Claw Code 作為本地 CLI Agent Harness，在 **結構化命令系統、多 Provider 路由、Permission 分級、確定性測試、事件路由分離** 方面有成熟的設計可借鏡。其「Context Window Hygiene」和「Structured Convergence」的設計哲學對雲端 Agent 系統同樣適用。

本專案的雲端原生架構（K8s 隔離、多租戶、IM Channel、Storage 雙模式）是 Claw Code 不具備的能力，兩者互補而非競爭。建議優先採納 slash command 系統和 doctor 健康檢查，這兩項投入小、收益高，可立即提升使用者體驗。
