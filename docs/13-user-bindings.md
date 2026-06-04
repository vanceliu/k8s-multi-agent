# 13 多平台身份綁定（User Bindings）

> **狀態**：POC 已實作 LIFF 綁定路徑（LINE only）；本文件描述的 6 位驗證碼流程為 Production 設計目標，尚未實作。

本文件描述使用者帳號與外部 IM 平台（LINE、Slack、Teams）的身份綁定機制，解決「同一使用者跨平台存取同一 workspace」的問題。

> **與其他文件的關係**：
> - 03-data-model.md：資料模型（新增 `user_bindings` 表）
> - 10-scheduler-service.md：排程通知需要透過綁定表查詢推播目標
> - 02-api-design.md：API 端點設計（新增 `/api/v1/users/bindings/*`）

---

## 1. 問題描述

### 1.1 核心需求

使用者在 Web 端建立帳號、workspace、排程任務後，希望透過 LINE 等 IM 平台：

1. **對話**：在 LINE 上直接跟 Agent 互動，存取同一個 workspace
2. **接收通知**：排程任務執行結果推播到 LINE
3. **跨平台一致性**：Web 和 LINE 看到的是同一份資料

### 1.2 設計原則

| 原則 | 說明 |
|------|------|
| **帳號優先** | 必須先有系統帳號才能綁定 IM 平台，不允許從 IM 平台直接建立新帳號 |
| **一對一** | 一個 user 在同一平台只能綁定一個帳號（如一個 LINE 帳號） |
| **平台唯一** | 一個平台帳號只能綁定到一個系統 user |
| **可恢復** | 使用者封鎖/刪除官方帳號後重新加好友，自動恢復綁定，不需重新驗證 |
| **雙向解綁** | Web 端和 IM 端都能操作解綁，IM 端需二次確認 |

---

## 2. 資料模型

### 2.1 Table: user_bindings

```sql
CREATE TABLE user_bindings (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,              -- 'line' / 'slack' / 'teams'
    platform_uid VARCHAR(255) NOT NULL,         -- LINE userId / Slack member ID / Teams user ID
    display_name VARCHAR(255),                  -- 平台上的顯示名稱
    status VARCHAR(20) NOT NULL DEFAULT 'active',  -- 'active' / 'inactive'
    metadata JSONB,                             -- 平台特定資料（profile pic URL、language 等）
    bound_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (platform, platform_uid),            -- 一個平台帳號只能綁一個 user
    UNIQUE (user_id, platform),                 -- 一個 user 在同一平台只能綁一個帳號
    INDEX idx_binding_user (user_id),
    INDEX idx_binding_platform_uid (platform, platform_uid)
);
```

### 2.2 Table: binding_verifications（驗證碼暫存）

```sql
CREATE TABLE binding_verifications (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,              -- 'line' / 'slack' / 'teams'
    code VARCHAR(10) NOT NULL,                  -- 6 位數驗證碼
    expires_at TIMESTAMP NOT NULL,             -- 有效期限（建立後 5 分鐘）
    used BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_verification_code (platform, code, used, expires_at)
);
```

---

## 3. 狀態機

### 3.1 綁定狀態

```
[無綁定] ──(Web 發起 + LINE 輸入驗證碼)──→ [active]
[active] ──(使用者主動解綁：Web 或 LINE /unbind)──→ [刪除記錄]
[active] ──(LINE unfollow event)──→ [inactive]
[inactive] ──(LINE follow event)──→ [active]（自動恢復）
[inactive] ──(Web 端解綁)──→ [刪除記錄]
```

### 3.2 狀態說明

| 狀態 | 意義 | 推播行為 |
|------|------|---------|
| `active` | 綁定有效，連線正常 | 正常推播 |
| `inactive` | 使用者封鎖/刪除官方帳號 | 不推播，fallback 到 pull 模式 |

---

## 4. 綁定流程

### 4.1 綁定（驗證碼方式）

```
┌─────────┐         ┌─────────┐         ┌──────────┐
│  Web UI │         │  LINE   │         │  System  │
└────┬────┘         └────┬────┘         └────┬─────┘
     │ 1. POST /api/v1/users/bindings/init   │
     │   { "platform": "line" }              │
     │───────────────────────────────────────→│
     │                    │    產生驗證碼 847291│
     │                    │    存入 binding_   │
     │                    │    verifications   │
     │←───────────────────────────────────────│
     │ { "code": "847291",│                   │
     │   "expires_in": 300 }                  │
     │                    │                   │
     │ 2. 使用者在 LINE    │                   │
     │    傳送「847291」   │                   │
     │                    │──────────────────→│
     │                    │                   │ 比對驗證碼
     │                    │                   │ （platform + code + !used + !expired）
     │                    │                   │ 寫入 user_bindings
     │                    │                   │ 標記 verification.used = true
     │                    │  「綁定成功！      │
     │                    │   已連結帳號 XXX」 │
     │                    │←──────────────────│
     │                    │                   │
```

### 4.2 前置條件

- 使用者必須已登入 Web 端（有有效的 session/JWT）
- 使用者必須已加入 LINE 官方帳號為好友
- 該 user 在 LINE 平台尚未有 active 綁定

### 4.3 驗證碼規則

| 項目 | 規則 |
|------|------|
| 格式 | 6 位數字（000000-999999） |
| 有效期 | 5 分鐘 |
| 使用次數 | 一次性（used 後失效） |
| 同時存在 | 同一 user + platform 只能有一個未過期的驗證碼（新建時清除舊的） |
| 錯誤嘗試 | 同一 LINE userId 每 5 分鐘最多嘗試 5 次（防暴力破解） |

---

## 5. 解綁流程

### 5.1 Web 端解綁

```
DELETE /api/v1/users/bindings/{platform}
→ 刪除 user_bindings 記錄
→ 回傳 204 No Content
```

直接操作，不需二次確認（已登入 = 已驗證身份）。

### 5.2 LINE 端解綁（二次確認）

```
使用者：/unbind
Bot：確定要解除 LINE 綁定嗎？
     解綁後將無法在 LINE 上使用工作區及接收排程通知。
     請在 60 秒內回覆「確認解綁」以完成操作。

使用者：確認解綁
Bot：已解除綁定。如需重新綁定，請至 Web 端操作。
```

實作要點：
- 收到 `/unbind` 後，在記憶體中記錄 `{line_uid: unbind_requested_at}`
- 60 秒內收到「確認解綁」→ 執行解綁（刪除 user_bindings 記錄）
- 超過 60 秒或其他訊息 → 取消解綁流程
- 確認文字必須完全匹配「確認解綁」，防止對話中誤觸

---

## 6. 平台事件處理

### 6.1 LINE 事件對應

| LINE Event | 系統行為 |
|------------|---------|
| `follow`（加好友） | 查 user_bindings：有 inactive 記錄 → 恢復為 active；無記錄 → 歡迎訊息 + 提示綁定 |
| `unfollow`（封鎖/刪除） | 標記 user_bindings.status = 'inactive' |
| `message`（文字訊息） | 查 binding → 路由到 ChannelManager |

### 6.2 Unfollow 處理

```python
async def _handle_unfollow(self, line_uid: str) -> None:
    """使用者封鎖或刪除官方帳號。"""
    await db.execute(
        update(user_bindings)
        .where(platform == "line", platform_uid == line_uid)
        .values(status="inactive", updated_at=now())
    )
```

### 6.3 Follow 處理（重新加好友）

```python
async def _handle_follow(self, line_uid: str) -> None:
    """使用者重新加好友。"""
    binding = await db.query(user_bindings).filter_by(
        platform="line", platform_uid=line_uid
    ).first()

    if binding and binding.status == "inactive":
        # 曾經綁定過，自動恢復
        binding.status = "active"
        binding.updated_at = now()
        await db.commit()
        await self._push_message(line_uid, "歡迎回來！已恢復通知功能。")
    else:
        # 從未綁定
        await self._push_message(
            line_uid,
            "歡迎使用！請先在 Web 端完成帳號綁定，才能開始使用。"
        )
```

---

## 7. API 設計

### 7.1 使用者端 API（Gateway proxy）

#### POST /api/v1/users/bindings/init

**描述**：發起綁定流程，產生驗證碼

**請求**：
```json
{
  "platform": "line"
}
```

**響應** (200 OK)：
```json
{
  "platform": "line",
  "code": "847291",
  "expires_in": 300,
  "instruction": "請在 LINE 官方帳號中輸入此驗證碼完成綁定"
}
```

**錯誤**：
- 409 Conflict：該 user 已有此平台的 active 綁定
- 400 Bad Request：不支援的 platform

---

#### GET /api/v1/users/bindings

**描述**：列出當前使用者的所有綁定

**響應** (200 OK)：
```json
{
  "bindings": [
    {
      "platform": "line",
      "display_name": "Vance Liu",
      "status": "active",
      "bound_at": "2026-05-20T10:00:00Z"
    }
  ]
}
```

---

#### DELETE /api/v1/users/bindings/{platform}

**描述**：解除指定平台的綁定

**響應**：204 No Content

**錯誤**：
- 404 Not Found：無此平台的綁定記錄

---

### 7.2 內部查詢 API（供 Scheduler / ChannelManager 使用）

#### GET /internal/bindings/resolve?platform={platform}&platform_uid={uid}

**描述**：根據平台 UID 查詢系統 user_id（LINE webhook 收到訊息時使用）

**響應** (200 OK)：
```json
{
  "user_id": "testuser1",
  "platform": "line",
  "platform_uid": "U1234abcd",
  "status": "active"
}
```

**錯誤**：
- 404 Not Found：無綁定記錄

---

#### GET /internal/bindings/user/{user_id}?platform={platform}

**描述**：根據 user_id 查詢綁定的平台帳號（Scheduler 推播時使用）

**響應** (200 OK)：
```json
{
  "user_id": "testuser1",
  "platform": "line",
  "platform_uid": "U1234abcd",
  "status": "active"
}
```

---

## 8. 與 Scheduler Service 的整合

### 8.1 推播流程

docs/10 §6 定義了 `deliver_to` 結構，`to: "self"` 語意為「透過 user_bindings 動態查詢目標」：

```
排程任務執行完畢
  → Worker 解析 deliver_to.targets，遇到 {"channel": "line", "to": "self"}
  → 呼叫 resolve_self(user_id, platform="line")
    → 查詢 GET /internal/bindings/user/{user_id}?platform=line
    → 取得 platform_uid + 確認 status = "active"
  → MessageBus.publish(channel="line", OutboundMessage(
      chat_id=platform_uid,
      user_id=user_id,
      text=result_text,
      format=target.format or "text",
      metadata={"source": "scheduler", "task_id": ...}
    ))
  → ChannelManager 路由到 LINEChannel
  → LINEChannel.send() → LINE Push API → 使用者手機收到通知
  → delivery_log 記錄 {"channel": "line", "to": platform_uid, "status": "sent", "at": "..."}
```

### 8.2 Fallback 機制

對應 docs/10 §6.2 的 `on_failure` 兜底語意：

```
resolve_self(user_id, platform) 查詢 binding：
  → status = "active" → 正常推播
  → status = "inactive" → 該 target 失敗，delivery_log 記錄 failed
  → 無記錄 → 該 target 失敗，delivery_log 記錄 failed
  → 推播失敗（LINE API error） → 該 target 失敗，delivery_log 記錄 failed

所有 targets 都失敗時：
  → 觸發 on_failure（預設 "pull"）
  → 結果寫入 scheduled_task_results（notification_status = 'failed'）
  → 使用者上線時看到未讀通知
```

### 8.3 deliver_to 設計（取代舊的 notification_channel/notification_config）

docs/10 §3.2 使用 `deliver_to` JSONB 欄位取代舊的 `notification_channel` + `notification_config`。`to: "self"` 語意讓排程定義**不需要**存具體的 `platform_uid`，而是執行時透過 `user_bindings` 表動態解析。這樣使用者更換 LINE 帳號（解綁 + 重綁）後，排程通知自動跟著走，不需要逐一更新每個排程任務。

```json
{
  "deliver_to": {
    "targets": [
      {"channel": "line", "to": "self", "format": "text"}
    ],
    "on_failure": "pull"
  }
}
```

> **注意**：`to: "self"` 是語法糖，實際投遞時由 Worker 呼叫 `resolve_self()` 展開為具體的 `platform_uid`。若 binding 不存在或為 inactive，該 target 視為失敗，走 `on_failure`。

---

## 9. IM 訊息身份解析

### 9.1 LINEChannel 收到訊息時的處理流程

```
LINE Webhook 收到 message event
  │
  ├─ 查 user_bindings (platform='line', platform_uid=LINE_userId)
  │
  ├─ [有記錄 + active]
  │   → 用 binding.user_id 作為 InboundMessage.user_id
  │   → 正常走 ChannelManager 流程
  │
  ├─ [有記錄 + inactive]
  │   → 不應發生（inactive 代表 unfollow，不會收到訊息）
  │   → 防禦性處理：恢復為 active，正常路由
  │
  ├─ [無記錄 + 訊息為 6 位數字]
  │   → 嘗試綁定流程（比對 binding_verifications）
  │   → 成功 → 建立 user_bindings，回覆「綁定成功」
  │   → 失敗 → 回覆「驗證碼錯誤或已過期」
  │
  └─ [無記錄 + 一般訊息]
      → 回覆「請先在 Web 端完成帳號綁定」
```

### 9.2 與 ChannelStore 的關係

| 元件 | 職責 | 資料 |
|------|------|------|
| `user_bindings`（DB） | 平台帳號 ↔ 系統帳號的身份映射 | user_id, platform, platform_uid |
| `ChannelStore`（JSON/DB） | 對話 ↔ workspace/session 的路由映射 | channel:chat_id → workspace_id, session_id |

兩者互補：
1. 先查 `user_bindings` 確認身份（LINE userId → system user_id）
2. 再查 `ChannelStore` 確認路由（chat_id → workspace/session）
3. 若 ChannelStore 無記錄 → 走 ensure workspace 建立新 session

---

## 10. 安全考量

### 10.1 綁定安全

| 威脅 | 防護 |
|------|------|
| 暴力破解驗證碼 | 同一 LINE userId 每 5 分鐘最多嘗試 5 次 |
| 驗證碼竊取 | 5 分鐘有效期 + 一次性使用 |
| 重放攻擊 | 驗證碼 used 後立即失效 |
| Webhook 偽造 | LINE signature 驗證（HMAC-SHA256） |

### 10.2 解綁安全

| 威脅 | 防護 |
|------|------|
| 他人拿到手機解綁 | LINE 端需二次確認（60 秒內回覆特定文字） |
| Web 端未授權解綁 | 需有效 JWT/session |

### 10.3 推播安全

| 威脅 | 防護 |
|------|------|
| 推播到錯誤對象 | 透過 user_bindings 表查詢，不依賴外部輸入 |
| 敏感資訊外洩 | 排程結果推播前檢查敏感資訊 pattern（同 10-scheduler-service.md §12.8） |

---

## 11. LINE 平台特定設定

### 11.1 LINE Developers Console 設定

| 項目 | 值 |
|------|-----|
| Channel type | Messaging API |
| Webhook URL | `https://api.yourdomain.com/api/v1/channels/line/webhook` |
| Use webhooks | Enabled |
| Auto-reply messages | Disabled（由系統回覆） |
| Greeting messages | Disabled（由 follow event handler 處理） |

### 11.2 環境變數

| 變數 | 說明 |
|------|------|
| `LINE_CHANNEL_SECRET` | Channel secret（驗證 webhook 簽章） |
| `LINE_CHANNEL_ACCESS_TOKEN` | Long-lived channel access token（推播用） |

### 11.3 LINE Push API 限制

| 項目 | 限制 |
|------|------|
| 免費訊息數 | 每月 500 則（2024 年方案） |
| 付費方案 | Light: 5,000 則/月、Standard: 30,000 則/月 |
| Push API rate limit | 100,000 requests/min |
| 訊息長度 | 單則文字最長 5,000 字元 |

排程通知需注意免費額度。Production 建議：
- 合併同一使用者同一時段的多個排程結果為一則訊息
- 提供使用者設定「靜音時段」避免深夜推播
- 監控每月推播量，接近上限時告警

---

## 12. POC vs Production 差異

| 項目 | POC | Production |
|------|-----|------------|
| 驗證碼暫存 | `binding_verifications` 表 | Redis（TTL 自動過期） |
| Rate limit | 應用層計數 | Redis sliding window |
| LINE token | 環境變數 | AWS Secrets Manager |
| Webhook 驗證 | HMAC-SHA256 | 同左 |
| 綁定表 | PostgreSQL | 同左 |
| 多平台 | LINE only | + Slack + Teams |
| 推播監控 | Log only | Prometheus metrics（推播成功率、延遲） |

---

## 13. 未來擴展

### 13.1 Slack 綁定

Slack 使用 OAuth 2.0 flow，綁定流程不同：

```
Web 端點「綁定 Slack」→ 導向 Slack OAuth 授權頁
→ 使用者授權 → callback 取得 Slack user ID + access token
→ 寫入 user_bindings (platform='slack', platform_uid=slack_member_id)
```

不需要驗證碼，因為 OAuth flow 本身就是身份驗證。

### 13.2 Teams 綁定

類似 Slack，使用 Microsoft OAuth 2.0 + Bot Framework。

### 13.3 多帳號支援（未來考慮）

目前設計為一個 user 在同一平台只能綁一個帳號。若未來需要支援多帳號（如工作 LINE + 私人 LINE），移除 `UNIQUE(user_id, platform)` constraint，改為應用層控制「預設通知帳號」。
