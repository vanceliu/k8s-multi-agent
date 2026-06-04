# 10 排程服務（Scheduler Service）

> **狀態**：討論稿（Draft），尚未進入實作階段（POC 尚未實作）。

本文件描述使用者定時任務的排程機制設計，解決「Pod 閒置回收後，如何在指定時間喚醒 Pod 並執行任務」的問題。

設計參考 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 cron job 子系統（`cron/jobs.py` + `cron/scheduler.py`），吸收其多種 schedule 格式、`attach_skills`、`model_override`、`pre_script`、`context_from`（job DAG）、`deliver_to`（多目的地投遞）、`skip_memory`、framing header/footer 等成熟設計，與 dbt-openclaw 既有的 K8s 多租戶 + Dispatcher/Worker 架構整合。詳見 `refer_docs/04-hermes-agent-comparison.md`。

> **與其他文件的關係**：
> - 05-lifecycle.md：Pod 生命週期（idle reaping、ensure 流程）
> - 06-agent-container.md：Agent 容器架構（runtime、tools）
> - 09-agent-tools.md：Agent 工具定義（新增 CreateScheduleTool）
> - 02-api-design.md：API 端點設計（新增 `/api/v1/schedules/*`）
> - 03-data-model.md：資料模型（新增 `scheduled_tasks` 表）
> - 11-memory-system.md：`skip_memory` 短路 MemoryService 寫入路徑
> - 13-user-bindings.md：`deliver_to` 的 `to: "self"` 透過 `user_bindings` 查目標 platform_uid
> - Channel Layer（`poc/gateway/channels/`）：`deliver_to` 直接重用既有 channel adapters

---

## 1. 問題描述

### 1.1 核心矛盾

使用者希望設定定時任務（例如「每天早上 08:00 告知今天天氣」），但 Agent Pod 是 ephemeral 的：

- Pod 閒置超過 `IDLE_TIMEOUT_MINUTES`（POC: 10 分鐘）後被回收
- 使用者不在線上時 Pod 不存在
- 沒有常駐程序可以觸發排程任務

因此需要一個**在 Pod 生命週期之外**的排程機制，負責：

1. 在指定時間**提前喚醒** Pod
2. 等待 Pod 就緒後**執行任務**
3. 將結果透過**通知管道**送達使用者

### 1.2 時間線範例

使用者設定「每天 08:00 (UTC+8) 查詢天氣」，lead time = 5 分鐘：

```
07:55:00  Scheduler 掃到任務即將到期
07:55:01  呼叫 Orchestrator ensure → 建立 Pod
07:55:01  K8s 拉 image、啟動容器、readiness probe
07:55:30  Pod ready（冷啟動 ~20-30 秒）
07:55:30  Pod 就緒，等待執行時間
          （緩衝時間：即使 image pull 慢或啟動異常，仍有 ~4 分鐘重試空間）
07:59:50  接近 scheduled_at，送 prompt 給 Agent 執行
08:00:00  結果送出通知
08:00~08:10  Pod 閒置，走正常 idle timeout 回收
```

---

## 2. 架構設計

### 2.1 元件定位

```
Scheduler Service (:8092, ClusterIP, Gateway proxy /api/v1/schedules/*)
  │
  ├─→ DB (scheduled_tasks 表，排程定義 + 執行紀錄)
  ├─→ Orchestrator API (ensure workspace，喚醒 Pod)
  ├─→ Agent Pod (送 prompt 執行任務)
  └─→ 通知管道 (Channel Layer / Email / Webhook)
```

### 2.2 系統架構圖

```
Client → API Gateway (:8000)
           │
           ├─ /api/v1/schedules/* ──→ Scheduler Service (:8092)
           │                            ├─→ DB (scheduled_tasks)
           │                            ├─→ Orchestrator API (ensure)
           │                            ├─→ Agent Pod (execute)
           │                            └─→ 通知管道
           │
           ├─ /api/v1/chat ──→ Channel Layer → Agent Pod
           │   (使用者對話中可透過 Agent tool 建立排程)
           │
           └─ (其他既有路由...)
```

### 2.3 演進路線

| 階段 | 方案 | Replica | 說明 |
|------|------|---------|------|
| POC | Orchestrator 內建 | 1 | 在 Orchestrator 加 scheduler loop，最快落地 |
| Production v1 | Dispatcher + Worker（獨立 Service） | 2-3 | DB as Queue，水平擴展 |
| Production v2 | 外部排程 + Worker | auto-scale | AWS EventBridge + SQS，大規模場景 |

> POC 即支援 Hermes 風格的進階 cron 欄位：`attach_skills`、`model_override`、`pre_script`、`context_from`（DAG）、`deliver_to`、`skip_memory`、`framing`。schema 一次到位，避免日後 migration 痛點；實作可依需求漸進啟用（NULL 表示走預設行為）。

### 2.4 Dispatcher + Worker 架構（Production）

將排程服務拆為兩個角色，解決多 replica 重複執行問題：

```
┌─────────────────────────────────────────────────────┐
│ Scheduler Service                                    │
│                                                      │
│  ┌──────────┐         ┌──────────────┐              │
│  │Dispatcher│────────→│ task_executions │              │
│  │(leader)  │ enqueue │  (DB Queue)    │              │
│  └──────────┘         └──────┬───────┘              │
│                              │ dequeue               │
│              ┌───────────────┼───────────────┐      │
│              ▼               ▼               ▼      │
│        ┌──────────┐   ┌──────────┐   ┌──────────┐  │
│        │ Worker-1 │   │ Worker-2 │   │ Worker-3 │  │
│        └──────────┘   └──────────┘   └──────────┘  │
│              │               │               │      │
└──────────────┼───────────────┼───────────────┼──────┘
               ▼               ▼               ▼
         Orchestrator     Orchestrator     Orchestrator
         (ensure pod)     (ensure pod)     (ensure pod)
               ▼               ▼               ▼
          Agent Pod        Agent Pod        Agent Pod
```

**角色分工**：

| 角色 | Replica | 職責 | Scaling |
|------|---------|------|---------|
| **Dispatcher** | 1（K8s Lease leader election） | 每 30 秒掃 `scheduled_tasks`，到期任務寫入 `task_executions`（status=queued） | 不需要 scale，輕量 |
| **Worker** | N（HPA auto-scale） | 從 `task_executions` 搶任務，ensure pod → execute → notify | **水平擴展** |

同一個 Deployment 內，所有 replica 都跑 Worker，透過 K8s Lease 選出一個 leader 額外跑 Dispatcher：

```python
if is_leader():
    start_dispatcher_loop()   # 掃描 + 派發（只有 leader 跑）

start_worker_loop()           # 所有 replica 都跑 worker
```

**Worker 取任務（原子操作，不衝突）**：

```sql
UPDATE task_executions
SET status = 'picked', picked_by = $worker_id, picked_at = NOW()
WHERE id = (
    SELECT id FROM task_executions
    WHERE status = 'queued'
      AND scheduled_at <= NOW()
    ORDER BY scheduled_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

每個 Worker 搶到的任務一定不同，**加 Worker replica = 加吞吐量**，線性擴展。

### 2.5 容量規劃

**基準線：每 500 使用者 = 1 Worker replica**

```
500 使用者 × 1-2 個排程 = 500-1000 個任務
尖峰集中率 ~30-40% = 150-200 個任務同時到期
單 Worker 併發 10 個 × 每個 ~30 秒 = 每分鐘處理 ~20 個
配合分批預熱 + 併發限流 → 單 replica 穩定處理

1000 使用者 = 2 replica
2000 使用者 = 4 replica
線性關係，好預估
```

**HPA 設定**：

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: scheduler-worker-hpa
  namespace: agent-system
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: scheduler-service
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: External
    external:
      metric:
        name: scheduler_queue_depth
      target:
        type: AverageValue
        averageValue: "20"
```

### 2.6 尖峰流量控制

大量任務集中在同一時間點（如 08:00）時的應對策略：

**併發限流**：

```python
SCHEDULER_MAX_CONCURRENT_ENSURES = 10   # 同時 ensure 上限，保護 K8s API Server
SCHEDULER_MAX_CONCURRENT_TASKS = 20     # 同時執行任務上限，保護 LLM API

ensure_semaphore = asyncio.Semaphore(MAX_CONCURRENT_ENSURES)
task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
```

**分批預熱**：

```
Dispatcher 提前 15 分鐘掃描未來到期的任務
→ 分批寫入 queue（每批 10 個，間隔 5 秒）
→ Worker 分批 ensure pod
→ 到達 scheduled_at 時 Pod 都已 ready，只需送 prompt
```

---

## 3. 資料模型

### 3.1 Table: scheduled_tasks

```sql
CREATE TABLE scheduled_tasks (
    id BIGSERIAL PRIMARY KEY,
    task_id VARCHAR(255) UNIQUE NOT NULL,             -- UUID，外部識別用
    user_id VARCHAR(255) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    workspace_id VARCHAR(255) NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,

    -- 基本資訊
    name VARCHAR(255) NOT NULL,                       -- 顯示名稱，如「每日天氣通知」
    prompt TEXT NOT NULL,                             -- 要送給 Agent 的 prompt

    -- 排程定義（Hermes 風格：多格式統一）
    schedule_format VARCHAR(20) NOT NULL,             -- 'duration' / 'every_phrase' / 'cron' / 'iso_timestamp'
    schedule_expr TEXT NOT NULL,                      -- 對應的表達式
                                                      --   duration: '5m' / '2h'
                                                      --   every_phrase: 'every monday 9am' / 'every day 08:00'
                                                      --   cron: '0 8 * * *'
                                                      --   iso_timestamp: '2026-05-05T08:00:00+08:00'
    timezone VARCHAR(100) NOT NULL DEFAULT 'Asia/Taipei',  -- 使用者時區
    lead_time_minutes INTEGER NOT NULL DEFAULT 5,     -- 提前喚醒時間（分鐘）

    -- 排程模式
    schedule_type VARCHAR(50) NOT NULL DEFAULT 'recurring',  -- 'recurring' / 'one_shot'
                                                             -- iso_timestamp 自動視為 one_shot

    -- Hermes 風格進階欄位
    attach_skills JSONB,                              -- skill 名稱陣列，執行時掛載
                                                      -- 範例: ["daily-summary", "xlsx"]
    model_override VARCHAR(255),                      -- 覆寫 LLM model (e.g. 'claude-sonnet-4-6')
    provider_override VARCHAR(100),                   -- 覆寫 LLM provider (e.g. 'anthropic')
    pre_script TEXT,                                  -- 執行 prompt 前先跑的 shell 腳本
                                                      -- stdout 自動注入 effective_prompt
    context_from JSONB,                               -- 上游 job 的 task_id 陣列（DAG）
                                                      -- 範例: ["sched-collect-weekly-data"]
                                                      -- 執行時自動載入上游最近一次成功 result
    deliver_to JSONB NOT NULL,                        -- 投遞目的地清單，取代 notification_channel
                                                      -- 結構見 §3.2
    skip_memory BOOLEAN NOT NULL DEFAULT TRUE,        -- True: 不寫入 workspace memory
    workdir VARCHAR(255),                             -- Agent 工作目錄，預設 sessions/sched-exec-{task_id}/
    framing JSONB,                                    -- header/footer 訊息，維持 role alternation
                                                      -- 結構見 §3.3
    authorized_tools JSONB,                           -- 高風險工具授權清單，見 §12.5
                                                      -- 範例: {"level_1": ["book_restaurant"], ...}

    -- 狀態（one_shot 執行完自動設為 'disabled'）
    status VARCHAR(50) NOT NULL DEFAULT 'enabled',    -- 'enabled' / 'disabled' / 'deleted'
    execution_status VARCHAR(50) DEFAULT 'idle',      -- 'idle' / 'waking' / 'running' / 'blocked' / 'completed' / 'failed'
                                                      -- blocked: 上游 context_from job 尚無 success result
    next_run_at TIMESTAMP WITH TIME ZONE,             -- 下次執行時間（UTC）
    last_run_at TIMESTAMP WITH TIME ZONE,             -- 上次執行時間
    last_result TEXT,                                 -- 上次執行結果摘要
    last_error TEXT,                                  -- 上次錯誤訊息（如有）
    consecutive_failures INTEGER NOT NULL DEFAULT 0,  -- 連續失敗次數（用於告警/自動停用）

    -- 中繼資料
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by VARCHAR(50) DEFAULT 'user',            -- 'user'（對話中建立）/ 'admin'

    INDEX idx_scheduled_next_run (status, next_run_at),
    INDEX idx_scheduled_user (user_id),
    INDEX idx_scheduled_workspace (workspace_id)
);
```

> **與舊設計的差異**：
> - 舊版 `cron_expr` 已併入 `schedule_format='cron'` + `schedule_expr`
> - 舊版 `notification_channel` + `notification_config` 已由 `deliver_to` JSONB 取代
> - 一次性排程改用 `schedule_format='iso_timestamp'`，不再有獨立的 `run_at` 欄位

### 3.2 `deliver_to` 結構

統一描述「執行結果要投遞到哪些目的地」，**直接重用 Channel Layer adapters**（`poc/gateway/channels/`），不重複造輪子。

```json
{
  "targets": [
    {"channel": "line",    "to": "self",                       "format": "text"},
    {"channel": "email",   "to": "user@example.com",           "format": "html"},
    {"channel": "webhook", "to": "https://hooks.example.com/x", "format": "json"},
    {"channel": "pull"}
  ],
  "on_failure": "pull"
}
```

| 欄位 | 必填 | 說明 |
|---|---|---|
| `targets[].channel` | ✓ | `pull` / `web` / `line` / `slack` / `teams` / `email` / `webhook`（對應 Channel Layer adapter） |
| `targets[].to` | 視 channel 而定 | `pull` 不需要；IM/Email 可填 `"self"`（透過 `user_bindings` 查目標）或具體地址；webhook 填 URL |
| `targets[].format` | 否 | `text` / `markdown` / `html` / `json`，預設 `text` |
| `on_failure` | 否 | 所有 target 都投遞失敗時的兜底通道，預設 `pull`（結果永遠存得到 DB） |

> **`to: "self"` 的解析**：執行時由 Worker 透過 `user_bindings` 表查詢該 user 在指定 platform 的 active 綁定，取出 `platform_uid` 作為投遞目標。若 binding 不存在或為 inactive，該 target 視為失敗，走 `on_failure`。

### 3.3 `framing` 結構

維持對話 role alternation（避免 LLM 看到連續 assistant message），對應 Hermes 的 header/footer framing。

```json
{
  "header": "system: 以下為自動排程任務，請以正式語氣回覆。",
  "footer": "system: 結束。請輸出 markdown 格式的摘要。"
}
```

Worker 組裝 `effective_prompt` 時，header/footer 會包夾在主 prompt 與上游 context 外層（見 §5.6）。

### 3.4 Table: task_executions（DB as Queue）

Worker 消費的任務佇列，由 Dispatcher 寫入，Worker 搶佔執行。

```sql
CREATE TABLE task_executions (
    id BIGSERIAL PRIMARY KEY,
    task_id VARCHAR(255) NOT NULL REFERENCES scheduled_tasks(task_id) ON DELETE CASCADE,
    user_id VARCHAR(255) NOT NULL,
    workspace_id VARCHAR(255) NOT NULL,
    scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL,   -- 原定執行時間
    prompt TEXT NOT NULL,                             -- scheduled_tasks.prompt 的快照
    deliver_to JSONB NOT NULL,                        -- scheduled_tasks.deliver_to 的快照

    -- Hermes 進階欄位的執行快照
    upstream_results JSONB,                           -- 從 context_from 載入的上游 result 快照
                                                      -- 凍結於執行時刻，便於重現
    effective_prompt TEXT,                            -- 組裝後的最終 prompt（含 framing/upstream/pre_script 輸出）
                                                      -- 主要供 debug 與 audit

    -- Queue 狀態
    status VARCHAR(50) NOT NULL DEFAULT 'queued',     -- 'queued' / 'picked' / 'executing' / 'done' / 'failed' / 'blocked'
    picked_by VARCHAR(255),                            -- Worker replica ID
    picked_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    result_text TEXT,
    error_message TEXT,

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_exec_queue (status, scheduled_at),
    INDEX idx_exec_task (task_id, created_at DESC)
);
```

### 3.5 Table: scheduled_task_results

儲存每次執行的詳細結果，供使用者查閱歷史及 `pull` 通道兜底。

```sql
CREATE TABLE scheduled_task_results (
    id BIGSERIAL PRIMARY KEY,
    task_id VARCHAR(255) NOT NULL REFERENCES scheduled_tasks(task_id) ON DELETE CASCADE,
    user_id VARCHAR(255) NOT NULL,
    workspace_id VARCHAR(255) NOT NULL,

    -- 執行紀錄
    scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL,   -- 原定執行時間
    started_at TIMESTAMP WITH TIME ZONE,              -- 實際開始時間
    completed_at TIMESTAMP WITH TIME ZONE,            -- 完成時間
    execution_status VARCHAR(50) NOT NULL,             -- 'completed' / 'failed' / 'timeout'
    result_text TEXT,                                  -- Agent 回覆內容
    error_message TEXT,                                -- 錯誤訊息

    -- Delivery 狀態（取代舊的 notification_status 單一欄位）
    notification_status VARCHAR(50) DEFAULT 'pending', -- 'pending' / 'sent' / 'partial' / 'failed' / 'read'
                                                       -- 語意：所有 deliver_to.targets 的綜合狀態
                                                       --   sent: 全部成功
                                                       --   partial: 部分成功
                                                       --   failed: 全部失敗（已走 on_failure）
    notified_at TIMESTAMP WITH TIME ZONE,
    delivery_log JSONB,                                -- 每個 target 的個別投遞狀態
                                                       -- [{"channel":"line","to":"...","status":"sent","at":"..."},...]

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_result_task (task_id, created_at DESC),
    INDEX idx_result_user_unread (user_id, notification_status)
);
```

---

## 4. API 設計

### 4.1 使用者端 API（Gateway proxy → Scheduler Service）

#### POST /api/v1/schedules

**描述**：建立排程任務（也可由 Agent tool 呼叫）

**請求**（最小範例）：
```json
{
  "name": "每日天氣通知",
  "schedule_format": "every_phrase",
  "schedule_expr": "every day 08:00",
  "timezone": "Asia/Taipei",
  "prompt": "請查詢今天台北的天氣預報，包含溫度、降雨機率、建議穿著",
  "deliver_to": {
    "targets": [{"channel": "line", "to": "self", "format": "text"}],
    "on_failure": "pull"
  }
}
```

**請求**（完整範例，含 Hermes 進階欄位）：
```json
{
  "name": "每週週報產生",
  "schedule_format": "every_phrase",
  "schedule_expr": "every friday 17:00",
  "timezone": "Asia/Taipei",
  "prompt": "根據上游收集的本週活動資料，撰寫週報，輸出為 docx 檔案",
  "attach_skills": ["daily-summary", "docx"],
  "model_override": "claude-sonnet-4-6",
  "pre_script": "ls -la ~/workspace/data/weekly/ | head -20",
  "context_from": ["sched-collect-weekly-data"],
  "skip_memory": true,
  "framing": {
    "header": "system: 以下為自動週報任務，請以正式語氣回覆。",
    "footer": "system: 結束。請輸出 markdown 摘要。"
  },
  "deliver_to": {
    "targets": [
      {"channel": "email", "to": "self", "format": "html"},
      {"channel": "email", "to": "boss@example.com", "format": "html"}
    ],
    "on_failure": "pull"
  }
}
```

**響應** (201 Created)：
```json
{
  "task_id": "sched-a1b2c3d4",
  "name": "每日天氣通知",
  "schedule_format": "every_phrase",
  "schedule_expr": "every day 08:00",
  "timezone": "Asia/Taipei",
  "next_run_at": "2026-05-05T00:00:00Z",
  "status": "enabled",
  "deliver_to": {
    "targets": [{"channel": "line", "to": "self", "format": "text"}],
    "on_failure": "pull"
  }
}
```

---

#### GET /api/v1/schedules

**描述**：列出當前使用者的所有排程任務

**響應** (200 OK)：
```json
{
  "tasks": [
    {
      "task_id": "sched-a1b2c3d4",
      "name": "每日天氣通知",
      "schedule_format": "every_phrase",
      "schedule_expr": "every day 08:00",
      "timezone": "Asia/Taipei",
      "next_run_at": "2026-05-05T00:00:00Z",
      "status": "enabled",
      "last_run_at": "2026-05-04T00:00:00Z",
      "last_result": "台北今天晴天，氣溫 26-32°C，降雨機率 10%...",
      "deliver_to": {
        "targets": [{"channel": "line", "to": "self"}],
        "on_failure": "pull"
      }
    }
  ]
}
```

---

#### PATCH /api/v1/schedules/{task_id}

**描述**：更新排程任務（修改 schedule、prompt、deliver_to、啟用/停用等）

**請求**：
```json
{
  "status": "disabled"
}
```

---

#### DELETE /api/v1/schedules/{task_id}

**描述**：刪除排程任務（軟刪除，status 設為 `deleted`）

---

#### GET /api/v1/schedules/{task_id}/results

**描述**：查詢排程任務的執行歷史

**Query params**：`limit`（預設 10）、`offset`

---

#### GET /api/v1/schedules/notifications

**描述**：取得未讀的排程執行結果（**僅對 `deliver_to.targets` 含 `pull` 或走 `on_failure: pull` 的結果**）

**響應** (200 OK)：
```json
{
  "unread": [
    {
      "task_id": "sched-a1b2c3d4",
      "task_name": "每日天氣通知",
      "scheduled_at": "2026-05-04T00:00:00Z",
      "result_text": "台北今天晴天，氣溫 26-32°C...",
      "execution_status": "completed",
      "delivery_log": [
        {"channel": "line", "to": "U1234...", "status": "failed", "at": "..."},
        {"channel": "pull", "status": "sent", "at": "..."}
      ]
    }
  ],
  "count": 1
}
```

#### POST /api/v1/schedules/notifications/read

**描述**：標記通知為已讀

**請求**：
```json
{
  "result_ids": [1, 2, 3]
}
```

---

### 4.2 內部 API（Scheduler → Orchestrator / Agent）

排程執行時，Scheduler 呼叫既有的內部 API，不需要新增端點：

| 步驟 | API | 說明 |
|------|-----|------|
| 喚醒 Pod | `POST /api/v1/orchestrator/ensure` | 既有，確保 workspace Pod 運行 |
| 執行任務 | `POST http://svc-{wid}:8080/api/v1/chat` | 既有，送 prompt 給 Agent |
| 更新活動 | `POST /api/v1/orchestrator/activity` | 既有，延長 idle timeout |

---

## 5. 執行流程

### 5.1 Scheduler Loop（核心迴圈）

```
每 30 秒執行一次：

1. 查詢即將到期的任務
   SELECT * FROM scheduled_tasks
   WHERE status = 'enabled'
     AND next_run_at - INTERVAL '{lead_time} minutes' <= NOW()
     AND execution_status = 'idle'
   FOR UPDATE SKIP LOCKED;

2. 按 workspace_id 分組（同一 workspace 的任務只 ensure 一次）

3. 對每個 workspace group：
   a. [喚醒階段] ensure workspace → wait pod ready
   b. [等待階段] sleep 直到最早的 scheduled_at（盡量準時執行）
   c. [執行階段] 依序送 prompt 給 Agent，收集結果
   d. [通知階段] 依通知管道送出結果
   e. [收尾階段] 更新 next_run_at、寫入 results 表
```

### 5.2 兩階段執行

```
階段 1：提前喚醒（scheduled_at - lead_time）
  ├─ ensure workspace（呼叫 Orchestrator）
  ├─ wait pod ready
  └─ 失敗重試（lead_time 內最多重試 3 次）

階段 2：準時執行（盡量貼近 scheduled_at）
  ├─ 送 prompt 給 Agent
  ├─ 收集回覆
  ├─ 寫入 scheduled_task_results
  └─ 送通知
```

### 5.3 Workspace 分組優化

同一使用者 08:00 有多個任務時，Pod 只喚醒一次：

```
07:55  掃到 workspace ws-user1 有 2 個 08:00 任務
       → ensure pod（一次）
       → wait ready
       → 07:59:50 依序執行「查天氣」「查股價」
       → 送 2 則通知（或合併成 1 則）
```

### 5.4 錯誤處理

| 錯誤場景 | 處理方式 |
|----------|---------|
| Pod ensure 失敗 | lead_time 內重試最多 3 次，全部失敗則標記 `execution_status = 'failed'` |
| Pod ready 超時 | 同上 |
| Agent 執行超時 | 設定單次執行 timeout（預設 120 秒），超時標記 failed |
| Agent 回覆錯誤 | 記錄錯誤，標記 failed，`consecutive_failures += 1` |
| 通知發送失敗 | 結果仍存 DB（`on_failure: pull` 兜底），`delivery_log` 記錄個別 target 狀態 |
| 上游 job 無 success result | `context_from` 指定的上游 job 還沒成功執行過 → 標記 `execution_status = 'blocked'`，下次掃描重試（最多重試 N 次後標記 failed） |
| pre_script 執行失敗 | 收集 stderr 寫入 `error_message`，但仍嘗試執行主 prompt（pre_script 為輔助，非必要） |
| 連續失敗 N 次 | `consecutive_failures >= 5` 時自動停用任務，通知使用者 |

### 5.5 啟動補掃

Scheduler Service 啟動時，掃描錯過的任務：

```sql
SELECT * FROM scheduled_tasks
WHERE status = 'enabled'
  AND next_run_at < NOW()
  AND execution_status = 'idle';
```

對這些任務立即執行（跳過 lead_time 等待），確保重啟期間的任務不會遺漏。

### 5.6 Job 執行管線（Hermes 風格）

Worker 從 `task_executions` 搶到任務後的完整管線。每個步驟對應 §3.1 的進階欄位：

```
Worker 取到任務（status: queued → picked）
  │
  ├─ 1. [上游 context 載入] context_from
  │     對每個 upstream task_id：
  │       SELECT result_text FROM scheduled_task_results
  │       WHERE task_id = $upstream
  │         AND execution_status = 'completed'
  │       ORDER BY completed_at DESC LIMIT 1
  │     全部缺漏 → execution_status = 'blocked'，下次重試
  │     部分缺漏 → 仍可執行，缺漏 upstream 略過
  │     凍結快照寫入 task_executions.upstream_results
  │
  ├─ 2. [Pre-script 執行] pre_script
  │     在 Agent Pod 的 SandboxedShellTool 內執行 pre_script
  │     timeout 5 秒，stdout 截斷至 4KB
  │     收集 stdout 為 script_output（stderr 寫 error_message 但不中斷）
  │     ※ 安全防護：禁止 rm -rf / curl 任意外部 URL（見 §12.7）
  │
  ├─ 3. [Prompt 組裝]
  │     effective_prompt = (
  │         framing.header                    ← 若有
  │       + "\n## Upstream context\n" + json(upstream_results)  ← 若有
  │       + "\n## Pre-script output\n" + script_output           ← 若有
  │       + "\n## Task\n" + prompt
  │       + "\n" + framing.footer             ← 若有
  │     )
  │     寫入 task_executions.effective_prompt（debug / audit 用）
  │
  ├─ 4. [Model 解析] model_override / provider_override
  │     ModelFactory.create_model(
  │         model = model_override or workspace.default_model,
  │         provider = provider_override or workspace.default_provider,
  │     )
  │     僅作用於此次執行，不影響使用者即時對話
  │
  ├─ 5. [Skill 注入] attach_skills
  │     依列表載入 skill metadata（三層優先級：shared > bundled > workspace）
  │     透過 session context message 注入給 Supervisor
  │     未列出的 skill 不可見（縮小決策空間，提升穩定性）
  │
  ├─ 6. [Memory mode] skip_memory
  │     skip_memory = True（預設）→ Agent runtime 偵測 execution_context.skip_memory
  │       → MemoryService.save() 短路（不寫入 workspace memory）
  │       → 但仍寫 sessions/sched-exec-{task_id}/（檔案層級可追溯）
  │     skip_memory = False → 走標準路徑，會影響使用者長期記憶
  │
  ├─ 7. [執行] 送 effective_prompt 給 Agent
  │     workdir = scheduled_tasks.workdir or "sessions/sched-exec-{task_id}/"
  │     session_id = "sched-exec-{task_id}-{timestamp}"（隔離，見 §11.2）
  │     timeout = SCHEDULER_TASK_TIMEOUT_SECONDS（預設 120）
  │     收集 result_text
  │
  ├─ 8. [投遞] deliver_to.targets
  │     對每個 target：
  │       channel == "pull"     → 寫 scheduled_task_results, delivery_log 記 sent
  │       channel == "line"     → resolve to (self → user_bindings 查 platform_uid)
  │                              → Channel Layer LineChannel.send(OutboundMessage)
  │       channel == "email"    → EmailChannel.send(SMTP)
  │       channel == "webhook"  → POST result_text 到 URL
  │       ...
  │     全部失敗 → 走 on_failure（預設 pull，永遠存得到）
  │     delivery_log 逐筆記錄 {channel, to, status, at, error?}
  │     彙整 notification_status: sent / partial / failed
  │
  └─ 9. [收尾]
        recurring  → next_run_at = parse_schedule(schedule_format, schedule_expr, timezone)
        one_shot   → status = 'disabled'
        execution_status = 'idle'
        task_executions.status = 'done' / 'failed'
```

> **關鍵設計原則**：
> - 上游 context 與 pre_script output **不是**「執行授權依據」（避免間接 prompt injection 提升權限，見 §12.8）
> - `effective_prompt` 全文持久化便於 audit；敏感資料應由 prompt 設計時就避免帶入
> - 任何步驟失敗皆不影響 `pull` 兜底，使用者永遠取得到結果（或錯誤訊息）

---

## 6. Delivery Channels（投遞通道）

排程執行結果透過 `deliver_to.targets` 指定一個或多個目的地。**直接重用 Channel Layer adapters**（`poc/gateway/channels/`），與使用者即時對話走同一套訊息基礎設施，不重複造輪子。

### 6.1 支援的 Channel

| Channel | 對應 adapter | POC | Production | 說明 |
|---|---|---|---|---|
| `pull` | （無，存 DB） | ✓ | ✓ | 兜底通道；結果寫 `scheduled_task_results`，使用者上線取 |
| `web` | WebChannel | ✓ | ✓ | SSE 推送至前端 web client |
| `line` | LineChannel | ✓（依 13 設計） | ✓ | 透過 `user_bindings` 解析 `platform_uid` |
| `slack` | SlackChannel | — | ✓ | 同上 |
| `teams` | TeamsChannel | — | ✓ | 同上 |
| `email` | EmailChannel（新） | — | ✓ | SMTP；支援 `text` / `html` format |
| `webhook` | WebhookChannel（新） | — | ✓ | POST 結果到使用者指定 URL，預設 `json` format |

> **`pull` 永遠保證可用**：即使 `deliver_to.targets` 沒列 `pull`，所有結果仍寫入 `scheduled_task_results`（差別只在 `notification_status` 標記），確保使用者永遠取得到。`on_failure: pull` 是兜底語意：當其他 target 全失敗時主動標記為 unread。

### 6.2 Delivery 流程（重用 Channel Layer）

Worker 完成 Agent 執行後：

```
collect result_text
  │
  ├─→ 對每個 target ∈ deliver_to.targets：
  │     1. resolve target.to（"self" → user_bindings 查 platform_uid）
  │     2. 包成 OutboundMessage:
  │          OutboundMessage(
  │              chat_id=resolved_to,
  │              user_id=task.user_id,
  │              text=result_text,
  │              format=target.format or "text",
  │              metadata={"source": "scheduler", "task_id": ...}
  │          )
  │     3. MessageBus.publish(channel=target.channel, message=OutboundMessage)
  │     4. 對應 adapter（LineChannel / SlackChannel / ...）接手投遞
  │     5. 記錄結果到 delivery_log
  │
  ├─ 若所有 target 失敗：
  │     觸發 on_failure 通道（預設 pull）
  │     確保結果至少存得到 DB
  │
  └─ 彙整 notification_status (sent / partial / failed)
```

### 6.3 `to: "self"` 解析

IM channel（line/slack/teams/email）常見的用法：使用者只想推給自己。`"self"` 表示由系統查詢綁定關係：

```python
def resolve_self(user_id: str, platform: str) -> str | None:
    """從 user_bindings 查 user 在 platform 的 active 綁定。"""
    binding = db.query(UserBinding).filter(
        UserBinding.user_id == user_id,
        UserBinding.platform == platform,
        UserBinding.status == "active",
    ).first()
    return binding.platform_uid if binding else None
```

- 找不到綁定 / binding 為 `inactive` → 該 target 失敗，走 `on_failure`
- 使用者重新綁定後（13-user-bindings.md 的 unfollow → follow 恢復流程），下次排程自動跟著走，不需修改排程定義

### 6.4 Server-initiated Push

現有 Channel Layer 是 request-response 模式（使用者發訊息 → 等回覆）。排程通知需要 **server-initiated push**（無 InboundMessage 對應的 OutboundMessage）：

```
Scheduler → MessageBus.publish(channel="line", OutboundMessage(...))
  → ChannelManager 路由到 LineChannel
  → LineChannel.send(OutboundMessage) ← 直接呼叫 LINE Messaging API push
```

這需要每個 channel adapter 提供 `send(OutboundMessage)` 介面（非 reply-only）。WebChannel 之外的 adapter（LINE / Slack / Teams）天然支援 push API；POC 階段先做 `pull` + `web` + `line`，Production 補齊 `email` / `webhook` / `slack` / `teams`。

### 6.5 Pull 通道流程（兜底語意）

```
排程執行完畢
  → 結果寫入 scheduled_task_results（notification_status = 'pending'）
  → delivery_log 記錄各 target 投遞狀態

使用者下次上線（ensure workspace）
  → Orchestrator 或 Gateway 檢查該使用者是否有 unread pull 結果
  → 有的話，在 ensure 回應中附帶 unread_notifications count
  → 前端顯示通知 badge
  → 使用者點擊查看 → GET /api/v1/schedules/notifications
  → 標記已讀 → POST /api/v1/schedules/notifications/read
```

### 6.6 Format 對應

| format | 適用 channel | 說明 |
|---|---|---|
| `text` | 全部 | 純文字（預設） |
| `markdown` | web / slack / line（轉換為 Flex Message） | Markdown 語法 |
| `html` | email | HTML 郵件 |
| `json` | webhook | 結構化 payload，含 metadata |

Adapter 收到不支援的 format 時，自動 fallback 到 `text`。

---

## 7. Agent Tool：CreateScheduleTool

使用者在對話中自然地建立排程，而非手動呼叫 API。

### 7.1 對話範例（基本）

```
使用者：每天早上 8 點告訴我今天台北的天氣
Agent（Supervisor）：好的，我幫你建立一個每日天氣通知排程。
  → 呼叫 CreateScheduleTool
  → {
      "name": "每日天氣通知",
      "schedule_format": "every_phrase",
      "schedule_expr": "every day 08:00",
      "timezone": "Asia/Taipei",
      "prompt": "請查詢今天台北的天氣預報，包含溫度、降雨機率、紫外線指數，並給出穿著建議",
      "deliver_to": {"targets": [{"channel": "pull"}]}
    }
Agent：已建立排程「每日天氣通知」，每天早上 8:00 (台灣時間) 會自動查詢天氣。
      目前通知方式為「下次上線時推送」，你也可以告訴我要推到 LINE 或 Email。
```

### 7.2 對話範例（多目的地 + skill + DAG）

```
使用者：每週五下午 5 點用 daily-summary skill 寫週報，寄 email 給我和 boss
Agent → CreateScheduleTool({
  "name": "週報產生與寄送",
  "schedule_format": "every_phrase",
  "schedule_expr": "every friday 17:00",
  "timezone": "Asia/Taipei",
  "prompt": "請根據本週的活動資料撰寫週報，輸出 markdown 格式",
  "attach_skills": ["daily-summary", "docx"],
  "context_from": ["sched-collect-weekly-data"],
  "skip_memory": true,
  "deliver_to": {
    "targets": [
      {"channel": "email", "to": "self",              "format": "html"},
      {"channel": "email", "to": "boss@example.com",  "format": "html"}
    ],
    "on_failure": "pull"
  }
})
Agent：已建立每週週報排程。每週五 17:00 會：
  1. 載入「每週資料收集」排程的最新成果（context_from）
  2. 用 daily-summary 和 docx 兩個 skill 寫週報
  3. 同步寄 HTML email 給你和 boss@example.com
  寄送失敗時結果仍會保留在你的通知列表。
```

### 7.3 Tool 定義

```python
class CreateScheduleTool(BaseTool):
    """建立定時排程任務。

    使用者在對話中表達定時需求時，由 Supervisor 呼叫此工具。
    工具會透過 Scheduler Service API 建立排程。
    """
    name: str = "create_schedule"
    description: str = (
        "建立定時排程任務。當使用者要求定期執行某件事時使用。"
        "例如：每天早上告知天氣、每週一產生週報、每小時檢查系統狀態、明天下午 3 點提醒開會。"
        ""
        "必要參數："
        "- name: 排程顯示名稱"
        "- schedule_format: 'duration' / 'every_phrase' / 'cron' / 'iso_timestamp'"
        "- schedule_expr: 對應的表達式（如 '5m'、'every monday 9am'、'0 8 * * *'、'2026-05-05T08:00:00+08:00'）"
        "- prompt: 排程到期時要送給 Agent 的指令"
        "- deliver_to: 投遞目的地清單，{targets: [{channel, to?, format?}], on_failure?}"
        ""
        "進階參數（可選）："
        "- timezone: 預設 Asia/Taipei"
        "- attach_skills: 執行時要掛載的 skill 名稱陣列"
        "- model_override / provider_override: 覆寫此 job 的 LLM"
        "- pre_script: 執行 prompt 前先跑的 shell 腳本（stdout 注入 context）"
        "- context_from: 上游 job 的 task_id 陣列，自動載入其最近成功 result"
        "- skip_memory: 預設 true，不污染主對話記憶"
        "- framing: {header, footer}，維持 role alternation"
    )
```

**分配給**：Supervisor 直接使用（不分配給 sub-agent），因為排程建立是系統層級操作。

### 7.4 相關 Tools

| Tool | 說明 | 分配給 |
|------|------|--------|
| `create_schedule` | 建立排程 | Supervisor |
| `list_schedules` | 列出使用者的排程 | Supervisor |
| `update_schedule` | 修改排程（含啟用/停用、改 deliver_to 等） | Supervisor |
| `delete_schedule` | 刪除排程 | Supervisor |

---

## 8. K8s 資源

### 8.1 Scheduler Service Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: scheduler-service
  namespace: agent-system
spec:
  replicas: 1                    # POC: 單 replica；Production: 2-3
  selector:
    matchLabels:
      app: scheduler-service
  template:
    metadata:
      labels:
        app: scheduler-service
    spec:
      serviceAccountName: orchestrator-sa   # 不需要 K8s API 權限，複用或建新 SA
      containers:
      - name: scheduler
        image: k8s-scheduler-service:latest
        ports:
        - containerPort: 8092
        env:
        - name: DATABASE_URL
          value: "postgresql+asyncpg://..."
        - name: ORCHESTRATOR_URL
          value: "http://orchestrator-service:8080"
        - name: SCHEDULER_LEAD_TIME_MINUTES
          value: "5"
        - name: SCHEDULER_SCAN_INTERVAL_SECONDS
          value: "30"
        resources:
          requests:
            cpu: 100m
            memory: 128Mi
          limits:
            cpu: 500m
            memory: 256Mi
```

### 8.2 NetworkPolicy

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: scheduler-service-netpol
  namespace: agent-system
spec:
  podSelector:
    matchLabels:
      app: scheduler-service
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          app: gateway              # Gateway proxy 進來的使用者請求
  egress:
  - to:
    - podSelector:
        matchLabels:
          app: orchestrator         # 呼叫 ensure / activity
    - podSelector:
        matchLabels:
          app: agent-runtime        # 送 prompt 給 Agent Pod
    # DB egress（依部署方式設定）
```

---

## 9. 設定參數

| 參數 | 環境變數 | 預設值 | 說明 |
|------|---------|--------|------|
| Lead time | `SCHEDULER_LEAD_TIME_MINUTES` | 5 | 提前喚醒 Pod 的時間（分鐘） |
| 掃描間隔 | `SCHEDULER_SCAN_INTERVAL_SECONDS` | 30 | Dispatcher 掃描頻率 |
| 執行超時 | `SCHEDULER_TASK_TIMEOUT_SECONDS` | 120 | 單次任務 Agent 執行超時 |
| 最大連續失敗 | `SCHEDULER_MAX_CONSECUTIVE_FAILURES` | 5 | 超過後自動停用任務 |
| 每使用者任務上限 | `SCHEDULER_MAX_TASKS_PER_USER` | 20 | 防止濫用 |
| 最小 cron 間隔 | `SCHEDULER_MIN_INTERVAL_MINUTES` | 5 | 不允許過於頻繁的排程 |
| 併發 ensure 上限 | `SCHEDULER_MAX_CONCURRENT_ENSURES` | 10 | 同時 ensure Pod 數量上限 |
| 併發任務上限 | `SCHEDULER_MAX_CONCURRENT_TASKS` | 20 | 同時執行任務數量上限 |
| 單次 token 上限 | `SCHEDULER_MAX_TOKENS_PER_EXECUTION` | 4000 | 單次排程執行的 LLM token 上限 |
| Prompt 長度上限 | `SCHEDULER_MAX_PROMPT_LENGTH` | 2000 | 排程 prompt 最大字元數 |

---

## 10. POC vs Production 差異

| 項目 | POC | Production |
|------|-----|------------|
| 部署方式 | Orchestrator 內建 scheduler loop | 獨立 Scheduler Service Pod（Dispatcher + Worker） |
| Replica | 1 | 2-3（每 500 使用者 +1 replica，HPA auto-scale） |
| Schedule 格式 | duration + every_phrase + cron + iso_timestamp（schema 一次到位） | 同左 |
| 排程模式 | Recurring + One-Shot | 同左 |
| Attach skills | 支援（三層優先級載入） | 同左 |
| Model / provider override | 支援（YAML 預設 + per-job 覆寫） | 同左 |
| Pre-script | sandbox 內執行（5 秒 timeout，4KB stdout 上限） | 同左 + 額外資源限制 |
| Job DAG (`context_from`) | 支援單層上游 | 多層上游 + cycle detection |
| Delivery channels | `pull` + `web` + `line` | + `email` + `webhook` + `slack` + `teams` |
| `to: "self"` 解析 | 透過 user_bindings（LINE only） | 全平台 |
| `skip_memory` | 預設 True（cron session 不寫入 MemoryService） | 同左 |
| Framing header/footer | 支援 | 同左 |
| Agent Tool | CreateScheduleTool（基本 + 進階參數） | + 自然語言 schedule 解析 |
| 排程執行權限 | Research tools only（read-only） | 同左，可依 resource_tier 放寬 |
| 認證 | Static token | JWT（複用 Gateway 認證） |
| 監控 | Log only | Prometheus metrics（queue depth、執行延遲、失敗率、delivery 成功率） |
| 任務上限 | 每使用者 20 個 | 可依 resource_tier 調整 |
| 併發控制 | 單 replica，無需控制 | DB Queue + FOR UPDATE SKIP LOCKED |
| 尖峰流量 | 併發限流 | 同左 + 分批預熱 + HPA |

---

## 11. 排程與即時對話的併發處理

### 11.1 問題

排程任務執行時，Agent Pod 可能同時面對兩種請求：

- **排程任務**：Worker 送來的 prompt（無人監督）
- **即時對話**：使用者剛好上線，正在跟 Agent 對話

兩者共用同一個 Pod，如果搶同一個 LangGraph thread 會互相干擾。

### 11.2 Session 隔離策略

排程任務使用**獨立的 session**，與使用者的即時對話完全隔離：

```
使用者即時對話：session_id = "sess-abc123"  → thread_id = "sess-abc123"
排程任務執行：  session_id = "sched-exec-{task_id}-{timestamp}"  → 獨立 thread

兩個 session 各自有獨立的：
  - LangGraph thread（對話歷史不互相污染）
  - 工作目錄（sessions/{session_id}/）
  - Agent workflow instance（per-session cache）
```

### 11.3 併發行為

```
情境 1：排程執行中，使用者上線
  → ensure workspace → Pod 已存在，直接回傳 endpoint
  → 使用者的對話走自己的 session，不影響排程任務
  → 兩者併發執行，互不干擾

情境 2：使用者在線上，排程到期
  → Worker ensure → Pod 已存在
  → Worker 用排程專屬 session 送 prompt
  → 使用者的對話不受影響

情境 3：排程執行中，Pod 被 reap
  → 不應發生：Worker 執行期間會呼叫 mark_activity 延長 idle timeout
  → 防護：Worker 在 ensure 後、execute 前，先 mark_activity
```

### 11.4 排程 Session 的生命週期

```
Worker 開始執行
  → 建立排程專屬 session（透過 Orchestrator ensure，帶 session_id = "sched-exec-..."）
  → 執行 prompt → 收集結果
  → 執行完畢 → 標記 session 為 terminated
  → session 工作目錄由正常的清理機制處理（或保留供除錯）
```

排程 session 不會出現在使用者的「對話歷史」列表中（前端可透過 session_id prefix `sched-exec-` 過濾）。

> **與 Memory 的隔離**：當 `skip_memory=True`（預設）時，排程 session 不會寫入 workspace MemoryService，與使用者即時對話的長期記憶完全隔離。換言之，排程的執行記錄留在 `scheduled_task_results` + `sessions/sched-exec-{task_id}/` 檔案層級，但**不會影響**使用者「上次我們聊到 XXX」的記憶連續性。詳見 11-memory-system.md。

---

## 12. 排程任務的安全與權限

### 12.1 問題

排程任務是「無人監督」的 Agent 執行。使用者設定的 prompt 在沒有人在線的情況下被送給 Agent，存在風險：

- **Prompt injection**：惡意 prompt 嘗試突破 Agent 限制
- **越權操作**：排程 prompt 嘗試存取其他 workspace 的資料
- **資源濫用**：排程 prompt 觸發大量 LLM 呼叫或長時間運算
- **非預期副作用**：排程 prompt 觸發有副作用的外部 API 呼叫（如訂餐、付款）

### 12.2 Tool 風險分級

不同工具的風險等級不同，排程任務應依據風險等級決定是否允許執行：

| 等級 | 定義 | 範例 | 排程行為 |
|------|------|------|---------|
| **Level 0 — Safe** | 純查詢，無副作用 | duckduckgo_search、taiwan_weather、file read | 排程可直接執行 |
| **Level 1 — Reversible** | 有副作用，但可取消/復原 | 訂餐、建立行事曆事件、建立草稿 | 排程可執行，需使用者建立時明確授權 |
| **Level 2 — Irreversible** | 有副作用，不可復原 | 付款、刪除資料、發送正式郵件 | 排程不可直接執行，必須走確認流程 |

### 12.3 各等級的排程行為

**Level 0（預設）**：排程任務預設只能使用 Level 0 工具。

```
排程到期 → Agent 執行（僅 Level 0 tools）→ 結果直接送通知
```

**Level 1（授權執行）**：使用者建立排程時明確授權特定 Level 1 工具。

```
建立排程時：
  使用者：每週五下午 3 點幫我訂下週一 XX 餐廳的午餐
  Agent：這個排程會自動執行「訂餐」操作，不會事先確認。確定要授權嗎？
  使用者：確定

排程到期時：
  → Agent 執行（Level 0 + 已授權的 Level 1 tools）
  → 結果送通知，附帶操作摘要（「已訂 XX 餐廳 5/12 12:00，2 位」）
  → 操作記錄供事後取消
```

**Level 2（確認後執行）**：排程到期時不直接執行，產生待確認的執行計畫。

```
排程到期時：
  → Agent 查詢資訊、準備執行計畫
  → 推送「待確認」通知給使用者
    「排程『月繳帳單』準備執行：支付 XX 帳單 $1,200，確認嗎？」
  → 使用者確認 → 才真正執行
  → 超過確認期限（如 24 小時）→ 自動取消，通知使用者
```

### 12.4 Tool 分級對照表

| 工具 | 風險等級 | 即時對話 | 排程（預設） | 排程（授權後） |
|------|---------|---------|-------------|--------------|
| duckduckgo_search | Level 0 | ✓ | ✓ | ✓ |
| taiwan_weather | Level 0 | ✓ | ✓ | ✓ |
| file read | Level 0 | ✓ | ✓ | ✓ |
| book_restaurant | Level 1 | ✓ | ✗ | ✓（需授權） |
| create_calendar_event | Level 1 | ✓ | ✗ | ✓（需授權） |
| file write | Level 1 | ✓ | ✗ | ✓（需授權） |
| terminal (shell) | Level 2 | ✓ | ✗ | ✗（走確認流程） |
| python_repl | Level 2 | ✓ | ✗ | ✗（走確認流程） |
| payment | Level 2 | ✓ | ✗ | ✗（走確認流程） |
| file delete | Level 2 | ✓ | ✗ | ✗（走確認流程） |
| create_schedule | — | ✓ | ✗ | ✗（防遞迴，一律禁止） |

### 12.5 資料模型支援

`scheduled_tasks` 表的 `authorized_tools` JSONB 欄位記錄授權資訊（與 `deliver_to` 完全分離，因為投遞通道與工具授權是兩件獨立的事）：

```json
{
  "level_1": ["book_restaurant"],
  "confirmation_timeout_hours": 24
}
```

對應 `deliver_to` 只描述「結果送去哪」：

```json
{
  "targets": [{"channel": "pull"}],
  "on_failure": "pull"
}
```

Worker 送 prompt 給 Agent 時，帶上 execution context：

```json
{
  "message": "幫我訂下週一 XX 餐廳 12:00 的位子，2 位",
  "session_id": "sched-exec-task123-20260505",
  "execution_context": {
    "type": "scheduled_task",
    "task_id": "sched-a1b2c3d4",
    "tool_policy": {
      "level_0": "allow",
      "level_1": ["book_restaurant"],
      "level_2": "confirm"
    }
  }
}
```

Agent runtime 根據 `execution_context.tool_policy` 決定：
- Level 0 tools：直接載入
- Level 1 tools：僅載入 `authorized_tools` 中列出的
- Level 2 tools：不載入，若 Agent 判斷需要則回傳「待確認」狀態

### 12.6 Level 2 確認流程

```
排程到期
  → Worker ensure pod + 送 prompt
  → Agent 執行，遇到 Level 2 操作
  → Agent 回傳 execution_plan（不執行）：
    {
      "status": "pending_confirmation",
      "plan": "支付 XX 帳單 $1,200",
      "tool": "payment",
      "args": { "amount": 1200, "payee": "XX" },
      "expires_at": "2026-05-07T08:00:00+08:00"
    }
  → Worker 寫入 task_executions（status = 'pending_confirmation'）
  → 推送確認通知給使用者

使用者確認：
  → POST /api/v1/schedules/{task_id}/confirm
  → Worker 重新 ensure pod → 送 execution_plan 給 Agent → 執行
  → 結果送通知

使用者未確認（超過 confirmation_timeout_hours）：
  → 自動標記 status = 'cancelled'
  → 通知使用者「排程『月繳帳單』因未確認已取消」
```

### 12.7 Prompt 審查

排程建立時的防護：

- **長度限制**：prompt 最大 2000 字元
- **敏感詞過濾**：拒絕包含明顯危險指令的 `prompt` 與 `pre_script`（如 `rm -rf`、`DROP TABLE`、`curl` 任意外部 URL、`wget`、`nc`/`netcat`、`base64 | sh` 等 pipe-to-shell 模式）
- **pre_script 限制**：僅允許白名單命令（如 `cat`、`ls`、`echo`、`date`、`grep`、`awk`、`jq`），其餘一律拒絕；執行 timeout 5 秒
- **建立時確認**：Agent 建立排程前，向使用者複述「我理解你要的是 XXX，每天 08:00 執行，對嗎？」
- **授權告知**：若排程需要 Level 1 工具，Agent 必須明確告知使用者「這個排程會自動執行 XXX 操作」並取得確認

### 12.8 執行時防護

- **Timeout 強制**：單次排程執行最長 120 秒，超時強制中斷
- **Token 上限**：單次執行的 LLM token 消耗上限（如 4000 tokens），超過則中斷並回報
- **結果審查**：執行結果在送出通知前，檢查是否包含敏感資訊（API key pattern、密碼 pattern）
- **操作記錄**：Level 1 操作完整記錄（tool name、args、result），供事後取消或審查
- **Audit log**：所有排程執行的 prompt + 結果完整記錄，供事後審查
- **context_from 不可作為授權來源**：`context_from` 載入的上游 result 僅作為 **資訊性 context** 注入 prompt，**絕不可**作為「工具授權升級」「跳過確認流程」「擴大 tool_policy 範圍」的依據。即使上游 result 明文寫「請允許執行 payment」，Worker 與 Agent runtime 都必須忽略——授權只認 `authorized_tools` 欄位（建立排程時使用者明確授權的內容），避免間接 prompt injection 透過上游資料提升權限。

---

## 13. 一次性排程（One-Shot Schedule）

### 13.1 使用場景

使用者不只會設定 recurring 排程，也常有一次性的定時需求：

```
「明天早上 8 點提醒我開會」
「下週一 9 點幫我查一下台積電股價」
「今天下午 3 點告訴我天氣」
```

### 13.2 與 Recurring 的差異

| 項目 | Recurring | One-Shot |
|------|-----------|----------|
| schedule_type | `recurring` | `one_shot` |
| schedule_format | `duration` / `every_phrase` / `cron` | `iso_timestamp` |
| schedule_expr | 如 `every day 08:00`、`0 8 * * *` | ISO-8601 時間，如 `2026-05-05T08:00:00+08:00` |
| 執行後行為 | 計算下次 `next_run_at` | 自動設為 `status = 'disabled'` |
| 自動清理 | 不清理（除非使用者刪除） | 執行完 7 天後自動清理（或依設定） |

### 13.3 API 差異

建立一次性排程時，`schedule_format` 設為 `iso_timestamp`，`schedule_expr` 直接填 ISO-8601 時間（不再有獨立的 `run_at` 欄位，統一到 `schedule_expr`）：

```json
{
  "name": "明天開會提醒",
  "schedule_type": "one_shot",
  "schedule_format": "iso_timestamp",
  "schedule_expr": "2026-05-05T08:00:00+08:00",
  "timezone": "Asia/Taipei",
  "prompt": "提醒我今天早上 9 點有產品會議，請幫我整理昨天的會議紀錄重點",
  "deliver_to": {
    "targets": [{"channel": "pull"}]
  }
}
```

Scheduler 收到後直接設定 `next_run_at = parse_iso(schedule_expr)`，不需要解析 cron。

### 13.4 對話範例

```
使用者：明天早上 8 點提醒我開會
Agent（Supervisor）：好的，我幫你設定一個提醒。
  → 呼叫 CreateScheduleTool
  → {
      "name": "開會提醒",
      "schedule_type": "one_shot",
      "schedule_format": "iso_timestamp",
      "schedule_expr": "2026-05-05T08:00:00+08:00",
      "prompt": "提醒使用者今天有會議要參加",
      "deliver_to": {"targets": [{"channel": "line", "to": "self"}]}
    }
Agent：已設定提醒，明天 (5/5) 早上 8:00 會透過 LINE 通知你。
```

### 13.5 Dispatcher 處理

Dispatcher 掃描時不區分 recurring / one_shot，統一看 `next_run_at`：

```sql
-- 同一個查詢，兩種類型都會被掃到
SELECT * FROM scheduled_tasks
WHERE status = 'enabled'
  AND next_run_at - INTERVAL '{lead_time} minutes' <= NOW()
  AND execution_status = 'idle';
```

差異只在執行完畢後的收尾：

```python
if task.schedule_type == 'recurring':
    task.next_run_at = calculate_next_run(task.schedule_format, task.schedule_expr, task.timezone)
    task.execution_status = 'idle'
elif task.schedule_type == 'one_shot':
    task.status = 'disabled'  # 不再執行
    task.execution_status = 'idle'
```

---

## 14. 待討論事項

- [ ] **Schedule 格式解析器歸屬**：自然語言（如「明天早上 8 點」「每週五下午 5 點」）由 Agent 直接轉成 `every_phrase` / `iso_timestamp` / `cron`？還是 Scheduler Service 提供 NL parser endpoint 統一處理？前者簡單但解析品質依賴 LLM；後者可控但需維護 parser。
- [ ] **DAG 深度限制**：`context_from` 允許多層上游串接嗎？POC 只做單層、不做 cycle detection；Production 是否支援多層 + topological sort + cycle detection？深度上限定多少（如 3 層）？
- [ ] **deliver_to 投遞重試策略**：單一 target 失敗是否重試？重試幾次（如 3 次、exponential backoff）？所有 target 都失敗才走 `on_failure`，還是任一失敗就走？
- [ ] **Cron 表達式 vs every_phrase**：兩者表達力重疊，是否在 API 層面強制統一（如只接受 `every_phrase` 與 `cron` 二選一，不允許混用）？
- [ ] **Group workspace 排程**：群組工作區的排程由誰建立？結果通知所有成員還是只通知建立者？（建議 POC 先不支援 group 排程）
- [ ] **排程結果的保留期限**：`scheduled_task_results` 要保留多久？需要自動清理機制嗎？
- [ ] **與 Skill 系統的整合**：`attach_skills` 設計已支援，但是否需要 skill 風險分級（某些 skill 不可在排程中使用）？
- [ ] **費用控制**：每次排程執行都會消耗 LLM token，是否需要設定每日/每月 token 預算上限？`model_override` 是否限制只能選 cost-effective 模型？
- [ ] **pre_script 命令白名單**：白名單該包含哪些命令？是否需要 per-workspace 自訂白名單？
- [ ] **可觀測性**：Production 需要哪些 Prometheus metrics？（queue depth、執行延遲、失敗率、Worker 利用率、deliver_to 各 channel 投遞成功率、DAG 上游缺漏率）
