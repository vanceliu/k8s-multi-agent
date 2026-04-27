# 03 資料模型

## 1. 資料庫 Schema（PostgreSQL）

### 1.1 表結構

#### Table: users

存儲使用者的基本信息

```sql
CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(255) UNIQUE NOT NULL,           -- 外部 ID（如來自 OAuth）
    username VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(512),                     -- 若使用本地驗證
    auth_provider VARCHAR(50),                      -- "local", "oauth2", "saml" 等
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    
    INDEX idx_user_id (user_id),
    INDEX idx_username (username),
    INDEX idx_email (email)
);
```

---

#### Table: workspaces

存儲工作區資訊，支援個人工作區（`personal`）與群組工作區（`group`）。

```sql
CREATE TABLE workspaces (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(255) UNIQUE NOT NULL,      -- 工作區唯一識別符
    workspace_type VARCHAR(50) NOT NULL DEFAULT 'personal',  -- 'personal' / 'group'
    owner_user_id VARCHAR(255) REFERENCES users(user_id) ON DELETE SET NULL,  -- nullable（group workspace 可無主）
    name VARCHAR(255),                              -- 顯示名稱（group workspace 使用）
    s3_prefix VARCHAR(512) NOT NULL,                -- S3 prefix，例 "users/user123/" 或 "groups/grp_abc/"
    resource_tier VARCHAR(50) NOT NULL DEFAULT "standard",  -- "standard" / "premium" / "enterprise"
    status VARCHAR(50) NOT NULL DEFAULT "active",   -- "active" / "idle" / "archived"
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_reap_at TIMESTAMP,                         -- 最後回收時間

    INDEX idx_owner_user_id (owner_user_id),
    INDEX idx_workspace_type (workspace_type),
    INDEX idx_status (status),
    INDEX idx_created_at (created_at)
);
```

> **變更說明**：
> - 移除 `UNIQUE(user_id)` 約束（允許 group workspace）
> - 移除 `pvc_name`、`pvc_size_gb`（PVC 改為 S3）
> - 新增 `workspace_type`、`owner_user_id`、`name`、`s3_prefix`

---

#### Table: sessions

存儲活動 session 與 Pod 的映射

```sql
CREATE TABLE sessions (
    id BIGSERIAL PRIMARY KEY,
    session_id VARCHAR(255) UNIQUE NOT NULL,        -- 由 Orchestrator 生成的 UUID
    user_id VARCHAR(255) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    workspace_id VARCHAR(255) REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    pod_name VARCHAR(255),                          -- K8s Pod 名稱，例 "pod-ws-user123"
    service_name VARCHAR(255),                      -- K8s Service 名稱
    token_hash VARCHAR(512),                        -- 用於快速查詢 token 對應的 session
    pod_status VARCHAR(50),                         -- "pending" / "running" / "failed" / "terminated"
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_active_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    terminated_at TIMESTAMP,                        -- Pod 終止時間
    
    INDEX idx_session_id (session_id),
    INDEX idx_user_id (user_id),
    INDEX idx_last_active_at (last_active_at),
    INDEX idx_pod_status (pod_status)
);
```

---

#### Table: workspace_members

工作區成員存取控制。個人 workspace 建立時自動加入 owner；group workspace 支援多成員。

```sql
CREATE TABLE workspace_members (
    id BIGSERIAL PRIMARY KEY,
    workspace_id VARCHAR(255) NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    user_id VARCHAR(255) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    role VARCHAR(50) NOT NULL DEFAULT 'member',     -- 'owner' / 'admin' / 'member' / 'readonly'
    granted_by VARCHAR(255) REFERENCES users(user_id),
    granted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(workspace_id, user_id),
    INDEX idx_user_id (user_id),
    INDEX idx_workspace_id (workspace_id)
);
```

**角色權限對照**：

| 角色 | 讀取工作區 | 寫入/執行任務 | 管理成員 | 刪除工作區 |
|------|----------|-------------|---------|----------|
| `owner` | ✅ | ✅ | ✅ | ✅ |
| `admin` | ✅ | ✅ | ✅ | ❌ |
| `member` | ✅ | ✅ | ❌ | ❌ |
| `readonly` | ✅ | ❌ | ❌ | ❌ |

> **POC 實作狀態**：`workspace_members` 表已在 POC 中實作（`poc/db/models.py` 的 `WorkspaceMember` ORM），透過 Admin Service（:8090）提供 CRUD API。POC 版本使用 `UniqueConstraint("workspace_id", "user_id")` 確保每個使用者在同一工作區只有一個角色。

---

#### Table: activity_logs

審計日誌，記錄所有活動

```sql
CREATE TABLE activity_logs (
    id BIGSERIAL PRIMARY KEY,
    activity_id VARCHAR(255) UNIQUE NOT NULL DEFAULT gen_random_uuid()::text,
    user_id VARCHAR(255) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    session_id VARCHAR(255) REFERENCES sessions(session_id) ON DELETE SET NULL,
    workspace_id VARCHAR(255) REFERENCES workspaces(workspace_id) ON DELETE SET NULL,
    activity_type VARCHAR(100) NOT NULL,            -- "pod_created" / "pod_deleted" / "api_call" / "mcp_task" 等
    action VARCHAR(255),                            -- 具體動作描述
    resource_type VARCHAR(100),                     -- "pod" / "service" / "session" / "s3" 等
    resource_id VARCHAR(255),
    http_method VARCHAR(10),                        -- "GET" / "POST" 等（若為 API 呼叫）
    http_endpoint VARCHAR(512),
    status_code INT,
    error_message TEXT,
    metadata JSONB,                                 -- 額外資訊（如參數、結果摘要等）
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_user_id (timestamp),
    INDEX idx_user_id_session (user_id, session_id),
    INDEX idx_activity_type (activity_type),
    INDEX idx_timestamp (timestamp DESC)
);
```

---

#### Table: pod_states

實時記錄 Pod 的預期狀態與實際狀態（用於故障診斷）

```sql
CREATE TABLE pod_states (
    id BIGSERIAL PRIMARY KEY,
    pod_name VARCHAR(255) UNIQUE NOT NULL,
    workspace_id VARCHAR(255) REFERENCES workspaces(workspace_id) ON DELETE CASCADE,

    -- 預期狀態
    desired_state VARCHAR(50) NOT NULL,             -- "running" / "stopped" / "terminated"
    desired_resources_cpu VARCHAR(20),              -- "100m" / "1000m" 等
    desired_resources_memory VARCHAR(20),           -- "256Mi" / "1Gi" 等

    -- 實際狀態
    actual_state VARCHAR(50),                       -- 來自 K8s API
    actual_resources_cpu_usage VARCHAR(20),
    actual_resources_memory_usage VARCHAR(20),
    restart_count INT DEFAULT 0,
    last_error_message TEXT,

    last_state_update_at TIMESTAMP,
    sync_status VARCHAR(50) DEFAULT "synced",      -- "synced" / "out_of_sync" / "error"

    INDEX idx_workspace_id (workspace_id),
    INDEX idx_sync_status (sync_status)
);
```

---

### 1.2 關鍵設計決策

1. **一對多關係**：`workspace ↔ members`
   - 個人 workspace：使用者登入自動建立，`workspace_id = ws-{user_id}`
   - Group workspace：Admin 預建，多人共用，`workspace_id` 自訂（如 `ws-team-eng`）
   - 存取控制透過 `workspace_members` 表統一管理
   - 使用者 Pod 啟動時自動掛載 personal PVC (`/workspace/{wid}`) + 所有 group PVC (`/shared/{wid}`)

2. **多對一關係**：`user_id ↔ session_id`
   - 同一使用者可以有多個活動 session
   - 同一 workspace 的所有 session 共用同一個 Pod（透過 `workspace_id` 關聯）

3. **Workspace 與儲存對應**：`workspace_id ↔ s3_prefix`
   - 個人：`s3://bucket/users/{user_id}/`
   - 群組：`s3://bucket/groups/{group_id}/`

4. **狀態同步**：`desired_state` vs `actual_state`
   - Desired：業務期望的狀態
   - Actual：K8s 實際反映的狀態
   - 若不同步 → 診斷工具可發現問題

5. **審計追蹤**：`activity_logs` 保留所有操作
   - 安全合規性
   - 故障診斷
   - 使用者行為分析

---

## 2. Kubernetes 資源對應

### 2.1 命名規則

| 資源類型 | Personal Workspace | Group Workspace | 生命週期 |
|---------|--------------|---------------|---------|
| Namespace | `agent-platform` | `agent-platform` | 永久 |
| S3 prefix | `users/{user_id}/` | `groups/{group_id}/` | 永久 |
| PVC (POC) | `pvc-ws-{user_id}` | `pvc-{workspace_id}` (自訂) | 永久 |
| Pod | `pod-ws-{user_id}` | 無獨立 Pod（掛載到使用者 Pod 的 `/shared/`） | 臨時（閒置時刪除） |
| Service | `svc-ws-{user_id}` | 無獨立 Service | 臨時 |
| Pod 掛載 | `/workspace/ws-{user_id}` (read-write) | `/shared/{workspace_id}` (依 role) | — |
| Gateway Ingress | `gateway-public-ingress` | `gateway-public-ingress` | 永久（單一入口） |
| Orchestrator | `orchestrator` | `orchestrator` | 永久（Deployment） |
| Gateway | `gateway` | `gateway` | 永久（Deployment） |

---

### 2.2 資源 Manifest 骨架

#### Namespace

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: agent-platform
  labels:
    app: k8s-agent-platform
```

---

#### Pod（臨時，閒置時刪除。無 PVC，使用 S3 + ephemeral storage）

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: pod-ws-{workspace_id}
  namespace: agent-platform
  labels:
    workspace_id: "{workspace_id}"
    app: k8s-agent-platform
    component: agent
spec:
  serviceAccountName: agent-s3-access  # IRSA
  containers:
  - name: agent
    image: your-registry/k8s-agent-deep:latest
    imagePullPolicy: IfNotPresent

    ports:
    - name: http
      containerPort: 8080
      protocol: TCP

    env:
    - name: USER_ID
      value: "{owner_user_id}"
    - name: WORKSPACE_ID
      value: "{workspace_id}"
    - name: S3_WORKSPACE_BUCKET
      value: "agent-workspaces-{account_id}"
    - name: AWS_REGION
      value: "ap-northeast-1"
    - name: POD_NAME
      valueFrom:
        fieldRef:
          fieldPath: metadata.name
    - name: POD_NAMESPACE
      valueFrom:
        fieldRef:
          fieldPath: metadata.namespace

    volumeMounts:
    - name: cache
      mountPath: /tmp/agent-cache

    livenessProbe:
      httpGet:
        path: /health
        port: 8080
      initialDelaySeconds: 30
      periodSeconds: 10
      timeoutSeconds: 5
      failureThreshold: 3

    readinessProbe:
      httpGet:
        path: /readiness
        port: 8080
      initialDelaySeconds: 10
      periodSeconds: 5
      timeoutSeconds: 3
      failureThreshold: 3

    resources:
      requests:
        cpu: "200m"
        memory: "512Mi"
      limits:
        cpu: "1000m"
        memory: "2Gi"

  volumes:
  - name: cache
    emptyDir:
      sizeLimit: 500Mi

  restartPolicy: OnFailure
  terminationGracePeriodSeconds: 30
```

---

#### Service（ClusterIP，用於 Gateway 內部路由）

```yaml
apiVersion: v1
kind: Service
metadata:
  name: svc-ws-{workspace_id}
  namespace: agent-platform
  labels:
    workspace_id: "{workspace_id}"
    app: k8s-agent-platform
spec:
  type: ClusterIP
  selector:
    workspace_id: "{workspace_id}"
    component: agent
  ports:
  - name: http
    port: 80
    targetPort: 8080
    protocol: TCP
```

---

> **注意**：本架構不建立 per-user Ingress。對外入口統一由 Gateway 的單一 Ingress 處理（見 [04-K8s 資源](./04-kubernetes-resources.md) 的 Gateway Ingress 章節）。

---

#### ConfigMap（可選，儲存使用者配置）

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: user-{user_id}-config
  namespace: agent-platform
  labels:
    user_id: "{user_id}"
data:
  config.yaml: |
    user_id: {user_id}
    resource_tier: standard
    mcp_timeout_seconds: 300
    max_concurrent_tasks: 10
```

---

## 3. 資料流與狀態轉移

### 3.1 使用者工作區生命週期

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
├─ Database (Persistent) ─────────────────────────────────┤
│  users
│  └─ workspaces (status: active / idle / archived)
│     ├─ workspace_type: personal / group
│     ├─ s3_prefix: users/{id}/ 或 groups/{id}/
│     └─ workspace_members (存取控制)
│         ├─ owner   → 完整控制
│         ├─ admin   → 管理 + 使用
│         ├─ member  → 讀寫
│         └─ readonly → 唯讀
│
├─ Storage (AWS S3) ─────────────────────────────────────┤
│  s3://agent-workspaces-{account}/
│  ├─ users/{user_id}/data/              (共用資料，永久)
│  ├─ users/{user_id}/memories/          (跨 session 記憶，永久)
│  ├─ users/{user_id}/skills/            (技能定義，永久)
│  ├─ users/{user_id}/sessions/{sid}/    (session 專屬工作目錄，session 刪除時清除)
│  └─ groups/{group_id}/data/            (群組資料)
│
├─ K8s Resources ────────────────────────────────────────┤
│  Namespace: agent-platform
│  ├─ Pod: pod-ws-{workspace_id} (臨時，閒置時刪除)
│  ├─ Service: svc-ws-{workspace_id} (跟 Pod 一起刪除)
│  ├─ Orchestrator Deployment (永久)
│  └─ Gateway Deployment + Ingress (永久，單一對外入口)
│
├─ Session ──────────────────────────────────────────────┤
│  sessions 表
│  └─ 可多個 session 指向同一個 workspace
│  └─ last_active_at 決定是否被回收
│
└─────────────────────────────────────────────────────────┘
```

### 3.2 狀態機圖

```
        [First Login]
             │
             ▼
    ┌────────────────┐
    │ Check user_id  │
    │ in DB          │
    └────────┬───────┘
             │
      ┌──────┴──────┐
      ▼             ▼
   Exists      New User
      │             │
      ├─ Check ─────┤
      │ workspace   │
      │ exists      │
      │             │
  Exists       Create workspace
      │         + workspace_members(owner)
      │         + S3 prefix (首次寫入時建立)
      │             │
      │◄────────────┤
      │
      ├─ Check Pod exists?
      │
   ┌──┴──┐
   ▼     ▼
 Yes    No
   │     │
   │     ├─ Create Pod
   │     ├─ Create Service
   │     ├─ Agent initializes
   │     │
   │     ▼
   └────→ Pod Ready
          │
          ├─ Return endpoint
          ├─ mark_activity (last_active_at = now)
          │
          ▼
    ┌──────────────────┐
    │   30 min timer   │
    │   starts         │
    └────────┬─────────┘
             │
      ┌──────▼──────┐
      │ Activity?   │
      └──┬────────┬─┘
         │        │
       Yes       No
         │        │
    Continue   Timer runs
         │        │
         │    ┌───▼───┐
         │    │ 30min │
         │    │ passed│
         │    └───┬───┘
         │        │
         │        ├─ reap_idle_workspaces
         │        ├─ Delete Pod
         │        ├─ Delete Service
         │        ├─ Update DB: status=idle
         │        │
         │        ▼
         │    ┌──────────┐
         │    │ S3 kept  │
         │    │ Pod gone │
         │    └──────────┘
         │        │
         │   [User returns]
         │        │
         └────┬───┘
              │
              ├─ Find existing workspace (S3 data intact)
              ├─ Create new Pod
              ├─ Agent recovers from S3 + PostgreSQL
              │
              ▼
         Pod Ready (again)
```

---

## 4. 資料訪問權限

### 4.1 Workspace 存取控制流程

Gateway 在轉發請求前，必須驗證 `token 中的 user_id` 是否有權存取 `URL 中的 workspace_id`：

```
Client → POST /workspaces/{workspace_id}/{path}
                    │
    1. Token 驗證 → user_id
                    │
    2. 查 workspace_members:
       SELECT role FROM workspace_members
       WHERE workspace_id = ? AND user_id = ?
                    │
       ├─ 找到 → 根據 role 決定權限
       │   ├─ owner/admin/member → 完整存取（讀寫 + 執行）
       │   └─ readonly → 只允許 GET 請求
       │
       └─ 找不到 → 403 Forbidden
                    │
    3. 查 workspace → service endpoint
       (route_table 以 workspace_id 為 key)
                    │
    4. 轉發到 svc-ws-{workspace_id}:80
```

### 4.2 Row-Level Security (RLS) 與隔離

建議在 PostgreSQL 中啟用 RLS 策略：

```sql
-- 啟用 RLS
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE activity_logs ENABLE ROW LEVEL SECURITY;

-- 例：使用者只能查看自己有權存取的 workspaces
CREATE POLICY user_workspace_access ON workspaces
  USING (
    workspace_id IN (
      SELECT workspace_id FROM workspace_members
      WHERE user_id = current_user_id
    )
  );

CREATE POLICY user_session_access ON sessions
  USING (
    workspace_id IN (
      SELECT workspace_id FROM workspace_members
      WHERE user_id = current_user_id
    )
  );
```

### 4.3 應用層權限檢查

- **API Gateway**：驗證 token → `user_id`，查 `workspace_members` 確認存取權限
- **Orchestrator**：所有操作必須指定 `user_id` 與 `workspace_id`，驗證成員資格
- **Agent Pod**：信任 Gateway 已驗證，但可檢查 `X-User-Id` header 做日誌記錄
- 禁止跨 workspace 操作（除非是 workspace 成員）
- 記錄所有權限檢查失敗的嘗試至 `activity_logs`


