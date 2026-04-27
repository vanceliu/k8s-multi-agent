---
name: weekly-report
description: >
  彙整本週（週一至今日）工作區的所有更新內容，產生結構化週報。掃描所有 session 的
  檔案變更、memories 更新、共用資料異動，並依類別分類摘要。當使用者提到「週報」、
  「這週做了什麼」、「本週摘要」、「weekly report」、「weekly summary」、
  「本週進度」、「一週回顧」時觸發。
---

# 週報產生

彙整工作區本週（週一至今日）所有活動，產生結構化週報。

## 執行步驟

1. 取得今日日期與本週一日期（用於 `-newermt` 範圍）：
   ```bash
   date +%Y-%m-%d
   date -d "last monday" +%Y-%m-%d 2>/dev/null || date -v-mon +%Y-%m-%d
   ```
2. 用 terminal 掃描 `sessions/` 下所有 session 目錄，找出本週有修改的檔案：
   ```bash
   find ../../sessions/ -type f -newermt "{monday_date}" -ls
   ```
3. 用 terminal 掃描 `../../memories/` 中本週新增或修改的記錄：
   ```bash
   find ../../memories/ -type f -newermt "{monday_date}" -ls
   ```
4. 用 terminal 掃描 `../../data/` 中本週新增或修改的檔案：
   ```bash
   find ../../data/ -type f -newermt "{monday_date}" -ls
   ```
5. 讀取有變更的檔案，理解變更內容
6. 依日期與類別彙整為結構化週報

## 輸出格式

```markdown
# 週報 — {YYYY-MM-DD} ~ {YYYY-MM-DD}

## 本週摘要

{2-3 句話概述本週主要成果}

## 每日活動

### {YYYY-MM-DD}（週一）
- {活動描述}

### {YYYY-MM-DD}（週二）
- {活動描述}

...（依此類推到今日）

## Session 活動總覽
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

- 如果本週沒有任何活動，回報「本週無活動紀錄」
- 週報儲存到當前 session 的工作目錄：`weekly-report-{YYYY-MM-DD}.md`（以今日日期命名）
- 同時將週報副本存到 `../../memories/weekly-report-{YYYY-MM-DD}.md` 供跨 session 查閱
- 只報告事實，不做推測
- 如果某天沒有活動，在「每日活動」中標註「無活動」
- 優先按時間順序呈現，讓讀者能快速掌握本週脈絡
