# 12 對話壓縮（Context Compaction）

> **狀態**：POC 已實作（包含 memory flush + summarize + 3 個 SSE 進度事件）。

本文件描述 Agent 對話歷史的自動壓縮機制，解決長對話超出 LLM context window 上限的問題。壓縮前先觸發記憶沖刷（Memory Flush），確保重要資訊不因壓縮而遺失。

> **與其他文件的關係**：
> - 06-agent-container.md：Agent 容器架構、LangGraph runtime
> - 11-memory-system.md：Memory System，壓縮前的記憶沖刷目標
> - 10-scheduler-service.md：未來 Heartbeat 機制可觸發定期壓縮檢查

---

## 1. 問題描述

### 1.1 現狀

- Agent 使用 LangGraph checkpointer（AsyncPostgresSaver）保存完整對話歷史
- `output_mode="full_history"` 導致 sub-agent 的 tool_call/tool_result 全部累積在 checkpoint state
- **沒有任何 token 計數或 context window 管理機制**
- 長對話最終會超出 model context window（GLM: 128K, Claude: 200K, DeepSeek: 64K），導致 API 錯誤或截斷

### 1.2 目標

- 在對話接近 context window 上限前自動壓縮歷史
- 壓縮前執行 Silent Memory Flush，將重要資訊存入 Memory System
- 壓縮後保留近期訊息完整性，舊訊息以摘要形式保留
- 對使用者透明（不中斷對話流程）

---

## 2. 架構設計

### 2.1 整體流程

```
使用者訊息進入
       │
       ▼
┌─────────────────────────────┐
│  Token Counter               │
│  計算當前 checkpoint 總 token │
│  （messages + system prompt） │
└─────────────────────────────┘
       │
       ▼ token_count > threshold?
       │
  No ──┤──→ 正常執行 ainvoke
       │
  Yes ─┤
       ▼
┌─────────────────────────────┐
│  Phase 1: Silent Memory Flush│
│                              │
│  LLM 掃描即將被壓縮的訊息    │
│  提取重要資訊 → MemoryService │
│  （facts, decisions, todos） │
└─────────────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│  Phase 2: Summarize          │
│                              │
│  將舊訊息壓縮為摘要           │
│  保留最近 N 輪完整對話        │
└─────────────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│  Phase 3: Checkpoint Update  │
│                              │
│  用壓縮後的 messages 替換     │
│  checkpoint state            │
└─────────────────────────────┘
       │
       ▼
  正常執行 ainvoke（壓縮後的 context）
```

### 2.2 觸發條件

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `compaction_threshold_ratio` | `0.75` | 當 token 數達到 model context window 的 75% 時觸發 |
| `compaction_target_ratio` | `0.40` | 壓縮後目標 token 數為 context window 的 40% |
| `preserve_recent_turns` | `6` | 保留最近 6 輪對話（user + assistant = 1 輪）完整不壓縮 |
| `min_messages_to_compact` | `20` | 至少累積 20 條訊息才觸發壓縮（避免短對話頻繁壓縮） |

### 2.3 Model Context Window 對照

| Model | Context Window | Trigger (75%) | Target (40%) |
|-------|---------------|---------------|--------------|
| glm-5.1 | 128,000 | 96,000 | 51,200 |
| glm-5-turbo | 128,000 | 96,000 | 51,200 |
| deepseek-chat | 64,000 | 48,000 | 25,600 |
| claude-sonnet-4-20250514 | 200,000 | 150,000 | 80,000 |
| minimax-01 | 1,000,000 | 750,000 | 400,000 |

---

## 3. 詳細設計

### 3.1 Token 計數策略

由於支援多種 model provider，token 計數採用估算方式：

```python
class TokenCounter:
    """估算 token 數量，不依賴特定 tokenizer"""

    CHARS_PER_TOKEN = 3.5  # 中英混合平均值

    @classmethod
    def estimate(cls, messages: list[BaseMessage]) -> int:
        total_chars = sum(
            len(msg.content) if isinstance(msg.content, str)
            else sum(len(block.get("text", "")) for block in msg.content if isinstance(block, dict))
            for msg in messages
        )
        return int(total_chars / cls.CHARS_PER_TOKEN)

    @classmethod
    def get_context_window(cls, model_name: str) -> int:
        """根據 model name 回傳 context window 大小"""
        windows = {
            "glm-5.1": 128_000,
            "glm-5-turbo": 128_000,
            "deepseek-chat": 64_000,
            "deepseek-reasoner": 64_000,
            "claude-sonnet-4-20250514": 200_000,
            "minimax-01": 1_000_000,
        }
        return windows.get(model_name, 128_000)  # 預設 128K
```

**為什麼用估算而非精確 tokenizer：**
- 多 provider 各有不同 tokenizer（tiktoken / SentencePiece / 自訂）
- 估算誤差 ±15% 在 threshold 設計中已有足夠 buffer（75% trigger → 實際可能在 65%~85% 觸發）
- 避免引入額外依賴

### 3.2 Silent Memory Flush

壓縮前，用 LLM 掃描即將被壓縮的訊息，提取值得長期保留的資訊：

```python
MEMORY_FLUSH_PROMPT = """你是一個記憶提取助手。以下是一段即將被壓縮的對話歷史。
請從中提取值得長期記住的重要資訊，分類如下：

1. **事實** (facts)：使用者提到的個人資訊、偏好、專案背景
2. **決策** (decisions)：討論後做出的技術或業務決策
3. **待辦** (todos)：提到但尚未完成的事項
4. **教訓** (lessons)：從錯誤或討論中學到的經驗

對話歷史：
{messages_to_compact}

請以 JSON 格式回傳，每個項目包含 category 和 content：
[{"category": "fact", "content": "..."}, ...]

如果沒有值得記住的資訊，回傳空陣列 []。"""
```

提取結果透過 `MemoryService.store()` 存入記憶系統，category 對應 memory type：
- facts → `core` memory
- decisions → `core` memory
- todos → `core` memory（標記 `[TODO]` prefix）
- lessons → `core` memory

### 3.3 對話摘要生成

```python
SUMMARIZE_PROMPT = """你是一個對話摘要助手。請將以下對話歷史壓縮為簡潔的摘要。

要求：
1. 保留關鍵上下文（討論了什麼主題、做了什麼決定、目前進度）
2. 保留重要的技術細節（檔案路徑、指令、錯誤訊息）
3. 用條列式整理，每點一句話
4. 摘要長度控制在原文的 10%~20%

對話歷史：
{messages_to_compact}

請直接輸出摘要，不需要額外格式。"""
```

### 3.4 壓縮後的 Message 結構

壓縮後的 checkpoint state 結構：

```
messages = [
    SystemMessage("[對話摘要]\n{summary}\n\n[摘要涵蓋時間: {start_time} ~ {end_time}，共 {n} 輪對話]"),
    # --- 以下為保留的近期完整對話 ---
    HumanMessage("..."),      # turn -6
    AIMessage("..."),         # turn -6
    HumanMessage("..."),      # turn -5
    AIMessage("..."),         # turn -5
    ...
    HumanMessage("最新訊息"),  # turn -1 (current)
]
```

### 3.5 Checkpoint 更新策略

LangGraph 的 checkpointer 是 append-only 設計，不支援直接修改歷史 state。壓縮策略：

**方案：State Modifier（推薦）**

利用 LangGraph 的 `state_modifier` 在每次 invoke 前動態裁剪傳入 LLM 的 messages，但 checkpoint 中仍保留完整歷史：

```python
def compaction_state_modifier(state: dict) -> dict:
    """在 messages 送入 LLM 前，檢查並壓縮"""
    messages = state["messages"]
    token_count = TokenCounter.estimate(messages)
    context_window = TokenCounter.get_context_window(model_name)

    if token_count > context_window * threshold_ratio:
        # 執行壓縮
        compacted = await compact_messages(messages)
        return {"messages": compacted}
    return state
```

**優點：**
- 不修改 checkpoint 原始資料（完整歷史仍可查詢）
- 符合 LangGraph 設計哲學
- 可隨時調整壓縮策略而不影響已存資料

**缺點：**
- Checkpoint 持續增長（需要定期清理策略，但這是 DB 層面的問題）

### 3.6 與現有架構的整合點

```
poc/agent/
  runtime.py          ← 修改：invoke 前加入 compaction check
  compaction/         ← 新增：壓縮模組
    __init__.py
    counter.py        ← TokenCounter
    compactor.py      ← ContextCompactor（flush + summarize + rebuild）
    prompts.py        ← Memory flush + summarize prompts
  config.py           ← 修改：新增 compaction 相關 env vars
```

整合到 `runtime.py` 的 `invoke()` 流程：

```python
async def invoke(self, message: str, session_id: str) -> str:
    agent = await self._get_or_create_agent(session_id)
    memory_context = await self._recall_memory(message)

    # --- 新增：壓縮檢查 ---
    checkpoint_messages = await self._get_checkpoint_messages(session_id)
    if self.compactor.should_compact(checkpoint_messages):
        await self.compactor.compact(session_id, checkpoint_messages)
    # --- 結束 ---

    messages = self._build_messages(memory_context, message)
    result = await agent.ainvoke(...)
    return self._extract_response(result)
```

---

## 4. 配置

### 4.1 環境變數

| 環境變數 | 預設值 | 說明 |
|----------|--------|------|
| `COMPACTION_ENABLED` | `true` | 是否啟用自動壓縮 |
| `COMPACTION_THRESHOLD_RATIO` | `0.75` | 觸發壓縮的 token 比例 |
| `COMPACTION_TARGET_RATIO` | `0.40` | 壓縮後目標 token 比例 |
| `COMPACTION_PRESERVE_TURNS` | `6` | 保留最近幾輪完整對話 |
| `COMPACTION_MIN_MESSAGES` | `20` | 最少訊息數才觸發 |
| `COMPACTION_MEMORY_FLUSH` | `true` | 壓縮前是否執行記憶沖刷 |
| `COMPACTION_MODEL` | （同 supervisor model） | 用於摘要/提取的 model |

### 4.2 config.yaml 擴展

```yaml
agent:
  compaction:
    enabled: true
    threshold_ratio: 0.75
    target_ratio: 0.40
    preserve_recent_turns: 6
    min_messages: 20
    memory_flush: true
    model: null  # null = 使用 supervisor model
```

---

## 5. SSE 事件

壓縮過程中透過 SSE 通知前端：

```
event: compaction_start
data: {"session_id": "sess-001", "message_count": 45, "token_estimate": 98000}

event: compaction_memory_flush
data: {"session_id": "sess-001", "extracted_count": 3}

event: compaction_complete
data: {"session_id": "sess-001", "before_tokens": 98000, "after_tokens": 42000, "summary_length": 850}
```

前端可選擇顯示壓縮通知（如「對話歷史已自動整理」）或靜默處理。

---

## 6. 邊界情況

### 6.1 壓縮期間的並發請求

同一 session 的壓縮操作需要加鎖：

```python
self._compaction_locks: dict[str, asyncio.Lock] = {}

async def compact(self, session_id: str, ...):
    lock = self._compaction_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        # 執行壓縮
```

### 6.2 壓縮失敗

- Memory flush 失敗：跳過 flush，繼續壓縮（記錄 warning log）
- Summarize 失敗：不壓縮，記錄 error log，下次 invoke 重試
- 壓縮後 token 仍超標：降低 `preserve_recent_turns` 到 2，再次壓縮

### 6.3 Sub-Agent Tool Messages

Supervisor 的 `output_mode="full_history"` 會保留大量 tool_call/tool_result 訊息。壓縮時：
- Tool messages 優先被壓縮（資訊密度低、佔空間大）
- 保留 tool_call 的 name + 簡短 result，丟棄完整 output

### 6.4 多次壓縮

對話極長時可能觸發多次壓縮。每次壓縮的摘要會被下一次壓縮再次摘要，形成遞進式壓縮：

```
[第 1 次壓縮摘要] + [第 1~2 次之間的完整對話] → [第 2 次壓縮摘要] + [近期完整對話]
```

---

## 7. 效能考量

| 操作 | 預估耗時 | 說明 |
|------|----------|------|
| Token 計數 | < 5ms | 純字元計算，無 I/O |
| Memory Flush（LLM call） | 2~5s | 一次 LLM 呼叫，輸入為待壓縮訊息 |
| Summarize（LLM call） | 3~8s | 一次 LLM 呼叫，輸入為待壓縮訊息 |
| Checkpoint 讀取 | < 100ms | PostgreSQL 查詢 |
| 總壓縮時間 | 5~13s | 使用者會感受到延遲 |

**最佳化策略：**
- Memory Flush 和 Summarize 可並行執行（兩個獨立 LLM call）
- 使用較快的 sub-agent model 執行壓縮（如 glm-5-turbo）
- 壓縮期間前端顯示 loading 狀態（透過 SSE event）

---

## 8. 與 Memory System 的整合

壓縮觸發的 Memory Flush 與現有 `MemoryService` 整合：

```python
async def memory_flush(self, messages_to_compact: list[BaseMessage]) -> int:
    """提取重要資訊並存入記憶系統"""
    extracted = await self._extract_memories(messages_to_compact)

    stored_count = 0
    for item in extracted:
        await self.memory_service.store(
            content=item["content"],
            memory_type="core",
            metadata={"source": "compaction_flush", "category": item["category"]},
        )
        stored_count += 1

    return stored_count
```

---

## 9. 未來擴展

### 9.1 Embedding-based 重要性評分

結合向量搜尋，在壓縮時根據語意相關性決定哪些訊息值得保留完整內容。

### 9.2 Selective Compaction

不是一刀切壓縮所有舊訊息，而是根據訊息類型選擇性壓縮：
- 純聊天 → 高度壓縮
- 程式碼討論 → 保留 code block
- 決策討論 → 保留結論

### 9.3 Heartbeat 整合

搭配 Heartbeat 機制，在 Agent 閒置時主動執行壓縮（而非等到下次使用者訊息時才觸發），減少使用者感知延遲。

---

## 10. 實作計畫

| 步驟 | 內容 | 預估工時 |
|------|------|----------|
| 1 | `TokenCounter` + context window 對照表 | 0.5h |
| 2 | `ContextCompactor` 核心邏輯（flush + summarize + rebuild） | 2h |
| 3 | 整合到 `runtime.py` invoke 流程 | 1h |
| 4 | SSE 事件支援 | 0.5h |
| 5 | 配置擴展（env vars + config.yaml） | 0.5h |
| 6 | 單元測試 | 1h |
| 7 | 整合測試（長對話模擬） | 1h |
| **合計** | | **6.5h** |