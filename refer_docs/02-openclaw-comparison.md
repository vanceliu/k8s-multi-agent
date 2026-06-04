# 15 - OpenClaw 平台比較與學習方向

## 概述

本文件比較 OpenClaw（開源 AI Agent 平台）與本專案（dbt-openclaw）的架構差異，並整理可借鏡的設計模式與具體行動建議。

OpenClaw 專案：https://github.com/openclaw/openclaw

## OpenClaw 簡介

OpenClaw 是一個通用 AI Agent 平台，核心由 Gateway + Agent Runtime 組成，支援多 Agent 路由、Plugin 擴展、多平台 IM 整合、排程自動化等功能。

### 技術棧

- **Gateway**：Rust + actix-web（HTTP + WebSocket RPC）
- **Agent Runtime**：TypeScript/JavaScript（自建 Workflow 引擎）
- **通訊協定**：WebSocket 雙向 + Broker pub/sub
- **套件管理**：pnpm
- **序列化**：serde (Rust) + JSON

### 核心元件

| 元件 | 職責 |
|------|------|
| Gateway | 認證、授權、WebSocket RPC、HTTP API、Session 管理、Multi-Agent 路由 |
| Agent Runtime | Workflow 執行、工具策略、記憶管理、搜尋、Context Compaction |
| Messaging Channels | LINE / Slack / Teams 等 IM 平台整合 |
| Skills System | 技能定義、動態載入、SkillRegistry |
| Plugin Architecture | 插件管理、PluginManager、擴展註冊 |
| Automation + Cron | 事件驅動自動化 + 定時排程 |
| Native Clients | iOS / macOS / Android 原生客戶端 + Device Node Protocol |

---

## 架構比較

### 整體定位

| 面向 | dbt-openclaw | OpenClaw |
|------|-------------|----------|
| 定位 | K8s 使用者專屬 Agent 工作區管理系統 | 通用 AI Agent 平台（Gateway + Runtime） |
| 語言 | Python (FastAPI) | Rust (actix-web) + TypeScript |
| 通訊協定 | HTTP REST + SSE 串流 | WebSocket RPC + HTTP APIs |
| 部署模型 | 每個 workspace 一個 Pod（動態建立/回收） | Gateway + Agent Runtime 固定服務 |
| Agent 架構 | LangGraph Supervisor + Sub-Agent | 自建 AgentRuntime + Workflow 引擎 |
| 多租戶隔離 | workspace_members ACL + Pod 級隔離 | UserSession + 角色權限（共享 Runtime） |

### Gateway 層

| | dbt-openclaw | OpenClaw |
|--|-------------|----------|
| 認證 | Static token (POC) → JWT (Production) | AuthToken + UserSession (Rust struct) |
| 路由 | workspace-based proxy → Agent Pod | Multi-agent routing (shouldHandle 模式) |
| 即時通訊 | SSE (Server-Sent Events) 單向串流 | WebSocket + Broker pub/sub 雙向 |
| Session | DB 持久化 + 同 Pod 多 session 共用 | WsSession (heartbeat + subscription) |
| 效能 | Python async (uvicorn) | Rust native (actix-web, 高吞吐) |

### Agent Runtime

| | dbt-openclaw | OpenClaw |
|--|-------------|----------|
| 執行引擎 | LangGraph + create_supervisor | 自建 AgentRuntime + Workflow object |
| 工具管理 | 按角色靜態分組 (research/code/common) | ToolPolicyManager（動態策略過濾） |
| Context Compaction | TokenCounter + ContextCompactor (75% 觸發) | ContextCompactor（類似概念） |
| 記憶系統 | MemoryService (SQLite FTS5, Active Memory) | MemoryManager + SearchManager |
| 排程 | Scheduler Service（設計完成，未實作）— Dispatcher+Worker 架構、Hermes 風格進階欄位（attach_skills/model_override/pre_script/context_from DAG/deliver_to/skip_memory/framing）、Tool 風險三級分級 | AutomationManager + CronManager |
| 技能系統 | SKILL.md + 三層優先級 (shared > bundled > workspace) | Skills class + SkillRegistry 動態載入 |
| 插件 | 無獨立插件概念 | PluginManager + Plugin class |

### IM Channel 整合

| | dbt-openclaw | OpenClaw |
|--|-------------|----------|
| 抽象層 | MessageBus + ChannelManager + ChannelStore | MessagingService + MessagingChannel interface |
| 路由模式 | InboundMessage → ensure workspace → Agent Pod → OutboundMessage | handleWebhook → onMessage callback → response |
| 擴展方式 | Channel adapter 插拔（registry stubs） | switch-case 分派到具體 Channel class |
| 已實作 | WebChannel | LINE (@line/bot-sdk) + Slack (@slack/bolt) |

---

## 可學習的設計模式

### 1. WebSocket RPC 取代 SSE

**OpenClaw 做法**：Gateway 使用 WebSocket 雙向通訊 + Broker pub/sub，支援 subscription 機制。

- `WsSession` 管理連線狀態（heartbeat、user_id）
- `WsMessage` enum 定義 RPC 方法（Auth、Subscribe、Unsubscribe、Call）
- `Broker` 負責 subscription 管理和訊息廣播

**對本專案的啟示**：

- 目前 SSE 是單向的，客戶端無法主動推送（取消請求、中斷生成）
- 群組 workspace 多人協作時，WebSocket 的 subscription 模式更適合即時同步
- heartbeat 機制可以更精確地偵測連線斷開，優於 SSE 的 retry

**建議**：Production Phase 3 實作 Gateway 時，評估 WebSocket 作為主要通訊協定，SSE 作為 fallback。

### 2. Tool Policy Manager（工具策略管理）

**OpenClaw 做法**：獨立的 `ToolPolicyManager` 元件，根據策略動態決定 agent 可用工具。

**對本專案的啟示**：

- 目前工具按 agent 角色靜態分組（research_agent 用 research_tools，code_agent 用 code_tools）
- 缺乏根據 workspace 類型、使用者角色、安全等級動態調整的能力
- 例如：readonly 成員不應該有 code_tools，group workspace 可能需要限制某些工具

**建議實作方向**：

```python
class ToolPolicyManager:
    def filter_tools(self, tools: list, context: ToolContext) -> list:
        """根據 context 過濾可用工具"""
        # context 包含：workspace_type, user_role, security_level
        pass

@dataclass
class ToolContext:
    workspace_type: str  # personal / group
    user_role: str       # owner / admin / member / readonly
    security_level: str  # standard / restricted
```

### 3. Plugin Architecture（插件化架構）

**OpenClaw 做法**：

- `Plugin` class 可同時註冊 skills + tools
- `PluginManager` 負責發現、載入、管理插件
- `SkillRegistry` / `ToolRegistry` 統一管理已註冊的能力

**對本專案的啟示**：

- 目前 skills 和 tools 是分離的概念（skills 用 SKILL.md，tools 在 tools.py 硬編碼）
- Channel adapters 也是獨立的擴展點
- 三者缺乏統一的擴展機制

**建議**：將 skills + tools + channel adapters 統一到 Plugin 概念下：

```
plugins/
  weather/
    PLUGIN.md          # name, description, type: tool
    tool.py            # CWAWeatherTool
  daily-summary/
    PLUGIN.md          # name, description, type: skill
    SKILL.md           # 原有 skill 定義
  line-channel/
    PLUGIN.md          # name, description, type: channel
    adapter.py         # LineChannelAdapter
```

### 4. Multi-Agent Routing（shouldHandle 模式）

**OpenClaw 做法**：

- 每個 sub-agent 實作 `shouldHandle(message)` 方法
- 路由時遍歷 sub-agents，第一個回傳 true 的處理訊息
- 無 sub-agent 處理時 fallback 到主 agent

**對本專案的啟示**：

- 目前 Supervisor 集中分派，每次都需要 LLM 判斷該交給誰
- 對於明確的指令（如 `/weather`、`/doc`），不需要 LLM 介入

**建議**：結合兩種模式：

1. 先用 shouldHandle 做 keyword/command 快速路由（零 LLM 成本）
2. 無法快速路由時，才交給 Supervisor LLM 分派

```python
class SubAgent:
    def should_handle(self, message: str) -> bool:
        """快速判斷是否處理（keyword match, regex, etc.）"""
        pass

# 路由邏輯
for agent in sub_agents:
    if agent.should_handle(message):
        return await agent.process(message)
# fallback to supervisor LLM routing
return await supervisor.route(message)
```

### 5. Configuration Schema + UI Integration

**OpenClaw 做法**：配置系統支援 schema 自動生成，可與 UI 整合自動渲染設定表單。

**對本專案的啟示**：

- 目前 config.yaml + env vars 覆蓋，Admin UI 需要手動維護表單
- 新增配置項時需要同步更新 UI

**建議**：為 config 加上 JSON Schema 定義，Admin UI 可自動渲染：

```yaml
# config.schema.yaml
properties:
  agent:
    properties:
      idle_timeout_minutes:
        type: integer
        default: 10
        description: "Pod 閒置超時（分鐘）"
        minimum: 1
        maximum: 120
```

### 6. Automation + Cron 雙軌制

**OpenClaw 做法**：

- `AutomationManager`：事件驅動（when X happens, do Y）
- `CronManager`：時間驅動（every N minutes, do Z）

**對本專案的啟示**：

- docs/10 Scheduler Service 已具備完整的時間驅動設計：
  - 多格式排程（duration / every_phrase / cron / iso_timestamp）
  - Hermes 風格進階欄位（`attach_skills`、`model_override`、`pre_script`、`context_from` DAG、`deliver_to` 多目的地投遞、`skip_memory`、`framing`）
  - Dispatcher + Worker 水平擴展架構
  - Tool 風險三級分級（Level 0 Safe / Level 1 Reversible / Level 2 Irreversible）
  - 與 Channel Layer 整合的 delivery 通道（pull/web/line/email/webhook）
- 但缺乏**事件驅動**的自動化（例如：workspace 建立時自動初始化、檔案上傳後自動處理、git push 後觸發 review）

**建議**：在 Scheduler Service 基礎上擴展 event trigger 機制（Production Phase）：

```python
# 時間驅動（docs/10 已設計完成）
schedule = ScheduledTask(
    schedule_format="every_phrase",
    schedule_expr="every monday 9am",
    prompt="產生本週週報",
    deliver_to={"targets": [{"channel": "line", "to": "self"}]}
)

# 事件驅動（待擴展）
automation = EventAutomation(
    trigger="workspace.created",
    action=initialize_workspace_defaults
)
automation = EventAutomation(
    trigger="file.uploaded",
    condition={"path_pattern": "data/*.csv"},
    action=auto_analyze_csv
)
```

> **注意**：event trigger 機制與 docs/10 的 `context_from`（job DAG）互補但不重複——`context_from` 是「排程 A 完成後餵結果給排程 B」，event trigger 是「系統事件觸發自動化動作」。兩者可共存於同一個 Scheduler Service 內。

---

## dbt-openclaw 的優勢（無需改變）

以下是本專案優於 OpenClaw 的設計，應繼續保持：

### 1. K8s 原生 Pod 隔離

- 每個 workspace 獨立 Pod + PVC，提供作業系統級隔離
- OpenClaw 是共享 Runtime，隔離僅靠程式邏輯
- 對於企業場景，Pod 隔離的安全性遠優於共享進程

### 2. Storage 雙模式（Pod 在線 / 離線 Job）

- Pod 在線時 proxy 到 Agent Pod 操作檔案
- Pod 離線時建立短暫 K8s Job 掛載 PVC 操作
- OpenClaw 沒有類似的離線檔案操作能力

### 3. Workspace 生命週期管理

- idle reaping（閒置回收）
- orphan repair（孤兒修復）
- graceful shutdown（優雅關閉 + 狀態保存）
- Pod readiness 等待
- OpenClaw 是固定服務，無此需求也無此能力

### 4. IM Channel 抽象層設計

- MessageBus async pub/sub + ChannelManager 中央 dispatcher
- 比 OpenClaw 的 switch-case MessagingService 更具擴展性
- 支援 chat → workspace/session 映射持久化

### 5. Session-scoped 工具隔離

- 每個 session 有獨立工作目錄 `sessions/{sid}/`
- Shell/Python 工具 cwd 強制為 session 目錄
- 跨 session 只能讀取，不能寫入
- OpenClaw 沒有這層細粒度隔離

### 6. Multi-Workspace 掛載

- 使用者 Pod 自動掛載 personal PVC + 所有 group PVC
- readonly role 強制 K8s 層級 read-only mount
- 這是 K8s 原生能力的充分利用，OpenClaw 無法做到

---

## 行動建議（優先級排序）

### 短期（可立即開始）

1. **Tool Policy Layer** — 在 `poc/agent/tools.py` 加入策略過濾，根據 workspace_type + user_role 動態限制工具
2. **shouldHandle 快速路由** — 在 Supervisor 前加入 keyword/command 快速分派，減少 LLM 呼叫

### 中期（Phase 2-3）

3. **WebSocket 通訊** — Production Gateway 評估 WebSocket 作為主要協定，支援雙向通訊 + subscription
4. **Plugin 統一架構** — 將 skills + tools + channel adapters 統一到 Plugin 概念
5. **Event Automation** — 在 docs/10 Scheduler Service 基礎上擴展事件驅動觸發機制（與現有 cron/DAG 設計互補）

### 長期（Phase 4+）

6. **Config Schema** — 加入 JSON Schema 定義，Admin UI 自動渲染設定表單
7. **Gateway 效能** — 如果 Python Gateway 成為瓶頸，評估 Rust/Go 重寫（僅 Gateway 層）

---

## 總結

OpenClaw 作為通用 AI Agent 平台，在 **工具策略管理、插件化架構、WebSocket 通訊、Multi-Agent 路由** 方面有成熟的設計模式可借鏡。但本專案在 **K8s 原生隔離、workspace 生命週期、Storage 雙模式、session-scoped 隔離** 方面有明確的架構優勢，這些是企業級部署的關鍵差異化能力。

建議採取「取長補短」策略：學習 OpenClaw 的軟體設計模式（策略模式、插件化、事件驅動），但保持本專案的 K8s 原生架構優勢不變。