# 11. Memory System 設計文件

> Status: Implemented（POC 已實作，使用 SQLite FTS5 trigram tokenizer）
> Created: 2026-05-15
> Branch: feature/memory-system-upgrade

## 1. 動機與目標

### 1.1 現有問題

| 問題 | 影響 |
|------|------|
| 記憶搜尋靠 LLM 讀 MEMORY_INDEX.md 判斷 | 不可靠（LLM 可能跳過）、佔用 tool call 額度、準確度隨記憶量下降 |
| 100 行索引上限 | 長期使用撞天花板 |
| 無語意搜尋 | 「上次提到的部署問題」→ 純關鍵字比對找不到 |
| 無主動注入 | 每次對話都要 LLM 自己決定是否讀記憶，follow-up 訊息幾乎不會觸發 |
| 無自動記憶提取 | 完全依賴使用者說「記住」或 LLM 主動判斷 |
| 無時間衰減 | 過時記憶不會自動降級 |
| 記憶操作走 shell tool | 無法追蹤、無法驗證格式、無法做存取控制 |

### 1.2 設計目標

1. **程式化搜尋**：用 SQLite FTS5（PVC 本地）取代 LLM 讀索引判斷
2. **主動記憶注入**：對話前自動搜尋相關記憶，注入 system context
3. **專用記憶工具**：memory_search / memory_save / memory_update / memory_delete
4. **自動記憶提取**：對話結束後自動提取值得記憶的資訊（Dreaming Light Sleep）
5. **效能保護**：timeout + circuit breaker + cache，記憶搜尋不拖垮回應速度
6. **向下相容**：保留 Markdown 格式、四種記憶類型、Why/How 結構

### 1.3 不做的事

- 不做向量嵌入（Phase 1 用 FTS 就夠，向量留 Phase 2）
- 不做跨 workspace 記憶共享（各 workspace 記憶獨立）
- 不做記憶版本控制（git 已提供）
- 不改變 PVC 目錄結構（memories/ 目錄保留）

---

## 2. 架構總覽

```
使用者訊息
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  Active Memory Pre-hook (MemoryService.recall)       │
│                                                      │
│  1. 從使用者訊息提取搜尋 query                       │
│  2. SQLite FTS5 搜尋（本地 PVC）                     │
│  3. 讀取 Top-K 記憶檔案內容                          │
│  4. 格式化為 context snippet                         │
│  5. 注入 supervisor prompt（動態追加）               │
│                                                      │
│  效能保護：timeout 3s / circuit breaker / cache 30s  │
└─────────────────────────────────────────────────────┘
    │
    ▼
Supervisor Agent 執行（已帶記憶上下文）
    │
    ├─→ research_agent（含 memory_search tool）
    └─→ code_agent（含 memory_save tool）
              │
              ▼
        memories/ 目錄（PVC）
              │
              ▼
        MemoryService.index_file()
              │
              ▼
        memories/.index/memory.db（SQLite FTS5 索引）
```

---

## 3. 資料模型

### 3.1 儲存架構決策：SQLite on PVC（非 PostgreSQL）

**設計決策**：記憶索引使用 PVC 本地的 SQLite FTS5，而非系統 PostgreSQL。

| 考量 | SQLite on PVC | PostgreSQL |
|------|---------------|------------|
| 資料局部性 | 記憶檔 + 索引同一 PVC，Pod 重建自動帶走 | 記憶檔在 PVC，索引在外部 DB，需同步 |
| 關注點分離 | PostgreSQL 專注系統管理（sessions, workspaces, members） | 記憶索引混在系統表裡 |
| 故障隔離 | DB 掛了 → 記憶搜尋照常運作 | DB 掛了 → 記憶也搜不到 |
| 遷移簡單 | workspace 搬家 = 複製 PVC | 還要額外遷移 DB rows |
| 效能 | 本地 I/O，FTS5 查詢 < 1ms | 網路 round-trip 2-5ms |
| 併發 | WAL mode，同 Pod 多 session 安全 | 天生支援 |
| 容量 | 單 workspace < 1000 筆，綽綽有餘 | 殺雞用牛刀 |

**結論**：記憶是 workspace 的私有資料，生命週期跟 PVC 一致。放在 PostgreSQL 會讓系統 DB 的職責模糊，且增加不必要的網路依賴。

### 3.2 SQLite Schema（`memories/.index/memory.db`）

```sql
-- 記憶條目表
CREATE TABLE memory_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT NOT NULL UNIQUE,           -- 相對於 memories/ 的路徑
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,                   -- user/feedback/project/reference
    tags        TEXT DEFAULT '',                 -- 逗號分隔的標籤
    summary     TEXT NOT NULL,                   -- 一句話摘要
    content     TEXT NOT NULL,                   -- 完整記憶內容（不含 frontmatter）
    recall_count    INTEGER DEFAULT 0,           -- 被召回次數（Dreaming 評分用）
    last_recalled   TEXT,                        -- ISO 8601 時間戳
    created_at      TEXT NOT NULL,               -- ISO 8601
    updated_at      TEXT NOT NULL                -- ISO 8601
);

-- FTS5 全文搜尋虛擬表（BM25 ranking）
CREATE VIRTUAL TABLE memory_fts USING fts5(
    name,
    summary,
    tags,
    content,
    content=memory_entries,
    content_rowid=id,
    tokenize='trigram case_sensitive 0'         -- 三字元分詞，支援中英文部分匹配（POC 實作選擇）
);

-- FTS 同步 triggers
CREATE TRIGGER trg_memory_ai AFTER INSERT ON memory_entries BEGIN
    INSERT INTO memory_fts(rowid, name, summary, tags, content)
    VALUES (new.id, new.name, new.summary, new.tags, new.content);
END;

CREATE TRIGGER trg_memory_ad AFTER DELETE ON memory_entries BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, name, summary, tags, content)
    VALUES ('delete', old.id, old.name, old.summary, old.tags, old.content);
END;

CREATE TRIGGER trg_memory_au AFTER UPDATE ON memory_entries BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, name, summary, tags, content)
    VALUES ('delete', old.id, old.name, old.summary, old.tags, old.content);
    INSERT INTO memory_fts(rowid, name, summary, tags, content)
    VALUES (new.id, new.name, new.summary, new.tags, new.content);
END;

-- 類型篩選索引
CREATE INDEX idx_memory_type ON memory_entries (type);

-- 時間排序索引
CREATE INDEX idx_memory_updated ON memory_entries (updated_at DESC);
```

**FTS5 搜尋範例**：

```sql
-- 基本搜尋（BM25 排序）
SELECT me.*, rank
FROM memory_fts
JOIN memory_entries me ON memory_fts.rowid = me.id
WHERE memory_fts MATCH :query
ORDER BY rank
LIMIT :limit;

-- 帶類型篩選
SELECT me.*, rank
FROM memory_fts
JOIN memory_entries me ON memory_fts.rowid = me.id
WHERE memory_fts MATCH :query AND me.type = :type_filter
ORDER BY rank
LIMIT :limit;

-- 欄位加權搜尋（name 權重最高）
SELECT me.*, bm25(memory_fts, 10.0, 5.0, 3.0, 1.0) as rank
FROM memory_fts
JOIN memory_entries me ON memory_fts.rowid = me.id
WHERE memory_fts MATCH :query
ORDER BY rank
LIMIT :limit;
```

### 3.3 PVC 目錄結構

```
/workspace/{wid}/
├── memories/
│   ├── *.md                    # 記憶檔案（source of truth）
│   ├── MEMORY_INDEX.md         # 人類可讀總覽（副產品，不再是搜尋入口）
│   └── .index/
│       └── memory.db           # SQLite FTS5 索引（可從 *.md 重建）
├── sessions/{sid}/
└── data/
```

### 3.4 Production（S3）環境策略

POC 用 PVC 本地檔案，Production 用 S3。SQLite 不能直接放 S3（不支援隨機讀寫），
因此 Production 的索引改為 Pod 本地 ephemeral 檔案，啟動時從 S3 重建。

```
┌─────────────────────────────────────────────────────────────────┐
│  POC（PVC）                                                      │
│                                                                  │
│  PVC /workspace/{wid}/memories/                                  │
│  ├── *.md              ← source of truth                         │
│  └── .index/memory.db  ← 持久化索引（Pod 重建後直接可用）        │
│                                                                  │
│  特點：索引跟檔案一起在 PVC，Pod 重建不需重建索引                │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Production（S3）                                                │
│                                                                  │
│  S3: s3://{bucket}/users/{uid}/memories/                         │
│  ├── *.md              ← source of truth（持久化）               │
│  └── （無 .index/）                                              │
│                                                                  │
│  Pod 本地: /tmp/memory/memory.db                                 │
│  └── SQLite FTS5 索引  ← ephemeral（Pod 啟動時從 S3 重建）      │
│                                                                  │
│  特點：                                                          │
│  - 索引是 ephemeral，Pod 死了就沒了，但重建成本低（<1000 筆）    │
│  - 寫入雙寫：S3 + 本地 SQLite                                   │
│  - 啟動時 list S3 prefix → 下載 *.md → 批量建索引（<5s）        │
│  - 與現有 LocalBackend → S3Backend 遷移模式一致                  │
└─────────────────────────────────────────────────────────────────┘
```

**MemoryService 抽象層**：

```python
class MemoryBackend(ABC):
    """記憶儲存後端抽象"""

    @abstractmethod
    async def read_file(self, path: str) -> str: ...

    @abstractmethod
    async def write_file(self, path: str, content: str) -> None: ...

    @abstractmethod
    async def delete_file(self, path: str) -> None: ...

    @abstractmethod
    async def list_files(self, pattern: str = "*.md") -> list[str]: ...


class LocalMemoryBackend(MemoryBackend):
    """POC：PVC 本地檔案"""
    def __init__(self, memories_dir: str): ...


class S3MemoryBackend(MemoryBackend):
    """Production：S3 儲存"""
    def __init__(self, bucket: str, prefix: str): ...
```

MemoryService 不直接操作檔案系統，而是透過 `MemoryBackend` 抽象。
SQLite 索引永遠在本地（POC: PVC `.index/`，Production: `/tmp/memory/`），
差別只在索引是否持久化：

| 環境 | 記憶檔案 | SQLite 索引 | 啟動行為 |
|------|----------|-------------|----------|
| POC | PVC `memories/*.md` | PVC `memories/.index/memory.db`（持久） | 檢查索引存在 → 增量更新 |
| Production | S3 `memories/*.md` | `/tmp/memory/memory.db`（ephemeral） | 全量重建（list + download + index） |

**重建效能估算**：
- 1000 筆記憶 × 平均 500 bytes = ~500KB S3 讀取
- S3 ListObjectsV2 + BatchGetObject：< 2s
- SQLite FTS5 批量插入 1000 筆：< 100ms
- 總計：Pod 啟動增加 < 3s（可接受，與 checkpointer 初始化並行）

### 3.5 記憶檔案格式（保持不變）

```markdown
---
name: 記憶名稱
type: user | feedback | project | reference
tags: [標籤1, 標籤2]
created: 2026-05-15
updated: 2026-05-15
---

記憶內容。

**Why:** 為什麼這件事重要
**How to apply:** 未來如何應用
```

### 3.6 雙寫策略（File + SQLite）

記憶的 source of truth 是 Markdown 檔案（人類可讀、可 git 管理）。
SQLite 是搜尋索引，可隨時從檔案重建。

```
寫入流程：
  1. 寫入 Markdown 檔案到 memories/（透過 MemoryBackend）
  2. 解析 frontmatter + content
  3. UPSERT 到本地 SQLite memory_entries 表
  4. 同步更新 MEMORY_INDEX.md（人類可讀副產品）

讀取流程：
  搜尋 → SQLite FTS5 query → 取得 file_path → 讀取完整 Markdown 檔案

重建流程（Pod 啟動時）：
  POC:  檢查 .index/memory.db 存在 → 增量更新（比對 mtime）
  Prod: 無本地索引 → list S3 prefix → download *.md → 全量建索引
```

---

## 4. 核心元件

### 4.1 MemoryService（新增 `poc/agent/memory_service.py`）

```python
class MemoryService:
    """Workspace 記憶管理服務（SQLite FTS5 索引 + 檔案後端）"""

    def __init__(self, workspace_id: str, memories_dir: str, index_path: str):
        self.workspace_id = workspace_id
        self.memories_dir = memories_dir       # PVC: memories/, S3: 透過 backend 抽象
        self.index_path = index_path           # PVC: memories/.index/memory.db, Prod: /tmp/memory/memory.db
        self.db: aiosqlite.Connection = None   # lazy init

    async def initialize(self) -> None:
        """建立 SQLite 連線 + 確保 schema 存在"""

    # --- 搜尋 ---
    async def search(
        self,
        query: str,
        type_filter: str | None = None,
        limit: int = 5
    ) -> list[MemoryEntry]:
        """FTS5 搜尋記憶（BM25 排序，欄位加權）"""

    # --- 主動召回（Active Memory） ---
    async def recall(
        self,
        user_message: str,
        recent_messages: list[str] | None = None
    ) -> str | None:
        """
        對話前主動搜尋相關記憶。
        回傳格式化的 context snippet，或 None（無相關記憶）。
        """

    # --- CRUD ---
    async def save(self, name: str, type: str, tags: list, content: str, why: str, how: str) -> str:
        """建立新記憶（寫檔 + 索引 + 更新 MEMORY_INDEX.md）"""

    async def update(self, file_path: str, content: str | None, tags: list | None) -> None:
        """更新記憶"""

    async def delete(self, file_path: str) -> None:
        """刪除記憶（刪檔 + 移除索引 + 更新 MEMORY_INDEX.md）"""

    # --- 索引管理 ---
    async def reindex(self) -> int:
        """全量重建索引（掃描 memories/*.md → 批量插入 SQLite）"""

    async def index_file(self, file_path: str) -> None:
        """單檔索引更新（UPSERT）"""

    async def close(self) -> None:
        """關閉 SQLite 連線"""
```

### 4.2 Active Memory Pre-hook

整合位置：`runtime.py` 的 `invoke()` / `invoke_stream()` 方法中，在送入 supervisor 前執行。

```python
# runtime.py invoke_stream() 中
async def invoke_stream(self, session_id, message, ...):
    # --- Active Memory Pre-hook ---
    memory_context = await self._recall_memory(session_id, message)

    # 注入到 messages 中（作為 SystemMessage，排在 supervisor prompt 之後）
    messages = []
    if memory_context:
        messages.append(SystemMessage(content=memory_context))
    messages.append(HumanMessage(content=message))

    # 送入 supervisor graph
    ...
```

效能保護：

```python
class ActiveMemoryHook:
    """帶 circuit breaker 和 cache 的記憶召回"""

    TIMEOUT_MS = 3000           # 硬超時 3 秒
    CACHE_TTL_MS = 30000        # 相同 query 快取 30 秒
    CB_MAX_FAILURES = 3         # 連續 3 次失敗後跳過
    CB_COOLDOWN_MS = 60000      # 跳過後冷卻 60 秒

    async def recall(self, message: str) -> str | None:
        # 1. Circuit breaker 檢查
        if self._is_circuit_open():
            return None

        # 2. Cache 檢查
        cached = self._get_cache(message)
        if cached is not None:
            return cached

        # 3. 帶 timeout 的搜尋
        try:
            result = await asyncio.wait_for(
                self.memory_service.recall(message),
                timeout=self.TIMEOUT_MS / 1000
            )
            self._reset_failures()
            self._set_cache(message, result)
            return result
        except asyncio.TimeoutError:
            self._record_failure()
            return None
```

### 4.3 記憶工具（新增到 tools.py）

```python
# 加入 research_agent 的工具組
class MemorySearchTool(BaseTool):
    name = "memory_search"
    description = "搜尋工作區記憶。輸入關鍵字或問題，回傳相關記憶摘要。"

    async def _arun(self, query: str, type: str = None) -> str:
        results = await self.memory_service.search(query, type_filter=type, limit=5)
        if not results:
            return "未找到相關記憶。"
        return self._format_results(results)

# 加入 code_agent 的工具組
class MemorySaveTool(BaseTool):
    name = "memory_save"
    description = "儲存新記憶。需提供名稱、類型(user/feedback/project/reference)、標籤、內容、原因(why)、應用方式(how)。"

    async def _arun(self, name: str, type: str, tags: list, content: str, why: str, how: str) -> str:
        file_path = await self.memory_service.save(name, type, tags, content, why, how)
        return f"記憶已儲存：{file_path}"

class MemoryUpdateTool(BaseTool):
    name = "memory_update"
    description = "更新既有記憶的內容或標籤。"

    async def _arun(self, file_path: str, content: str = None, tags: list = None) -> str:
        await self.memory_service.update(file_path, content, tags)
        return f"記憶已更新：{file_path}"

class MemoryDeleteTool(BaseTool):
    name = "memory_delete"
    description = "刪除記憶。"

    async def _arun(self, file_path: str) -> str:
        await self.memory_service.delete(file_path)
        return f"記憶已刪除：{file_path}"
```

### 4.4 工具分配

| 工具 | 分配給 | 原因 |
|------|--------|------|
| memory_search | research_agent | 搜尋是 research 的職責 |
| memory_save | code_agent | 寫入涉及檔案操作 |
| memory_update | code_agent | 同上 |
| memory_delete | code_agent | 同上 |

---

## 5. Active Memory 注入格式

```xml
<active_memory>
相關記憶（自動召回，僅供參考）：

1. [使用者偏好] (user) — 回覆使用繁體中文，偏好簡潔風格
   Why: 使用者明確要求
   How: 所有回覆用繁中，避免冗長解釋

2. [Auth 架構決策] (project) — Auth 放在 Gateway，Admin 獨立 Pod
   Why: 關注點分離，Admin 不直接碰 K8s
   How: 設計新元件時遵循此分層

（共找到 2 筆相關記憶，搜尋耗時 45ms）
</active_memory>
```

---

## 6. 索引重建策略

### 6.1 Pod 啟動時（全量或增量重建）

```python
# Agent 啟動流程中
async def on_startup():
    index_path = os.path.join(memories_dir, ".index", "memory.db")
    memory_service = MemoryService(workspace_id, memories_dir, index_path)
    await memory_service.initialize()

    # POC (PVC): 索引持久化，只需增量更新（新增/修改的檔案）
    # Production (S3): 索引 ephemeral，每次全量重建
    count = await memory_service.reindex()
    logger.info(f"Memory index ready: {count} entries")
```

### 6.2 記憶變更時（增量更新）

每次 memory_save / memory_update / memory_delete 操作後，自動更新 DB 索引。
不需要 file watcher（所有寫入都經過 MemoryService）。

### 6.3 MEMORY_INDEX.md 的角色轉變

- **之前**：AI 搜尋的入口（必須讀取）
- **之後**：人類可讀的總覽（選擇性維護）

MemoryService 在 save/update/delete 時仍會同步更新 MEMORY_INDEX.md，
但搜尋不再依賴它。它變成一個「方便人看」的副產品。

---

## 7. 與 Scheduler Service 整合（Dreaming）

> 此部分為 Phase 2，依賴 Scheduler Service 實作完成。

### 7.1 Dreaming Light Sleep（每日自動提取）

排程任務：每日一次（凌晨 3:00 或 workspace 閒置時）

```yaml
# 對應 docs/10 scheduled_tasks schema
scheduled_task:
  name: memory_dreaming_light
  schedule_format: cron
  schedule_expr: "0 3 * * *"
  timezone: Asia/Taipei
  schedule_type: recurring
  workspace_scope: per_workspace            # 每個 workspace 各建一個排程
  skip_memory: true                         # 不寫入自身 workspace memory（避免遞迴）
  workdir: "sessions/sched-dreaming-{workspace_id}-{date}/"
  attach_skills: []                         # 不需要額外 skill
  authorized_tools:                         # Level 1: 可逆寫入（memory_save/update）
    level_1: ["memory_save", "memory_update"]
  deliver_to:
    targets: [{"channel": "pull"}]          # 結果存 DB，不推播
    on_failure: "pull"
  prompt: |
    掃描今日所有 session 的對話紀錄（checkpointer），提取值得長期記憶的資訊。
    判斷標準：
    - 使用者明確表達的偏好或修正
    - 重要的專案決策或時程變更
    - 新發現的外部資源位置
    - 反覆出現的模式或主題

    對每個值得記憶的項目，呼叫 memory_save 工具儲存。
    如果已有相似記憶，呼叫 memory_update 更新而非重複建立。
```

### 7.2 Dreaming Deep Sleep（定期精煉）

排程任務：每週一次

```yaml
# 對應 docs/10 scheduled_tasks schema
scheduled_task:
  name: memory_dreaming_deep
  schedule_format: cron
  schedule_expr: "0 4 * * 0"               # 每週日凌晨 4:00
  timezone: Asia/Taipei
  schedule_type: recurring
  skip_memory: true
  workdir: "sessions/sched-dreaming-deep-{workspace_id}/"
  authorized_tools:
    level_1: ["memory_save", "memory_update", "memory_delete"]
  deliver_to:
    targets: [{"channel": "pull"}]
    on_failure: "pull"
  prompt: |
    審視所有記憶：
    1. 合併重複或高度相似的記憶
    2. 標記超過 30 天未被召回的記憶為候選清理項
    3. 更新過時的 project 類型記憶
    4. 確保 MEMORY_INDEX.md 與實際檔案一致
```

---

## 8. 配置

### 8.1 config.yaml 新增區段

```yaml
memory:
  # Active Memory 主動召回
  active_recall:
    enabled: true
    timeout_ms: 3000
    cache_ttl_ms: 30000
    max_results: 3
    circuit_breaker:
      max_failures: 3
      cooldown_ms: 60000

  # 索引設定
  index:
    reindex_on_startup: true

  # Dreaming（Phase 2，依賴 Scheduler Service）
  dreaming:
    enabled: false
    light_sleep:
      schedule_format: cron
      schedule_expr: "0 3 * * *"
    deep_sleep:
      schedule_format: cron
      schedule_expr: "0 4 * * 0"
```

### 8.2 環境變數覆蓋

| 環境變數 | 說明 | 預設值 |
|----------|------|--------|
| `MEMORY_ACTIVE_RECALL_ENABLED` | 啟用主動記憶召回 | true |
| `MEMORY_ACTIVE_RECALL_TIMEOUT_MS` | 召回超時（ms） | 3000 |
| `MEMORY_ACTIVE_RECALL_MAX_RESULTS` | 最多召回幾筆 | 3 |
| `MEMORY_DREAMING_ENABLED` | 啟用 Dreaming | false |

---

## 9. 實作計畫

### Phase 0：SQLite Schema + MemoryService 骨架

- [ ] 建立 `poc/agent/memory_service.py`（MemoryService + MemoryBackend 抽象）
- [ ] 實作 `LocalMemoryBackend`（PVC 檔案操作）
- [ ] SQLite FTS5 schema 初始化 + reindex 邏輯
- [ ] Pod 啟動時執行 initialize + reindex
- [ ] 新增 `aiosqlite` 到 requirements.txt
- [ ] 單元測試

### Phase 1：專用工具 + Active Memory

- [ ] 新增 memory_search / memory_save / memory_update / memory_delete 工具
- [ ] 整合到 research_agent / code_agent 工具組
- [ ] 實作 ActiveMemoryHook（pre-hook + circuit breaker + cache）
- [ ] 整合到 runtime.py invoke/invoke_stream
- [ ] 更新 supervisor prompt（移除舊的「讀 MEMORY_INDEX.md」指令）
- [ ] 更新 memory-management SKILL.md（反映新工具）
- [ ] E2E 測試

### Phase 2：Dreaming（依賴 Scheduler Service）

- [ ] 實作 Dreaming Light Sleep 排程任務
- [ ] 實作 Dreaming Deep Sleep 排程任務
- [ ] recall_count / last_recalled 追蹤
- [ ] 記憶品質評分（頻率 + 多樣性 + 時效性）

### Phase 3：向量搜尋（可選）

- [ ] 評估 embedding provider（本地 GGUF vs API）
- [ ] 新增 embedding 欄位到 memory_entries
- [ ] Hybrid search（FTS + 向量相似度加權）

---

## 10. 遷移策略

### 從現有 memory-management skill 遷移

1. **記憶檔案格式不變** — 現有 memories/*.md 檔案完全相容
2. **Pod 啟動時自動索引** — 現有記憶自動被索引到 DB
3. **MEMORY_INDEX.md 保留** — 繼續同步更新，但不再是搜尋入口
4. **Supervisor prompt 更新** — 移除「讀 MEMORY_INDEX.md」指令，改為「相關記憶已自動注入」
5. **SKILL.md 更新** — 觸發詞保留，操作方式改為呼叫專用工具

### 回滾方案

如果新系統有問題：
- 設定 `MEMORY_ACTIVE_RECALL_ENABLED=false` 關閉主動召回
- Supervisor prompt 恢復舊版（讀 MEMORY_INDEX.md）
- 記憶檔案本身不受影響（source of truth 在 PVC）

---

## 11. 與現有元件的關係

```
┌─────────────────────────────────────────────────────────────────┐
│                        Agent Pod                                 │
│                                                                  │
│  ┌──────────────┐    ┌──────────────────┐    ┌───────────────┐  │
│  │   runtime.py │───▶│ ActiveMemoryHook │───▶│ MemoryService │  │
│  │              │    └──────────────────┘    │               │  │
│  │  supervisor  │                            │  search()     │  │
│  │      │       │                            │  save()       │  │
│  │      ├─ research_agent                    │  update()     │  │
│  │      │    └─ memory_search ──────────────▶│  delete()     │  │
│  │      │                                    │  reindex()    │  │
│  │      └─ code_agent                        │  recall()     │  │
│  │           └─ memory_save ────────────────▶│               │  │
│  │           └─ memory_update ──────────────▶│               │  │
│  │           └─ memory_delete ──────────────▶│               │  │
│  └──────────────┘                            └───────┬───────┘  │
│                                                      │          │
│                                              ┌───────▼───────┐  │
│                                              │  SQLite FTS5   │  │
│                                              │  memory.db     │  │
│                                              │  (本地索引)    │  │
│                                              └───────┬───────┘  │
│                                                      │          │
│                                              ┌───────▼───────┐  │
│                                              │  PVC / S3     │  │
│                                              │  memories/    │  │
│                                              │  ├─ *.md      │  │
│                                              │  └─ INDEX.md  │  │
│                                              └───────────────┘  │
│                                                                  │
│  PostgreSQL（系統管理，不碰記憶）：                               │
│  ├─ checkpointer（對話歷史）                                     │
│  ├─ users / workspaces / sessions                                │
│  └─ workspace_members / activity_logs                            │
└─────────────────────────────────────────────────────────────────┘
```

### 11.1 與 Context Compaction 的關係

Context Compaction（[12-context-compaction.md](./12-context-compaction.md)）在壓縮對話歷史前會執行 **Silent Memory Flush**，透過 LLM 從即將被壓縮的訊息中提取重要資訊，呼叫 `MemoryService.save()` 存入記憶系統：

```
對話超過 context window 75%
       │
       ▼
  ContextCompactor._memory_flush()
       │
       ├─ LLM 提取 facts / decisions / todos / lessons
       │
       ▼
  MemoryService.save(type="core", tags=["compaction_flush", category])
       │
       ▼
  SQLite FTS5 索引更新 → 未來 Active Memory 可召回
```

這確保了長對話中的重要資訊不會因壓縮而遺失，而是轉化為可被 Active Memory 召回的長期記憶。

---

## 12. 開放問題

| # | 問題 | 選項 | 建議 |
|---|------|------|------|
| 1 | FTS5 tokenizer | `unicode61`（Unicode 分詞）vs `trigram`（三字元匹配，支援部分匹配） | **已決定**：POC 採用 `trigram case_sensitive 0`，中文部分匹配優於 `unicode61`（中文無空格分隔），索引大小於 POC 規模可接受 |
| 2 | memory_save 放 research_agent 還是 code_agent？ | research 更語意、code 更操作 | code_agent（寫入是操作行為） |
| 3 | Active Memory 用主模型還是輕量模型？ | 主模型（準確）vs 輕量模型（快） | 不用模型，純程式化 FTS 搜尋（Phase 1 夠用） |
| 4 | recall_count 追蹤粒度 | 每次 active recall 都算 vs 只算使用者主動搜尋 | 兩者都算，分欄位追蹤 |
| 5 | 記憶容量上限 | 無限 vs 設上限 | 暫不設限，靠 Dreaming Deep Sleep 定期清理 |
| 6 | Group workspace 的記憶歸屬 | workspace 層級 vs 使用者層級 | workspace 層級（所有成員共享） |
| 7 | Production S3 索引重建時機 | 僅 Pod 啟動 vs 啟動 + 定期校驗 | 僅啟動時（寫入都經過 MemoryService，不會不同步） |