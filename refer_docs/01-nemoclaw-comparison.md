# 14 - NemoClaw 架構比較與安全借鏡

## 概述

本文件比較 NVIDIA NemoClaw（開源 AI Agent 安全參考架構）與本專案（dbt-openclaw）的設計差異，並整理可學習的安全強化方向。

NemoClaw 專案：https://github.com/NVIDIA/NemoClaw/

## NemoClaw 簡介

NVIDIA NemoClaw 是一個開源參考架構，用於安全地運行 AI Agent（OpenClaw）。核心理念是 **「安全優先的 Agent 沙箱」**，透過三層架構組成：

- **OpenClaw** — Agent 本體（runtime、tools、memory、behavior）
- **OpenShell** — 執行環境（sandbox lifecycle、network/fs/process policy、inference routing）
- **NemoClaw** — 膠合層（CLI、versioned blueprint、state management、channel messaging）

### 關鍵安全設計

1. **Deny-by-default egress** — 所有對外連線預設封鎖，需 operator 明確允許
2. **Credential 不進 sandbox** — Agent 只看到 `inference.local`，L7 proxy 在邊界注入真實 API key
3. **四層 Policy 系統** — network / filesystem / process / inference 各自獨立宣告式 YAML 控制
4. **Landlock + seccomp** — OS 層級的 filesystem 和 syscall 限制
5. **Secret Scanner** — 攔截 Agent 將敏感資訊寫入持久化檔案
6. **Blueprint 版本化** — resolve → verify → plan → apply → status 完整生命週期

---

## 架構比較

| 面向 | dbt-openclaw (本專案) | NemoClaw (NVIDIA) |
|------|----------------------|-------------------|
| **部署單位** | K8s Pod per workspace | OpenShell sandbox per agent (k3s inside Docker) |
| **隔離層級** | K8s NetworkPolicy + PVC 隔離 | Landlock + seccomp + network namespace + process limit |
| **網路控制** | Ingress-only NetworkPolicy (deny agent-to-agent) | Egress deny-by-default + L7 proxy 檢查每個 HTTP request |
| **Credential 管理** | Agent Pod 直接持有 LLM API key (env var) | Host-side gateway 持有 key，Agent 只看到 `inference.local` |
| **LLM 路由** | ModelFactory 直連各 provider | OpenShell Gateway 統一路由，支援 model router (cost/accuracy) |
| **Policy 系統** | K8s RBAC + NetworkPolicy | 宣告式 YAML policy（network/fs/process/inference 四層） |
| **部署版本化** | 手動 deploy.sh | Versioned blueprint（resolve → verify → plan → apply） |
| **Secret 防護** | 無特殊處理 | Secret scanner（CLI redact + memory write 攔截） |
| **Filesystem 控制** | PVC 掛載隔離，session folder 權限 | Landlock — 只有 `/sandbox` 和 `/tmp` 可寫 |
| **Process 控制** | 無 | seccomp filter + ulimit -u 512 + no-new-privileges |
| **多使用者** | 原生支援（workspace_members ACL, group workspace） | 單使用者導向（每人管自己的 sandbox） |
| **IM 整合** | Channel 抽象層（Web/LINE/Slack/Teams） | Telegram/Discord/Slack（OpenShell-managed process） |
| **持久化** | PostgreSQL + PVC (Production: S3) | Host filesystem (`~/.nemoclaw/sandboxes.json`) |
| **Orchestration** | 自建 Orchestrator（Pod lifecycle, reaper, orphan repair） | Blueprint runner + OpenShell adapter |

---

## 可學習的安全強化方向

### 1. Egress NetworkPolicy（優先級：最高）

**現況：** 本專案只管 ingress，egress 完全開放。被入侵的 Pod 可對外連線洩漏資料。

**NemoClaw 做法：** Deny-by-default egress，所有對外連線需在 YAML policy 中明確允許，未知 host 會被攔截並提示 operator 審批。

**建議實作：**

```yaml
# Agent Pod Egress NetworkPolicy
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-egress-policy
  namespace: agent-system
spec:
  podSelector:
    matchLabels:
      app: agent
  policyTypes:
    - Egress
  egress:
    # 允許連到 Gateway
    - to:
        - podSelector:
            matchLabels:
              app: gateway
    # 允許連到 Orchestrator (reap-self)
    - to:
        - podSelector:
            matchLabels:
              app: orchestrator
    # 允許 DNS
    - to:
        - namespaceSelector: {}
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
    # 允許 LLM API (Production: 改為 Inference Proxy)
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
      ports:
        - protocol: TCP
          port: 443
```

---

### 2. Credential Proxy — API Key 不進 Pod（優先級：高）

**現況：** Agent Pod 透過 env var 直接持有 LLM API key，Pod 被入侵即洩漏。

**NemoClaw 做法：** Agent 只呼叫 `inference.local`（sandbox 內部 endpoint），OpenShell Gateway 的 L7 proxy 攔截請求，注入真實 credential 後轉發到 LLM provider。

**建議實作：**

```
Agent Pod                    Gateway/Inference Proxy           LLM Provider
    │                              │                              │
    ├─ POST /v1/chat/completions ─→│                              │
    │  (no API key)                │                              │
    │                              ├─ inject Authorization header ─→│
    │                              │  (Bearer sk-xxx...)           │
    │                              │                              │
    │                              │←─────── response ────────────┤
    │←──────── response ───────────┤                              │
```

實作選項：
- **方案 A：Gateway 新增 `/api/v1/inference` endpoint** — Agent 呼叫 Gateway，Gateway 注入 key 轉發
- **方案 B：獨立 Inference Proxy sidecar** — 每個 Pod 旁掛一個 proxy container，持有 key

方案 A 較簡單，且 Gateway 已有 token 驗證能力。

---

### 3. Pod SecurityContext Hardening（優先級：高，低成本）

**現況：** Agent Pod 無特殊 securityContext 設定。

**NemoClaw 做法：** seccomp + no-new-privileges + process limit + 獨立 user。

**建議實作（加入 Agent Pod spec）：**

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
resources:
  limits:
    cpu: "2"
    memory: "2Gi"
    # pids limit (K8s 1.20+)
    pid: "512"
```

需配合：
- `/tmp` 和 `/workspace` 用 emptyDir / PVC 提供可寫空間
- Agent image 確保以 non-root user 運行

---

### 4. Secret Scanner（優先級：中）

**現況：** Agent 可自由將任何內容寫入 PVC，包括可能的 API key 或 token。

**NemoClaw 做法：** 
- CLI 層自動 redact secret pattern（API key、Bearer token）
- Memory write 攔截 — Agent 嘗試寫入 memory/workspace 路徑時，scanner 檢查內容是否含 secret pattern

**建議實作：**

在 Agent 的 `LocalBackend` file write 路徑加入 scanner：

```python
import re

SECRET_PATTERNS = [
    r'(?i)(sk-[a-zA-Z0-9]{20,})',           # OpenAI key
    r'(?i)(Bearer\s+[a-zA-Z0-9\-._~+/]+=*)', # Bearer token
    r'(?i)(AKIA[0-9A-Z]{16})',               # AWS Access Key
    r'(?i)(ghp_[a-zA-Z0-9]{36})',            # GitHub PAT
    r'(?i)([a-zA-Z0-9]{32,})',               # Generic long secret (需調整閾值)
]

def scan_for_secrets(content: str) -> list[str]:
    """掃描內容中的疑似 secret，回傳匹配列表"""
    findings = []
    for pattern in SECRET_PATTERNS:
        matches = re.findall(pattern, content)
        findings.extend(matches)
    return findings
```

整合點：
- `LocalBackend.write_file()` — 寫入前掃描
- `MemoryService.save()` — 記憶存儲前掃描
- 發現 secret 時：記錄 warning log + 回傳 error 給 Agent（不寫入）

---

### 5. 宣告式 Policy Config（優先級：中，長期）

**現況：** 安全設定散落在 K8s YAML 和程式碼中，無統一管理。

**NemoClaw 做法：** 單一 YAML 定義所有 policy，per-blueprint 可客製化。

**建議實作（workspace 層級 policy）：**

```yaml
# workspace-policy.yaml (存在 workspace PVC 或 DB 中)
version: "1.0"
workspace_id: "ws-testuser1"

network:
  egress:
    allowed_hosts:
      - "api.openai.com"
      - "api.anthropic.com"
    blocked_hosts:
      - "*.torproject.org"

filesystem:
  writable_paths:
    - "/workspace/sessions/"
    - "/workspace/data/"
    - "/tmp/"
  readonly_paths:
    - "/workspace/skills/"
    - "/shared/"

tools:
  allowed:
    - "web_search"
    - "python_repl"
    - "shell"
  blocked:
    - "network_scan"

inference:
  allowed_providers:
    - "openai"
    - "anthropic"
  max_tokens_per_request: 8192
  rate_limit_per_minute: 60
```

此 policy 可由 Admin Service 管理，Orchestrator 在建立 Pod 時注入對應設定。

---

### 6. Blueprint 版本化部署（優先級：低，Production 階段）

**現況：** `deploy.sh` 一鍵部署，無版本驗證或 rollback。

**NemoClaw 做法：** Blueprint 有完整的 resolve → verify (digest check) → plan → apply → status 流程。

**建議 Production 實作：**
- Helm chart + values.yaml 版本化
- Image signing (cosign) + admission controller (Kyverno/OPA) 驗證 image digest
- ArgoCD GitOps 自動 sync + rollback

---

## 本專案的優勢（NemoClaw 未涵蓋）

| 本專案優勢 | 說明 |
|-----------|------|
| 原生多使用者 + ACL | workspace_members 表，owner/admin/member/readonly 角色控制 |
| Group workspace 協作 | 多人共享 workspace，shared PVC 掛載 |
| 集中式 Orchestrator | Pod lifecycle 自動管理、idle reap、orphan repair |
| Session 管理 | 多 session 共用 Pod，session-scoped 隔離 |
| Context Compaction | 自動對話壓縮，防 context window overflow |
| Skill 系統 | 三層優先級（shared > bundled > workspace）、動態注入 |
| Storage Service | 獨立檔案管理服務，雙模式操作（Pod 在線 proxy / 離線 K8s Job） |
| Admin Service | 完整的管理後台，user/workspace/member CRUD |
| IM Channel 抽象層 | 統一 MessageBus + ChannelManager，可插拔 adapter |

---

## 實作優先順序

| 優先級 | 項目 | 預估工作量 | 影響 |
|--------|------|-----------|------|
| P0 | Credential Proxy (API key 不進 Pod) | 2-3 天 | 防止最嚴重的 key 洩漏 |
| P0 | Egress NetworkPolicy | 0.5 天 | 防止資料外洩到任意外部 |
| P1 | Pod securityContext hardening | 0.5 天 | 低成本高收益，限制攻擊面 |
| P1 | Secret Scanner | 1-2 天 | 防止 Agent 持久化敏感資訊 |
| P2 | 宣告式 Policy Config | 3-5 天 | 統一安全管理，per-workspace 客製 |
| P3 | Blueprint 版本化部署 | Production 階段 | Supply chain safety |

---

## 結論

NemoClaw 的核心哲學是 **「Agent 是不可信的」** — 所有安全機制都假設 Agent 可能被 prompt injection 或 tool abuse 攻擊。本專案在多使用者管理和 K8s orchestration 上更成熟，但在 **Pod 內部安全**（credential isolation、egress control、process hardening）上應借鏡 NemoClaw 的設計。

兩個專案的定位不同：
- **NemoClaw** — 單使用者、安全沙箱導向、CLI 驅動
- **dbt-openclaw** — 多使用者、平台服務導向、API 驅動

結合兩者優勢，可以打造一個既有完善多租戶管理，又有深度安全防護的 Agent 平台。
