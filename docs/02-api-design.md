# 02 API 設計

## 1. HTTP API（外部接口）

### 1.1 認證端點

#### POST /api/v1/auth/token

**描述**：交換用戶認證信息（用戶名/密碼或 OAuth code）為 JWT Token

**請求**：
```json
{
  "username": "user@example.com",
  "password": "secret",
  "grant_type": "password"
}
```

或 OAuth2：
```json
{
  "grant_type": "authorization_code",
  "code": "auth_code_from_provider",
  "redirect_uri": "https://yourdomain.com/callback"
}
```

**響應** (200 OK):
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "user_id": "user123"
}
```

**錯誤** (401 Unauthorized):
```json
{
  "error": "invalid_credentials",
  "error_description": "Username or password incorrect"
}
```

---

#### POST /api/v1/auth/logout

**描述**：登出當前 session，廢止 Token

**請求頭**：
```
Authorization: Bearer {access_token}
```

**請求體**：
```json
{
  "session_id": "session_abc123"
}
```

**響應** (200 OK):
```json
{
  "message": "Logged out successfully",
  "session_id": "session_abc123"
}
```

---

### 1.2 工作區管理端點

#### POST /api/v1/workspaces/ensure

**描述**：確保使用者的個人工作區存在；若不存在則建立，若存在但 Pod 已回收則恢復。`user_id` 從 token 取得（不需在 body 中傳遞）。

**請求頭**：
```
Authorization: Bearer {access_token}
Content-Type: application/json
```

**請求體**：
```json
{
  "session_id": "session_abc123",
  "resource_tier": "standard"
}
```

其中 `resource_tier` 可選，預設為 "standard"，支援 "standard" / "premium" / "enterprise"

**響應** (200 OK):
```json
{
  "workspace_id": "ws_user123",
  "workspace_type": "personal",
  "user_id": "user123",
  "session_id": "session_abc123",
  "pod_name": "pod-ws-user123",
  "service_name": "svc-ws-user123",
  "gateway_endpoint": "https://api.yourdomain.com",
  "gateway_route": "/workspaces/ws_user123",
  "status": "ready",
  "created_at": "2026-03-30T10:00:00Z",
  "last_active_at": "2026-03-30T10:05:00Z"
}
```

> **存取控制**：僅驗證 token 中的 user_id。個人 workspace 建立時自動在 `workspace_members` 加入 owner 角色。

---

#### POST /api/v1/workspaces/create

**描述**：建立群組工作區（需 owner 或 admin 權限）。個人 workspace 請用 `/ensure`。

**請求頭**：
```
Authorization: Bearer {access_token}
Content-Type: application/json
```

**請求體**：
```json
{
  "name": "Project Alpha",
  "resource_tier": "standard",
  "initial_members": [
    {"user_id": "user456", "role": "member"},
    {"user_id": "user789", "role": "readonly"}
  ]
}
```

**響應** (201 Created):
```json
{
  "workspace_id": "ws_grp_a1b2c3d4",
  "workspace_type": "group",
  "name": "Project Alpha",
  "owner_user_id": "user123",
  "pod_name": "pod-ws-grp-a1b2c3d4",
  "service_name": "svc-ws-grp-a1b2c3d4",
  "gateway_route": "/workspaces/ws_grp_a1b2c3d4",
  "status": "ready",
  "members": [
    {"user_id": "user123", "role": "owner"},
    {"user_id": "user456", "role": "member"},
    {"user_id": "user789", "role": "readonly"}
  ]
}
```

---

#### POST /api/v1/workspaces/{workspace_id}/members

**描述**：新增工作區成員（需 owner 或 admin 權限）

**請求頭**：
```
Authorization: Bearer {access_token}
```

**請求體**：
```json
{
  "user_id": "user456",
  "role": "member"
}
```

**響應** (200 OK):
```json
{
  "workspace_id": "ws_grp_a1b2c3d4",
  "user_id": "user456",
  "role": "member",
  "granted_by": "user123",
  "granted_at": "2026-04-08T10:00:00Z"
}
```

**錯誤** (403 Forbidden):
```json
{
  "error": "insufficient_permission",
  "error_description": "Only owner or admin can manage members"
}
```

---

#### DELETE /api/v1/workspaces/{workspace_id}/members/{user_id}

**描述**：移除工作區成員（需 owner 或 admin 權限，不可移除 owner）

**請求頭**：
```
Authorization: Bearer {access_token}
```

**響應** (200 OK):
```json
{
  "message": "Member removed",
  "workspace_id": "ws_grp_a1b2c3d4",
  "removed_user_id": "user456"
}
```

---

#### GET /api/v1/workspaces/{workspace_id}/members

**描述**：列出工作區成員（需為成員）

**響應** (200 OK):
```json
{
  "workspace_id": "ws_grp_a1b2c3d4",
  "members": [
    {"user_id": "user123", "role": "owner", "granted_at": "2026-04-01T08:00:00Z"},
    {"user_id": "user456", "role": "member", "granted_at": "2026-04-08T10:00:00Z"}
  ]
}
```

---

#### GET /api/v1/workspaces/{workspace_id}

**描述**：查詢工作區詳情（需為 workspace 成員）

**請求頭**：
```
Authorization: Bearer {access_token}
```

**響應** (200 OK):
```json
{
  "workspace_id": "ws_user123",
  "workspace_type": "personal",
  "owner_user_id": "user123",
  "pod_name": "pod-ws-user123",
  "pod_status": "running",
  "gateway_endpoint": "https://api.yourdomain.com",
  "gateway_route": "/workspaces/ws_user123",
  "last_active_at": "2026-03-30T10:05:00Z",
  "your_role": "owner",
  "members": [
    {"user_id": "user123", "role": "owner"}
  ],
  "sessions": [
    {
      "session_id": "session_abc123",
      "created_at": "2026-03-30T10:00:00Z",
      "last_active_at": "2026-03-30T10:05:00Z"
    }
  ]
}
```

**錯誤** (403 Forbidden):
```json
{
  "error": "access_denied",
  "error_description": "User is not a member of this workspace"
}
```

---

#### DELETE /api/v1/workspaces/{workspace_id}

**描述**：刪除工作區（需 owner 權限）。包括 Pod、Service，S3 資料可選保留或刪除。

**請求頭**：
```
Authorization: Bearer {access_token}
```

**請求體**：
```json
{
  "delete_s3_data": false
}
```

**響應** (200 OK):
```json
{
  "message": "Workspace deleted successfully",
  "workspace_id": "ws_user123",
  "resources_deleted": ["pod", "service", "activity_logs", "pod_states", "sessions", "workspace_members", "workspace"],
  "s3_data_deleted": false
}
```

---

#### DELETE /api/v1/sessions/{session_id}

**描述**：刪除指定 session 及其所有相關資料：
- Session 資料夾（PVC 上的 `sessions/{session_id}/`，透過 K8s cleanup Job 刪除）
- LangGraph checkpoint 資料（對話歷史）
- 活動日誌

**請求頭**：
```
Authorization: Bearer {access_token}
```

**響應** (200 OK):
```json
{
  "session_id": "session_abc123",
  "user_id": "user123",
  "workspace_id": "ws_user123",
  "deleted": true
}
```

**錯誤** (404 Not Found):
```json
{
  "error": "session_not_found",
  "error_description": "Session not found"
}
```

---

### 1.3 活動追蹤端點

#### POST /api/v1/activity/mark

**描述**：標記使用者工作區活動（由 API Gateway 或客戶端調用）

**請求頭**：
```
Authorization: Bearer {access_token}
```

**請求體**：
```json
{
  "user_id": "user123",
  "session_id": "session_abc123",
  "activity_type": "request",
  "metadata": {
    "endpoint": "/mcp/process",
    "method": "POST"
  }
}
```

**響應** (200 OK):
```json
{
  "message": "Activity recorded",
  "last_active_at": "2026-03-30T10:05:00Z"
}
```

---

#### GET /api/v1/activity/{user_id}

**描述**：查詢使用者的活動日誌

**請求頭**：
```
Authorization: Bearer {access_token}
```

**查詢參數**：
```
?session_id=session_abc123&limit=100&offset=0&from=2026-03-29T00:00:00Z&to=2026-03-30T23:59:59Z
```

**響應** (200 OK):
```json
{
  "user_id": "user123",
  "total": 150,
  "activities": [
    {
      "activity_id": "act_123",
      "session_id": "session_abc123",
      "activity_type": "request",
      "timestamp": "2026-03-30T10:05:00Z",
      "metadata": {
        "endpoint": "/mcp/process",
        "method": "POST",
        "status_code": 200
      }
    }
  ]
}
```

---

### 1.4 管理端點（獨立 Admin Service，需要管理員權限）

> **架構決策**：Admin 端點由獨立的 Admin Service Pod（:8090）提供，走內部網路或獨立 Ingress + IP 白名單。
> Admin Service 透過 Orchestrator API 操作 K8s 資源，維持「只有 Orchestrator 碰 K8s Control Plane」原則。
> PVC 管理已遷移至獨立 Storage Service（:8091），見 §1.5。

#### GET /api/v1/admin/workspaces

**描述**：列出所有工作區（分頁）

**請求頭**：
```
Authorization: Bearer {admin_token}
```

**查詢參數**：
```
?limit=50&offset=0&status=running&sort_by=last_active_at&order=desc
```

**響應** (200 OK):
```json
{
  "total": 500,
  "workspaces": [
    {
      "workspace_id": "ws_user123",
      "display_name": "My Workspace",
      "user_id": "user123",
      "workspace_type": "personal",
      "resource_tier": "standard",
      "status": "active",
      "active_sessions": 1,
      "created_at": "2026-03-20T08:00:00Z",
      "updated_at": "2026-03-30T10:05:00Z",
      "last_reap_at": null
    }
  ]
}
```

---

#### POST /api/v1/admin/reap

**描述**：手動觸發閒置工作區回收（通常由背景任務自動執行）

**請求頭**：
```
Authorization: Bearer {admin_token}
```

**請求體**：
```json
{
  "idle_timeout_minutes": 30,
  "dry_run": false
}
```

**響應** (200 OK):
```json
{
  "message": "Reap operation completed",
  "workspaces_reaped": [
    {
      "workspace_id": "ws_user456",
      "user_id": "user456",
      "idle_duration_minutes": 45
    }
  ],
  "total_reaped": 1
}
```

---

#### GET /api/v1/admin/users

**描述**：列出所有使用者（分頁）

**請求頭**：
```
Authorization: Bearer {admin_token}
```

**查詢參數**：
```
?limit=50&offset=0&is_active=true
```

**響應** (200 OK):
```json
{
  "total": 10,
  "users": [
    {
      "user_id": "user123",
      "username": "user123",
      "email": "user123@example.com",
      "auth_provider": "oauth2",
      "is_active": true,
      "created_at": "2026-03-20T08:00:00Z",
      "updated_at": "2026-03-30T10:05:00Z"
    }
  ],
  "limit": 50,
  "offset": 0
}
```

---

#### GET /api/v1/admin/users/{user_id}

**描述**：取得使用者詳情（含 workspace 與 session 統計）

**響應** (200 OK):
```json
{
  "user_id": "user123",
  "username": "user123",
  "email": "user123@example.com",
  "auth_provider": "oauth2",
  "is_active": true,
  "created_at": "2026-03-20T08:00:00Z",
  "updated_at": "2026-03-30T10:05:00Z",
  "workspace": {
    "workspace_id": "ws-user123",
    "status": "active"
  },
  "total_sessions": 5
}
```

---

#### PUT /api/v1/admin/users/{user_id}/active

**描述**：啟用或停用使用者

**請求體**：
```json
{
  "is_active": false
}
```

**響應** (200 OK):
```json
{
  "user_id": "user123",
  "is_active": false
}
```

---

#### GET /api/v1/admin/workspaces/{workspace_id}/members

**描述**：列出工作區成員

**響應** (200 OK):
```json
{
  "workspace_id": "ws-user123",
  "members": [
    {
      "workspace_id": "ws-user123",
      "user_id": "user123",
      "role": "owner",
      "granted_by": null,
      "granted_at": "2026-03-20T08:00:00Z"
    }
  ]
}
```

---

#### POST /api/v1/admin/workspaces/{workspace_id}/members

**描述**：新增工作區成員（需 owner 或 admin 權限）

**請求體**：
```json
{
  "user_id": "user456",
  "role": "member",
  "granted_by": "user123"
}
```

**響應** (200 OK):
```json
{
  "workspace_id": "ws-user123",
  "user_id": "user456",
  "role": "member",
  "granted_by": "user123"
}
```

**錯誤** (400 Bad Request):
```json
{
  "detail": "User user456 is already a member of ws-user123"
}
```

---

#### PUT /api/v1/admin/workspaces/{workspace_id}/members/{user_id}

**描述**：更新成員角色（不可變更 owner）

**請求體**：
```json
{
  "role": "admin"
}
```

**響應** (200 OK):
```json
{
  "workspace_id": "ws-user123",
  "user_id": "user456",
  "role": "admin"
}
```

---

#### DELETE /api/v1/admin/workspaces/{workspace_id}/members/{user_id}

**描述**：移除工作區成員（不可移除 owner）

**響應** (200 OK):
```json
{
  "workspace_id": "ws-user123",
  "user_id": "user456",
  "removed": true
}
```

---

#### DELETE /api/v1/admin/workspaces/{workspace_id}

**描述**：刪除工作區（PVC + DB 記錄 + K8s 資源）。流程：Admin Service → Storage Service（刪除 PVC）→ Orchestrator（刪除 DB 記錄 + K8s Pod/Service）。

**響應** (200 OK):
```json
{
  "workspace_id": "ws-team-eng",
  "deleted": true
}
```

**錯誤響應**：
- `404`：工作區不存在
- `400`：Pod 仍在運行（Storage Service 要求 Pod 離線才能刪除 PVC）

> **注意**：若 Storage Service 刪除 PVC 失敗（如 Pod 在線），DB 記錄仍會被清除，回應中會包含 `warnings` 欄位。

---

#### GET /api/v1/admin/pods/status

**描述**：Pod 狀態總覽

**響應** (200 OK):
```json
{
  "summary": {
    "running": 3,
    "terminated": 10,
    "pending": 0
  },
  "total_sessions": 13,
  "running_pods": [
    {
      "pod_name": "pod-ws-user123",
      "workspace_id": "ws-user123",
      "user_id": "user123",
      "session_id": "session_abc123",
      "last_active_at": "2026-03-30T10:05:00Z"
    }
  ]
}
```

---

### 1.5 Workspace Storage 端點（獨立 Storage Service）

> **架構決策**：Workspace storage 管理由獨立的 Storage Service Pod（:8091）提供，Gateway proxy `/api/v1/workspaces/storage/*` 和 `/api/v1/workspaces/{wid}/storage/*`。
> Storage Service 直接操作 K8s API（PVC CRUD、File Operation Job）和 DB（workspace 映射、workspace_members 權限）。
> 支援 user token（操作自己有權限的 workspace）和 admin token（操作所有 workspace）雙模式認證。
> 檔案操作雙模式：Pod 在線 → proxy 到 Agent Pod；Pod 離線 → 建立短暫 K8s Job 掛載 PVC 操作。

#### GET /api/v1/workspaces/storage

**描述**：列出 workspace storage（admin 全部 / user 自己有權限的）

**請求頭**：
```
Authorization: Bearer {user_token 或 admin_token}
```

**查詢參數**：
```
?limit=50&offset=0
```

**響應** (200 OK):
```json
{
  "workspaces": [
    {
      "workspace_id": "ws-user123",
      "display_name": "User123 Workspace",
      "capacity": "1Gi",
      "status": "Bound",
      "storage_class": "standard",
      "created_at": "2026-03-20T08:00:00Z"
    }
  ],
  "total": 1
}
```

---

#### POST /api/v1/workspaces/storage

**描述**：Admin 建立新的 group workspace + PVC（personal workspace 由使用者登入時自動建立）

**請求體**：
```json
{
  "workspace_id": "ws-team-engineering",
  "workspace_type": "group",
  "size_gb": 1,
  "display_name": "Engineering Team",
  "owner_user_id": "user123"
}
```

| 欄位 | 必填 | 說明 |
|------|------|------|
| workspace_id | 是 | 自訂 workspace ID |
| workspace_type | 是 | `group`（不可建立 `personal`，personal 由使用者登入自動建立） |
| size_gb | 否 | PVC 大小，預設 1 |
| display_name | 否 | 顯示名稱 |
| owner_user_id | 否 | 指定 owner（必須是已存在的 user），自動加入 workspace_members |

**響應** (200 OK):
```json
{
  "workspace_id": "ws-team-engineering",
  "workspace_type": "group",
  "display_name": "Engineering Team",
  "status": "created"
}
```

---

#### POST /api/v1/workspaces/storage/ensure

**描述**：確保 workspace PVC 存在（由 Orchestrator 在 ensure 流程中呼叫，admin only）

**請求體**：
```json
{
  "workspace_id": "ws-user123",
  "size_gb": 1
}
```

**響應** (200 OK):
```json
{
  "workspace_id": "ws-user123",
  "status": "exists"
}
```

---

#### PUT /api/v1/workspaces/{workspace_id}/rename

**描述**：修改 workspace 顯示名稱（DB only）。需要 workspace admin+ 或系統 admin 權限。

**請求體**：
```json
{
  "display_name": "新名稱"
}
```

**響應** (200 OK):
```json
{
  "workspace_id": "ws-user123",
  "display_name": "新名稱"
}
```

---

#### GET /api/v1/workspaces/{workspace_id}/storage/files

**描述**：列出 workspace 檔案。readonly+ 權限。

**查詢參數**：`?path=data/`

**響應** (200 OK):
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

> `mode`: `"online"`（proxy to Agent Pod）或 `"offline"`（K8s Job）

---

#### POST /api/v1/workspaces/{workspace_id}/storage/files/upload

**描述**：上傳檔案到 workspace。member+ 權限。離線模式限制 10MB。

**請求**：multipart/form-data + `?path=data/`

**響應** (200 OK):
```json
{
  "status": "uploaded",
  "path": "data/report.pdf",
  "size_bytes": 1024,
  "mode": "online"
}
```

---

#### GET /api/v1/workspaces/{workspace_id}/storage/files/download

**描述**：從 workspace 下載檔案。readonly+ 權限。

**查詢參數**：`?path=data/report.pdf`

**響應**：binary file（`Content-Type: application/octet-stream`）

---

#### DELETE /api/v1/workspaces/{workspace_id}/storage/files

**描述**：刪除 workspace 檔案或目錄。member+ 權限。

**查詢參數**：`?path=data/old-report.pdf`

**響應** (200 OK):
```json
{
  "status": "deleted",
  "path": "data/old-report.pdf",
  "mode": "online"
}
```

---

#### POST /api/v1/workspaces/{workspace_id}/storage/files/mkdir

**描述**：建立 workspace 目錄。member+ 權限。

**查詢參數**：`?path=data/output`

**響應** (200 OK):
```json
{
  "status": "created",
  "path": "data/output",
  "mode": "online"
}
```

---

#### GET /api/v1/workspaces/{workspace_id}/storage/access

**描述**：查詢 workspace 存取權限（= workspace members）。workspace admin+ 或系統 admin 權限。

**響應** (200 OK):
```json
{
  "workspace_id": "ws-user123",
  "members": [
    {
      "user_id": "user123",
      "role": "owner",
      "granted_by": "admin",
      "granted_at": "2026-03-20T08:00:00Z"
    }
  ]
}
```

---

## 2. 內部 RPC API（Orchestrator 服務）

### 2.1 WorkspaceOrchestrator 服務

#### Method: ensure_session_workspace

**描述**：確保工作區存在，或從 S3 恢復 Pod

**簽名**：
```python
async def ensure_session_workspace(
    user_id: str,
    session_id: str,
    resource_tier: str = "standard"
) -> WorkspaceInfo:
    """
    Returns:
        WorkspaceInfo with pod_name, service_name, endpoint, status ("ready" | "starting"), etc.

    Raises:
        WorkspaceExistsError: 若工作區配置不相容
        K8sResourceError: 若 K8s 操作失敗
        DatabaseError: 若 DB 操作失敗
    """
```

**邏輯流程**：
1. 取得 per-workspace advisory lock：`pg_advisory_xact_lock(hash(workspace_id))`
2. 查 DB sessions 表，確認 session_id 存在
3. 查 DB workspaces 表，檢查 user_id 是否有 workspace
   - 無 → 建立 workspace 記錄 + workspace_members(owner)
   - 有 → 檢查使用者是否為成員（workspace_members）
4. 查 K8s，檢查 Pod 是否運行
   - 是 → 返回既有 Pod 資訊
   - 否 → 建立新 Pod、Service（冪等：409 Conflict 視為成功）
5. **等待 Pod readiness**（輪詢 K8s Pod status，最多 120 秒）
   - Pod condition `Ready=True` → 繼續
   - 超時 → 返回 `status: "starting"`，Gateway 需等待或重試
6. Agent 容器啟動，初始化 Deep Agents，連接 S3 + PostgreSQL
7. 記錄 last_active_at
8. commit（自動釋放 advisory lock）
9. 返回工作區資訊（含 `status: "ready"` 或 `"starting"`）

> **併發安全**：Advisory lock 確保同一 workspace 的 ensure/reap 操作序列化執行，不同 workspace 完全不阻塞。K8s 409 冪等處理作為第二層防護。詳見 [05-生命週期 8.3 節](./05-lifecycle.md)。

---

#### Method: mark_activity

**描述**：更新使用者工作區的活動時間戳

**簽名**：
```python
async def mark_activity(
    user_id: str,
    session_id: Optional[str] = None,
    activity_type: str = "request",
    metadata: Optional[Dict] = None
) -> None:
    """
    Updates last_active_at in sessions/workspaces table.
    """
```

---

#### Method: reap_idle_workspaces

**描述**：後台任務，清理超過 idle_timeout 的工作區

**簽名**：
```python
async def reap_idle_workspaces(
    idle_timeout_minutes: int = 30,
    dry_run: bool = False
) -> ReapResult:
    """
    Returns:
        ReapResult with list of reaped workspace IDs
    """
```

**邏輯流程**：
1. 查 DB sessions/workspaces 表，找 last_active_at < now - idle_timeout_minutes
2. 對各 workspace：
   - 取得 per-workspace advisory lock：`pg_advisory_xact_lock(hash(workspace_id))`
   - **拿鎖後再次檢查** last_active_at（防止與 ensure 競爭）
   - 若仍閒置：刪除 Pod、Service（K8s 操作）
   - 更新 DB 狀態為 "idle"
   - 記錄回收事件
   - commit（釋放 advisory lock）
3. 返回回收結果

> **併發安全**：Reaper 與 ensure 共用同一把 advisory lock。Reaper 拿鎖後再次檢查 last_active_at，若使用者剛回訪（ensure 更新了 last_active_at），則跳過回收。

**Gateway 路由策略**：
- 對外固定入口：`https://api.yourdomain.com`
- Gateway 依 token 解析 `user_id`，查 `workspace_members` 驗證存取權限
- 路由表以 `workspace_id` 為 key：`_route_table[workspace_id] → (endpoint, cached_at)`
- 內部路由至 `svc-ws-{workspace_id}:80`
- 個人 workspace：`workspace_id = ws-{user_id}`，使用者登入自動建立
- Group workspace：`workspace_id` 自訂（如 `ws-team-eng`），Admin 預建，依 workspace_members 角色決定權限
- 使用者 Pod 掛載 personal PVC (`/workspace/{wid}`) + 所有 group PVC (`/shared/{wid}`)
- Pod 回收時不需動態建立/刪除每使用者公開 Ingress

**Route Cache TTL + 失效策略**：

```python
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass
class RouteEntry:
    endpoint: str
    cached_at: datetime

# 路由表：workspace_id → RouteEntry
_route_table: dict[str, RouteEntry] = {}

ROUTE_CACHE_TTL_SECONDS = 300  # 5 分鐘 TTL

def _get_route(workspace_id: str) -> str | None:
    """取得路由，過期則返回 None。"""
    entry = _route_table.get(workspace_id)
    if not entry:
        return None
    age = (datetime.now(timezone.utc) - entry.cached_at).total_seconds()
    if age > ROUTE_CACHE_TTL_SECONDS:
        del _route_table[workspace_id]  # 過期，清除
        return None
    return entry.endpoint

def _set_route(workspace_id: str, endpoint: str):
    _route_table[workspace_id] = RouteEntry(
        endpoint=endpoint, cached_at=datetime.now(timezone.utc)
    )

def _invalidate_route(workspace_id: str):
    _route_table.pop(workspace_id, None)
```

**Proxy 失敗自動恢復**（Production Gateway）：

```python
async def proxy_to_agent(workspace_id: str, path: str, body: dict, user_id: str):
    endpoint = _get_route(workspace_id)

    # 1) cache miss 或過期 → 先 ensure
    if not endpoint:
        result = await ensure_workspace(user_id, workspace_id)
        endpoint = result["service_endpoint"]
        _set_route(workspace_id, endpoint)

    # 2) 嘗試轉發
    try:
        resp = await client.post(f"http://{endpoint}/{path}", json=body)
        return resp
    except httpx.ConnectError:
        # 3) 連線失敗 → 清除 cache → 自動 ensure 重建 → 重試一次
        _invalidate_route(workspace_id)
        result = await ensure_workspace(user_id, workspace_id)
        if result["status"] != "ready":
            raise HTTPException(503, "Workspace is starting, please retry in a few seconds")
        endpoint = result["service_endpoint"]
        _set_route(workspace_id, endpoint)
        resp = await client.post(f"http://{endpoint}/{path}", json=body)
        return resp
```

> **設計要點**：
> - TTL 5 分鐘：平衡 cache 命中率與過期偵測速度
> - ConnectError 觸發立即失效 + 自動 ensure：使用者最多等一次 Pod 重建時間
> - 最多重試一次：避免無限迴圈（若第二次仍失敗，返回 503）
> - SSE streaming：`chat/stream` 端點使用 `httpx.stream()` 串流轉發，避免 buffer 整個 response
> - POC 不實作 TTL 和自動恢復（單副本 + 簡化版）

---

#### Method: resume_workspace

**描述**：從既有 S3 資料恢復工作區（重建 Pod）

**簽名**：
```python
async def resume_workspace(
    user_id: str,
    session_id: str,
    workspace_id: str
) -> WorkspaceInfo:
    """
    Assumes workspace and S3 data exist.
    Verifies user_id is a workspace member.
    Rebuilds Pod and Service.
    """
```

---

### 2.2 K8sClient 包裝

#### Method: create_pod

**簽名**：
```python
async def create_pod(
    workspace_id: str,
    pod_name: str,
    image: str,
    env: Dict[str, str],
    resources: ResourceLimits,
    service_account_name: str = "agent-s3-access",
) -> PodInfo:
```

#### Method: create_service

**簽名**：
```python
async def create_service(
    workspace_id: str,
    service_name: str,
    pod_name: str,
    port: int = 8080
) -> ServiceInfo:
```

#### Method: check_workspace_access

**描述**：驗證使用者是否有權存取工作區（供 Gateway 呼叫）

**簽名**：
```python
async def check_workspace_access(
    workspace_id: str,
    user_id: str,
    required_role: str = "member"  # 最低需要的角色
) -> WorkspaceAccessInfo:
    """
    Returns:
        WorkspaceAccessInfo with role, service_endpoint, etc.

    Raises:
        AccessDeniedError: 若使用者非成員或角色不足
    """
```

#### Method: sync_gateway_route

**簽名**：
```python
async def sync_gateway_route(
    workspace_id: str,
    service_name: str
) -> None:
```

---

## 3. Agent 容器內 API

### 3.1 MCP 服務端點

Agent 容器實作 MCP (Model Context Protocol) 服務，支援以下工具：

#### Tool: execute_task

**描述**：執行使用者提交的任務

**輸入**：
```json
{
  "task_id": "task_abc123",
  "task_type": "analyze|generate|transform|etc",
  "parameters": {
    "input_file": "/workspace/data.csv",
    "output_file": "/workspace/result.json",
    "model": "gpt-4",
    "temperature": 0.7
  }
}
```

**輸出**：
```json
{
  "task_id": "task_abc123",
  "status": "completed|running|failed",
  "result": {...},
  "metadata": {
    "duration_ms": 5000,
    "tokens_used": 1500
  }
}
```

---

#### Tool: list_files

**描述**：列出工作區內的檔案

**輸入**：
```json
{
  "path": "/workspace",
  "recursive": true
}
```

**輸出**：
```json
{
  "files": [
    {
      "name": "data.csv",
      "path": "/workspace/data.csv",
      "size_bytes": 1024,
      "modified_at": "2026-03-30T10:00:00Z"
    }
  ]
}
```

---

#### Tool: read_file

**描述**：讀取工作區內的檔案

**輸入**：
```json
{
  "path": "/workspace/config.yaml"
}
```

**輸出**：
```json
{
  "content": "...",
  "encoding": "utf-8",
  "size_bytes": 512
}
```

---

#### Tool: write_file

**描述**：寫入檔案到工作區

**輸入**：
```json
{
  "path": "/workspace/output.txt",
  "content": "...",
  "mode": "w|a"
}
```

**輸出**：
```json
{
  "path": "/workspace/output.txt",
  "bytes_written": 1024
}
```

---

#### Tool: duckduckgo_search

**描述**：搜尋網路上的即時資訊（免費，無需 API Key）

**來源**：`langchain-community` 的 `DuckDuckGoSearchRun`

**輸入**：
```json
{
  "query": "lofi non-copyright music sources"
}
```

**輸出**：
```json
{
  "result": "搜尋結果摘要文字..."
}
```

---

#### Tool: taiwan_weather

**描述**：查詢台灣天氣資訊（中央氣象署 CWA 開放資料 API，需設定 `CWA_API_KEY`）

**來源**：`poc/agent/tools.py` 的 `CWAWeatherTool`

**支援資料集**：

| 查詢類型 | 資料集 ID | 說明 |
|---------|-----------|------|
| `forecast_36h` | F-C0032-001 | 今明 36 小時天氣預報 |
| `forecast_7d` | F-C0032-005 | 一週天氣預報 |
| `observation` | O-A0003-001 | 即時氣象觀測 |
| `rain` | O-A0002-001 | 即時雨量觀測 |

**輸入**：
```json
{
  "query": "forecast_36h 臺北市"
}
```

**輸出**：
```json
{
  "result": "📋 一般天氣預報-今明 36 小時天氣預報\n\n🏙️ 臺北市\n  Wx: 晴時多雲 (2025-04-28 06:00:00 ~ 2025-04-28 18:00:00)\n  ..."
}
```

---

### 3.2 健康檢查端點

#### GET /health

**描述**：Kubernetes liveness probe

**響應** (200 OK):
```json
{
  "status": "healthy",
  "timestamp": "2026-03-30T10:05:00Z"
}
```

---

#### GET /readiness

**描述**：Kubernetes readiness probe

**響應** (200 OK):
```json
{
  "status": "ready",
  "dependencies": {
    "mcp_server": "ok",
    "workspace": "mounted",
    "storage": "ok"
  }
}
```

---

### 3.3 檔案操作端點（POC 新增）

#### POST /api/v1/files/upload

**描述**：上傳檔案至工作區 PVC 指定目錄

**參數**：
- `file`（multipart/form-data）：上傳的檔案
- `path`（query, optional）：相對於 workspace 的目標目錄，例如 `data/` 或 `sessions/sess-001/`

**響應** (200 OK):
```json
{
  "status": "uploaded",
  "path": "data/report.csv",
  "size_bytes": 1024,
  "filename": "report.csv"
}
```

---

#### GET /api/v1/files/download

**描述**：從工作區 PVC 下載檔案

**參數**：
- `path`（query, required）：相對於 workspace 的檔案路徑

**響應**：檔案二進位內容（`application/octet-stream`）

---

#### GET /api/v1/files/list

**描述**：列出工作區目錄內容

**參數**：
- `path`（query, optional）：相對於 workspace 的目錄路徑

**響應** (200 OK):
```json
{
  "path": "sessions/sess-001",
  "files": [
    {"name": "test.md", "type": "file", "size": 256},
    {"name": "output", "type": "dir"}
  ],
  "total": 2
}
```

---

#### DELETE /api/v1/files/delete

**描述**：刪除工作區 PVC 中的指定檔案或目錄（目錄會遞迴刪除）

**參數**：
- `path`（query, required）：相對於 workspace 的檔案或目錄路徑

**響應** (200 OK):
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

**錯誤**：
- `403` — 路徑逃逸或無寫入權限
- `404` — 路徑不存在

---

#### POST /api/v1/files/mkdir

**描述**：在工作區 PVC 中建立目錄（支援多層建立）

**參數**：
- `path`（query, required）：相對於 workspace 的目錄路徑

**響應** (200 OK):
```json
{
  "status": "created",
  "path": "sessions/sess-001/output"
}
```

**錯誤**：
- `403` — 路徑逃逸或無寫入權限

---

### 3.4 對話端點（POC 新增）

#### POST /api/v1/chat

**描述**：同步對話端點

**輸入**：
```json
{
  "message": "請幫我分析 data/report.csv",
  "session_id": "sess-001"
}
```

**響應** (200 OK):
```json
{
  "content": "AI 回應內容...",
  "session_id": "sess-001",
  "tool_calls": []
}
```

---

#### POST /api/v1/chat/stream

**描述**：串流對話端點（SSE）

**輸入**：同 `/api/v1/chat`

**響應**：Server-Sent Events
- `event: content` — AI 回應文字片段
- `event: tool_call` — 工具呼叫（僅 `AGENT_DISPLAY_MODE=full_history` 時輸出）
- `event: tool_result` — 工具執行結果（僅 `AGENT_DISPLAY_MODE=full_history` 時輸出）
- `event: file` — 工具產生的檔案（圖片、文件等），包含 `path`、`type`（`image`/`document`）、`name`
- `event: error` — Agent 錯誤（recursion limit、LLM 連線失敗等），包含 `error` 訊息
- `data: [DONE]` — 串流結束

> **Display Mode**：透過環境變數 `AGENT_DISPLAY_MODE` 控制 SSE 事件可見性。
> `normal`（預設）僅輸出 `content`/`file`/`error`；`full_history` 額外輸出 `tool_call`/`tool_result`。
> 同時影響 `/api/v1/sessions/{sid}/messages` 的歷史訊息過濾。

---

#### GET /api/v1/sessions/{session_id}/messages

**描述**：取得 session 對話歷史（從 LangGraph checkpointer）

**響應** (200 OK):
```json
{
  "session_id": "sess-001",
  "messages": [
    {"role": "human", "content": "你好"},
    {"role": "ai", "content": "你好！有什麼可以幫你的？"}
  ],
  "total": 2
}
```

---

## 4. 資料流示例

### 示例 1：使用者首次登入

```
1. Client: POST /api/v1/auth/token
   → API Gateway 驗證用戶名密碼
   → 生成 JWT token (包含 user_id)
   ← 返回 access_token

2. Client: POST /api/v1/workspaces/ensure
   + Header: Authorization: Bearer {token}
   + Body: { user_id: "user123", session_id: "sess_abc" }
   → API Gateway 驗證 token，解析 user_id
   → 呼叫 Orchestrator.ensure_session_workspace("user123", "sess_abc")
      → K8s: 檢查 Pod 不存在 → 建立 pod-ws-user123 + svc-ws-user123
      → DB: 記錄 session、workspace、activity
   ← 返回 gateway_endpoint: "https://api.yourdomain.com"
      返回 gateway_route: "/workspaces/ws_user123"

3. Client: 連線到 https://api.yourdomain.com/workspaces/ws_user123
   → API Gateway 驗證 token
   → Gateway 驗證 workspace ACL → 路由到 svc-ws-user123
   → Service 將流量轉向 pod-ws-user123 port 8080
   → Pod 內 Agent 接收連線
      → 透過 S3Backend 存取 S3 工作區
      → 初始化 MCP 伺服器
      → 加載使用者配置
   ← 可執行任務
```

### 示例 2：同一使用者第二個 session 登入

```
1. Client_B: POST /api/v1/auth/token (username: user123, password: xxx)
   ← access_token_B (user_id: user123)

2. Client_B: POST /api/v1/workspaces/ensure
   + Body: { user_id: "user123", session_id: "sess_xyz" }
   → Orchestrator.ensure_session_workspace("user123", "sess_xyz")
      → K8s: 檢查 Pod pod-ws-user123 存在且運行
      → 直接返回既有 Pod 資訊
      → DB: 記錄新 session
   ← 返回同一 gateway_endpoint: "https://api.yourdomain.com"
      返回同一 gateway_route: "/workspaces/ws_user123"

3. Client_A & Client_B 同時連線到同一 Pod
   → Pod 內 Agent 透過 session_id 區分連線
   → 共用工作區檔案 & 應用狀態
```

### 示例 3：30 分鐘無活動後回訪

```
1. 系統背景任務 (每 5 分鐘執行):
   → reap_idle_workspaces(idle_timeout_minutes=30)
      → 發現 pod-ws-user123 的 last_active_at < 30 min
      → K8s: 刪除 Pod、Service
      → DB: 更新狀態為 "idle"
   ✓ Pod 被回收，S3 資料保留

2. Client_A 回訪 (例如 35 分鐘後):
   POST /api/v1/workspaces/ensure
   + Body: { user_id: "user123", session_id: "sess_new_c" }
   → Orchestrator.ensure_session_workspace("user123", "sess_new_c")
      → K8s: 檢查 Pod 不存在
      → DB: 找到既有 workspace（S3 資料完整）
      → 用既有 S3 資料重建 Pod、Service
      → Pod 啟動時透過 S3Backend 存取 S3 工作區，恢復應用狀態
   ← 返回 gateway_endpoint 與 gateway_route
   ✓ 工作區與資料恢復
```

---

## 5. 錯誤處理與狀態碼

| HTTP 狀態碼 | 場景 | 響應體 |
|-----------|------|------|
| 200 OK | 操作成功 | `{ "data": {...} }` |
| 201 Created | 資源建立成功 | 同上 |
| 400 Bad Request | 請求格式錯誤 | `{ "error": "invalid_request", "message": "..." }` |
| 401 Unauthorized | Token 無效/過期 | `{ "error": "invalid_token", "message": "..." }` |
| 403 Forbidden | 沒有權限 | `{ "error": "forbidden", "message": "..." }` |
| 404 Not Found | 資源不存在 | `{ "error": "not_found", "message": "..." }` |
| 409 Conflict | 工作區配置衝突 | `{ "error": "workspace_conflict", "message": "..." }` |
| 500 Internal Server Error | 伺服器錯誤 | `{ "error": "internal_error", "message": "..." }` |
| 503 Service Unavailable | 服務暫時不可用 | `{ "error": "service_unavailable", "message": "..." }` |


