# 前端串接 API 文件

## 基本資訊

| 項目 | 值 |
|------|-----|
| Base URL | `http://localhost:8000` (POC) / `https://api.yourdomain.com` (Production) |
| 認證方式 | Bearer Token（POC: `$POC_STATIC_TOKEN:{user_id}`） |
| Content-Type | `application/json` |
| Swagger UI | `http://localhost:8000/docs` |

## 認證

所有 API 都需要在 Header 帶上 Bearer Token：

```
Authorization: Bearer $POC_STATIC_TOKEN:testuser1
```

Production 環境將改為 JWT / OAuth2。

---

## API 總覽

| #  | Method | Endpoint | 說明 |
|----|--------|----------|------|
| 1  | GET | `/health` | Gateway 健康檢查 |
| 2  | POST | `/api/v1/workspaces/ensure` | 建立或恢復工作區（登入後第一步） |
| 3  | GET | `/api/v1/workspaces/{workspace_id}` | 查詢工作區狀態 |
| 4  | POST | `/api/v1/chat` | **推薦** 對話（經 Channel 抽象層，自動 ensure workspace） |
| 5  | GET | `/api/v1/sessions` | 列出使用者的所有 session |
| 6  | GET | `/api/v1/sessions/{session_id}/messages` | 查詢 session 對話歷史（從 LangGraph checkpointer） |
| 7  | GET | `/api/v1/sessions/{session_id}/history` | 查詢 session 活動紀錄（API 操作 log） |
| 8  | DELETE | `/api/v1/sessions/{session_id}` | 刪除 session（含對話歷史、活動紀錄） |
| 9  | POST | `/workspaces/{workspace_id}/api/v1/chat` | 對話（同步，直接 proxy） |
| 10 | POST | `/workspaces/{workspace_id}/api/v1/chat/stream` | 對話（串流 SSE，直接 proxy） |
| 11 | POST | `/workspaces/{workspace_id}/mcp/execute` | MCP 檔案操作 |
| 12 | POST | `/workspaces/{workspace_id}/api/v1/files/upload` | 上傳檔案至工作區 |
| 13 | GET | `/workspaces/{workspace_id}/api/v1/files/download` | 從工作區下載檔案 |
| 14 | GET | `/workspaces/{workspace_id}/api/v1/files/list` | 列出工作區目錄 |
| 15 | DELETE | `/workspaces/{workspace_id}/api/v1/files/delete` | 刪除工作區檔案或目錄 |
| 16 | POST | `/workspaces/{workspace_id}/api/v1/files/mkdir` | 新增工作區目錄 |

### Admin Service API（透過 Gateway proxy 存取）

> Admin Service 是獨立 Pod（:8090, ClusterIP），前端透過 Gateway proxy（`/api/v1/admin/*`）統一存取。
> 認證方式：`Authorization: Bearer $POC_ADMIN_TOKEN`

| #  | Method | Endpoint | 說明 |
|----|--------|----------|------|
| 17 | GET | `/api/v1/admin/users` | 列出所有使用者（分頁） |
| 18 | GET | `/api/v1/admin/users/{user_id}` | 使用者詳情 |
| 19 | PUT | `/api/v1/admin/users/{user_id}/active` | 啟用/停用使用者 |
| 20 | GET | `/api/v1/admin/workspaces` | 列出所有工作區（分頁、篩選） |
| 21 | GET | `/api/v1/admin/workspaces/{workspace_id}` | 工作區詳情 |
| 22 | POST | `/api/v1/admin/reap` | 手動觸發 Pod 回收 |
| 23 | GET | `/api/v1/admin/workspaces/{wid}/members` | 列出工作區成員 |
| 24 | POST | `/api/v1/admin/workspaces/{wid}/members` | 新增工作區成員 |
| 25 | PUT | `/api/v1/admin/workspaces/{wid}/members/{uid}` | 更新成員角色 |
| 26 | DELETE | `/api/v1/admin/workspaces/{wid}/members/{uid}` | 移除工作區成員 |
| 27 | DELETE | `/api/v1/admin/workspaces/{workspace_id}` | 刪除工作區（PVC + DB + K8s） |
| 28 | GET | `/api/v1/admin/pods/status` | Pod 狀態總覽 |

### Storage Service API（透過 Gateway proxy 存取）

> Storage Service 是獨立 Pod（:8091, ClusterIP），前端透過 Gateway proxy（`/api/v1/workspaces/storage/*`、`/api/v1/workspaces/{wid}/storage/*`）存取。
> 支援 user token（操作自己有權限的 workspace）和 admin token（操作所有 workspace）。

| #  | Method | Endpoint | 說明 |
|----|--------|----------|------|
| 29 | POST | `/api/v1/workspaces/storage` | 建立 group workspace + PVC（admin only） |
| 30 | POST | `/api/v1/workspaces/storage/ensure` | 確保 workspace PVC 存在（Orchestrator 呼叫） |
| 31 | PUT | `/api/v1/workspaces/{wid}/rename` | 修改 workspace 顯示名稱 |
| 32 | GET | `/api/v1/workspaces/{wid}/storage/files` | 列出 workspace 檔案 |
| 33 | POST | `/api/v1/workspaces/{wid}/storage/files/upload` | 上傳檔案到 workspace |
| 34 | GET | `/api/v1/workspaces/{wid}/storage/files/download` | 從 workspace 下載檔案 |
| 35 | DELETE | `/api/v1/workspaces/{wid}/storage/files` | 刪除 workspace 檔案或目錄 |
| 36 | POST | `/api/v1/workspaces/{wid}/storage/files/mkdir` | 建立 workspace 目錄 |
| 37 | GET | `/api/v1/workspaces/{wid}/storage/access` | Workspace 存取權限（workspace members） |

---

## 1. 健康檢查

```
GET /health
```

不需要認證。

**Response:**
```json
{
  "status": "healthy",
  "component": "gateway",
  "channels": {
    "channels": { "web": { "running": true } },
    "manager_running": true,
    "store_mappings": 0,
    "inbound_pending": 0
  }
}
```

---

## 2. 建立/恢復工作區

使用者登入後的第一步。系統會自動建立 K8s Pod 和 workspace，或恢復已存在的 workspace。

> 若使用推薦的 `/api/v1/chat` 端點，此步驟可省略（ChannelManager 會自動 ensure）。

```
POST /api/v1/workspaces/ensure
```

**Request:**
```json
{
  "session_id": "sess-001",
  "resource_tier": "standard"
}
```

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| session_id | string | 否 | 前端產生的 session ID，不傳則自動產生 UUID |
| resource_tier | string | 否 | 資源等級：`standard`（預設）/ `premium` / `enterprise` |

**Response:**
```json
{
  "workspace_id": "ws-testuser1",
  "user_id": "testuser1",
  "session_id": "sess-001",
  "gateway_endpoint": "http://localhost:8000",
  "gateway_route": "/workspaces/ws-testuser1",
  "status": "ready"
}
```

| 欄位 | 說明 |
|------|------|
| workspace_id | 工作區 ID，後續 API 都需要用到 |
| gateway_route | 前端呼叫 Agent 的路由前綴 |
| status | `ready` = Pod 已就緒可用，`pending` = Pod 啟動中 |

---

## 3. 查詢工作區狀態

```
GET /api/v1/workspaces/{workspace_id}
```

**Response:**
```json
{
  "workspace_id": "ws-testuser1",
  "user_id": "testuser1",
  "status": "active",
  "sessions": [
    {
      "session_id": "sess-001",
      "pod_status": "running",
      "last_active_at": "2026-04-10T00:40:04.000000"
    }
  ]
}
```

| status 值 | 說明 |
|-----------|------|
| active | Pod 運行中 |
| idle | Pod 已被回收（閒置超時），資料保留在 PVC |

---

## 4. 對話（經 Channel 抽象層）— 推薦

透過 IM Channel 抽象層的對話端點。ChannelManager 會自動 ensure workspace、管理路由、呼叫 Agent Pod。前端不需要先取得 workspace_id，只需提供 session_id 即可。

```
POST /api/v1/chat
```

**Request:**
```json
{
  "message": "請列出工作區的檔案",
  "session_id": "sess-001"
}
```

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| message | string | 是 | 使用者訊息（支援 `/new`、`/status`、`/help` 指令） |
| session_id | string | 是 | 對話 session ID |

**Response:**
```json
{
  "content": "工作區的 data/ 目錄目前有以下檔案：\n- hello.txt (21 bytes)",
  "session_id": "sess-001",
  "workspace_id": "ws-testuser1",
  "tool_calls": []
}
```

| 欄位 | 說明 |
|------|------|
| content | AI 回應文字（Markdown 格式） |
| session_id | 對話 session ID |
| workspace_id | 自動分配的工作區 ID |
| tool_calls | Agent 使用的工具清單 |

**支援的指令：**

| 指令 | 說明 |
|------|------|
| `/new` | 重置對話，建立新 session |
| `/status` | 查看工作區狀態 |
| `/help` | 顯示可用指令 |

**與直接 proxy 端點的差異：**

| 項目 | `/api/v1/chat`（Channel） | `/workspaces/{wid}/api/v1/chat`（Direct Proxy） |
|------|--------------------------|------------------------------------------------|
| 需要先 ensure | 否（自動處理） | 是 |
| 需要 workspace_id | 否 | 是（路徑參數） |
| 支援 IM 指令 | 是（`/new`, `/status`, `/help`） | 否 |
| 未來多平台擴展 | 是（Slack/LINE/Teams 共用邏輯） | 否 |
| 串流支援 | 尚未（規劃中） | 是（`/chat/stream`） |

**錯誤處理：**
- `503` — Channel service 未初始化或 Web channel 不可用
- `408` — 請求超時（預設 120 秒）

---

## 5. 列出使用者 Session

列出目前使用者的所有 session，包含運行中和已終止的。

```
GET /api/v1/sessions
```

**Response:**
```json
{
  "user_id": "testuser1",
  "sessions": [
    {
      "session_id": "sess-001",
      "workspace_id": "ws-testuser1",
      "pod_status": "running",
      "created_at": "2026-04-10T00:40:16.865110",
      "last_active_at": "2026-04-10T02:14:34.709610",
      "terminated_at": null
    },
    {
      "session_id": "sess-002",
      "workspace_id": "ws-testuser1",
      "pod_status": "terminated",
      "created_at": "2026-04-09T10:00:00.000000",
      "last_active_at": "2026-04-09T11:30:00.000000",
      "terminated_at": "2026-04-09T12:00:00.000000"
    }
  ],
  "total": 2
}
```

| 欄位 | 說明 |
|------|------|
| sessions[].session_id | Session ID |
| sessions[].workspace_id | 所屬工作區 |
| sessions[].pod_status | `running` / `terminated` / `pending` |
| sessions[].created_at | 建立時間 |
| sessions[].last_active_at | 最後活動時間 |
| sessions[].terminated_at | 終止時間（null = 仍在運行） |
| total | Session 總數 |

---

## 6. 查詢 Session 對話歷史

取得特定 session 的完整對話訊息，包含使用者訊息、AI 回應、工具呼叫和工具結果。資料來自 LangGraph PostgreSQL checkpointer，由 Orchestrator 直接讀取 DB，不需要 Agent Pod 在線。Pod 回收重建後對話歷史仍然保留。

```
GET /api/v1/sessions/{session_id}/messages
```

**Response:**
```json
{
  "session_id": "sess-001",
  "messages": [
    {
      "role": "human",
      "content": "我叫小明，請記住"
    },
    {
      "role": "ai",
      "content": "",
      "tool_calls": [
        {
          "name": "write_workspace_file",
          "args": { "path": "memories/user_name.txt", "content": "使用者姓名：小明" }
        }
      ]
    },
    {
      "role": "tool",
      "content": "已寫入 memories/user_name.txt（24 字元）",
      "tool_name": "write_workspace_file"
    },
    {
      "role": "ai",
      "content": "好的，小明！我已經記住您的名字了。"
    },
    {
      "role": "tool",
      "content": "降雨分布圖已儲存：taoyuan_rainfall.png",
      "tool_name": "python_repl",
      "files": [
        {
          "path": "sessions/sess-001/taoyuan_rainfall.png",
          "type": "image",
          "name": "taoyuan_rainfall.png"
        }
      ]
    }
  ],
  "total": 5
}
```

| 欄位 | 說明 |
|------|------|
| messages[].role | `human`（使用者）/ `ai`（Agent 回應）/ `tool`（工具執行結果） |
| messages[].content | 訊息內容（AI 的 tool_call 回合 content 可能為空） |
| messages[].tool_calls | AI 呼叫的工具清單（僅 `ai` role） |
| messages[].tool_name | 工具名稱（僅 `tool` role） |
| messages[].files | 工具產生的檔案清單（僅 `tool` role，偵測到檔案時才有） |
| messages[].files[].path | 檔案相對路徑（可直接用於下載 API） |
| messages[].files[].type | `image` 或 `document` |
| messages[].files[].name | 檔案名稱 |
| total | 訊息總數 |

### 從 Session History 下載檔案/圖片

當 messages 回傳中包含 `files` 欄位時，前端需要組合正確的下載 URL 來顯示圖片或提供下載連結。

**下載 URL 格式：**
```
GET /workspaces/{workspace_id}/api/v1/files/download?path={file.path}
```

**重要事項：**
- 必須使用 `files[].path`（完整相對路徑，如 `sessions/sess-001/chart.png`），**不是** `files[].name`（僅檔名）
- 必須帶上 `workspace_id`（從 session 列表 API 或 ensure 回傳取得）
- 需要 Agent Pod 在線才能下載（Pod 離線時改用 Storage Service 端點）

**兩種下載端點：**

| 端點 | 條件 | 說明 |
|------|------|------|
| `GET /workspaces/{wid}/api/v1/files/download?path=...` | Pod 在線 | 直接 proxy 到 Agent Pod |
| `GET /api/v1/workspaces/{wid}/storage/files/download?path=...` | Pod 在線或離線 | 透過 Storage Service（Pod 在線 proxy，離線走 K8s Job） |

> 建議前端統一使用 Storage Service 端點（`/api/v1/workspaces/{wid}/storage/files/download`），因為它在 Pod 離線時仍可存取檔案。

**前端範例（顯示歷史中的圖片）：**
```javascript
// 從 session 列表取得 workspace_id
const sessions = await fetch(`${baseUrl}/api/v1/sessions`, { headers });
const workspaceId = sessions.sessions[0].workspace_id; // e.g. "ws-testuser1"

// 取得對話歷史
const history = await fetch(
  `${baseUrl}/api/v1/sessions/${sessionId}/messages`,
  { headers }
);

// 渲染訊息中的檔案
for (const msg of history.messages) {
  if (msg.files) {
    for (const file of msg.files) {
      // 使用 file.path（完整路徑），不是 file.name（僅檔名）
      const downloadUrl = `${baseUrl}/api/v1/workspaces/${workspaceId}/storage/files/download?path=${encodeURIComponent(file.path)}`;

      if (file.type === 'image') {
        // 顯示圖片
        const img = document.createElement('img');
        img.src = downloadUrl;
        img.alt = file.name;
        chatContainer.appendChild(img);
      } else {
        // 顯示下載連結
        const a = document.createElement('a');
        a.href = downloadUrl;
        a.download = file.name;
        a.textContent = `📎 ${file.name}`;
        chatContainer.appendChild(a);
      }
    }
  }
}
```

**常見錯誤：**

| 問題 | 原因 | 修正 |
|------|------|------|
| URL 出現 `/workspaces//api/v1/files/download` | workspace_id 為空 | 確保從 sessions API 取得 workspace_id |
| 404 File not found | 使用了 `file.name` 而非 `file.path` | 改用 `file.path`（含 `sessions/{sid}/` 前綴） |
| 502 Cannot reach agent pod | Pod 已被回收 | 改用 Storage Service 端點，或先 ensure workspace |

**錯誤處理：**
- `404` — Session 不存在或無對話紀錄

---

## 7. 查詢 Session 活動紀錄

取得特定 session 的詳細資訊和活動歷史紀錄。

```
GET /api/v1/sessions/{session_id}/history?limit=50
```

| 參數 | 類型 | 必填 | 說明 |
|------|------|------|------|
| session_id | string | 是 | 路徑參數，Session ID |
| limit | int | 否 | 回傳的活動紀錄數量上限（預設 50） |

**Response:**
```json
{
  "session_id": "sess-001",
  "user_id": "testuser1",
  "workspace_id": "ws-testuser1",
  "pod_name": "pod-ws-testuser1",
  "pod_status": "running",
  "created_at": "2026-04-10T00:40:16.865110",
  "last_active_at": "2026-04-10T02:14:34.709610",
  "terminated_at": null,
  "activity_logs": [
    {
      "activity_id": "c1942532-3ce9-457a-...",
      "activity_type": "workspace_ensured",
      "action": "create",
      "resource_type": "pod",
      "resource_id": "pod-ws-testuser1",
      "timestamp": "2026-04-10T00:40:16.865110",
      "metadata": null
    }
  ]
}
```

| 欄位 | 說明 |
|------|------|
| pod_name | 對應的 K8s Pod 名稱 |
| pod_status | Pod 狀態 |
| activity_logs | 活動紀錄列表（按時間倒序） |
| activity_logs[].activity_type | 活動類型：`workspace_ensured` / `pod_reaped` 等 |
| activity_logs[].action | 動作：`create` / `reuse` / `agent_idle_self_report` 等 |
| activity_logs[].resource_type | 資源類型：`pod` |
| activity_logs[].resource_id | 資源 ID（Pod 名稱） |

**錯誤處理：**
- `404` — Session 不存在

---

## 8. 刪除 Session

刪除指定 session 及所有相關資料，包含：
- LangGraph checkpoint 對話歷史（checkpoints, checkpoint_blobs, checkpoint_writes）
- 活動紀錄（activity_logs）
- Session 記錄

```
DELETE /api/v1/sessions/{session_id}
```

**Response:**
```json
{
  "session_id": "sess-001",
  "user_id": "testuser1",
  "workspace_id": "ws-testuser1",
  "deleted": true
}
```

**錯誤處理：**
- `404` — Session 不存在

> 此操作不可逆。刪除後該 session 的對話歷史無法恢復。

---

## 9. 對話（同步，直接 Proxy）

直接 proxy 到 Agent Pod 的對話介面。需要先呼叫 ensure 取得 workspace_id。

```
POST /workspaces/{workspace_id}/api/v1/chat
```

**Headers（建議）：**
```
X-Session-Id: sess-001
```

> `X-Session-Id` 用於更新活動時間戳，避免 Pod 被誤判閒置回收。

**Request:**
```json
{
  "message": "請列出工作區的檔案",
  "session_id": "sess-001"
}
```

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| message | string | 是 | 使用者訊息 |
| session_id | string | 是 | 對話 session ID（同一 session 共享對話記憶） |

**Response:**
```json
{
  "content": "工作區的 data/ 目錄目前有以下檔案：\n- hello.txt (21 bytes)",
  "session_id": "sess-001",
  "tool_calls": []
}
```

| 欄位 | 說明 |
|------|------|
| content | AI 回應文字（Markdown 格式） |
| session_id | 對話 session ID |
| tool_calls | Agent 使用的工具清單（通常為空，工具已執行完畢） |

**錯誤處理：**
- `500` — Agent 內部錯誤（如 LLM 連線失敗）
- `502` — 無法連線到 Agent Pod（需先呼叫 ensure）

---

## 10. 對話（串流 SSE，直接 Proxy）

與同步對話相同，但以 Server-Sent Events 串流回傳。適合即時顯示 AI 回應。

```
POST /workspaces/{workspace_id}/api/v1/chat/stream
```

**Request:** 同 `/workspaces/{wid}/api/v1/chat`

**Response:** `text/event-stream`

### SSE Event 類型

| Event | Data 格式 | 說明 |
|-------|----------|------|
| `content` | `{"content": "文字片段"}` | AI 回應文字（token-by-token） |
| `tool_call` | `{"name": "工具名", "args": {...}}` | AI 請求呼叫工具（僅 `full_history` 模式） |
| `tool_result` | `{"tool_name": "工具名", "content": "結果"}` | 工具執行結果（僅 `full_history` 模式） |
| `file` | `{"path": "相對路徑", "type": "image\|document", "name": "檔名"}` | 工具產生的檔案（圖片、文件等） |
| `error` | `{"error": "錯誤訊息"}` | Agent 錯誤（recursion limit、LLM 連線失敗等） |
| *(無 event)* | `[DONE]` | 串流結束 |

> **Display Mode**：後端環境變數 `AGENT_DISPLAY_MODE` 控制 `tool_call` / `tool_result` 事件是否輸出。
> - `normal`（預設）：僅 `content` + `file` + `error`
> - `full_history`：額外輸出 `tool_call` + `tool_result`
>
> 前端應同時處理兩種模式（有 tool 事件時顯示，無時忽略）。此設定同時影響 `/api/v1/sessions/{sid}/messages` 回傳的歷史訊息。

### `event: file` 說明

當工具執行結果中偵測到已知副檔名的檔案時，會自動在 `tool_result` 之後推送 `event: file`。前端可直接用 `path` 組合下載 URL，不需要解析 AI 回覆文字。

支援的副檔名：
- 圖片：`.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp`（`type: "image"`）
- 文件：`.pdf`, `.docx`, `.xlsx`, `.pptx`, `.csv`, `.txt`（`type: "document"`）

下載 URL 組合方式：
```
GET /workspaces/{workspace_id}/api/v1/files/download?path={file.path}
```

### SSE 串流範例

```
event: content
data: {"content": "我來幫你畫一張天氣圖表"}

event: tool_call
data: {"name": "python_repl", "args": {"command": "import matplotlib..."}}

event: tool_result
data: {"tool_name": "python_repl", "content": "圖表已儲存為 weather.png"}

event: file
data: {"path": "sessions/sess-001/weather.png", "type": "image", "name": "weather.png"}

event: content
data: {"content": "圖表已產生完成，請查看上方的天氣趨勢圖。"}

data: [DONE]
```

### `event: error` 範例

當 Agent 處理步驟達上限或發生錯誤時：
```
event: error
data: {"error": "Agent 處理步驟已達上限，已基於目前結果回覆。"}

data: [DONE]
```

### 前端處理範例

```javascript
const response = await fetch(`${baseUrl}/workspaces/${workspaceId}/api/v1/chat/stream`, {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json',
    'X-Session-Id': sessionId,
  },
  body: JSON.stringify({ message, session_id: sessionId }),
});

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
  const { done, value } = await reader.read();
  if (done) break;

  buffer += decoder.decode(value, { stream: true });
  const lines = buffer.split('\n');
  buffer = lines.pop(); // 保留未完成的行

  let currentEvent = 'message'; // SSE 預設 event type
  for (const line of lines) {
    if (line.startsWith('event: ')) {
      currentEvent = line.slice(7).trim();
    } else if (line.startsWith('data: ')) {
      const raw = line.slice(6);
      if (raw === '[DONE]') break;

      const data = JSON.parse(raw);
      switch (currentEvent) {
        case 'content':
          // AI 回應文字，append 到對話 UI
          appendToChat(data.content);
          break;
        case 'tool_call':
          // 顯示工具呼叫狀態（可選）
          showToolStatus(`呼叫 ${data.name}...`);
          break;
        case 'tool_result':
          // 顯示工具結果（可選）
          hideToolStatus();
          break;
        case 'file':
          // 檔案產出 — 直接渲染
          const fileUrl = `${baseUrl}/workspaces/${workspaceId}/api/v1/files/download?path=${encodeURIComponent(data.path)}`;
          if (data.type === 'image') {
            appendImage(fileUrl, data.name);
          } else {
            appendDownloadLink(fileUrl, data.name);
          }
          break;
        case 'error':
          // Agent 錯誤（recursion limit、LLM 失敗等）
          appendError(data.error);
          break;
      }
      currentEvent = 'message'; // reset
    }
  }
}
```

---

## 11. MCP 檔案操作

直接操作工作區檔案，不經過 LLM。適合前端需要直接讀寫檔案的場景。

```
POST /workspaces/{workspace_id}/mcp/execute
```

### 11.1 列出檔案

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "list_files",
  "params": { "path": "data" },
  "id": "1"
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "files": [
      { "name": "hello.txt", "type": "file", "size": 21, "last_modified": 1775726752.62 }
    ],
    "total": 1
  },
  "id": "1"
}
```

### 11.2 讀取檔案

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "read_file",
  "params": { "path": "data/hello.txt" },
  "id": "2"
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "content": "Hello from Deep Agent",
    "size_bytes": 21
  },
  "id": "2"
}
```

### 11.3 寫入檔案

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "write_file",
  "params": {
    "path": "data/notes.txt",
    "content": "這是一段筆記"
  },
  "id": "3"
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "path": "data/notes.txt",
    "bytes_written": 18
  },
  "id": "3"
}
```

### 11.4 執行任務（經 LLM）

**Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "execute_task",
  "params": {
    "task_type": "analyze",
    "parameters": { "target": "data/report.csv" }
  },
  "id": "4"
}
```

**Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "content": "分析結果：..."
  },
  "id": "4"
}
```

---

## 12. 上傳檔案

上傳檔案至工作區 PVC 的指定目錄。使用 `multipart/form-data` 格式。

```
POST /workspaces/{workspace_id}/api/v1/files/upload?path=data/
```

| 參數 | 類型 | 必填 | 說明 |
|------|------|------|------|
| workspace_id | string | 是 | 路徑參數，工作區 ID |
| file | file | 是 | 上傳的檔案（multipart/form-data） |
| path | string | 否 | 目標目錄（相對於 workspace），例如 `data/` 或 `sessions/sess-001/` |

**前端範例：**
```javascript
const formData = new FormData();
formData.append('file', fileInput.files[0]);

const response = await fetch(
  `${baseUrl}/workspaces/${workspaceId}/api/v1/files/upload?path=data/`,
  {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${token}` },
    body: formData,
  }
);
```

**Response:**
```json
{
  "status": "uploaded",
  "path": "data/report.csv",
  "size_bytes": 1024,
  "filename": "report.csv"
}
```

**錯誤處理：**
- `403` — 路徑逃逸（path traversal）或 session 寫入權限不足
- `502` — 無法連線到 Agent Pod
- `503` — Backend 未初始化

---

## 13. 下載檔案

從工作區 PVC 下載指定檔案。

```
GET /workspaces/{workspace_id}/api/v1/files/download?path=sessions/sess-001/report.pdf
```

| 參數 | 類型 | 必填 | 說明 |
|------|------|------|------|
| workspace_id | string | 是 | 路徑參數，工作區 ID |
| path | string | 是 | 檔案路徑（相對於 workspace） |

**前端範例：**
```javascript
const response = await fetch(
  `${baseUrl}/workspaces/${workspaceId}/api/v1/files/download?path=${encodeURIComponent(filePath)}`,
  { headers: { 'Authorization': `Bearer ${token}` } }
);
const blob = await response.blob();
const url = URL.createObjectURL(blob);
// 觸發下載
const a = document.createElement('a');
a.href = url;
a.download = filename;
a.click();
```

**Response：** 檔案二進位內容（`application/octet-stream`）

**錯誤處理：**
- `403` — 路徑逃逸（path traversal）
- `404` — 檔案不存在
- `502` — 無法連線到 Agent Pod

---

## 14. 列出工作區目錄

列出工作區指定目錄下的檔案和子目錄。

```
GET /workspaces/{workspace_id}/api/v1/files/list?path=sessions/sess-001
```

| 參數 | 類型 | 必填 | 說明 |
|------|------|------|------|
| workspace_id | string | 是 | 路徑參數，工作區 ID |
| path | string | 否 | 目錄路徑（相對於 workspace），空字串 = workspace 根目錄 |

**Response:**
```json
{
  "path": "sessions/sess-001",
  "files": [
    { "name": "test.md", "type": "file", "size": 256, "last_modified": 1775726752.62 },
    { "name": "output", "type": "dir" }
  ],
  "total": 2
}
```

| 欄位 | 說明 |
|------|------|
| files[].name | 檔案或目錄名稱 |
| files[].type | `file` 或 `dir` |
| files[].size | 檔案大小（bytes，僅 file） |
| files[].last_modified | 最後修改時間（Unix timestamp，僅 file） |

**錯誤處理：**
- `403` — 路徑逃逸（path traversal）
- `502` — 無法連線到 Agent Pod

---

## 15. 刪除工作區檔案或目錄

刪除工作區 PVC 中的指定檔案或目錄。寫入權限由 Agent 的 LocalBackend 檢查（session-scoped）。刪除目錄時會遞迴刪除其下所有內容。

```
DELETE /workspaces/{workspace_id}/api/v1/files/delete?path=sessions/sess-001/report.pdf
```

| 參數 | 類型 | 必填 | 說明 |
|------|------|------|------|
| workspace_id | string | 是 | 路徑參數，工作區 ID |
| path | string | 是 | 檔案或目錄路徑（相對於 workspace） |

**Response:**
```json
{
  "status": "deleted",
  "path": "sessions/sess-001/report.pdf",
  "type": "file"
}
```

| 欄位 | 說明 |
|------|------|
| type | 被刪除的類型：`file` 或 `dir` |

**前端範例：**
```javascript
const response = await fetch(
  `${baseUrl}/workspaces/${workspaceId}/api/v1/files/delete?path=${encodeURIComponent(filePath)}`,
  {
    method: 'DELETE',
    headers: { 'Authorization': `Bearer ${token}` },
  }
);
const result = await response.json();
// { "status": "deleted", "path": "sessions/sess-001/report.pdf", "type": "file" }
```

**錯誤處理：**
- `403` — 路徑逃逸（path traversal）或無寫入權限
- `404` — 路徑不存在
- `502` — 無法連線到 Agent Pod

---

## 16. 新增工作區目錄

在工作區 PVC 中建立目錄。支援多層建立（parents=True）。

```
POST /workspaces/{workspace_id}/api/v1/files/mkdir?path=sessions/sess-001/output
```

| 參數 | 類型 | 必填 | 說明 |
|------|------|------|------|
| workspace_id | string | 是 | 路徑參數，工作區 ID |
| path | string | 是 | 目錄路徑（相對於 workspace） |

**Response:**
```json
{
  "status": "created",
  "path": "sessions/sess-001/output"
}
```

**前端範例：**
```javascript
const response = await fetch(
  `${baseUrl}/workspaces/${workspaceId}/api/v1/files/mkdir?path=${encodeURIComponent(dirPath)}`,
  {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${token}` },
  }
);
const result = await response.json();
// { "status": "created", "path": "sessions/sess-001/output" }
```

**錯誤處理：**
- `403` — 路徑逃逸（path traversal）或無寫入權限
- `502` — 無法連線到 Agent Pod

---

## 17. 管理員：手動回收

```
POST /api/v1/admin/reap
```

強制回收所有閒置的 workspace Pod。

**Response:**
```json
{
  "total_reaped": 1,
  "workspaces_reaped": [
    {
      "workspace_id": "ws-testuser1",
      "pod_name": "pod-ws-testuser1",
      "reason": "admin_manual_reap"
    }
  ]
}
```

---

## 18. 管理員：查詢 Channel 狀態

```
GET /api/v1/admin/channels
```

**Response:**
```json
{
  "channels": {
    "web": { "running": true }
  },
  "manager_running": true,
  "store_mappings": 3,
  "inbound_pending": 0
}
```

| 欄位 | 說明 |
|------|------|
| channels | 各 IM channel 的運行狀態 |
| manager_running | ChannelManager dispatcher 是否運行中 |
| store_mappings | 目前 chat→workspace 映射數量 |
| inbound_pending | MessageBus 中待處理的 inbound 訊息數 |

---

## Admin Service API（透過 Gateway proxy）

Admin Service 是獨立的 FastAPI Pod（:8090, ClusterIP），前端透過 Gateway 的 `/api/v1/admin/*` proxy 統一存取，不需要直接連線 Admin Service。Gateway 驗證 admin token 後轉發請求。PVC 管理已遷移至獨立 Storage Service（:8091），見下方 Storage Service API 章節。

### 存取方式

| 項目 | 值 |
|------|-----|
| Base URL | `http://localhost:8000`（與 Gateway 相同） |
| 路由前綴 | `/api/v1/admin/*` |
| 認證方式 | Bearer Token（POC: `$POC_ADMIN_TOKEN`） |
| Swagger UI（Admin 直連） | `kubectl port-forward svc/admin -n agent-platform 8090:80` → `http://localhost:8090/docs` |

```
Authorization: Bearer $POC_ADMIN_TOKEN
```

---

### 17. 列出所有使用者

```
GET /api/v1/admin/users?limit=50&offset=0&is_active=true
```

| 參數 | 類型 | 必填 | 說明 |
|------|------|------|------|
| limit | int | 否 | 每頁數量（預設 50，最大 200） |
| offset | int | 否 | 偏移量（預設 0） |
| is_active | bool | 否 | 篩選啟用/停用狀態 |

**Response:**
```json
{
  "total": 10,
  "users": [
    {
      "user_id": "testuser1",
      "username": "testuser1",
      "email": "testuser1@poc.local",
      "auth_provider": "static_token",
      "is_active": true,
      "created_at": "2026-04-16T10:00:00.000000",
      "updated_at": "2026-04-16T10:00:00.000000"
    }
  ],
  "limit": 50,
  "offset": 0
}
```

---

### 18. 使用者詳情

```
GET /api/v1/admin/users/{user_id}
```

**Response:**
```json
{
  "user_id": "testuser1",
  "username": "testuser1",
  "email": "testuser1@poc.local",
  "auth_provider": "static_token",
  "is_active": true,
  "created_at": "2026-04-16T10:00:00.000000",
  "updated_at": "2026-04-16T10:00:00.000000",
  "workspace": {
    "workspace_id": "ws-testuser1",
    "status": "active"
  },
  "total_sessions": 3
}
```

---

### 19. 啟用/停用使用者

```
PUT /api/v1/admin/users/{user_id}/active
```

**Request:**
```json
{
  "is_active": false
}
```

**Response:**
```json
{
  "user_id": "testuser1",
  "is_active": false
}
```

---

### 20. 列出所有工作區

```
GET /api/v1/admin/workspaces?limit=50&offset=0&status=active
```

| 參數 | 類型 | 必填 | 說明 |
|------|------|------|------|
| limit | int | 否 | 每頁數量（預設 50，最大 200） |
| offset | int | 否 | 偏移量（預設 0） |
| status | string | 否 | 篩選狀態：`active` / `idle` |

**Response:**
```json
{
  "total": 5,
  "workspaces": [
    {
      "workspace_id": "ws-testuser1",
      "user_id": "testuser1",
      "resource_tier": "standard",
      "status": "active",
      "active_sessions": 2,
      "created_at": "2026-04-16T10:00:00.000000",
      "updated_at": "2026-04-16T12:00:00.000000",
      "last_reap_at": null
    }
  ],
  "limit": 50,
  "offset": 0
}
```

---

### 21. 工作區詳情

```
GET /api/v1/admin/workspaces/{workspace_id}
```

**Response:** 同 Gateway 的 `GET /api/v1/workspaces/{workspace_id}`。

---

### 22. 手動觸發 Pod 回收

```
POST /api/v1/admin/reap
```

**Request:**
```json
{
  "timeout_minutes": 10,
  "force_all": false
}
```

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| timeout_minutes | int | 否 | 閒置超過此時間的 Pod 會被回收（預設 10） |
| force_all | bool | 否 | `true` = 回收所有 Pod（含活躍中的） |

**Response:**
```json
{
  "total_reaped": 1,
  "workspaces_reaped": [
    {
      "workspace_id": "ws-testuser1",
      "pod_name": "pod-ws-testuser1",
      "reason": "admin_force_reap"
    }
  ]
}
```

---

### 23. 列出工作區成員

```
GET /api/v1/admin/workspaces/{workspace_id}/members
```

**Response:**
```json
{
  "workspace_id": "ws-testuser1",
  "members": [
    {
      "workspace_id": "ws-testuser1",
      "user_id": "testuser1",
      "role": "owner",
      "granted_by": null,
      "granted_at": "2026-04-16T10:00:00.000000"
    }
  ]
}
```

**角色權限對照：**

| 角色 | 讀取工作區 | 寫入/執行任務 | 管理成員 | 刪除工作區 |
|------|----------|-------------|---------|----------|
| `owner` | ✅ | ✅ | ✅ | ✅ |
| `admin` | ✅ | ✅ | ✅ | ❌ |
| `member` | ✅ | ✅ | ❌ | ❌ |
| `readonly` | ✅ | ❌ | ❌ | ❌ |

---

### 24. 新增工作區成員

```
POST /api/v1/admin/workspaces/{workspace_id}/members
```

**Request:**
```json
{
  "user_id": "testuser2",
  "role": "member",
  "granted_by": "testuser1"
}
```

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| user_id | string | 是 | 要新增的使用者 ID |
| role | string | 否 | 角色：`admin` / `member`（預設）/ `readonly` |
| granted_by | string | 否 | 授權者 user_id |

**Response:**
```json
{
  "workspace_id": "ws-testuser1",
  "user_id": "testuser2",
  "role": "member",
  "granted_by": "testuser1"
}
```

**錯誤處理：**
- `400` — 使用者已是成員 / 使用者不存在 / 工作區不存在

---

### 25. 更新成員角色

```
PUT /api/v1/admin/workspaces/{workspace_id}/members/{user_id}
```

**Request:**
```json
{
  "role": "admin"
}
```

**Response:**
```json
{
  "workspace_id": "ws-testuser1",
  "user_id": "testuser2",
  "role": "admin"
}
```

**錯誤處理：**
- `400` — 不可變更 owner 角色 / 無效角色 / 成員不存在

---

### 26. 移除工作區成員

```
DELETE /api/v1/admin/workspaces/{workspace_id}/members/{user_id}
```

**Response:**
```json
{
  "workspace_id": "ws-testuser1",
  "user_id": "testuser2",
  "removed": true
}
```

**錯誤處理：**
- `400` — 不可移除 owner / 成員不存在

---

### 27. 刪除工作區

```
DELETE /api/v1/admin/workspaces/{workspace_id}
Authorization: Bearer $POC_ADMIN_TOKEN
```

**說明：** 刪除工作區及所有相關資源。流程：Admin Service → Storage Service（刪除 PVC）→ Orchestrator（刪除 DB 記錄 + K8s Pod/Service）。

**Response:**
```json
{
  "workspace_id": "ws-team-eng",
  "deleted": true
}
```

若 Storage Service 刪除 PVC 失敗（如 Pod 仍在運行），DB 記錄仍會被清除，回應中會包含 `warnings`：
```json
{
  "workspace_id": "ws-team-eng",
  "deleted": true,
  "warnings": ["storage: Pod is still running"]
}
```

**錯誤處理：**
- `404` — 工作區不存在
- `400` — Pod 仍在運行（Storage Service 要求 Pod 離線才能刪除 PVC）

---

### 28. Pod 狀態總覽

```
GET /api/v1/admin/pods/status
```

**Response:**
```json
{
  "summary": {
    "running": 3,
    "terminated": 5,
    "pending": 0
  },
  "total_sessions": 8,
  "running_pods": [
    {
      "pod_name": "pod-ws-testuser1",
      "workspace_id": "ws-testuser1",
      "user_id": "testuser1",
      "session_id": "sess-001",
      "last_active_at": "2026-04-16T12:00:00.000000"
    }
  ]
}
```

---

### 29. 建立 Workspace（Admin）

```
POST /api/v1/workspaces/storage
```

> Storage Service API。Admin only。建立 group workspace + PVC。Personal workspace 由使用者登入時自動建立。

**Request:**
```json
{
  "workspace_id": "ws-team-engineering",
  "workspace_type": "group",
  "size_gb": 1,
  "display_name": "Engineering Team",
  "owner_user_id": "testuser1"
}
```

| 欄位 | 必填 | 說明 |
|------|------|------|
| workspace_id | 是 | 自訂 workspace ID |
| workspace_type | 是 | `group`（不可建立 `personal`，personal 由使用者登入自動建立） |
| size_gb | 否 | PVC 大小（GB），預設 1 |
| display_name | 否 | 顯示名稱 |
| owner_user_id | 否 | 指定 owner（必須是已存在的 user），自動加入 workspace_members |

**Response:**
```json
{
  "workspace_id": "ws-team-engineering",
  "workspace_type": "group",
  "display_name": "Engineering Team",
  "status": "created"
}
```

> 建立後，透過 Admin Service 的 workspace_members API 設定成員權限。
> 使用者下次登入（或 Pod 重建）時，會自動掛載有權限的 shared workspace PVC。

---

### 30. 確保 Workspace Storage 存在

```
POST /api/v1/workspaces/storage/ensure
```

> Storage Service API。Admin only。由 Orchestrator 在 ensure 流程中呼叫。

**Request:**
```json
{
  "workspace_id": "ws-testuser1",
  "size_gb": 1
}
```

**Response:**
```json
{
  "workspace_id": "ws-testuser1",
  "status": "exists"
}
```

---

### 31. 修改 Workspace 顯示名稱

```
PUT /api/v1/workspaces/{workspace_id}/rename
```

> Storage Service API。Workspace admin+ 或系統 admin。

**Request:**
```json
{
  "display_name": "新名稱"
}
```

**Response:**
```json
{
  "workspace_id": "ws-testuser1",
  "display_name": "新名稱"
}
```

---

### 32. 列出 Workspace 檔案

```
GET /api/v1/workspaces/{workspace_id}/storage/files?path=data/
```

> Storage Service API。Readonly+ 權限。Pod 在線走 proxy，離線走 K8s Job。

**Response:**
```json
{
  "path": "data/",
  "files": [
    {"name": "report.pdf", "type": "file", "size": 1024, "last_modified": 1713340800.0},
    {"name": "output", "type": "dir"}
  ],
  "total": 2,
  "mode": "online"
}
```

| 欄位 | 說明 |
|------|------|
| mode | `"online"`（proxy to Agent Pod）或 `"offline"`（K8s Job） |

---

### 33. 上傳檔案到 Workspace

```
POST /api/v1/workspaces/{workspace_id}/storage/files/upload?path=data/
```

> Storage Service API。Member+ 權限。離線模式限制 10MB。

**Request:** multipart/form-data（file 欄位）

**Response:**
```json
{
  "status": "uploaded",
  "path": "data/report.pdf",
  "size_bytes": 1024,
  "mode": "online"
}
```

---

### 34. 從 Workspace 下載檔案

```
GET /api/v1/workspaces/{workspace_id}/storage/files/download?path=data/report.pdf
```

> Storage Service API。Readonly+ 權限。

**Response:** binary file（`Content-Type: application/octet-stream`）

Header `X-Storage-Mode` 回傳 `online` 或 `offline`。

---

### 35. 刪除 Workspace 檔案或目錄

```
DELETE /api/v1/workspaces/{workspace_id}/storage/files?path=data/old-report.pdf
```

> Storage Service API。Member+ 權限。

**Response:**
```json
{
  "status": "deleted",
  "path": "data/old-report.pdf",
  "mode": "online"
}
```

---

### 36. 建立 Workspace 目錄

```
POST /api/v1/workspaces/{workspace_id}/storage/files/mkdir?path=data/output
```

> Storage Service API。Member+ 權限。

**Response:**
```json
{
  "status": "created",
  "path": "data/output",
  "mode": "online"
}
```

---

### 37. Workspace 存取權限

```
GET /api/v1/workspaces/{workspace_id}/storage/access
```

> Storage Service API。Workspace admin+ 或系統 admin。

**Response:**
```json
{
  "workspace_id": "ws-testuser1",
  "members": [
    {
      "user_id": "testuser1",
      "role": "owner",
      "granted_by": "admin",
      "granted_at": "2026-04-16T10:00:00.000000"
    }
  ]
}
```

| 角色 | 檔案列表 | 下載 | 上傳 | 刪除 | 建目錄 | 查看權限 | rename | 刪除 storage |
|------|---------|------|------|------|--------|---------|--------|-------------|
| readonly | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| member | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| admin(ws) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| owner | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| admin(系統) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

---

## 前端整合流程

### 推薦流程（使用 Channel 抽象層）

```
┌─────────────┐
│  使用者登入   │
└──────┬──────┘
       │
       ▼
┌──────────────────────────────────────┐
│ POST /api/v1/chat                    │  ← 推薦！自動 ensure workspace
│ { "message": "...",                  │
│   "session_id": "sess-001" }         │
│                                      │
│ 內部流程：                             │
│ WebChannel → MessageBus →            │
│ ChannelManager → ensure workspace →  │
│ Agent Pod → 回傳結果                   │
└──────────────────────────────────────┘
       │
       │ 閒置超時 → Pod 自動回收
       │ 使用者再次發送訊息 → 自動恢復
       ▼
┌──────────────────────────┐
│ GET /api/v1/workspaces/  │  ← 查詢狀態（可選）
│     {workspace_id}       │
└──────────────────────────┘
```

### 進階流程（直接 Proxy，需要串流或 MCP）

```
┌─────────────┐
│  使用者登入   │
└──────┬──────┘
       │
       ▼
┌──────────────────────────┐
│ POST /api/v1/workspaces/ │
│       ensure             │
│ → 取得 workspace_id      │
│ → 取得 session_id        │
└──────┬───────────────────┘
       │ status == "ready"?
       │
       ▼
┌──────────────────────────────────────┐
│ POST /workspaces/{wid}/api/v1/chat   │  ← 同步對話
│   或                                  │
│ POST /workspaces/{wid}/api/v1/chat/  │  ← 串流對話
│       stream                          │
│   或                                  │
│ POST /workspaces/{wid}/mcp/execute   │  ← 檔案操作（MCP）
│   或                                  │
│ POST /workspaces/{wid}/api/v1/files/ │  ← 檔案上傳
│       upload                          │
│   或                                  │
│ GET  /workspaces/{wid}/api/v1/files/ │  ← 檔案下載/列表
│       download | list                 │
└──────────────────────────────────────┘
```

---

## 錯誤碼

| HTTP Status | 說明 |
|-------------|------|
| 200 | 成功 |
| 401 | Token 無效或缺失 |
| 404 | Workspace 不存在 |
| 408 | 請求超時（Channel 層） |
| 500 | Agent 內部錯誤 |
| 502 | 無法連線到 Agent Pod（需先 ensure） |
| 503 | Channel service 未初始化 |

## 注意事項

1. **推薦使用 `/api/v1/chat`**：前端不需要管理 workspace_id，Channel 層自動處理 ensure、路由、重連
2. **Session 管理**：前端應自行產生唯一的 `session_id`（建議 UUID），同一 session 共享對話記憶
3. **活動追蹤**：使用 Channel 端點時自動追蹤；使用直接 proxy 時需帶 `X-Session-Id` header
4. **Pod 回收恢復**：Channel 端點自動處理；直接 proxy 收到 502 時需重新 ensure
5. **檔案路徑**：所有路徑相對於 workspace 根目錄，不需要前綴 `/`
6. **工作區目錄結構**：`data/`（共用資料）、`memories/`（AI 記憶）、`skills/`、`sessions/{sid}/`（session 專屬工作目錄）
7. **IM 指令**：在 `/api/v1/chat` 中可使用 `/new`（重置對話）、`/status`（查狀態）、`/help`（說明）
