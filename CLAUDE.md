# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

一個基於 Kubernetes 的使用者專屬 Agent 工作區管理系統。文件以繁體中文撰寫。專案包含完整設計文件（docs/01-09）以及已驗證通過的 POC 實作（poc/）。

系統讓使用者透過單一 API Gateway 登入，由 Orchestrator 在 K8s 中自動建立/恢復專屬 Agent Pod。Gateway 透過 IM Channel 抽象層（或直接 proxy）路由到對應 Pod。同一 workspace 的多個 session 共用同一個 Pod，閒置後自動回收 Pod 但透過 PVC 保留資料（Production 改用 AWS S3）。支援個人與群組兩種 workspace，透過 `workspace_members` 表做存取控制（owner/admin/member/readonly）。

## Architecture

```
Client → API Gateway (:8000, token 驗證, workspace ACL, proxy)
           │
           ├─(1) ensure_session_workspace
           ▼
         Orchestrator (:8080, workspace 生命週期管理)
           ├─(2a) DB (狀態持久化)
           └─(2b) K8s API (Pod/Service 管理)
                    ↓
Client → API Gateway ─(3a) /api/v1/chat ─→ Channel Layer ─→ Agent Pod (:8080)
                     │                      (WebChannel → MessageBus → ChannelManager)
                     ├─(3b) /workspaces/{wid}/* ─→ Direct Proxy ─→ Agent Pod (:8080)
                     └─(3c) /api/v1/workspaces/{wid}/storage/* ─→ Storage Service (:8091)

Storage Service (:8091, ClusterIP, Gateway proxy /api/v1/workspaces/storage/*, /api/v1/workspaces/{wid}/storage/*)
           │
           ├─→ K8s API (PVC CRUD, File Operation Job)
           ├─→ DB (workspace ↔ PVC 映射, workspace_members 權限)
           └─→ Agent Pod (Pod 在線時 proxy 檔案操作)

Admin Service (:8090, ClusterIP, Gateway proxy /api/v1/admin/*)
           │
           ├─→ Orchestrator API (user/workspace/member 管理, workspace 刪除)
           └─→ Storage Service API (workspace PVC 刪除)
```

Key components:
- **API Gateway** (`poc/gateway/`) — FastAPI on :8000, static token 驗證, workspace ACL, proxy to Orchestrator + Agent
- **Channel Layer** (`poc/gateway/channels/`) — IM channel 抽象層 (inspired by deer-flow)：MessageBus pub/sub, ChannelManager dispatcher, ChannelStore 映射, WebChannel adapter
- **Orchestrator** (`poc/orchestrator/`) — FastAPI on :8080, K8s resource lifecycle (Pod/Service), background reaper + orphan workspace repair, admin data APIs, PVC 建立委派給 Storage Service
- **Storage Service** (`poc/storage/`) — FastAPI on :8091, ClusterIP, 前端透過 Gateway proxy (`/api/v1/workspaces/storage/*`, `/api/v1/workspaces/{wid}/storage/*`) 存取, workspace storage CRUD, 檔案操作（雙模式：Pod 在線 proxy / 離線 K8s Job）, 存取權限（workspace_members）, 支援 user token + admin token
- **Admin Service** (`poc/admin/`) — FastAPI on :8090, ClusterIP, 前端透過 Gateway proxy (`/api/v1/admin/*`) 存取, user CRUD, workspace 管理（含刪除）, workspace_members 角色管理, 透過 Orchestrator API + Storage Service 操作
- **Agent Container** (`poc/agent/`) — Supervisor + Sub-Agent 架構（langgraph-supervisor）：Supervisor 分派任務給 research_agent（搜尋）+ code_agent（執行），FastAPI HTTP layer + LocalBackend (PVC)
- **Database** (`poc/db/`) — SQLAlchemy ORM, PostgreSQL 14+ (asyncpg)

## Tech Stack

- Python 3.13+, FastAPI, Uvicorn
- LangChain 1.0+ (create_supervisor + create_react_agent) + LangGraph + langgraph-supervisor (Agent runtime)
- SQLAlchemy + asyncpg / PostgreSQL 14+
- Kubernetes Python client
- Docker / Podman + kind (local K8s)

## POC Commands

```bash
# One-step deploy (creates cluster, builds images, deploys everything)
bash poc/k8s/deploy.sh

# Or step by step:

# 1. Setup K8s cluster (kind + Podman, with NodePort mapping)
KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name agent-poc --config poc/k8s/kind-config.yaml

# 2. Deploy namespace + RBAC
kubectl apply -f poc/k8s/namespace-rbac.yaml

# 3. Build all images
docker build -t k8s-agent-runtime:latest -f poc/docker/Dockerfile.agent .
docker build -t k8s-orchestrator:latest -f poc/docker/Dockerfile.orchestrator .
docker build -t k8s-gateway:latest -f poc/docker/Dockerfile.gateway .
docker build -t k8s-admin:latest -f poc/docker/Dockerfile.admin .
docker build -t k8s-storage-service:latest -f poc/docker/Dockerfile.storage .

# 4. Load images into kind
export KIND_EXPERIMENTAL_PROVIDER=podman
kind load docker-image k8s-agent-runtime:latest --name agent-poc
kind load docker-image k8s-orchestrator:latest --name agent-poc
kind load docker-image k8s-gateway:latest --name agent-poc
kind load docker-image k8s-admin:latest --name agent-poc
kind load docker-image k8s-storage-service:latest --name agent-poc

# 5. Deploy Orchestrator + Gateway + Admin + Storage Service into K8s
kubectl apply -f poc/k8s/orchestrator.yaml
kubectl apply -f poc/k8s/gateway.yaml
kubectl apply -f poc/k8s/admin.yaml
kubectl apply -f poc/k8s/storage-service.yaml

# 6. Test (Gateway exposed at localhost:8000 via NodePort 30080)
curl -s http://localhost:8000/health
curl -s -X POST http://localhost:8000/api/v1/workspaces/ensure \
  -H "Authorization: Bearer REDACTED_USER_TOKEN:testuser1" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "sess-001"}'

# 7. E2E test (automated)
bash poc/tests/e2e_test.sh

# 8. Cleanup
KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name agent-poc
```

## Directory Structure

```
docs/                              # 設計文件 (01-09)
poc/
  gateway/
    main.py                        # API Gateway (:8000)
    channels/                      # IM Channel 抽象層（inspired by deer-flow）
      __init__.py                  # 公開介面：Channel, InboundMessage, MessageBus, OutboundMessage
      base.py                      # Channel ABC（start/stop/send）
      message_bus.py               # 非同步 pub/sub hub + InboundMessage/OutboundMessage dataclass
      manager.py                   # 中央 dispatcher：IM → ensure workspace → Agent Pod → IM
      store.py                     # chat ↔ workspace/session 映射（JSON 持久化）
      service.py                   # 生命週期管理、channel registry、singleton
      adapters/
        __init__.py
        web.py                     # Web channel adapter（HTTP request-response）
  orchestrator/
    main.py                        # Orchestrator API (:8080) + background reaper + orphan repair + admin data APIs
    service.py                     # 核心邏輯 (ensure/reap/mark_activity/admin queries), PVC 建立委派 Storage Service
    k8s_client.py                  # K8s Pod/Service/Cleanup Job 操作
  storage/
    __init__.py
    main.py                        # Storage Service (:8091, ClusterIP, Gateway proxy)
    service.py                     # Workspace storage CRUD + 檔案操作（雙模式）+ 權限檢查
    k8s_client.py                  # K8s PVC CRUD + File Operation Job + Pod 狀態查詢
    auth.py                        # 雙模式認證（user token + admin token）
  admin/
    __init__.py
    main.py                        # Admin Service (:8090, ClusterIP, Gateway proxy)
    service.py                     # Orchestrator API proxy（user/workspace/member 管理）
    auth.py                        # Admin 角色驗證（POC: static admin token）
  agent/
    main.py                        # Agent 容器入口（asyncio + signal handling）
    config.py                      # AgentConfig dataclass（env vars）
    runtime.py                     # DeepAgentsRuntime（Supervisor + Sub-Agent: research_agent + code_agent）
    middleware.py                  # SkillsInjectionMiddleware（flat agent fallback 用，supervisor 透過 session context 注入）
    http_server.py                 # FastAPI HTTP 層（chat/mcp/shutdown/files CRUD）
    tools.py                       # 工具分組（research_tools, code_tools, common_tools）+ LoggingSearchTool + CWAWeatherTool
    skills/                        # 內建 skill 定義（隨 image 部署）
      daily-summary/SKILL.md       # 每日活動摘要
      docx/SKILL.md                # Word 文件產生
      pdf/SKILL.md                 # PDF 文件產生
      pptx/SKILL.md                # PowerPoint 簡報產生
      xlsx/SKILL.md                # Excel 試算表產生
    backends/
      local.py                     # LocalBackend — PVC 本地檔案操作（session-aware 寫入權限）
  db/
    models.py                      # 6 張表 ORM（含 workspace_members，POC 版）
    session.py                     # PostgreSQL async session
  utils/config.py                  # 設定 (env vars, 含 CWA_API_KEY/CWA_API_BASE/AGENT_DISPLAY_MODE)
  docker/
    Dockerfile.agent               # Agent 鏡像
    Dockerfile.orchestrator        # Orchestrator 鏡像
    Dockerfile.gateway             # Gateway 鏡像
    Dockerfile.admin               # Admin 鏡像
    Dockerfile.storage             # Storage Service 鏡像
  k8s/
    kind-config.yaml               # kind 叢集設定 (NodePort mapping)
    namespace-rbac.yaml            # Namespace + ServiceAccount + RBAC + NetworkPolicy
    orchestrator.yaml              # Orchestrator Deployment + ClusterIP Service
    gateway.yaml                   # Gateway Deployment + NodePort Service
    admin.yaml                     # Admin Deployment + ClusterIP Service
    storage-service.yaml           # Storage Service Deployment + ClusterIP Service
    deploy.sh                      # 一鍵部署腳本
  tests/
    e2e_test.sh                    # 基本端對端測試
    e2e_admin_test.sh              # Admin Service 端對端測試
    e2e_storage_test.sh            # Storage Service 端對端測試
  requirements.txt
```

## Design Documents

All under `docs/`, numbered 01-09. These are the source of truth for production implementation:
- **01** Requirements spec (functional + non-functional)
- **02** API design (HTTP endpoints, internal RPC, agent container API)
- **03** Data model (7 PostgreSQL tables including workspace_members, K8s resource naming conventions)
- **04** K8s resources (full YAML manifests)
- **05** Lifecycle (session/Pod state machines, idle reaping, recovery flows)
- **06** Agent container (LangChain Deep Agents runtime, FastAPI HTTP layer, MCP compatibility)
- **07** Deployment guide (local dev, Docker builds, Helm, monitoring)
- **08** Fault recovery (error classification, retry strategies, DRP)
- **09** Agent tools (tool groups, sandbox, CWA weather, how to add new tools)

## Key Design Decisions

- Single public Gateway entry (`api.yourdomain.com`), no per-user Ingress
- AWS S3 per workspace (permanent): personal `users/{user_id}/`, group `groups/{group_id}/`
- One Pod per workspace (ephemeral, reaped when idle)；使用者的 personal workspace 有自己的 Pod，group workspace 的 PVC 掛載到使用者的 Pod 上（不另建 Pod）
- Multiple sessions share the same Pod — concurrency handled in-process
- Orchestrator multi-replica concurrency: PostgreSQL Advisory Lock per workspace_id (no Redis/leader election needed)
- Workspace ACL via `workspace_members` table with roles: owner/admin/member/readonly
- K8s resource naming: `pod-{workspace_id}`, `svc-{workspace_id}`, `pvc-{workspace_id}` (workspace-centric；personal workspace: `ws-{user_id}`，group workspace: 自訂 ID 如 `ws-team-eng`)
- Gateway validates workspace access via `workspace_members` before proxying
- Gateway does NOT directly access K8s Control Plane — only Orchestrator does
- Orchestrator returns service endpoint info to Gateway, Gateway then proxies via K8s Internal Service
- Gateway route cache with TTL (5 min) + ConnectError auto-invalidate + re-ensure retry
- Orchestrator waits for Pod readiness before returning "ready" (polling K8s Ready condition, 120s timeout)
- Orphan workspace repair: background scanner detects `status='active'` workspaces with no running session and no K8s Pod, auto-fixes to `idle` (1 SQL query + 1 K8s list call, O(1) regardless of workspace count)
- Agent graceful shutdown: preStop hook → drain active requests → save state to PVC → close connections (Production: save to S3)
- NetworkPolicy restricts Agent Pods to only accept traffic from Gateway and Storage Service; Orchestrator accepts traffic from Gateway, Admin, Storage Service, and Agent (for idle reap-self); Storage Service accepts traffic from Gateway, Orchestrator, and Admin
- IM Channel abstraction layer (inspired by deer-flow): all chat messages normalized into InboundMessage/OutboundMessage, routed through async MessageBus, dispatched by ChannelManager — decouples IM platforms from Agent logic
- Channel adapters are pluggable: `web` (built-in), `slack`/`line`/`teams` (registry stubs ready)
- Web channel uses request-response pattern via asyncio.Future; other channels will use webhook/websocket patterns
- ChannelStore persists chat→workspace/session mappings (JSON file for POC, DB for production)
- `/api/v1/chat` (channel-based) is the recommended frontend endpoint; `/workspaces/{wid}/api/v1/chat` (direct proxy) remains for backward compatibility
- Session-scoped folder isolation: each session works in `sessions/{session_id}/`, can read other sessions' folders (read-only), shared `data/` and `memories/` are read-write for all sessions
- Session deletion triggers K8s cleanup Job to remove `sessions/{session_id}/` from PVC (Production: S3 prefix delete)
- Auth (JWT) stays in Gateway; Admin Service is a separate Pod (:8090, ClusterIP), Gateway proxy `/api/v1/admin/*` 統一入口, operates K8s via Orchestrator API
- Storage Service is a separate Pod (:8091, ClusterIP), Gateway proxy `/api/v1/workspaces/storage/*` 和 `/api/v1/workspaces/{wid}/storage/*`, 獨立管理 workspace storage CRUD + 檔案操作 + 存取權限, 支援 user token + admin token 雙模式認證
- PVC 檔案操作雙模式：Pod 在線 → proxy 到 Agent Pod `/api/v1/files/*`；Pod 離線 → 建立短暫 K8s Job 掛載 PVC 操作（busybox image, ttl=60s）
- PVC 存取權限透過 workspace_members 表控制：owner/admin 可管理，member 可讀寫，readonly 只能讀取
- Orchestrator ensure 流程中的 PVC 建立委派給 Storage Service（HTTP call），Storage Service 不可用時 graceful fallback
- Skill system (inspired by anthropics/skills): SKILL.md with YAML frontmatter (name + description) for triggering, progressive disclosure (metadata dynamically injected via middleware, body loaded on trigger, resources on demand)
- Skills three-layer priority (highest first): shared (mounted shared PVC `/shared/*/skills/`, 跨 workspace 共用) > bundled (image 內建, 不可覆蓋) > workspace (workspace PVC `skills/`, 各 workspace 自訂)
- Same-name skill dedup: higher priority source wins, lower priority skipped
- Per-session agent cache with session-scoped tools: model + checkpointer shared, supervisor workflow created per-session with cwd = `sessions/{session_id}/`
- Supervisor + Sub-Agent 架構（langgraph-supervisor）：Supervisor 負責理解需求、分派任務、整合結果；research_agent 專責搜尋（rate-limited, max 3 calls）；code_agent 專責執行（terminal + python_repl）。Skills metadata 透過 session context message 動態注入。若 langgraph-supervisor 不可用，fallback 到 create_agent + SkillsInjectionMiddleware

## POC Simplifications (vs Production Design)

| 項目 | POC | Production |
|------|-----|------------|
| 認證 | Static token (`REDACTED_USER_TOKEN:{user_id}`) | JWT / OAuth2 |
| 資料庫 | PostgreSQL (asyncpg) | PostgreSQL 14+ + Alembic migrations |
| K8s 環境 | kind + Podman | EKS / GKE / AKS |
| Agent 容器 | create_supervisor + create_react_agent（research_agent + code_agent）+ LocalBackend (PVC)，fallback: create_agent + SkillsInjectionMiddleware | LangChain Deep Agents + S3Backend + PostgresSaver |
| LLM | Local LLM via OPENAI_API_BASE | Anthropic / OpenAI API |
| 閒置超時 | 10 分鐘 | 30 分鐘 |
| 儲存 | PVC（personal 掛載 `/workspace/{wid}`，shared 掛載 `/shared/{wid}`） | AWS S3（透過 IRSA + S3Backend） |
| 工作區結構 | `data/`, `memories/`, `skills/`, `sessions/{sid}/` | 同左（S3 prefix） |
| Session 清理 | K8s Job 掛載 PVC 刪除 `sessions/{sid}/` | S3 prefix delete |
| Session 工具隔離 | Per-session agent cache，Shell/Python cwd 強制為 `sessions/{sid}/` | S3Backend session-scoped prefix |
| K8s 命名 | `pod-{workspace_id}`, `svc-{workspace_id}` | 同左 |
| Workspace 類型 | personal（使用者登入自動建立）/ group（Admin 預建），Pod 掛載 personal PVC + 所有 group PVC | 同左 |
| 存取控制 | workspace_members 表（owner/admin/member/readonly），Admin Service 管理 | 同左 + JWT role claim |
| 路由 key | workspace_id | 同左 |
| DB 表 | 6 張（users, workspaces, sessions, activity_logs, pod_states, workspace_members） | 7 張（含 workspace_members，無 pod_states） |
| IM Channel | WebChannel only | + Slack / LINE / Teams adapters |
| Checkpointer | AsyncPostgresSaver (PostgreSQL, 對話跨 Pod 重啟保留) | 同左 |
| 長期記憶 | 無 | AsyncPostgresStore (LangGraph Store) |
| Skills | 5 個 bundled skills（daily-summary, docx, pdf, pptx, xlsx），三層優先級載入（shared > bundled > workspace） | 同左 + 管理介面 |
| 檔案操作 | Storage Service 雙模式（Pod 在線 proxy / 離線 K8s Job），`/api/v1/workspaces/{wid}/storage/files/*` | 同左（S3 presigned URL） |
| 部署方式 | 全部容器化，五元件皆在 K8s 內（Gateway, Orchestrator, Admin, Storage Service, Agent） | 同左 + Helm chart |
| Admin 認證 | Static admin token (`REDACTED_ADMIN_TOKEN`) | JWT + admin role + IP 白名單 |
| 監控/TLS/Helm | 無 | Prometheus, TLS, Helm chart |

## Implementation Status

- **POC (已完成，已驗證)**: Gateway, Orchestrator, Agent (Supervisor + Sub-Agent: research_agent + code_agent), DB models, K8s deploy, E2E test — 全部通過
- **Channel Layer (已完成)**: IM channel 抽象層 — MessageBus, ChannelManager, ChannelStore, WebChannel adapter, `/api/v1/chat` endpoint, `/api/v1/admin/channels` endpoint
- **Session 隔離 (已完成)**: Per-session agent cache, SandboxedShellTool/PythonREPLTool cwd 強制 `sessions/{sid}/`, session context prompt injection
- **Skill 系統 (已完成)**: 三層優先級載入（shared > bundled > workspace）, SKILL.md frontmatter 解析, supervisor 透過 session context 動態注入 skills metadata, 5 個 bundled skills（daily-summary, docx, pdf, pptx, xlsx）
- **Supervisor 架構 (已完成)**: langgraph-supervisor, create_supervisor + create_react_agent, research_agent（搜尋, rate-limited）+ code_agent（執行, sandboxed）, GraphRecursionError 優雅處理（event: error）, fallback 到 flat agent
- **SSE 串流事件 (已完成)**: event: content/tool_call/tool_result/file/error, 檔案自動偵測推送, GraphRecursionError 不斷線, `AGENT_DISPLAY_MODE` 控制 tool_call/tool_result 可見性（`normal`: 僅 content, `full_history`: 含 tool 事件）
- **Supervisor 架構升級 (已完成)**: create_agent → create_supervisor + create_react_agent (research_agent + code_agent), skills 透過 session context 注入, K8s 整合驗證通過
- **台灣氣象查詢工具 (已完成)**: CWAWeatherTool（中央氣象署開放資料 API），整合至 research_agent，支援 36h 預報、一週預報、即時觀測、即時雨量，config 透過 `CWA_API_KEY` / `CWA_API_BASE` 環境變數設定，SSL verify 停用（CWA 憑證相容性問題）
- **檔案操作 (已完成)**: Agent `/api/v1/files/upload|download|list|delete|mkdir`, Gateway proxy endpoints, LocalBackend path traversal 防護, delete 支援檔案+目錄
- **對話歷史 (已完成)**: `/api/v1/sessions/{sid}/messages` 從 LangGraph checkpointer 取得對話紀錄
- **Admin Service (已完成)**: 獨立 Pod (:8090, ClusterIP), Gateway proxy `/api/v1/admin/*` 統一入口, user CRUD, workspace 列表/查詢/刪除, workspace_members 角色管理 (owner/admin/member/readonly), Pod 狀態總覽（K8s 即時查詢）, reap 觸發, workspace 刪除（Storage Service 刪 PVC → Orchestrator 刪 DB + K8s）, 透過 Orchestrator + Storage Service API 操作（不直接碰 K8s）, POC admin token 雙層驗證（Gateway + Admin Service）
- **Storage Service (已完成)**: 獨立 Pod (:8091, ClusterIP), Gateway proxy `/api/v1/workspaces/storage/*` 和 `/api/v1/workspaces/{wid}/storage/*`, workspace storage CRUD（create/ensure/rename/刪除/列表）, Admin 可預建 group workspace + PVC, 檔案操作雙模式（Pod 在線 proxy Agent / 離線 K8s Job）, 存取權限（workspace_members）, user token + admin token 雙模式認證, display_name 支援
- **Multi-Workspace (已完成)**: workspace_type（personal/group）, Admin 預建 group workspace, 使用者 Pod 自動掛載 personal PVC (`/workspace/{wid}`) + 所有 group PVC (`/shared/{wid}`), readonly role 強制 K8s 層級 read-only mount, Pod 重建時自動更新掛載
- **Phase 1 (未開始)**: Production Orchestrator + PostgreSQL + Alembic migrations
- **Phase 2 (設計完成，未開始)**: LangChain Deep Agents container + FastAPI HTTP layer + MCP compatibility
- **Phase 3 (未開始)**: Production API Gateway + JWT/OAuth2 + Slack/LINE/Teams channel adapters
- **Phase 4 (未開始)**: Helm, monitoring, TLS, load testing
