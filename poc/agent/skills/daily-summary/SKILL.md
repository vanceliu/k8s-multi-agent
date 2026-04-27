---
name: daily-summary
description: >
  產生工作區的每日活動摘要報告。掃描所有 session 的檔案變更、memories 更新，
  彙整為結構化報告。當使用者提到「今天做了什麼」、「每日報告」、「daily summary」、
  「進度摘要」、「活動紀錄」、「what did I do today」時觸發。
---

# 每日摘要

產生工作區當日活動的結構化摘要。

## 執行步驟

1. 取得今日日期
2. 用 terminal 掃描 `sessions/` 下所有 session 目錄，找出今日有修改的檔案：
   ```bash
   find ../../sessions/ -type f -newermt "today" -ls
   ```
3. 用 terminal 掃描 `../../memories/` 中今日新增或修改的記錄：
   ```bash
   find ../../memories/ -type f -newermt "today" -ls
   ```
4. 用 terminal 掃描 `../../data/` 中今日新增或修改的檔案：
   ```bash
   find ../../data/ -type f -newermt "today" -ls
   ```
5. 讀取有變更的檔案，理解變更內容
6. 彙整為結構化摘要

## 輸出格式

```markdown
# 每日摘要 — {YYYY-MM-DD}

## 活動 Session
| Session ID | 修改檔案數 | 主要活動 |
|------------|-----------|---------|
| {sid}      | {count}   | {描述}   |

## 新增/修改檔案
- `{相對路徑}` — {變更描述}

## 記憶更新
- {記憶檔名} — {摘要}

## 共用資料變更
- `data/{路徑}` — {變更描述}
```

## 注意事項

- 如果今日沒有任何活動，回報「今日無活動紀錄」
- 摘要儲存到當前 session 的工作目錄：`daily-summary-{YYYY-MM-DD}.md`
- 同時將摘要副本存到 `../../memories/daily-summary-{YYYY-MM-DD}.md` 供跨 session 查閱
- 只報告事實，不做推測
