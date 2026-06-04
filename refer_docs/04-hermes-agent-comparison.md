# Hermes Agent 研究與對照報告

> **來源**：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) · DeepWiki 索引：<https://deepwiki.com/NousResearch/hermes-agent>
> **整理日期**：2026-06-02
> **目的**：作為 `dbt-openclaw` 設計演進的外部參考，重點對照 Channel Layer / Skills / Memory / Subagent / Scheduler / Context Compaction 六大模組。

---

## 1. 專案定位

**Hermes Agent** 是 [Nous Research](https://nousresearch.com/) 開源的 **self-hosted, terminal-native 自主 AI Agent**。

| 項目 | 內容 |
|------|------|
| 開發團隊 | Nous Research（以開源 LLM 微調聞名，e.g. Hermes 系列模型） |
| 主 Repo | `github.com/NousResearch/hermes-agent` |
| 衛星專案 | `pyrate-llama/hermes-ui`（Web UI）、`fathah/hermes-desktop`（桌面 App） |
| 部署型態 | 跑在使用者**自己的伺服器**上，CLI / Gateway / Desktop / Web UI 多入口 |
| 語言 | Python |
| 主資料庫 | SQLite + FTS5（單機，WAL 模式） |
| 版本節奏 | 持續更新中（最新 `v2026.4.16`） |

**核心賣點**：persistent memory、agent 自我學習 skills、21+ 訊息平台 gateway、cron 自動化、跨 session 學習迴路。

---

## 2. 整體架構

```
┌─ Entry Points ─────────────────────────────────────────┐
│  CLI (cli.py) │ Gateway (gateway/run.py) │ ACP/IDE     │
│  Batch Runner │ API Server               │ Py Library  │
└────────────────────────────┬───────────────────────────┘
                             ▼
              ┌──── AIAgent (run_agent.py) ────┐
              │  Prompt Builder                │
              │  Provider Resolution (3 modes) │  ← chat_completion / codex_response / anthropic
              │  Tool Dispatch (70+ tools)     │
              │  Compression & Caching         │
              └──────────────┬─────────────────┘
                             ▼
   ┌──────────────────────┐    ┌──────────────────────┐
   │ Session Storage      │    │ Tool Backends         │
   │ SQLite + FTS5        │    │ Terminal (6 backends) │
   │ (hermes_state.py)    │    │ Browser  (5 backends) │
   └──────────────────────┘    │ Web      (4 backends) │
                               │ MCP, File, Vision...  │
                               └──────────────────────┘
```

**Agent Core Loop**（`run_agent.py`）：
1. Build system prompt + API args
2. Call OpenAI-compatible LLM
3. 若有 tool_calls → 執行 → 將結果加回對話 → 回到 step 2
4. 若是純文字 → 持久化 session → 回傳
5. 達 context 上限 → 觸發 compression

**內部訊息格式**統一為 OpenAI-compatible：
```json
{"role": "system", "content": "..."}
{"role": "user", "content": "..."}
{"role": "assistant", "content": "...", "tool_calls": [...]}
{"role": "tool", "tool_call_id": "...", "content": "..."}
```

---

## 3. 八大模組剖析

### 3.1 Messaging Gateway（對應 dbt-openclaw Channel Layer）

- 單一長駐 `GatewayRunner`（`gateway/run.py`）統一接收所有平台訊息
- **21+ 平台 adapters**（位於 `gateway/platforms/`）：
  - 國際：Telegram / Discord / Slack / WhatsApp / Signal / Matrix / Mattermost / Email / SMS
  - 中華區：**DingTalk / Feishu/Lark / WeCom / Weixin / QQ / Yuanbao**
  - 其他：BlueBubbles / Home Assistant / Webhook / API Server
- 平台訊息一律 normalize 成 `MessageEvent`（與 `dbt-openclaw` 的 `InboundMessage` 概念一致）
- **Session key 格式**：`agent:main:{platform}:{chat_type}:{chat_id}`，必須透過 `gateway/session.py::build_session_key()` 建構
- **DM Pairing 流程**做授權：admin 下 `/pair` → gateway 產生 code → 新使用者輸入 code 完成授權；state 持久化於 `gateway/pairing.py`

### 3.2 Skills System

- 採 `SKILL.md` 格式，**相容 [agentskills.io](https://agentskills.io) 開放標準**
- 所有 skills 位於 `~/.hermes/skills/`
- **三層 Progressive Disclosure**（節省 token）：
  - `skills_list()` → 只給 metadata（注入 system prompt）
  - `skill_view(name)` → 載入 SKILL.md 本體
  - `skill_view(name, path)` → 載入 reference 檔案
- **Skills Hub**：類似 plugin marketplace
  - `hermes skills search <keyword>`
  - `hermes skills install official/<path>`
- **Curator 子系統**（差異化武器）：
  - `hermes curator` CLI / `/curator` slash command
  - 追蹤每個 skill 使用頻率
  - idle 太久 → 標記 stale → archive → 保留 backup
  - **只管理 `created_by: "agent"` 的 skill**（不動 bundled / hub-installed）
  - 支援 agent 自己創造、修改、淘汰 skill（self-improving loop 核心）

### 3.3 Memory & Sessions

- SQLite `~/.hermes/state.db`（WAL 模式，多 reader / 單 writer）
- **核心表**：
  - `sessions`：metadata、token 計數、billing
  - `messages`：完整訊息歷史
  - `messages_fts`：FTS5 虛擬表，標準分詞，跨 content/tool_name/tool_calls 全文搜
  - `messages_fts_trigram`：**trigram tokenizer，支援 CJK 子字串搜尋**
  - `state_meta` + `schema_version`：metadata / schema migration
- AUTO trigger 同步寫入 FTS（INSERT / DELETE / UPDATE 三個 trigger）
- **Session lineage**：`parent_session_id` 追蹤「因壓縮而分裂」的 session 譜系
- **Honcho 整合**（外掛 memory provider，`MemoryManager` 載入）：
  - 注入兩層 context 到 system prompt：
    - **Base context**：session 摘要 + 使用者畫像
    - **Dialectic supplement**：LLM 合成的推理層
  - 透過 `contextCadence` / `dialecticCadence` / `dialecticDepth` 控制刷新頻率與深度
- **Media turn-scoped**：附件 raw bytes **不會**反覆塞進後續 prompt

#### FTS5 Schema 範例

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' ||
        COALESCE(new.tool_name, '') || ' ' ||
        COALESCE(new.tool_calls, '')
    );
END;
-- 另有 _delete / _update trigger 同步刪改
```

### 3.4 Subagent Delegation（對應 supervisor + sub-agent）

- 工具：`delegate_task`，實作於 `delegate_tool.py`
- **Single mode**：`delegate_task(goal, context, toolsets)`
- **Batch mode**：`delegate_task(tasks=[{goal, ...}, ...])` 並行
  - 上限 `delegation.max_concurrent_children`（預設 3）
- **兩種角色**：
  - `leaf`：不能再 delegate
  - `orchestrator`：可遞迴 delegate，但受 `delegation.max_spawn_depth` 限制
- **Subagent 非持久化**：parent 中斷 → child 自動 cancel
- 相較 `dbt-openclaw` 固定 `research_agent + code_agent` 編制，Hermes **動態任意 fan-out** 更靈活

### 3.5 Cron / Scheduled Tasks（對應 docs/10 Scheduler Service）

- 實作：`cron/jobs.py` + `cron/scheduler.py`
- 三種驅動方式：`cronjob` tool / `hermes cron` CLI / `/cron` slash command
- Gateway 後台 maintenance loop tick cron 排程

**支援的 schedule 格式**：
- duration（`5m`, `2h`）
- "every" phrases（`every monday 9am`）
- 5-field cron 表達式
- ISO timestamp（一次性）

**進階特性**（高度可借鑒）：
| 特性 | 說明 |
|------|------|
| `attach_skills` | 執行時掛載特定 skill |
| `model_override` / `provider_override` | 每個 job 用不同 LLM |
| `script` | 預跑 shell 腳本蒐集資料 |
| `context_from` | **串接其他 job 的 output（job DAG）** |
| `workdir` | 指定工作目錄 |
| `deliver_to` | **任意平台投遞結果（重用 Gateway adapters）** |
| `skip_memory=True`（預設） | cron session 不污染主對話記憶 |
| header/footer framing | 維持 role alternation（避免 LLM 看到連續 assistant message） |

### 3.6 Context Compression

雙觸發策略：
- **Pre-flight**：> 50% context window → 送 API 前壓縮
- **Gateway automatic**：> 85% → turn 間更激進壓縮

→ `dbt-openclaw` 目前 75% 單閾值，可升級為雙閾值更穩定

### 3.7 Tool System

- **70+ tools / 28 toolsets**，可組合
- Terminal 有 **6 個 backend**、Browser **5 個**、Web **4 個**
- MCP 動態載入
- **Security 模型**：command approval（高風險指令需人類確認，呼應 `dbt-openclaw` docs/10 的 risk level）

### 3.8 其他亮點

| 模組 | 說明 |
|------|------|
| Kanban / Task Orchestration | 內建任務看板資料庫 |
| Voice Mode + TTS + Transcription | 語音對話 |
| Profiles & Multi-Tenancy | 多租戶 / 多人格 |
| ACP Server + IDE Integration | 編輯器外掛協定 |
| LSP & Computer Use | GUI 自動化 |
| i18n | 多語系（含繁中、簡中） |

---

## 4. Hermes Agent vs dbt-openclaw 對照表

| 維度 | Hermes Agent | dbt-openclaw POC | 觀察 |
|------|--------------|------------------|------|
| **部署型態** | 單機 self-hosted（per-user process） | K8s multi-tenant（per-user Pod） | **dbt-openclaw 多租戶 + 隔離性勝出**；Hermes 走個人 server |
| **儲存** | SQLite + FTS5（單檔） | PostgreSQL + PVC/S3 | dbt-openclaw production-ready；Hermes 個人規模 |
| **Messaging** | 21+ adapters（含 Lark/WeCom/QQ/DingTalk） | Web only（LINE/Slack/Teams 設計中） | **Hermes 平台覆蓋遠超**，可借鑒 adapter 清單 |
| **Skills** | Bundled + Hub + Agent-created + Curator | Shared / Bundled / Workspace 三層 | dbt-openclaw **三層覆蓋優先級**更乾淨；Hermes **自我創造+淘汰**更強 |
| **Subagent** | 動態 `delegate_task`（leaf/orchestrator + 遞迴） | Supervisor + research/code（固定編制） | Hermes 更彈性；dbt-openclaw 更可預測 |
| **Memory** | SQLite FTS5 + Honcho（兩層 context 注入） | SQLite FTS5 + Active Memory | **設計理念相同**，Honcho dialectic supplement 可參考 |
| **Cron** | `jobs.py` + 多 schedule 格式 + DAG（`context_from`）+ 任意平台投遞 | docs/10 設計完成（已完整吸收 Hermes schema + 額外擴展） | **docs/10 已對齊並超越 Hermes**：加了 Dispatcher+Worker、Tool 風險分級、delivery 兜底、安全防護 |
| **Compression** | 50%（pre-flight）+ 85%（aggressive）雙閾值 | 75% 單閾值 | dbt-openclaw 可改雙閾值 |
| **Pairing/綁定** | `/pair` code 流程，state 存 `pairing.py` | docs/13 驗證碼綁定設計 | **流程一模一樣**，可直接對齊 |
| **Tool 安全** | Command approval（人類確認） | docs/10 §12 Tool 風險三級分級（Level 0 Safe / Level 1 Reversible / Level 2 Irreversible）+ 授權機制 + 確認流程 | **docs/10 已設計完成**，比 Hermes 更細緻（三級分級 vs 二元 approval） |
| **Curator / 自我學習** | ✅ Skill 用得多就保留，閒置就 archive | ❌ | Hermes 差異化武器 |
| **多入口** | CLI / Gateway / ACP / Web UI / Desktop | Gateway only | 可加 CLI 模式給內部開發者用 |
| **資料庫範疇** | 單機 SQLite | PostgreSQL（跨 Pod 共享） | dbt-openclaw 適合企業 |
| **多平台投遞 cron 結果** | ✅（任何 adapter） | docs/10 §6 設計完成（deliver_to + Channel Layer 整合 + on_failure 兜底） | **已對齊**，docs/10 設計了結構化 targets 陣列 + `to: "self"` 解析 + delivery_log |

---

## 5. 對 dbt-openclaw 的具體借鑒建議（按 ROI 排序）

### 🔥 高優先（直接照抄或對齊）

1. **Cron Job schema — 已完成對齊**（docs/10 已吸收 Hermes 全部進階特性）
   - 已納入 fields：`attach_skills` / `model_override` / `pre_script` / `context_from`（DAG）/ `deliver_to` / `skip_memory` / `framing`
   - 額外擴展：Dispatcher+Worker 水平擴展、Tool 風險三級分級（Level 0/1/2）、`to: "self"` 透過 user_bindings 解析、`on_failure` 兜底語意、prompt 審查 + pre_script 白名單、context_from 不可作為授權來源（防間接 prompt injection）
   - **狀態：設計完成，待實作**

2. **Trigram FTS5 加上去**（CJK 子字串搜）
   - docs/11 沒明確提到 trigram tokenizer
   - 對中文使用者體驗提升極大
   - SQL 範例：
     ```sql
     CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
         content, tokenize='trigram'
     );
     ```

3. **Pairing 流程對齊**：docs/13 已是這個方向，參考 `gateway/pairing.py` 的 state machine

### 🟡 中優先（值得規劃）

4. **Curator 機制**：給 skill 加 usage tracking + idle archive，是長期維護 skill library 的關鍵
5. **雙閾值 compression**：50% pre-flight + 85% emergency
6. **`delegate_task` 動態 fan-out**：把 supervisor 從「固定 2 個 sub-agent」升級成「動態 delegate」，配合 `max_spawn_depth` / `max_concurrent_children`
7. **Tool risk gate — 已設計完成**：docs/10 §12 定義三級風險分級（Level 0/1/2）+ `authorized_tools` 欄位 + Level 2 確認流程 + prompt 審查，待隨 Scheduler Service 一同實作

### 🟢 低優先（長期演進）

8. **Honcho 風格 dialectic supplement**：在 system prompt 注入「LLM 對使用者的推理層」，比純摘要更聰明
9. **平台 adapter 擴張**：加 **Lark / WeCom / DingTalk**（中華區企業常用）
10. **Session lineage**：`parent_session_id` 追蹤壓縮後分裂的 session 譜系
11. **多入口（CLI）**：給內部開發者直接 SSH 操作 agent

---

## 6. 架構哲學差異總結

| 哲學 | Hermes Agent | dbt-openclaw |
|------|--------------|--------------|
| **使用者模型** | 一台 server 一個主人，跨 session 學習 | 多租戶 + workspace 共享 + 嚴格 ACL |
| **隔離單位** | session（檔案層級） | K8s Pod + PVC（OS 層級） |
| **擴展單位** | 加 toolset / skill / platform | 加 workspace / replica / node |
| **資料持久性** | 單機 SQLite（適合個人） | PostgreSQL + PVC/S3（適合企業） |
| **Agent 自主性** | 高（自創 skill、Curator 自我淘汰、動態 delegate） | 中（固定 supervisor + sub-agent，skill 由人類管理） |
| **平台整合** | 廣度優先（21+ messaging） | 深度優先（K8s 原生 + RBAC） |

**結論**：
- **Hermes 像「個人 AI 管家」**——專注 self-hosted、跨 session 學習、覆蓋使用者所有通訊管道
- **dbt-openclaw 像「企業 AI 工作平台」**——專注多租戶隔離、workspace 共享、運維可控

兩者**不衝突，可互相吸收**：dbt-openclaw 已借鑒 Hermes 的 cron schema（docs/10 已完整對齊並擴展）、pairing flow（docs/13 已設計），後續可繼續借鑒 trigram FTS、Curator 等設計，加速 docs/11、docs/13 落地。

---

## 7. 參考資源

- 主 repo：<https://github.com/NousResearch/hermes-agent>
- DeepWiki 完整文件：<https://deepwiki.com/NousResearch/hermes-agent>
- 架構文件：`website/docs/developer-guide/architecture.md`
- Skills 開放標準：<https://agentskills.io>
- Web UI：<https://github.com/pyrate-llama/hermes-ui>
- Desktop App：<https://github.com/fathah/hermes-desktop>

### Hermes 重要原始檔速查

| 模組 | 檔案 |
|------|------|
| Agent 主迴圈 | `run_agent.py` |
| Gateway 主程式 | `gateway/run.py` |
| 平台 adapters | `gateway/platforms/*` |
| Session key 建構 | `gateway/session.py` |
| Pairing 授權 | `gateway/pairing.py` |
| Session 儲存 + FTS5 | `hermes_state.py` |
| Tool 註冊 | `model_tools.py` / `registry.py` |
| Subagent | `delegate_tool.py` |
| Cron | `cron/jobs.py` + `cron/scheduler.py` |
| Prompt builder | `prompt_builder.py` |
| Provider 解析 | `runtime_provider.py` |

