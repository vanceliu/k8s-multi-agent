# 01 需求規格

## 功能需求 (FR)

### FR1. 使用者認證與 Session 管理

#### FR1.1 Token 驗證
- **敘述**：API Gateway 在每個請求進入時驗證 Bearer Token
- **輸入**：HTTP 請求的 Authorization 標頭
- **處理**：
  - 解析 Token（支援 JWT / OAuth2）
  - 驗證簽名與過期時間
  - 從 Token 反查 `user_id`
- **輸出**：`user_id` 或拒絕請求（HTTP 401/403）
- **驗收**：合法 Token 通過，過期/偽造 Token 被拒

#### FR1.2 Session 生成
- **敘述**：Orchestrator 為每個認證使用者建立或查詢 session
- **輸入**：`user_id`、可選的 `session_id`
- **處理**：
  - 若無 `session_id` 則新增生成（UUID）並記錄到 DB
  - 若已存在則查詢返回
  - 記錄 `session_created_at`、`last_active_at`
- **輸出**：`session_id`
- **驗收**：同一 user_id 多次登入產生不同 session_id

### FR2. 工作區建立與恢復

#### FR2.1 工作區初始化
- **敘述**：首次使用者登入時建立 S3 prefix 與 Agent Pod（個人 workspace 自動建立；群組 workspace 需由 owner/admin 建立）
- **輸入**：`user_id`、`session_id`
- **處理**：
  - 檢查 DB：user_id 是否有既有 workspace 紀錄
    - 無 → 建立 workspace（個人類型）並配置 S3 prefix（首次寫入時自動建立）
    - 有 → 檢查 workspace 狀態，若為已回收則準備恢復
  - 檢查 K8s：workspace_id 對應 Pod 是否運行
    - 是 → 返回既有 Pod 端點
    - 否 → 部署新 Pod（S3 透過 IRSA 存取）
  - 建立對應 Service（`svc-ws-{workspace_id}`）
  - 啟動 Agent runtime 與 MCP 初始化
- **輸出**：工作區端點（URL）、Pod 名稱
- **驗收**：Pod 就緒、Service 可訪問

#### FR2.2 工作區恢復
- **敘述**：既有使用者在 Pod 被回收後登入時恢復工作區
- **輸入**：`user_id`、`session_id`
- **處理**：
  - 查 DB：找 user_id 對應的 workspace 與 S3 prefix
  - 查 K8s：確認 workspace 存在、Pod 不存在
  - 重建 Pod、Service（`svc-ws-{workspace_id}`）
  - Agent runtime 透過 S3Backend 存取 S3 工作區
  - 恢復應用狀態（從 S3 + PostgreSQL 載入使用者配置、模型、長期記憶等）
  - 記錄恢復事件到 activity_logs
- **輸出**：恢復後的工作區端點
- **驗收**：使用者資料完整、應用狀態可恢復

#### FR2.3 多 Session 共用 Pod
- **敘述**：同一 workspace 的多個 session 使用同一個 Pod（一個使用者可透過 workspace_members 存取多個 workspace）
- **輸入**：`user_id`、多個不同的 `session_id`
- **處理**：
  - 第一個 session 登入時建立 Pod
  - 後續 session 登入時發現 Pod 已存在，直接返回端點
  - Pod 內透過 session_id 區分不同連線
  - 每個 session 擁有專屬工作目錄 `sessions/{session_id}/`（僅該 session 可寫入）
  - 共用 workspace 層級目錄：`data/`、`memories/`（所有 session 可讀寫）
  - 其他 session 的工作目錄為 read-only
- **輸出**：所有 session 獲得同一工作區端點
- **驗收**：多 session 可同時運行，資料一致

### FR3. 使用者入口管理

#### FR3.1 單一 Gateway 路由
- **敘述**：所有使用者共用單一 Gateway 入口（`api.yourdomain.com`），由 Gateway 在應用層依 token 解析 user_id 後內部路由
- **輸入**：`user_id`（從 token 解析）
- **處理**：
  - Gateway 呼叫 Orchestrator 的 `ensure_session_workspace` 取得內部 Service 端點
  - Gateway 快取 user_id → service endpoint 的映射
  - 後續請求依 user_id 透過 K8s 內部 Service 轉發到對應 Agent Pod
  - 不建立 per-workspace Ingress 或子網域
  - Gateway 在轉發前驗證 workspace ACL（`workspace_members` 表確認存取權限）
- **輸出**：統一入口 `https://api.yourdomain.com`，路由路徑 `/workspaces/{workspace_id}`
- **驗收**：所有使用者透過同一入口存取各自的工作區

#### FR3.2 路由持久化
- **敘述**：即使 Pod 被回收，使用者再次請求時 Gateway 能自動觸發恢復
- **輸入**：`user_id`、`session_id`
- **處理**：
  - Gateway 轉發時若發現 Agent Service 不存在，先觸發 `ensure_session_workspace` 恢復
  - Orchestrator 從既有 S3 資料重建 Pod + Service
  - Gateway 更新路由快取
- **輸出**：使用者無感知的自動恢復
- **驗收**：回收前後使用者存取路徑不變

### FR4. 活動追蹤與閒置回收

#### FR4.1 活動記錄
- **敘述**：系統追蹤每個工作區的最後活動時間
- **輸入**：`user_id`、API 或 WebSocket 請求
- **處理**：
  - API Gateway 或 Orchestrator 在每個請求時呼叫 `mark_activity`
  - 更新 DB 中 sessions 表的 `last_active_at`
  - 可選：記錄活動類型到 activity_logs
- **輸出**：無（副作用：更新 DB）
- **驗收**：最後活動時間能正確反映實際活動

#### FR4.2 閒置檢測與回收
- **敘述**：30 分鐘無活動時自動終止 Pod 以節省資源
- **輸入**：background job 定期掃描
- **處理**：
  - 後台任務每 5 分鐘執行一次 `reap_idle_workspaces`
  - 掃描所有 sessions，找 last_active_at < 30 分鐘的
  - 對各使用者，刪除 Pod、Service（保留 DB 映射與 S3 資料）
  - S3 資料與 DB 紀錄完整保留
  - 記錄回收事件
- **輸出**：無
- **驗收**：Pod 按時回收，S3 資料保留

#### FR4.3 回訪喚醒
- **敘述**：使用者在回收後再次登入時快速恢復工作區
- **輸入**：`user_id`、`session_id`（新的或既有的）
- **處理**：
  - 同 FR2.2 工作區恢復流程
  - 檢測到 Pod 不存在但 workspace 記錄存在，觸發恢復
- **輸出**：恢復後的工作區端點
- **驗收**：恢復時間 < 1 分鐘（不含鏡像拉取）

#### FR4.4 Session 刪除與資料清理
- **敘述**：使用者或系統刪除指定 session 時，一併清除該 session 的所有相關資料
- **輸入**：`session_id`
- **處理**：
  - Orchestrator 建立 K8s cleanup Job，掛載 workspace PVC 刪除 `sessions/{session_id}/`
  - 刪除 LangGraph checkpoint 資料（對話歷史）
  - 刪除活動日誌
  - 刪除 session DB 記錄
  - Production：改為 S3 prefix delete
- **輸出**：刪除確認（session_id、workspace_id）
- **驗收**：session 資料夾、checkpoint、DB 記錄全部清除

### FR5. Agent 容器與 MCP 整合

#### FR5.1 LangChain Deep Agents Runtime
- **敘述**：Pod 內運行 LangChain Deep Agents 類型的 Agent 容器
- **輸入**：Pod 啟動參數（workspace_id、S3 prefix 等）
- **處理**：
  - 容器啟動時初始化 Deep Agents runtime
  - 透過 S3Backend 存取 S3 工作區（IRSA 自動注入 AWS 憑證）
  - 初始化工作進程池、日誌系統、監控客戶端
  - 加載使用者組態 (config.yaml 或環境變數)
- **輸出**：就緒狀態 (readiness probe 通過)
- **驗收**：容器日誌無錯誤，Kubernetes 標記為 Ready

#### FR5.2 MCP 協議支援
- **敘述**：Agent 支援接收並處理 MCP (Model Context Protocol) 格式的請求
- **輸入**：MCP 格式的工作區請求、任務執行請求
- **處理**：
  - 建立 MCP 服務端（或客戶端連線）
  - 解析 MCP 請求
  - 路由到對應的 agent 任務執行器
  - 回傳結果或流式輸出
- **輸出**：MCP 格式的響應
- **驗收**：MCP 客戶端能正常通訊

#### FR5.3 內建工具
- **敘述**：Agent 容器內建常用工具，無需額外 API Key
- **工具清單**：
  - `SandboxedShellTool`：在容器內執行 shell 命令（cwd 強制為 session 目錄，禁止逃逸 workspace）
  - `SandboxedPythonREPLTool`：執行 Python 程式碼（cwd 強制為 session 目錄，open() 限制在 workspace 內）
  - `DuckDuckGoSearchRun`：網路搜尋（免費，無需 API Key）
  - `notify_orchestrator_activity`：回報 session 活動，防止被閒置回收
- **驗收**：所有工具在 Agent 啟動時自動載入並可用

#### FR5.4 工作區資料恢復
- **敘述**：Agent 啟動時恢復使用者的應用狀態與檔案
- **輸入**：workspace_id、S3 prefix
- **處理**：
  - 透過 S3Backend 掃描 S3 工作區內的應用狀態檔案
  - 恢復模型、配置、運行狀態
  - 可選：驗證資料完整性
  - 初始化 MCP context（如果需要）
- **輸出**：應用就緒（可接收任務）
- **驗收**：使用者無縫切換至新 session

### FR6. 監控與故障恢復

#### FR6.1 Pod 健康檢查
- **敘述**：K8s 定期檢查 Pod 是否健康，故障時自動重啟
- **輸入**：Kubernetes livenessProbe、readinessProbe
- **處理**：
  - Pod 實作 HTTP endpoint `/health` 或 `/readiness`
  - K8s 定期調用，根據響應決定是否重啟
  - 配置重試策略與超時時間
- **輸出**：Pod 自動重啟（若故障）
- **驗收**：故障 Pod 被替換、恢復時間 < 1 分鐘

#### FR6.2 S3 存取故障恢復
- **敘述**：若 S3 存取失敗，系統應給出清晰的錯誤與可選的恢復方案
- **輸入**：S3Backend 初始化失敗、S3 存取被拒
- **處理**：
  - 檢查 IRSA annotation、IAM Role 信任策略
  - 檢查 S3 bucket policy 與 IAM Policy 權限
  - 若為網路問題，重試（指數退避）
  - 提供管理員介入指南
- **輸出**：錯誤日誌、告警通知
- **驗收**：故障可被識別與追蹤

#### FR6.3 工作區資料持久性
- **敘述**：確保 S3 工作區資料在多次 Pod 回收與恢復間保持一致
- **輸入**：S3 工作區資料
- **處理**：
  - S3 原生物件版本控制追蹤變更
  - 記錄工作區資料修改時間戳
  - 告警異常大量刪除
- **輸出**：資料一致性報告
- **驗收**：資料無損

## 非功能需求 (NFR)

### NFR1. 性能

#### NFR1.1 工作區啟動時間
- Pod 冷啟動：< 2 分鐘（含鏡像拉取）
- Pod 熱啟動（已存在）：< 5 秒
- 工作區恢復（從 S3 + PostgreSQL）：< 1 分鐘

#### NFR1.2 API 響應時間
- 認證與路由：< 100 ms
- 工作區查詢：< 200 ms
- 活動記錄：< 50 ms

#### NFR1.3 並發支援
- 支援 100+ 使用者同時登入
- 支援 10+ session 共用同一 Pod
- DB 連線池：20-50 連線
- Orchestrator 多副本併發控制：PostgreSQL Advisory Lock（per workspace_id 粒度）
- K8s 資源操作冪等性：409 Conflict 視為成功

### NFR2. 可靠性

#### NFR2.1 可用性
- Orchestrator 可用性：> 99.5%
- API Gateway 可用性：> 99.9%
- 工作區（Pod）恢復率：> 99% （故障後自動重啟）

#### NFR2.2 資料持久化
- S3 資料耐久性：99.999999999%（11 個 9）
- DB 事務 ACID 保證：是
- 備份頻率：每日全量 + 每小時增量

#### NFR2.3 故障恢復時間 (RTO/RPO)
- 計畫性維護 RTO：< 5 分鐘
- 非計畫故障 RTO：< 15 分鐘
- RPO（資料丟失容限）：< 5 分鐘

### NFR3. 安全性

#### NFR3.1 認證與授權
- 所有 API 端點均需 Token 驗證
- Token 過期時間：建議 1-8 小時
- 多租戶隔離：Pod namespace、S3 prefix、workspace ACL 按 workspace_id 隔離

#### NFR3.2 傳輸安全
- 所有外部通訊使用 HTTPS
- K8s 內部通訊可選 TLS
- API Gateway → Orchestrator 可配置 mTLS

#### NFR3.3 資料隔離
- 不同使用者的 S3 prefix 隔離 + IAM Policy 最小權限
- 不同 workspace 的 Pod 不能互相訪問（NetworkPolicy）
- Service/Ingress 按 workspace 隔離

#### NFR3.4 日誌審計
- 所有 API 請求記錄（user_id、action、timestamp）
- S3 存取日誌 + workspace 操作審計
- Pod 建立、刪除、恢復事件記錄
- 日誌保留期：最少 30 天

### NFR4. 可維護性

#### NFR4.1 監控
- Prometheus metrics：Pod 數、活動使用者數、API 延遲、錯誤率
- 日誌聚合：ELK 或類似
- 告警規則：CPU/Memory 異常、S3 存取錯誤率、Pod crash 等

#### NFR4.2 可觀測性
- 完整的結構化日誌（JSON 格式）
- 分佈式追蹤支援（可選 OpenTelemetry）
- Grafana 儀表板

#### NFR4.3 可配置性
- 所有參數（闊值、超時、資源限制）應在配置檔或環境變數中管理
- 不同環境（dev/staging/prod）配置隔離

### NFR5. 可擴展性

#### NFR5.1 水平擴展
- Orchestrator 支援多副本部署（透過 PostgreSQL Advisory Lock 做 per-workspace 併發控制，無需 leader election）
- API Gateway 支援多副本部署（負載均衡）
- K8s 集群可橫向擴展節點

#### NFR5.2 存儲擴展
- S3 無容量限制（按使用量計費）
- S3 跨區域複製支援災難恢復
- S3 生命週期策略自動歸檔冷資料

#### NFR5.3 服務架構
- Auth（JWT 簽發/驗證）內建於 API Gateway，減少 network hop
- Admin Service 為獨立 Pod（:8090，內部網路），負責使用者管理、workspace 狀態查詢、Pod 監控、手動回收
- Admin Service 透過 Orchestrator API 操作 K8s 資源，維持「只有 Orchestrator 碰 K8s Control Plane」原則
- Storage Service 為獨立 Pod（:8091，內部網路），負責 workspace storage CRUD、檔案操作（雙模式：Pod 在線 proxy / 離線 K8s Job）、存取權限控制
- Storage Service 直接操作 K8s API 和 DB，支援 user token + admin token 雙模式認證

## 約束條件

- **語言**：Python 3.13+
- **環境**：部署在 Kubernetes 集群（1.24+），需要 RBAC 權限
- **認證方式**：支援 JWT 或 OAuth2（POC 使用 static token）
- **入口策略**：單一公開 Gateway，不建立 per-workspace Ingress
- **DNS**：Production 需配置 DNS 記錄指向 Gateway（不需要 wildcard）
- **TLS**：在 Gateway/Ingress 層配置憑證
- **資料庫**：使用 PostgreSQL 14+（POC 使用 SQLite）

## 驗收準則

1. 新使用者首次登入可成功建立工作區 ✓
2. 同一使用者的多個 session 能共用同一 Pod ✓
3. 30 分鐘無活動後 Pod 被正確回收 ✓
4. 使用者回訪時能恢復既有資料與應用狀態 ✓
5. Gateway 路由在 Pod 回收前後保持有效 ✓
6. 系統能支援 100+ 並發使用者 ✓
7. Pod 故障時自動重啟，使用者無感知 ✓
8. API 平均響應時間 < 200 ms ✓
9. 支援 MCP 協議與多種 Agent 外掛 ✓
10. 完整的監控、日誌、審計能力 ✓

