# K8s 多 Session Agent 工作區平台

## 系統概述

一個基於 Kubernetes 的使用者專屬 Agent 工作區管理系統。支援使用者透過 API Gateway 登入，由系統自動在 K8s 中建立/恢復專屬的 Agent Pod。對外僅暴露單一 Gateway 入口（例如 `api.yourdomain.com`）；Gateway 透過 IM Channel 抽象層（或直接 proxy）路由到對應 Agent Pod。同一 workspace 的多個 session 共用同一個 Pod，閒置後自動回收 Pod 但透過 PVC 保留資料（Production 改用 AWS S3），使用者回訪時快速恢復工作區。支援個人與群組兩種 workspace 類型。

## 實作狀態

| 階段 | 狀態 | 說明 |
|------|------|------|
| POC | ✅ 已完成 | 全部容器化部署於 K8s，Supervisor + Sub-Agent 架構（langgraph-supervisor），E2E 測試通過 |
| Channel Layer | ✅ 已完成 | IM channel 抽象層（WebChannel），`/api/v1/chat` endpoint |
| Admin Service | ✅ 已完成 | 獨立 Pod (:8090, ClusterIP)，Gateway proxy `/api/v1/admin/*` 統一入口，user/workspace/member 管理 |
| Storage Service | ✅ 已完成 | 獨立 Pod (:8091, ClusterIP)，Gateway proxy `/api/v1/workspaces/{wid}/storage/*`，workspace storage CRUD + 檔案操作（雙模式）+ 存取權限 |
| Scheduler Service | 📐 設計完成 | 排程服務設計文件（Draft），定時任務、cron、通知管道、工具風險分級 |
| Phase 1 | 未開始 | Production Orchestrator + PostgreSQL + Alembic |
| Phase 2 | 📐 設計完成 | LangChain Deep Agents 容器 + S3Backend + PostgresStore |
| Phase 3 | 未開始 | Production API Gateway + JWT/OAuth2 + Slack/LINE/Teams adapters |
| Phase 4 | 未開始 | Helm、監控、TLS、負載測試 |

## 核心設計決策

| 決策項 | 選擇 | 原因 |
|------|------|------|
| Session 識別 | session_id + user_id | API Gateway 驗證 token 後反查 user_id，Orchestrator 負責 session 管理 |
| 入口策略 | 單一公開 Gateway + IM Channel 抽象層 | 對外只暴露 `api.yourdomain.com`，透過 Channel Layer 或直接 proxy 路由 |
| 資料歸屬 | workspace 對應 PVC（POC）/ S3 prefix（Production） | 個人：`users/{user_id}/`；群組：`groups/{group_id}/`（永久保留，跨 Pod） |
| Pod 生命週期 | 多 session 共用單 Pod | 同一 workspace 的多個 session 連線進同一個 Pod 內執行任務 |
| 恢復策略 | PVC + PostgreSQL 持久化，Pod 可回收 | Pod 因閒置被刪除後，下次 session 登入從 PVC + PostgreSQL checkpointer 恢復狀態 |
| 並發控制 | Pod 內 session 無需分散鎖 + Orchestrator 用 PG Advisory Lock | Pod 內多 session 同進程處理；Orchestrator 多副本透過 per-workspace advisory lock 序列化操作 |
| Gateway 路由 | ChannelManager route table + 自動恢復 | Channel 層自動 ensure workspace；直接 proxy 需先 ensure |
| Pod 就緒 | Orchestrator 等待 readiness | ensure 建立 Pod 後輪詢 K8s Ready condition（最多 120s），確保 Gateway 轉發時 Pod 已就緒 |
| 優雅關閉 | preStop hook + SIGTERM + drain | Pod 被回收時：preStop 通知 → 拒絕新請求 → 等待進行中請求完成 → 保存狀態到 PVC → 釋放連線 |
| IM 抽象 | Channel Layer (inspired by deer-flow) | MessageBus pub/sub 解耦 IM 平台與 Agent 邏輯，pluggable adapters |
| Agent 架構 | Supervisor + Sub-Agent（langgraph-supervisor） | Supervisor 分派任務給 research_agent（搜尋）+ code_agent（執行），避免 LLM 無限 tool calling |

## 架構層次

```
┌──────────────────────────────────────────────────────────────────────┐
│   Public Ingress / API Gateway (`api.yourdomain.com`)                │
│   (Token 驗證 → user_id → workspace ACL → 路由)                       │
│                                                                      │
│   ┌─────────────────────────────────────────────────────────┐        │
│   │  Channel Layer (IM 抽象層)                                │        │
│   │  WebChannel → MessageBus → ChannelManager               │        │
│   │  (自動 ensure workspace + 路由到 Agent Pod)               │        │
│   └─────────────────────────────────────────────────────────┘        │
└───────┬─────────────────────────────────────┬────────────────────────┘
        │                                     │
        │ (1) ensure_session_workspace        │ (3) 轉發使用者請求
        ▼                                     │
   ┌──────────────────────┐                   │
   │  Orchestrator        │                   ▼
   │  Service             │         ┌──────────────────────────────┐
   │                      │         │  K8s Internal Service        │
   │ • ensure_session     │         │  (svc-{workspace_id})        │
   │   _workspace         │         └──────────────┬───────────────┘
   │ • reap_workspace     │                        │
   │ • mark_activity      │                        ▼
   └──┬───────────┬───┬───┘       ┌──────────────────────────────────┐
      │           │   │           │  Agent Pod (per workspace_id)    │
      │           │   │           │  ┌──────────────────────────────┐│
      │           │   │           │  │ Supervisor (langgraph-supervisor)││
 (2a) │      (2b) │   │(1')       │  │ • research_agent (搜尋)      ││
 狀態  │      管理  │  │回傳端點     │  │ • code_agent (執行)          ││
 持久化│      K8s  │   │資訊給      │  │ • FastAPI HTTP Layer         ││
      │      資源  │   │Gateway    │  │ • MCP 相容介面                ││
      │           │   │           │  └──────────────────────────────┘│
      ▼           ▼   ▲           │  Storage: PVC (POC) / S3 (Prod) │
 ┌────────────┐ ┌────────────────┐│  Checkpointer: PostgreSQL       │
 │Database    │ │K8s Control     │└──────────────────────────────────┘
 │(PgSQL)     │ │Plane           │
 │            │ │(Kubernetes API)│
 │• users     │ │• Pod/Service   │
 │• sessions  │ │• PVC           │
 │• work-     │ │• Namespace     │
 │  spaces    │ │  /RBAC         │
 │• activity  │ │                │
 │  _logs     │ │                │
 │• pod_states│ │                │
 │• workspace │ │                │
 │  _members  │ │                │
 │• check-    │ │                │
 │  points    │ │                │
 └────────────┘ └────────────────┘

                ┌──────────────────────────────┐
                │  Admin Service Pod (:8090)    │
                │  (ClusterIP, Gateway proxy)   │
                │                              │
                │ • User CRUD                  │
                │ • Workspace 查詢              │
                │ • workspace_members 管理      │
                │ • Reap 觸發                   │
                │                              │
                │ 透過 Orchestrator API 操作     │
                │ (不直接碰 K8s Control Plane)   │
                └──────────────────────────────┘

                ┌──────────────────────────────┐
                │  Storage Service Pod (:8091)   │
                │  (ClusterIP, Gateway proxy)   │
                │                              │
                │ • Workspace Storage CRUD     │
                │ • 檔案操作（雙模式）           │
                │   - Pod 在線: proxy Agent     │
                │   - Pod 離線: K8s Job         │
                │ • 存取權限 (workspace_members) │
                │                              │
                │ 直接操作 K8s API + DB          │
                │ user token + admin token      │
                └──────────────────────────────┘
```

**架構要點：**
- **(1)** API Gateway 呼叫 Orchestrator 的 `ensure_session_workspace`
- **(1')** Orchestrator 回傳內部 Service 端點資訊（如 `svc-{workspace_id}:80`）給 Gateway
- **(2a)** Orchestrator 讀寫 Database 做狀態持久化
- **(2b)** Orchestrator 透過 K8s API 管理 Pod、Service、PVC 等資源
- **(3)** API Gateway 透過 Channel Layer 或直接 proxy 將使用者請求轉發到 Agent Pod
- Database 與 Agent Pod **共用 PostgreSQL**：Orchestrator 管理業務表，Agent 使用 LangGraph 的 checkpointer 表
- API Gateway **不直接操作** K8s Control Plane

## 使用者進入架構
```
  外部 Client → api.yourdomain.com (唯一對外入口)
                      │
                      │ (叢集內部，Client 看不到以下部分)
                      ▼
                API Gateway Pod (Token 驗證 → Channel Layer / Direct Proxy)
                      │
                      ▼
                svc-{workspace_id}:80 (ClusterIP, 叢集內部 DNS)
                      │
                      ▼
                Agent Pod (pod-{workspace_id})
```

## API 端點

| Method | Endpoint | 說明 |
|--------|----------|------|
| GET | `/health` | Gateway 健康檢查 |
| POST | `/api/v1/workspaces/ensure` | 建立或恢復工作區 |
| GET | `/api/v1/workspaces/{workspace_id}` | 查詢工作區狀態 |
| POST | `/api/v1/chat` | **推薦** 對話（經 Channel 抽象層，自動 ensure） |
| GET | `/api/v1/sessions` | 列出使用者所有 session |
| GET | `/api/v1/sessions/{id}/messages` | 對話歷史（從 PostgreSQL checkpointer） |
| GET | `/api/v1/sessions/{id}/history` | 活動紀錄（API 操作 log） |
| DELETE | `/api/v1/sessions/{id}` | 刪除 session（含對話歷史） |
| POST | `/workspaces/{wid}/api/v1/chat` | 對話（直接 proxy） |
| POST | `/workspaces/{wid}/api/v1/chat/stream` | 串流對話（SSE） |
| POST | `/workspaces/{wid}/mcp/execute` | MCP 檔案操作 |
| POST | `/workspaces/{wid}/api/v1/files/upload` | 上傳檔案至工作區 PVC |
| GET | `/workspaces/{wid}/api/v1/files/download` | 從工作區 PVC 下載檔案 |
| GET | `/workspaces/{wid}/api/v1/files/list` | 列出工作區目錄 |
| DELETE | `/workspaces/{wid}/api/v1/files/delete` | 刪除工作區檔案或目錄 |
| POST | `/workspaces/{wid}/api/v1/files/mkdir` | 新增工作區目錄 |
| POST | `/api/v1/admin/reap` | 管理員：手動回收 |
| GET | `/api/v1/admin/channels` | 管理員：Channel 狀態 |

### Admin Service API（透過 Gateway proxy，`/api/v1/admin/*`）

> Admin token: `Authorization: Bearer $POC_ADMIN_TOKEN`
> Base URL 與 Gateway 相同：`http://localhost:8000`

| Method | Endpoint | 說明 |
|--------|----------|------|
| GET | `/api/v1/admin/users` | 列出所有使用者（分頁） |
| GET | `/api/v1/admin/users/{user_id}` | 使用者詳情 |
| PUT | `/api/v1/admin/users/{user_id}/active` | 啟用/停用使用者 |
| GET | `/api/v1/admin/workspaces` | 列出所有工作區（分頁、篩選） |
| GET | `/api/v1/admin/workspaces/{workspace_id}` | 工作區詳情 |
| POST | `/api/v1/admin/reap` | 手動觸發 Pod 回收 |
| GET | `/api/v1/admin/workspaces/{wid}/members` | 列出工作區成員 |
| POST | `/api/v1/admin/workspaces/{wid}/members` | 新增工作區成員 |
| PUT | `/api/v1/admin/workspaces/{wid}/members/{uid}` | 更新成員角色 |
| DELETE | `/api/v1/admin/workspaces/{wid}/members/{uid}` | 移除工作區成員 |
| DELETE | `/api/v1/admin/workspaces/{workspace_id}` | 刪除工作區（PVC + DB + K8s） |
| GET | `/api/v1/admin/pods/status` | Pod 狀態總覽 |

詳見 [前端串接 API 文件](./docs/frontend-api.md)。

### Storage Service API（透過 Gateway proxy，`/api/v1/workspaces/{wid}/storage/*`）

> User token: `Authorization: Bearer $POC_STATIC_TOKEN:{user_id}`（操作自己有權限的 workspace）
> Admin token: `Authorization: Bearer $POC_ADMIN_TOKEN`（操作所有 workspace）
> Base URL 與 Gateway 相同：`http://localhost:8000`

| Method | Endpoint | 說明 |
|--------|----------|------|
| GET | `/api/v1/workspaces/storage` | Workspace storage 列表（admin 全部 / user 自己有權限的） |
| POST | `/api/v1/workspaces/storage` | 建立 group workspace + PVC（admin only） |
| POST | `/api/v1/workspaces/storage/ensure` | 確保 workspace PVC 存在（Orchestrator 呼叫） |
| PUT | `/api/v1/workspaces/{wid}/rename` | 修改 workspace 顯示名稱 |
| GET | `/api/v1/workspaces/{wid}/storage/files` | 列出 workspace 檔案 |
| POST | `/api/v1/workspaces/{wid}/storage/files/upload` | 上傳檔案到 workspace |
| GET | `/api/v1/workspaces/{wid}/storage/files/download` | 從 workspace 下載檔案 |
| DELETE | `/api/v1/workspaces/{wid}/storage/files` | 刪除 workspace 檔案或目錄 |
| POST | `/api/v1/workspaces/{wid}/storage/files/mkdir` | 建立 workspace 目錄 |
| GET | `/api/v1/workspaces/{wid}/storage/access` | Workspace 存取權限（workspace members） |

詳見 [前端串接 API 文件](./docs/frontend-api.md)。

## 文檔導航

- [01-需求規格](./docs/01-requirements-spec.md) - 完整功能需求與非功能需求
- [02-API 設計](./docs/02-api-design.md) - HTTP API、內部 RPC、K8s 操作接口
- [03-資料模型](./docs/03-data-model.md) - 資料庫 schema、K8s 資源對應
- [04-K8s 資源](./docs/04-kubernetes-resources.md) - Namespace、Deployment、Service、Ingress、RBAC
- [05-生命週期](./docs/05-lifecycle.md) - Session 建立、Pod 恢復、閒置回收、清理流程
- [06-Agent 容器](./docs/06-agent-container.md) - LangChain Deep Agents runtime、FastAPI HTTP、MCP 相容、三層記憶架構
- [07-部署指南](./docs/07-deployment.md) - 本地開發、Helm 部署、監控與日誌
- [08-故障恢復](./docs/08-fault-recovery.md) - 異常情況處理、重試策略、災難恢復
- [09-Agent 工具](./docs/09-agent-tools.md) - 工具分組、沙箱機制、CWA 氣象查詢、新增工具指南
- [10-排程服務](./docs/10-scheduler-service.md) - 定時任務、cron 表達式、通知管道、工具風險分級（Draft）
- [前端串接 API](./docs/frontend-api.md) - 前端串接完整 API 文件

## 快速開始（POC）

### 前置要求
- Python 3.13+
- Docker 或 Podman
- [kind](https://kind.sigs.k8s.io/)（Kubernetes in Docker）
- kubectl
- PostgreSQL 14+（本機或容器）

### 一鍵部署

```bash
# 建立叢集、構建鏡像、部署所有服務
bash poc/k8s/deploy.sh
```

### 手動部署

```bash
# 1. 建立 kind 叢集（含 NodePort mapping）
KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name agent-poc --config poc/k8s/kind-config.yaml

# 2. 部署 Namespace + RBAC
kubectl apply -f poc/k8s/namespace-rbac.yaml

# 3. 構建五個鏡像
docker build -t k8s-agent-runtime:latest -f poc/docker/Dockerfile.agent .
docker build -t k8s-orchestrator:latest -f poc/docker/Dockerfile.orchestrator .
docker build -t k8s-gateway:latest -f poc/docker/Dockerfile.gateway .
docker build -t k8s-admin:latest -f poc/docker/Dockerfile.admin .
docker build -t k8s-storage-service:latest -f poc/docker/Dockerfile.storage .

# 4. 載入鏡像到 kind
export KIND_EXPERIMENTAL_PROVIDER=podman
kind load docker-image k8s-agent-runtime:latest --name agent-poc
kind load docker-image k8s-orchestrator:latest --name agent-poc
kind load docker-image k8s-gateway:latest --name agent-poc
kind load docker-image k8s-admin:latest --name agent-poc
kind load docker-image k8s-storage-service:latest --name agent-poc

# 5. 部署 Orchestrator + Gateway + Admin + Storage Service
kubectl apply -f poc/k8s/orchestrator.yaml
kubectl apply -f poc/k8s/gateway.yaml
kubectl apply -f poc/k8s/admin.yaml
kubectl apply -f poc/k8s/storage-service.yaml

# 6. 驗證
kubectl get pods,svc -n agent-platform
```

### 測試

```bash
# 健康檢查
curl -s http://localhost:8000/health

# 建立工作區（Token 格式：$POC_STATIC_TOKEN:{user_id}）
curl -s -X POST http://localhost:8000/api/v1/workspaces/ensure \
  -H "Authorization: Bearer $POC_STATIC_TOKEN:testuser1" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "sess-001"}'

# 透過 Channel 層對話（推薦）
curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $POC_STATIC_TOKEN:testuser1" \
  -H "Content-Type: application/json" \
  -d '{"message":"你好","session_id":"sess-001"}'

# 透過 Gateway proxy 呼叫 Agent MCP
curl -s -X POST http://localhost:8000/workspaces/ws-testuser1/mcp/execute \
  -H "Authorization: Bearer $POC_STATIC_TOKEN:testuser1" \
  -H "Content-Type: application/json" \
  -d '{"method":"list_files","params":{"path":"data"}}'

# 列出 session
curl -s http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer $POC_STATIC_TOKEN:testuser1"

# 查看對話歷史
curl -s http://localhost:8000/api/v1/sessions/sess-001/messages \
  -H "Authorization: Bearer $POC_STATIC_TOKEN:testuser1"

# 自動化 E2E 測試
bash poc/tests/e2e_test.sh

# 清理
KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name agent-poc
```

## 目錄結構

```
dbt-openclaw/
├── README.md                          # 本文件
├── CLAUDE.md                          # Claude Code 指引
├── docs/                              # 設計文檔（01-10）+ 前端 API 文件
│   ├── 01-requirements-spec.md
│   ├── 02-api-design.md
│   ├── 03-data-model.md
│   ├── 04-kubernetes-resources.md
│   ├── 05-lifecycle.md
│   ├── 06-agent-container.md
│   ├── 07-deployment.md
│   ├── 08-fault-recovery.md
│   ├── 09-agent-tools.md             # Agent 工具（分組、沙箱、CWA 氣象、新增指南）
│   ├── 10-scheduler-service.md       # 排程服務（定時任務、cron、通知、風險分級）— Draft
│   └── frontend-api.md               # 前端串接 API 文件
├── poc/                               # POC 實作（已驗證）
│   ├── gateway/
│   │   ├── main.py                    # API Gateway (:8000)
│   │   └── channels/                  # IM Channel 抽象層
│   │       ├── base.py                # Channel ABC
│   │       ├── message_bus.py         # MessageBus + InboundMessage/OutboundMessage
│   │       ├── manager.py             # ChannelManager dispatcher
│   │       ├── store.py               # ChannelStore (chat→session 映射)
│   │       ├── service.py             # ChannelService lifecycle
│   │       └── adapters/
│   │           └── web.py             # WebChannel adapter
│   ├── orchestrator/
│   │   ├── main.py                    # Orchestrator API (:8080) + reaper + orphan repair + admin data APIs
│   │   ├── service.py                 # 核心邏輯（PVC 建立委派 Storage Service）
│   │   └── k8s_client.py             # K8s Pod/Service 操作
│   ├── storage/
│   │   ├── main.py                    # Storage Service (:8091, ClusterIP, Gateway proxy)
│   │   ├── service.py                 # Workspace storage CRUD + 檔案操作（雙模式）+ 權限檢查
│   │   ├── k8s_client.py             # K8s PVC CRUD + File Operation Job
│   │   └── auth.py                    # 雙模式認證（user + admin token）
│   ├── admin/
│   │   ├── main.py                    # Admin Service (:8090, ClusterIP, Gateway proxy)
│   │   ├── service.py                 # Orchestrator API proxy
│   │   └── auth.py                    # Admin 角色驗證（POC: static token）
│   ├── agent/
│   │   ├── main.py                    # Agent 入口（asyncio + signal handling）
│   │   ├── config.py                  # AgentConfig dataclass
│   │   ├── runtime.py                 # DeepAgentsRuntime（LangGraph ReAct）
│   │   ├── http_server.py             # FastAPI HTTP 層（chat/files/mcp）
│   │   ├── tools.py                   # SandboxedShellTool, SandboxedPythonREPLTool, LoggingSearchTool, CWAWeatherTool
│   │   ├── skills/                    # 內建 skill 定義
│   │   │   ├── daily-summary/         # 每日活動摘要
│   │   │   ├── docx/                  # Word 文件產生
│   │   │   ├── pdf/                   # PDF 文件產生
│   │   │   ├── pptx/                  # PowerPoint 簡報產生
│   │   │   └── xlsx/                  # Excel 試算表產生
│   │   └── backends/
│   │       └── local.py               # LocalBackend（PVC 檔案操作）
│   ├── db/
│   │   ├── models.py                  # 6 張表 ORM（含 workspace_members）
│   │   └── session.py                 # PostgreSQL async session
│   ├── utils/config.py                # 設定（env vars, 含 CWA_API_KEY/CWA_API_BASE/AGENT_DISPLAY_MODE）
│   ├── docker/
│   │   ├── Dockerfile.agent
│   │   ├── Dockerfile.orchestrator
│   │   ├── Dockerfile.gateway
│   │   ├── Dockerfile.admin
│   │   └── Dockerfile.storage
│   ├── k8s/
│   │   ├── kind-config.yaml
│   │   ├── namespace-rbac.yaml
│   │   ├── orchestrator.yaml
│   │   ├── gateway.yaml
│   │   ├── admin.yaml
│   │   ├── storage-service.yaml
│   │   └── deploy.sh
│   ├── tests/
│   │   ├── e2e_test.sh
│   │   ├── e2e_admin_test.sh
│   │   └── e2e_storage_test.sh
│   └── requirements.txt
├── app/                               # Production 源碼（規劃中）
├── infra/k8s/                         # Production K8s manifests（規劃中）
├── helm/                              # Helm Chart（規劃中）
└── scripts/                           # 運維腳本（規劃中）
```

## 主要流程圖

### 1. Session 建立與 Pod 啟動

```
User Login
    ↓
API Gateway (Token 驗證 → user_id)
    ↓
POST /api/v1/chat (Channel Layer 自動處理)
    ├─ ChannelManager 解析 user_id
    ├─ 查 ChannelStore: 是否有既有 session mapping?
    │  ├─ Y: 直接路由到 Agent Pod
    │  └─ N: 呼叫 Orchestrator.ensure_session_workspace
    │        ├─ 查 DB: workspace 是否存在?
    │        │  ├─ Y: 檢查 Pod 是否存活
    │        │  └─ N: 建立 workspace + PVC
    │        ├─ 查 K8s: Pod 是否存活?
    │        │  ├─ Y: 返回既有端點
    │        │  └─ N: 建立新 Pod + Service
    │        └─ 等待 Pod Ready → 返回端點
    ├─ 轉發訊息到 Agent Pod
    └─ 回傳 AI 回應給 Client
```

### 2. 多 Session 共用 Pod

```
Session_A 登入
    └─ ensure_session_workspace(user_id, session_A)
       └─ 建立 workspace + Pod（若不存在）→ 返回端點

Session_B（同一 user_id）登入
    └─ ensure_session_workspace(user_id, session_B)
       └─ 查 K8s 發現 Pod 已存活
          └─ 直接返回端點（無需重建）

Client_A & Client_B 同時連線到同一 Pod
    └─ Pod 內多路復用這兩個 session 連線
       └─ 透過 session_id 區分不同對話上下文（PostgreSQL checkpointer）
```

### 3. 閒置回收與恢復

```
Pod 運行中，mark_activity 定期更新 last_active_at

10分鐘無活動（POC，Production 為 30 分鐘）
    ↓
Agent Pod 偵測閒置 → 通知 Orchestrator reap-self
    ├─ 刪除 Pod + Service（保留 PVC）
    ├─ 更新 DB 狀態為 "idle"
    └─ 對話歷史保留在 PostgreSQL checkpointer

User 回訪（發送新訊息到 /api/v1/chat）
    ↓
ChannelManager 偵測 Agent 不可達
    ├─ 自動呼叫 ensure_session_workspace
    ├─ 建立新 Pod + Service（PVC 資料自動掛載）
    ├─ PostgreSQL checkpointer 恢復對話記憶
    └─ 轉發訊息，回傳回應
```

## 關鍵概念

### Session ID
- 由前端產生（建議 UUID）或由 Orchestrator 自動產生
- 用於在多 session 場景下追蹤各自的對話上下文
- 對應 LangGraph checkpointer 的 `thread_id`，對話記憶以 session 為單位

### Workspace Storage (PVC / S3)
- POC：PVC 本地掛載 `/workspace/{workspace_id}`
- Production：S3 prefix `users/{user_id}/` 或 `groups/{group_id}/`
- 目錄結構：`data/`（使用者資料）、`memories/`（AI 記憶）、`logs/`、`cache/`、`skills/`

### Agent Pod
- 命名規則：`pod-{workspace_id}`（personal: `ws-{user_id}`，group: 自訂 ID 如 `ws-team-eng`）
- 鏡像：Supervisor + Sub-Agent（langgraph-supervisor）+ FastAPI HTTP Layer + Tool Calling
- Checkpointer：AsyncPostgresSaver（對話記憶跨 Pod 重啟保留）
- 儲存掛載：personal PVC (`/workspace/{wid}`) + shared PVC (`/shared/{wid}`, 依 role 決定 read-only)
- 儲存：LocalBackend (PVC)，介面與 S3Backend 一致，未來可無縫切換

### IM Channel Layer
- 架構：Channel ABC → MessageBus (async queue) → ChannelManager (dispatcher)
- 目前：WebChannel（HTTP request-response via asyncio.Future）
- 未來：Slack / LINE / Teams adapters（registry stubs ready）
- 所有 IM 訊息統一為 InboundMessage/OutboundMessage，Agent 不知道訊息來源

## POC 簡化說明（vs Production 設計）

| 項目 | POC | Production |
|------|-----|------------|
| 認證 | Static token (env var `POC_STATIC_TOKEN`) | JWT / OAuth2 |
| 資料庫 | PostgreSQL (asyncpg) | PostgreSQL 14+ + Alembic migrations |
| K8s 環境 | kind + Podman | EKS / GKE / AKS |
| Agent 容器 | create_supervisor + create_react_agent（research_agent + code_agent）+ LocalBackend (PVC)，fallback: create_agent + SkillsInjectionMiddleware | LangChain Deep Agents + S3Backend |
| LLM | Local LLM via OPENAI_API_BASE | Anthropic / OpenAI API |
| 閒置超時 | 10 分鐘 | 30 分鐘 |
| 儲存 | PVC（personal 掛載 `/workspace/{wid}`，shared 掛載 `/shared/{wid}`） | AWS S3（IRSA + S3Backend） |
| Session 工具隔離 | Per-session agent cache，Shell/Python cwd 強制為 `sessions/{sid}/` | S3Backend session-scoped prefix |
| Skills | 5 個 bundled skills，三層優先級載入（shared > bundled > workspace） | 同左 + 管理介面 |
| 檔案操作 | Storage Service 雙模式（Pod 在線 proxy / 離線 K8s Job），`/api/v1/workspaces/{wid}/storage/files/*` | 同左（S3 presigned URL） |
| Checkpointer | AsyncPostgresSaver | 同左 |
| IM Channel | WebChannel only | + Slack / LINE / Teams |
| Workspace 類型 | personal（自動建立）/ group（Admin 預建），Pod 掛載 personal + group PVC | 同左 |
| 存取控制 | workspace_members 表（owner/admin/member/readonly），Admin Service 管理 | 同左 + JWT role claim |
| Admin 認證 | Static admin token (env var `POC_ADMIN_TOKEN`) | JWT + admin role + IP 白名單 |
| 部署方式 | 全部容器化，五元件皆在 K8s 內（Gateway, Orchestrator, Admin, Storage Service, Agent） | 同左 + Helm chart |

## POC 驗證結果

以下項目已在 kind + Podman 環境中全部驗證通過：

- ✅ Gateway / Orchestrator / Agent 全部容器化部署於 K8s
- ✅ LangGraph ReAct Agent + Tool Calling（list/read/write workspace files）
- ✅ IM Channel 抽象層（WebChannel → MessageBus → ChannelManager → Agent）
- ✅ PostgreSQL Checkpointer（對話記憶跨 request 保留）
- ✅ 首次登入建立 Pod + Service + PVC
- ✅ 多 Session 共用同一 Pod
- ✅ Gateway proxy 到 Agent Pod（chat / MCP / stream）
- ✅ 閒置自動回收 Pod + Service（Agent self-report + background reaper）
- ✅ 孤兒 workspace 狀態修復（背景掃描器自動偵測 status=active 但無 Pod 的 workspace，修正為 idle）
- ✅ 回收後 PVC 資料保留，回訪恢復 Pod
- ✅ Session 刪除清理所有相關資料（checkpoints + activity logs）
- ✅ Slash commands（/new, /status, /help）
- ✅ Local LLM 支援（OPENAI_API_BASE）
- ✅ CORS 支援（localhost dev servers）
- ✅ Admin Service 獨立 Pod + Gateway proxy（user CRUD, workspace 管理, workspace_members 角色管理）
- ✅ Storage Service 獨立 Pod + Gateway proxy（workspace storage CRUD, 檔案操作雙模式, 存取權限, user+admin 雙模式認證）
- ✅ Multi-Workspace 支援（personal/group workspace_type, Admin 預建 group workspace, Pod 自動掛載 personal + group PVC）
- ✅ workspace_members 表（owner/admin/member/readonly 角色，控制 shared workspace 掛載權限）
- ✅ NetworkPolicy（agent→gateway+storage-service ingress, orchestrator→gateway+admin+storage-service+agent ingress, storage-service→gateway+orchestrator+admin ingress）
- ✅ 台灣氣象查詢工具（CWAWeatherTool，中央氣象署開放資料 API，整合至 research_agent，支援 36h/一週預報、即時觀測、即時雨量）
- ✅ SSE Display Mode（`AGENT_DISPLAY_MODE`：`normal` 僅 content，`full_history` 含 tool_call/tool_result 事件）

詳見各文檔與 [前端串接 API 文件](./docs/frontend-api.md)。
