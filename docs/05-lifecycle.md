# 05 生命週期

本文件詳細描述工作區與 Session 的完整生命週期，包括各個階段的狀態轉移、操作流程與邊界情況。

## 1. 高層時間軸

```
Time ──────────────────────────────────────────────────────────────────►

[T0] User Login
     ├─ 驗證 Token → user_id
     ├─ 建立 session_id
     └─ 呼叫 ensure_session_workspace(user_id, session_id)

[T0+30s] Pod Ready
     ├─ S3 存取
     ├─ Agent 初始化
     ├─ Gateway 內部路由可用
     └─ 工作區可用

[T0+1min ~ T30min] Active Period
     ├─ 使用者執行任務
     ├─ mark_activity 每次請求時更新 last_active_at
     └─ 工作區持續運行

[T30min+1s] Idle Period Starts
     ├─ 無新活動
     ├─ last_active_at 停留在 30 分鐘前
     └─ reap_idle_workspaces 背景任務監測

[T30min+5min] Pod 回收
     ├─ 後台任務發現 last_active_at > 30 分鐘
     ├─ 刪除 Pod、Service（S3 資料保留）
     ├─ 更新 DB: pod_status = "terminated", status = "idle"
     └─ 資源釋放

[T30min+N hours] User Returns
     ├─ 使用者再次登入
     ├─ Token 驗證 → user_id
     ├─ 建立新 session_id（或複用舊的）
     └─ 呼叫 ensure_session_workspace(user_id, session_id)

[T30min+N hours+10s] Pod 恢復
     ├─ 查 DB：找既有 workspace
     ├─ K8s 建立新 Pod、Service
     ├─ Agent 透過 S3Backend 存取 S3 工作區
     ├─ 恢復應用狀態
     └─ last_active_at 重置為 now

[T30min+N hours+30s] Workspace Ready Again
     ├─ 使用者連線可用
     ├─ 資料完整無損
     └─ 循環重複
```

---

## 2. Session 生命週期狀態機

### 2.1 Session 狀態定義

| 狀態 | 含義 | Pod 狀態 | 可操作 |
|-----|------|---------|-------|
| `new` | 剛建立的 session，尚未對應 Pod | - | - |
| `initializing` | Orchestrator 正在建立 Pod 與其他資源 | pending | 否 |
| `active` | Pod 運行中，工作區可用 | running | 是 |
| `idle` | Pod 被回收（超過 30 分鐘無活動） | - | 有限（只能 reactivate） |
| `paused` | 使用者手動暫停（可選功能） | - | 否 |
| `terminated` | 使用者主動刪除或系統清理 | - | 否 |
| `error` | 資源建立失敗 | - | 否（需手動介入） |

### 2.2 狀態轉移圖

```
        ┌──────────┐
        │   new    │
        └────┬─────┘
             │
             ├─ ensure_session_workspace()
             │
             ▼
        ┌────────────────┐
        │ initializing   │
        └────┬───────────┘
             │
        ┌────┴────┐
        ▼         ▼
    Success   Failure
        │         │
        ▼         ▼
    ┌────────┐ ┌──────┐
    │ active │ │error │
    └───┬────┘ └──────┘
        │
        ├─────────────────────────┐
        │                         │
        ├─ 活動 → mark_activity()  │
        │ last_active_at = now     │
        │                         │
        ▼                         │
    ┌────────┐ ◄─────────────────┤
    │ active │                   │
    └────┬───┘                   │
         │                       │
         ├─ 30 min 無活動         │
         │                       │
         ▼                       │
    ┌──────┐                     │
    │ idle │ ◄───────────────────┘
    └────┬─┘
         │
         ├─ 使用者回訪
         ├─ ensure_session_workspace() 再次呼叫
         │
         ▼
    ┌────────────────┐
    │ initializing   │ (Pod 恢復)
    └────┬───────────┘
         │
         ▼
    ┌────────┐
    │ active │
    └────────┘
    
    ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ (Optional Termination)
    
    active → (用戶/管理員刪除)
        ↓
    terminated
```

---

## 3. Pod 生命週期狀態機

### 3.1 Pod 完整狀態

| 狀態 | 含義 | 是否計算資源 | 備註 |
|-----|------|-----------|------|
| `pending` | 資源調度中 | 否 | K8s 正在尋找合適節點 |
| `running` | 運行中 | 是 | 容器已啟動，接收流量 |
| `succeeded` | 執行完成 | 否 | （Agent Pod 不應到此狀態） |
| `failed` | 容器異常終止 | 否 | 需要診斷 |
| `unknown` | 狀態不明 | 否 | 網絡問題或 kubelet 故障 |
| `terminated` | 被刪除 | 否 | Pod 已不存在 |

### 3.2 Pod 建立流程

```
1. Orchestrator 接收 ensure_session_workspace(user_id, session_id)

2. 查 DB workspaces 表
   ├─ user_id 無對應 workspace
   │   ├─ 建立 workspace 記錄
   │   ├─ 建立 workspace_members 記錄（role=owner）
   │   └─ status = "active"
   │
   └─ user_id 有既有 workspace
       ├─ 檢查 workspace_members 存取權限
       └─ 使用既有 workspace 記錄

3. 查 K8s 中是否存在 user_id 對應的 Pod
   ├─ Pod 存在 & 運行中
   │   ├─ 直接返回 Pod 資訊
   │   └─ 跳過 Pod 建立
   │
   └─ Pod 不存在（首次或被回收）
       ├─ K8s.create_pod(...)
       │   ├─ 參數：workspace_id, pod_name, image, env, resources
       │   ├─ 返回 Pod 資訊 (name, uid, status=pending)
       │   └─ 等待 Pod 轉為 running
       │
       └─ K8s.create_service(...)
           └─ Service 指向新 Pod

4. 等待 Pod readiness
   ├─ GET /readiness probe 成功
   ├─ agent_container 初始化完成
   └─ status = "running"

5. 記錄到 DB
   ├─ sessions 表：session_id, pod_name, pod_status, created_at
   ├─ activity_logs 表：記錄 Pod 建立事件
   └─ pod_states 表：desired_state=running

6. 返回工作區資訊給客戶端
   ├─ gateway_endpoint: https://api.yourdomain.com
   ├─ gateway_route: /workspaces/ws_{user_id}
   ├─ pod_name, service_name
   └─ status: "ready"
```

---

## 4. 活動追蹤與自動回收

### 4.1 mark_activity 流程

每當使用者發起 API 請求、WebSocket 連線、MCP 任務時，系統應呼叫 `mark_activity`：

```
API 請求進入 API Gateway
    │
    ├─ Token 驗證 → user_id
    ├─ 提取 session_id
    │
    └─ 非認證請求則拒絕
    
    ▼

API Gateway 內部呼叫：
    Orchestrator.mark_activity(
        user_id=user_id,
        session_id=session_id,
        activity_type="http_request",
        metadata={
            endpoint: "/mcp/execute",
            method: "POST",
            timestamp: now
        }
    )

    │
    ├─ DB UPDATE sessions SET last_active_at = now WHERE session_id = ?
    ├─ 可選：INSERT activity_logs 記錄詳細
    └─ 返回成功

▼

業務邏輯執行
    │
    └─ 返回響應給客戶端
```

### 4.2 reap_idle_workspaces 流程

後台任務每 5 分鐘執行一次：

```
1. CronJob 觸發 reap_idle_workspaces() 
   
2. 連接 DB
   SELECT session_id, user_id, pod_name, last_active_at
   FROM sessions
   WHERE pod_status = 'running'
       AND last_active_at < now() - interval '30 minutes'

3. 遍歷每個需要回收的 session
   FOR EACH row IN result:
       
       a. 查詢使用者是否有其他活動的 session（同一 user_id）
          SELECT COUNT(*) FROM sessions
          WHERE user_id = row.user_id
              AND last_active_at >= now() - interval '30 minutes'
       
       IF count > 0:
           SKIP（使用者尚有其他活動 session，Pod 保持運行）
       
       ELSE:
           b. 刪除 Pod
              K8s.delete_pod(row.pod_name)
           
           c. 刪除 Service（或保留並置為 503）
              K8s.delete_service(service_name)
           
           d. 更新 DB
              UPDATE sessions SET
                  pod_status = 'terminated',
                  terminated_at = now
              WHERE session_id = row.session_id
              
              UPDATE workspaces SET
                  status = 'idle'
              WHERE user_id = row.user_id
           
           e. 記錄事件
              INSERT activity_logs (
                  user_id, activity_type, action, resource_type, resource_id
              ) VALUES (
                  row.user_id, 'pod_reaped', 'idle_termination', 'pod', row.pod_name
              )

4. 返回統計資訊
   {
       "workspaces_reaped": 5,
       "pods_deleted": 5,
       "timestamp": now
   }

5. 孤兒 workspace 狀態修復（每次掃描後自動執行）
   
   問題：workspace status 卡在 "active" 但實際上沒有 running session 也沒有 K8s Pod
   （例如 ensure 後 Pod 被外部刪除，Agent 無法發送 reap-self）
   
   修復流程：
   ├─ 單一 SQL 查詢：找出 status='active' 且無 pod_status='running' session 的 workspace
   ├─ 單一 K8s API call：list_pod_names()（label_selector 批次取得所有 managed Pod）
   ├─ 比對：若 workspace 對應的 Pod 不在 K8s 中 → 更新 status='idle'
   ├─ 批次 commit
   └─ 效能：O(1) API calls，不隨 workspace 數量增長
```

### 4.3 邊界情況

#### 情況 A：多 Session 共用 Pod，其中一個 Session 活動

```
Session_A: last_active_at = 10 min ago (活動)
Session_B: last_active_at = 40 min ago (閒置)
Session_C: last_active_at = 50 min ago (閒置)

Pod 對應 user_id 的 workspace

reap_idle_workspaces 檢查：
├─ 找所有 session last_active_at > 30 min
├─ 結果：Session_A（10 min ago）= 活動
├─ 決策：保留 Pod 運行
└─ 原因：使用者有活動 session，Pod 不應刪除

後續：只有 Session_A 標誌 Pod 為活動，Session_B/C 可安全忽略
```

#### 情況 B：Pod 啟動失敗

```
ensure_session_workspace() 呼叫 K8s.create_pod()

K8s 回傳：
{
    name: "pod-ws-user123",
    status: "pending",
    uid: "abc123"
}

等待 Pod running...
├─ 30 秒後：仍未進入 running
├─ 檢查 Pod 事件：ImagePullBackOff
├─ 決策：返回錯誤
│   {
│       error: "pod_startup_failed",
│       reason: "ImagePullBackOff",
│       pod_name: "pod-ws-user123"
│   }
│
└─ 更新 DB：pod_status = "failed", status = "error"

返回給客戶端 500 Internal Server Error，建議重試或聯繫管理員
```

#### 情況 C：S3 存取失敗

```
Pod 啟動後嘗試存取 S3 bucket/prefix

Agent 回傳錯誤：S3AccessDenied / S3ServiceUnavailable

重試邏輯：
├─ 等待 30 秒後重試
├─ 若 5 分鐘後仍未成功：
│   ├─ 診斷原因：
│   │   ├─ S3 存取權限？（IRSA 正確？Bucket 存在？）
│   │   ├─ 網路連線是否正常？
│   │   ├─ S3 服務是否故障？
│   ├─ 記錄詳細錯誤
│   └─ 返回 503 Service Unavailable
│
└─ 告警通知管理員
```

---

## 5. 工作區恢復流程

當 Pod 被回收後，使用者再次登入時的恢復過程：

```
1. 使用者再次登入
   POST /api/v1/workspaces/ensure
   {
       user_id: "user123",
       session_id: "sess_new_xyz"
   }

2. Orchestrator.ensure_session_workspace("user123", "sess_new_xyz")
   
3. 查 DB workspaces 表
   ├─ 找到既有 workspace
   │   ├─ workspace_id: ws_user123
   │   ├─ status: "idle"（因之前被回收）
   │   └─ resource_tier: "standard"

4. 檢查 K8s Pod
   ├─ pod-ws-user123 不存在（已被刪除）
   └─ 決策：重建 Pod

6. 建立新 Pod（同樣的配置，S3 透過 IRSA 存取）
   ├─ K8s.create_pod(
   │       workspace_id="ws_user123",
   │       ...
   │   )
   │
   ├─ Pod 啟動
   │   ├─ 容器啟動
   │   ├─ S3Backend 存取 s3://bucket/users/user123/
   │   └─ Agent runtime 初始化
   │       ├─ 建立 workspace 目錄結構：
   │       │   ├─ data/          （共用資料，永久）
   │       │   ├─ memories/      （跨 session 記憶，永久）
   │       │   ├─ skills/        （技能定義，永久）
   │       │   └─ sessions/      （各 session 專屬工作目錄）
   │       └─ 載入分層 system prompt（Global Policies → Workspace Prompt）
   │
   ├─ Agent 恢復流程
   │   ├─ 掃描 S3 workspace prefix
   │   ├─ 加載 config.yaml（若存在）
   │   ├─ 恢復應用狀態（模型、長期記憶等）
   │   ├─ 初始化 MCP 伺服器
   │   └─ readiness probe 通過
   
7. 建立 Service（若刪除）
   └─ svc-ws-user123

8. 更新 DB
   ├─ sessions 表：INSERT 新 session_id 記錄
   ├─ workspaces 表：status = "active", last_active_at = now
   ├─ activity_logs 表：記錄 workspace_resumed 事件
   └─ pod_states 表：更新 desired_state & actual_state

9. 返回工作區資訊
   {
       gateway_endpoint: "https://api.yourdomain.com",
       gateway_route: "/workspaces/ws_user123",
       status: "ready",
       pod_name: "pod-ws-user123",
       created_at: "2026-03-30T10:30:00Z"
   }

10. 使用者連線
    ├─ 通過 API Gateway
    ├─ Gateway 依 token 解析 user_id/workspace_id
    ├─ 內部路由到 svc-ws-user123
    ├─ Agent 接收連線
    └─ 透過 session_id 找到上次中斷的上下文（若有）
```

---

## 6. 清理與存檔流程（可選）

### 6.1 軟刪除（歸檔）

```
DELETE /api/v1/workspaces/{workspace_id}
Body: { keep_s3_data: true }

操作：
├─ 刪除 Pod、Service
├─ S3 資料保留
├─ 標記 workspace status = "archived"
├─ 活動日誌仍保留
└─ 用戶可在管理面板中恢復或永久刪除
```

### 6.2 Session 刪除（含資料夾清理）

```
DELETE /api/v1/sessions/{session_id}

操作：
├─ Orchestrator 建立 K8s cleanup Job
│   ├─ Job Pod 掛載 workspace PVC
│   ├─ 執行 rm -rf sessions/{session_id}/
│   └─ ttlSecondsAfterFinished=60（自動清理 Job Pod）
├─ 刪除 LangGraph checkpoint 資料（對話歷史）
├─ 刪除活動日誌
├─ 刪除 session DB 記錄
└─ Production：改為 S3 prefix delete（sessions/{session_id}/）
```

### 6.3 硬刪除

```
DELETE /api/v1/workspaces/{workspace_id}
Body: { keep_s3_data: false }

操作：
├─ 刪除 Pod、Service
├─ 刪除 S3 資料（可選）
├─ 標記 workspace status = "deleted"
├─ 保留審計日誌
└─ 無法恢復
```

---

## 7. 故障與恢復

### 7.1 Orchestrator 故障

```
若 Orchestrator 實例崩潰：

前提：Orchestrator 是多副本部署
├─ Deployment replicas: 2+
├─ 使用 leader election 協調（若需單點寫入）

故障時：
├─ 崩潰的副本被 K8s 自動重啟
├─ 其他副本接管請求
├─ 已建立的 Pod 継續運行（不受影響）
├─ 進行中的操作可能中斷
│   ├─ 客戶端重試
│   ├─ 幂等性設計確保安全
│   └─ DB 事務保證一致性

恢復步驟：
├─ 重啟的副本連接 DB
├─ 查詢所有活動 session
├─ 同步 K8s 狀態與 DB 狀態
├─ 修復不一致（若有）
└─ 恢復服務
```

### 7.2 K8s 節點故障

```
若運行 Agent Pod 的節點故障：

K8s 自動故障轉移：
├─ kubelet 無法回應（超時）
├─ API server 標記 Node NotReady
├─ Pod 被驅逐到其他節點（若設置了 PodDisruptionBudget）
│   或進入 Terminating 狀態

可能的結果：
├─ Option A：Pod 重新調度到其他節點
│   ├─ S3 無 AZ 限制，新 Pod 可直接存取同一 S3 資料
│   ├─ Pod 在新節點透過 IRSA 存取 S3
│   ├─ 應用狀態恢復
│   └─ 對使用者接近無感知（可能有短暫中斷）
│
└─ Option B：Pod 無法移動（資源不足）
    ├─ Pod 進入 Terminating → Terminated
    ├─ Orchestrator 檢測到 Pod 丟失
    ├─ 在有效節點上重建 Pod
    └─ S3 資料自動可用，無需額外操作

建議：
├─ 使用 S3 為 workspace 儲存（無 AZ 限制）
├─ 設置 PodDisruptionBudget 保護關鍵 Pod
├─ 配置自動節點修復
└─ 監控節點狀態
```

### 7.3 S3 存取異常

```
檢測：
├─ Pod 啟動但 S3Backend 初始化失敗
├─ 日誌：AccessDenied / NoSuchBucket / Timeout
└─ readiness probe 回傳失敗

恢復選項：

A. IRSA 配置錯誤
   ├─ 檢查 ServiceAccount annotation（eks.amazonaws.com/role-arn）
   ├─ 檢查 IAM Role 信任策略（OIDC provider）
   ├─ 檢查 IAM Policy 權限（s3:GetObject 等）
   └─ 修正後重建 Pod

B. S3 Bucket 不存在或權限不足
   ├─ 確認 bucket 名稱正確
   ├─ 檢查 bucket policy
   ├─ 確認 IAM Role 有 s3:ListBucket 權限
   └─ 建立缺失的 S3 prefix

防護措施：
├─ S3 提供 99.999999999%（11 個 9）資料耐久性
├─ 啟用 S3 版本控制追蹤變更
├─ 啟用 S3 跨區域複製（災難恢復）
├─ 監控 S3 存取錯誤率
└─ 告警 IRSA 認證失敗
```

---

## 8. 時間敏感的邊界情況

### 8.1 Race Condition：Pod 回收與新請求

```
時間軸：
T0: last_active_at = 30 min ago
T1: reap_idle_workspaces() 檢查開始
    ├─ 決定刪除 Pod
    └─ K8s.delete_pod() 呼叫已送出

T2: 使用者發起新請求
    ├─ API Gateway 收到
    ├─ 呼叫 mark_activity()
    ├─ 更新 DB: last_active_at = now
    └─ 但 Pod 正在被刪除...

T3: Pod 完全刪除

T4: ensure_session_workspace() 呼叫
    ├─ 檢查 Pod：不存在
    ├─ 建立新 Pod
    └─ 工作區恢復

解決方案（PostgreSQL Advisory Lock）：
├─ ensure 與 reap 都先取得同一 workspace 的 advisory lock
├─ reaper 拿鎖後再次檢查 last_active_at，若有新活動則跳過回收
├─ ensure 拿鎖後再檢查 Pod 狀態，避免重複建立
└─ 詳見下方 8.3 併發控制設計
```

### 8.2 Session 快速登出登入

```
T0: Session_A 登入，Pod 建立，last_active_at = T0
T5: Session_A 登出（未顯式調用 logout API）
T10: Session_A 停止發送任何請求
T35: reap 檢查，發現 last_active_at = T0 < 30 min，刪除 Pod

T36: 使用者（同一用戶）以新 Session_B 登入
T36+10s: ensure_session_workspace(user_id, session_B)
        └─ 發現 Pod 已刪除但 workspace 記錄存在
        └─ 從 S3 + PostgreSQL 恢復，重建 Pod

優化：
├─ 可選在 logout 時立即標記 session 為 "inactive"
├─ reaper 可選擇保留最近的 N 個 session 對應的 Pod
├─ 或設置更短的準備時間（如改為 60 分鐘）
└─ 依據業務需求在成本與用戶體驗間平衡
```

### 8.3 併發控制設計（PostgreSQL Advisory Lock）

#### 問題場景

Orchestrator 多副本部署時，同一 workspace 的操作可能併發執行：

```
場景 A：重複建立 Pod
├─ Session_1 ensure: 查 DB → workspace 不存在 → 準備建立
├─ Session_2 ensure: 查 DB → workspace 不存在 → 準備建立（同時進來）
└─ 結果：重複建立 Pod、workspace 記錄

場景 B：Reaper 與 Ensure 競爭
├─ Reaper: 發現 idle → 拿到 workspace → 開始刪 Pod
├─ User 回訪: ensure → 查到 Pod 存在 → 返回端點
└─ 結果：端點指向正在被刪除的 Pod

場景 C：K8s 操作成功但 DB 失敗
├─ ensure: K8s create_pod 成功
├─ ensure: DB commit 失敗（網路中斷）
└─ 結果：K8s 有孤兒 Pod，DB 不知道
```

#### 解法：Per-Workspace Advisory Lock

使用 PostgreSQL 內建的 `pg_advisory_xact_lock`，以 workspace_id 為粒度，確保同一 workspace 的操作序列化執行。不同 workspace 的操作完全不互相阻塞。

```python
import hashlib
from sqlalchemy import text

def _workspace_lock_key(workspace_id: str) -> int:
    """將 workspace_id 轉為 advisory lock key（int32）"""
    digest = hashlib.md5(workspace_id.encode()).digest()
    return int.from_bytes(digest[:4], "big") % (2**31)
```

#### ensure_session_workspace 併發控制

```python
async def ensure_session_workspace(
    self, db: AsyncSession, user_id: str, session_id: str | None = None, ...
) -> dict:
    workspace_id = self._workspace_id(user_id)
    lock_key = _workspace_lock_key(workspace_id)

    # 1) 取得 per-workspace advisory lock（同 workspace 排隊，不同 workspace 不阻塞）
    await db.execute(text(f"SELECT pg_advisory_xact_lock({lock_key})"))

    # 2) 鎖內操作：查/建 workspace、檢查 Pod 狀態
    workspace = await self._get_or_create_workspace(db, user_id, workspace_id)
    pod_name = self._pod_name(workspace_id)

    # 3) K8s 操作（冪等：409 Conflict 視為成功）
    pod_created = False
    if not self.k8s.pod_exists(pod_name):
        try:
            self.k8s.create_pod(workspace_id, pod_name)
            pod_created = True
        except ApiException as e:
            if e.status != 409:  # 409 = Already exists，冪等
                raise

    if not self.k8s.service_exists(service_name):
        try:
            self.k8s.create_service(workspace_id, service_name)
        except ApiException as e:
            if e.status != 409:
                raise

    # 4) 等待 Pod readiness（新建 Pod 時）
    status = "ready"
    if pod_created:
        status = await self.k8s.wait_for_pod_ready(
            pod_name, timeout_seconds=120
        )
        # status: "ready" | "starting"（超時但 Pod 仍在啟動中）

    # 5) 寫入 session、pod_states、activity_log
    ...

    # 6) commit 時自動釋放 advisory lock
    await db.commit()
    return {"status": status, ...}
```

#### reap_user_workspace 併發控制

```python
async def reap_user_workspace(
    self, db: AsyncSession, workspace_id: str, reason: str = "idle",
) -> dict | None:
    lock_key = _workspace_lock_key(workspace_id)

    # 1) 取得同一把 advisory lock
    await db.execute(text(f"SELECT pg_advisory_xact_lock({lock_key})"))

    # 2) 拿鎖後再次檢查 — 如果剛被 ensure 更新過，就不 reap
    result = await db.execute(
        select(Session)
        .where(Session.workspace_id == workspace_id, Session.pod_status == "running")
        .order_by(Session.last_active_at.desc())
    )
    latest = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)

    if latest and (now - latest.last_active_at).total_seconds() < idle_threshold_seconds:
        await db.commit()  # 釋放鎖
        return None  # 有新活動，跳過回收

    # 3) 確認閒置，執行回收（K8s 會自動觸發 graceful shutdown）
    #    流程：K8s delete_pod → preStop hook (/shutdown) → SIGTERM → Agent 保存工作 → Pod 終止
    #    terminationGracePeriodSeconds=30 確保 Agent 有足夠時間完成 shutdown
    self.k8s.delete_pod(pod_name)       # K8s 處理 preStop + SIGTERM
    self.k8s.delete_service(service_name)
    ...

    await db.commit()  # 釋放鎖
    return {"workspace_id": workspace_id, "reason": reason}
```

#### K8s 冪等操作

所有 K8s 資源操作都加上 409 Conflict 處理，確保冪等性：

```python
from kubernetes.client.exceptions import ApiException

class K8sClient:
    def create_pod_idempotent(self, workspace_id: str, pod_name: str, ...):
        try:
            self.core_v1.create_namespaced_pod(namespace=self.ns, body=pod_spec)
        except ApiException as e:
            if e.status == 409:  # Already exists — 冪等，不是錯誤
                logger.info("Pod %s already exists, skipping create", pod_name)
                return
            raise

    def create_service_idempotent(self, workspace_id: str, service_name: str, ...):
        try:
            self.core_v1.create_namespaced_service(namespace=self.ns, body=svc_spec)
        except ApiException as e:
            if e.status == 409:
                logger.info("Service %s already exists, skipping create", service_name)
                return
            raise

    async def wait_for_pod_ready(self, pod_name: str, timeout_seconds: int = 120) -> str:
        """等待 Pod readiness probe 通過。

        Returns:
            "ready" — Pod 已就緒，可接收流量
            "starting" — 超時但 Pod 仍在啟動中（未失敗）
        Raises:
            K8sResourceError — Pod 進入 Failed/CrashLoopBackOff 等不可恢復狀態
        """
        import asyncio
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            pod = self.core_v1.read_namespaced_pod(pod_name, self.ns)

            # 檢查是否進入不可恢復狀態
            if pod.status.phase in ("Failed", "Unknown"):
                raise K8sResourceError(f"Pod {pod_name} entered {pod.status.phase}")

            # 檢查 container 是否 CrashLoopBackOff
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff":
                        raise K8sResourceError(f"Pod {pod_name} CrashLoopBackOff")

            # 檢查 Ready condition
            if pod.status.conditions:
                for cond in pod.status.conditions:
                    if cond.type == "Ready" and cond.status == "True":
                        logger.info("Pod %s is ready", pod_name)
                        return "ready"

            await asyncio.sleep(2)

        logger.warning("Pod %s not ready after %ds, returning 'starting'", pod_name, timeout_seconds)
        return "starting"
```

#### 孤兒資源 Reconciler

背景任務定期比對 DB 與 K8s 實際狀態，清理不一致：

```python
async def reconcile_orphans(self, db: AsyncSession):
    """每 10 分鐘執行一次，清理 DB/K8s 不一致的孤兒資源"""
    # 1) K8s 有 Pod 但 DB 無對應 workspace → 刪除孤兒 Pod
    k8s_pods = self.k8s.list_agent_pods()
    for pod in k8s_pods:
        ws_id = pod.metadata.labels.get("workspace_id")
        db_ws = await db.execute(
            select(Workspace).where(Workspace.workspace_id == ws_id)
        )
        if not db_ws.scalar_one_or_none():
            logger.warning("Orphan pod %s (no DB record), deleting", pod.metadata.name)
            self.k8s.delete_pod(pod.metadata.name)

    # 2) DB 記錄 pod_status=running 但 K8s Pod 不存在 → 更新 DB 狀態
    running_sessions = await db.execute(
        select(Session).where(Session.pod_status == "running")
    )
    for session in running_sessions.scalars():
        if not self.k8s.pod_exists(session.pod_name):
            session.pod_status = "terminated"
            logger.warning("DB/K8s mismatch: %s marked terminated", session.pod_name)

    await db.commit()
```

#### 設計要點總結

```
┌─────────────────────────────────────────────────────────────┐
│  併發控制三層防護                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Layer 1: PostgreSQL Advisory Lock (per workspace_id)       │
│  ├─ ensure 與 reap 共用同一把鎖                               │
│  ├─ 不同 workspace 完全不阻塞                                 │
│  ├─ Transaction commit 時自動釋放                             │
│  └─ 零額外基礎設施（PostgreSQL 內建）                          │
│                                                             │
│  Layer 2: K8s 冪等操作                                       │
│  ├─ create_pod/create_service 捕獲 409 Conflict              │
│  ├─ 即使 advisory lock 失效，也不會建立重複資源                 │
│  └─ 防禦性程式碼，不依賴鎖的正確性                              │
│                                                             │
│  Layer 3: 背景 Reconciler                                    │
│  ├─ 每 10 分鐘比對 DB 與 K8s 實際狀態                         │
│  ├─ 清理孤兒 Pod（K8s 有但 DB 無）                            │
│  ├─ 修正 DB 狀態（DB 說 running 但 K8s Pod 不存在）            │
│  └─ 最終一致性保證                                            │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

> **POC 不實作併發控制**（單副本，無多 replica 競爭問題）。Phase 1 Production Orchestrator 實作時加入。
