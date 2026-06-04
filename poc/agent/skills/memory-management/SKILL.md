---
name: memory-management
description: >
  管理工作區的長期記憶系統。負責記憶的建立、讀取、更新、刪除。
  當使用者提到「記住」、「記下來」、「記錄」、「remember」、「別忘了」、「上次說過」、
  「之前提到」、「我的偏好」、「筆記」、「備忘」、「幫我記」、「存起來」、「note」時觸發。
  系統會在每次對話前自動搜尋相關記憶並注入上下文。
---

# 記憶管理系統

管理 `memories/` 目錄下的長期記憶，讓跨 session 的知識得以保留與檢索。

## 架構概述

記憶系統由三個層次組成：
1. **自動召回（Active Memory）**：每次對話前，系統自動搜尋相關記憶並注入 `<active_memory>` 區塊
2. **主動搜尋（memory_search）**：research_agent 可用 memory_search 工具搜尋記憶
3. **記憶寫入（memory_save/update/delete）**：code_agent 可用專用工具管理記憶

索引由 SQLite FTS5 自動維護，不需要手動管理 MEMORY_INDEX.md。

## 可用工具

### memory_search（research_agent）
搜尋工作區記憶。支援全文搜尋和類型篩選。

輸入格式：
- `'搜尋關鍵字'` — 全文搜尋
- `'type:搜尋關鍵字'` — 帶類型篩選（user/feedback/project/reference）

範例：
- `'部署架構'` → 搜尋所有包含「部署架構」的記憶
- `'project:截止日期'` → 只搜尋 project 類型的記憶

### memory_save（code_agent）
儲存新記憶。輸入 JSON 格式：

```json
{
  "name": "記憶名稱",
  "type": "user|feedback|project|reference",
  "tags": ["標籤1", "標籤2"],
  "content": "記憶內容",
  "why": "為什麼這件事重要",
  "how": "未來遇到相關情境時如何應用"
}
```

### memory_update（code_agent）
更新既有記憶。輸入 JSON 格式：

```json
{
  "file_path": "記憶檔名.md",
  "content": "新內容（可選）",
  "tags": ["新標籤"]
}
```

先用 memory_search 找到要更新的記憶檔名。

### memory_delete（code_agent）
刪除記憶。輸入記憶檔名（如 `user_preferences.md`）。
先用 memory_search 確認要刪除的記憶。

## 記憶類型定義

| 類型 | 用途 | 範例 |
|------|------|------|
| user | 使用者角色、偏好、知識背景 | 「使用者是後端工程師，熟悉 Python」 |
| feedback | 使用者對 AI 行為的修正或肯定 | 「不要在回覆結尾加摘要」 |
| project | 專案進度、決策、時程 | 「Phase 1 截止日 2026-06-01」 |
| reference | 外部資源位置 | 「API 文件在 Confluence/ENG」 |

## 判斷「記憶操作」vs「一般檔案操作」

使用者說「記錄」、「記住」、「幫我記」時，預設為記憶操作（使用 memory_save）。
只有在使用者明確指定檔案路徑或格式（如「存成 CSV」、「寫到 data/contacts.csv」）時，
才視為一般檔案操作（存到 sessions/ 或 data/）。

範例：
- 「幫我記錄王大明的電話」→ 記憶操作 → memory_save
- 「把這些資料存成 Excel」→ 檔案操作 → code_agent 寫檔
- 「記住我喜歡用繁體中文」→ 記憶操作 → memory_save
- 「把會議紀錄存到 data/」→ 檔案操作 → code_agent 寫檔

## 操作規則

### 自動召回（不需手動操作）
- 系統在每次對話前自動搜尋相關記憶
- 結果以 `<active_memory>` 區塊注入 supervisor 上下文
- 如果沒有相關記憶，不會注入任何內容

### 主動搜尋
- 當使用者問「之前提到的...」、「上次說過...」等
- 分派 research_agent 使用 memory_search 工具
- 搜尋結果包含記憶名稱、類型、摘要、Why/How

### 建立新記憶
觸發條件：
- 使用者明確說「記住」、「記下來」、「remember this」
- 對話中出現重要的非顯而易見資訊（偏好、決策、截止日、外部資源位置）
- 使用者修正 AI 的行為（正面或負面回饋）

步驟：
1. 決定記憶類型和標籤
2. 分派 code_agent 使用 memory_save 工具
3. 回覆使用者確認已記錄

### 更新記憶
觸發條件：新資訊與既有記憶相關但內容有變化

步驟：
1. 分派 research_agent 用 memory_search 找到既有記憶
2. 分派 code_agent 用 memory_update 更新內容
3. 回覆使用者確認已更新

### 刪除記憶
觸發條件：使用者明確說「忘記這個」、「刪除這個記憶」

步驟：
1. 分派 research_agent 用 memory_search 確認目標
2. 分派 code_agent 用 memory_delete 刪除
3. 回覆使用者確認已刪除

## 不該存為記憶的內容

- 程式碼片段（存在 data/ 或 sessions/ 裡）
- 當前對話的暫時性資訊
- 可以從檔案系統直接讀取的內容
- 已經寫在 skills 裡的規則

## 注意事項

- 記憶是跨 session 共享的，所有 session 都能讀寫
- 相對日期要轉成絕對日期再存（「下週五」→「2026-05-16」）
- 記憶工具會自動處理檔案格式、索引更新、MEMORY_INDEX.md 同步
- 不需要手動用 shell 操作 memories/ 目錄
