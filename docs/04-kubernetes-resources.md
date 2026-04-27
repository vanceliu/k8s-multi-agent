# 04 Kubernetes 資源

## 1. 資源部署清單

本文件提供可直接部署的 Kubernetes YAML 清單。可以單獨執行，或整合到 Helm Chart。

### 1.1 Namespace 與 RBAC

#### namespace.yaml

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: agent-platform
  labels:
    name: agent-platform
    app: k8s-agent-platform
---
# ServiceAccount for Orchestrator
apiVersion: v1
kind: ServiceAccount
metadata:
  name: orchestrator-sa
  namespace: agent-platform
---
# ClusterRole for Orchestrator (Pod/Service 操作，Ingress 僅查詢)
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: orchestrator-role
rules:
# Pod 操作
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["create", "delete", "get", "list", "watch", "patch"]
- apiGroups: [""]
  resources: ["pods/log"]
  verbs: ["get"]
# Service 操作
- apiGroups: [""]
  resources: ["services"]
  verbs: ["create", "delete", "get", "list", "watch", "patch"]
# Ingress 操作（僅查詢 Gateway 狀態）
- apiGroups: ["networking.k8s.io"]
  resources: ["ingresses"]
  verbs: ["get", "list", "watch"]
# ConfigMap 操作
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["create", "delete", "get", "list", "watch", "patch"]
# Events（用於監控）
- apiGroups: [""]
  resources: ["events"]
  verbs: ["create", "get", "list", "watch"]
# PVC 操作（workspace PVC 管理）
- apiGroups: [""]
  resources: ["persistentvolumeclaims"]
  verbs: ["create", "delete", "get", "list", "watch", "patch"]
# Job 操作（session cleanup Job）
- apiGroups: ["batch"]
  resources: ["jobs"]
  verbs: ["create", "delete", "get", "list", "watch"]
---
# ClusterRoleBinding
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: orchestrator-role-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: orchestrator-role
subjects:
- kind: ServiceAccount
  name: orchestrator-sa
  namespace: agent-platform
---
# Role for Agent Pods (每個 Pod 內的最小權限)
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: agent-pod-role
  namespace: agent-platform
rules:
# Pod 可讀取自己的配置與 ConfigMap
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "list", "watch"]
- apiGroups: [""]
  resources: ["endpoints"]
  verbs: ["get", "list", "watch"]
---
# RoleBinding
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: agent-pod-role-binding
  namespace: agent-platform
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: agent-pod-role
subjects:
- kind: ServiceAccount
  name: default
  namespace: agent-platform
```

---

### 1.2 StorageClass（僅供 DB 使用，workspace 資料存於 S3）

> **注意**：workspace 使用者資料已改為 AWS S3（透過 IRSA 存取），不再使用 PVC。以下 StorageClass 僅供 PostgreSQL DB 使用。

#### storage-class.yaml

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: agent-platform-storage
provisioner: kubernetes.io/aws-ebs           # 或其他 provisioner
allowVolumeExpansion: true
parameters:
  type: gp3
  iops: "3000"
  fstype: ext4
volumeBindingMode: WaitForFirstConsumer      # 等待 Pod 綁定後再建立
```

---

### 1.3 Orchestrator Deployment

#### orchestrator-deployment.yaml

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: orchestrator
  namespace: agent-platform
  labels:
    app: k8s-agent-platform
    component: orchestrator
spec:
  replicas: 2                   # 高可用
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      app: k8s-agent-platform
      component: orchestrator
  template:
    metadata:
      labels:
        app: k8s-agent-platform
        component: orchestrator
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8081"
        prometheus.io/path: "/metrics"
    spec:
      serviceAccountName: orchestrator-sa
      containers:
      - name: orchestrator
        image: your-registry/k8s-agent-orchestrator:latest
        imagePullPolicy: IfNotPresent
        
        ports:
        - name: http
          containerPort: 8080
          protocol: TCP
        - name: metrics
          containerPort: 8081
          protocol: TCP
        
        env:
        - name: ENVIRONMENT
          value: "production"
        - name: LOG_LEVEL
          value: "info"
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: db-credentials
              key: url
        - name: K8S_NAMESPACE
          value: agent-platform
        - name: AGENT_IMAGE
          value: "your-registry/k8s-agent-runtime:latest"
        - name: IDLE_TIMEOUT_MINUTES
          value: "30"
        - name: REAP_CHECK_INTERVAL_SECONDS
          value: "300"
        - name: GATEWAY_HOST
          value: "api.yourdomain.com"
        - name: POD_NAME
          valueFrom:
            fieldRef:
              fieldPath: metadata.name
        - name: POD_NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
        
        volumeMounts:
        - name: config
          mountPath: /etc/config
          readOnly: true
        
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
          failureThreshold: 2
        
        resources:
          requests:
            cpu: "200m"
            memory: "512Mi"
          limits:
            cpu: "1000m"
            memory: "2Gi"
      
      volumes:
      - name: config
        configMap:
          name: orchestrator-config
          optional: true
      
      terminationGracePeriodSeconds: 30
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            podAffinityTerm:
              labelSelector:
                matchExpressions:
                - key: app
                  operator: In
                  values:
                  - k8s-agent-platform
                - key: component
                  operator: In
                  values:
                  - orchestrator
              topologyKey: kubernetes.io/hostname
---
# Service for Orchestrator
apiVersion: v1
kind: Service
metadata:
  name: orchestrator
  namespace: agent-platform
  labels:
    app: k8s-agent-platform
    component: orchestrator
spec:
  type: ClusterIP
  selector:
    app: k8s-agent-platform
    component: orchestrator
  ports:
  - name: http
    port: 80
    targetPort: 8080
    protocol: TCP
  - name: metrics
    port: 8081
    targetPort: 8081
    protocol: TCP
```

---

### 1.4 ConfigMap

#### orchestrator-config.yaml

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: orchestrator-config
  namespace: agent-platform
data:
  config.yaml: |
    server:
      host: 0.0.0.0
      port: 8080
      metrics_port: 8081
    
    database:
      max_connections: 20
      timeout_seconds: 10
    
    kubernetes:
      namespace: agent-platform
      storage_class: standard
      s3_workspace_bucket: agent-workspaces
    
    agent:
      image: your-registry/k8s-agent-runtime:latest
      resources:
        requests:
          cpu: 100m
          memory: 256Mi
        limits:
          cpu: 500m
          memory: 1Gi
    
    lifecycle:
      idle_timeout_minutes: 30
      reap_check_interval_seconds: 300
      pod_termination_grace_period_seconds: 30
    
    network:
      gateway_host: api.yourdomain.com
      tls_issuer: letsencrypt-prod
      tls_issuer_kind: ClusterIssuer
```

---

### 1.5 Secret（敏感信息）

#### db-credentials-secret.yaml

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: db-credentials
  namespace: agent-platform
type: Opaque
stringData:
  url: "postgresql://user:password@postgres.example.com:5432/agent_platform_db"
  username: "agent_service"
  password: "secure_password"
---
# Docker Registry Secret（若使用私有倉庫）
apiVersion: v1
kind: Secret
metadata:
  name: docker-registry-credentials
  namespace: agent-platform
type: docker-registry
data:
  .dockercfg: <base64-encoded-docker-config>
```

---

### 1.6 後台任務（ClusterRole 執行閒置回收）

#### background-job-cronjob.yaml

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: reap-idle-workspaces
  namespace: agent-platform
spec:
  # 每 5 分鐘執行一次
  schedule: "*/5 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: orchestrator-sa
          containers:
          - name: reaper
            image: your-registry/k8s-agent-orchestrator:latest
            imagePullPolicy: IfNotPresent
            command: ["python", "-m", "app.orchestrator.reaper"]
            env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: db-credentials
                  key: url
            - name: K8S_NAMESPACE
              value: agent-platform
            - name: IDLE_TIMEOUT_MINUTES
              value: "30"
            - name: DRY_RUN
              value: "false"
          restartPolicy: OnFailure
          backoffLimit: 3
```

---

### 1.7 Gateway Deployment（對外單一入口）

> 本章節補齊 `gateway-service` 的實體來源。外部流量只進入 Gateway；Orchestrator 僅管理每 workspace `Pod/Service`。

#### gateway-deployment.yaml

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gateway
  namespace: agent-platform
  labels:
    app: k8s-agent-platform
    component: gateway
spec:
  replicas: 2
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels:
      app: k8s-agent-platform
      component: gateway
  template:
    metadata:
      labels:
        app: k8s-agent-platform
        component: gateway
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8081"
        prometheus.io/path: "/metrics"
    spec:
      containers:
      - name: gateway
        image: your-registry/k8s-agent-gateway:latest
        imagePullPolicy: IfNotPresent
        ports:
        - name: http
          containerPort: 8080
          protocol: TCP
        - name: metrics
          containerPort: 8081
          protocol: TCP
        env:
        - name: ENVIRONMENT
          value: "production"
        - name: LOG_LEVEL
          value: "info"
        - name: ORCHESTRATOR_URL
          value: "http://orchestrator.agent-platform.svc.cluster.local"
        - name: JWT_ISSUER
          value: "your-issuer"
        - name: JWT_AUDIENCE
          value: "your-audience"
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 20
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
            memory: "256Mi"
          limits:
            cpu: "1000m"
            memory: "1Gi"
```

#### gateway-service.yaml

```yaml
apiVersion: v1
kind: Service
metadata:
  name: gateway-service
  namespace: agent-platform
  labels:
    app: k8s-agent-platform
    component: gateway
spec:
  type: ClusterIP
  selector:
    app: k8s-agent-platform
    component: gateway
  ports:
  - name: http
    port: 80
    targetPort: 8080
    protocol: TCP
  - name: metrics
    port: 8081
    targetPort: 8081
    protocol: TCP
```

---

## 2. 使用者 Agent Pod 的動態資源

以下為 Orchestrator 運行時動態建立的資源示例。

### 2.1 IRSA — IAM Role 與 ServiceAccount（S3 存取）

> Agent Pod 透過 IRSA（IAM Roles for Service Accounts）取得 AWS S3 存取權限，無需在容器中配置 AWS 憑證。

#### Terraform 配置（IRSA）

```hcl
# IAM Role — 供 Agent Pod 使用
resource "aws_iam_role" "agent_s3_role" {
  name = "k8s-agent-s3-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Principal = {
        Federated = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/${replace(data.aws_eks_cluster.main.identity[0].oidc[0].issuer, "https://", "")}"
      }
      Condition = {
        StringEquals = {
          "${replace(data.aws_eks_cluster.main.identity[0].oidc[0].issuer, "https://", "")}:sub"   = "system:serviceaccount:agent-platform:agent-s3-access"
          "${replace(data.aws_eks_cluster.main.identity[0].oidc[0].issuer, "https://", "")}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

# IAM Policy — S3 最小權限
resource "aws_iam_policy" "agent_s3_policy" {
  name = "k8s-agent-s3-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
          "s3:ListBucket", "s3:HeadObject", "s3:GetBucketLocation"
        ]
        Resource = [
          "arn:aws:s3:::agent-workspaces-${data.aws_caller_identity.current.account_id}",
          "arn:aws:s3:::agent-workspaces-${data.aws_caller_identity.current.account_id}/users/*",
          "arn:aws:s3:::agent-workspaces-${data.aws_caller_identity.current.account_id}/groups/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "agent_s3_attach" {
  role       = aws_iam_role.agent_s3_role.name
  policy_arn = aws_iam_policy.agent_s3_policy.arn
}
```

#### agent-serviceaccount.yaml

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: agent-s3-access
  namespace: agent-platform
  annotations:
    eks.amazonaws.com/role-arn: "arn:aws:iam::ACCOUNT_ID:role/k8s-agent-s3-role"
```

---

### 2.2 Pod（每 workspace 臨時，可被回收）

#### agent-pod-template.yaml

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: "pod-ws-{workspace_id}"
  namespace: agent-platform
  labels:
    workspace_id: "{workspace_id}"
    app: k8s-agent-platform
    component: agent
  annotations:
    created_by: orchestrator
    created_at: "{timestamp}"
spec:
  serviceAccountName: agent-s3-access
  containers:
  - name: agent
    image: "{agent_image}"
    imagePullPolicy: IfNotPresent

    ports:
    - name: http
      containerPort: 8080
      protocol: TCP

    env:
    - name: WORKSPACE_ID
      value: "{workspace_id}"
    - name: S3_WORKSPACE_BUCKET
      value: "agent-workspaces-{account_id}"
    - name: S3_PREFIX
      value: "{s3_prefix}"               # users/{user_id}/ 或 groups/{group_id}/
    - name: AWS_REGION
      value: "{aws_region}"
    - name: ORCHESTRATOR_URL
      value: "http://orchestrator.agent-platform.svc.cluster.local"
    - name: POD_NAME
      valueFrom:
        fieldRef:
          fieldPath: metadata.name
    - name: POD_NAMESPACE
      valueFrom:
        fieldRef:
          fieldPath: metadata.namespace

    volumeMounts:
    - name: workspace
      mountPath: /workspace/{workspace_id}    # workspace PVC（永久儲存）
    - name: cache
      mountPath: /tmp/agent-cache             # ephemeral cache，Pod 刪除後消失

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

    lifecycle:
      preStop:
        httpGet:                              # 通知 Agent 開始 graceful shutdown
          path: /shutdown                     # Agent 標記 _shutting_down=True，開始 drain
          port: 8080
        # K8s 在 preStop 完成後才發送 SIGTERM
        # Agent 收到 SIGTERM 後執行 runtime.shutdown()（保存 S3、關閉連線）

    resources:
      requests:
        cpu: "{cpu_request}"
        memory: "{memory_request}"
      limits:
        cpu: "{cpu_limit}"
        memory: "{memory_limit}"

  volumes:
  - name: workspace
    persistentVolumeClaim:
      claimName: pvc-{workspace_id}           # workspace PVC（永久儲存）
  - name: cache
    emptyDir:
      sizeLimit: 500Mi                    # ephemeral storage，無 PVC

  restartPolicy: OnFailure
  terminationGracePeriodSeconds: 30
  affinity:
    podAntiAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 50
        podAffinityTerm:
          labelSelector:
            matchExpressions:
            - key: component
              operator: In
              values:
              - agent
          topologyKey: kubernetes.io/hostname
```

---

### 2.3 Session Cleanup Job（由 Orchestrator 動態建立）

#### cleanup-job-template.yaml

Session 刪除時，Orchestrator 建立短暫 K8s Job 掛載 workspace PVC，刪除 `sessions/{session_id}/` 資料夾。

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: "cleanup-{session_id_prefix}-{timestamp}"
  namespace: agent-platform
  labels:
    workspace_id: "{workspace_id}"
    app: k8s-agent-platform
    component: session-cleanup
spec:
  ttlSecondsAfterFinished: 60          # Job 完成後 60 秒自動清理
  backoffLimit: 2
  template:
    spec:
      containers:
      - name: cleanup
        image: busybox:latest
        command: ["sh", "-c", "rm -rf /workspace/{workspace_id}/sessions/{session_id}"]
        volumeMounts:
        - name: workspace
          mountPath: /workspace/{workspace_id}
      volumes:
      - name: workspace
        persistentVolumeClaim:
          claimName: pvc-{workspace_id}
      restartPolicy: Never
```

---

### 2.4 Service（每 workspace）

#### agent-service-template.yaml

```yaml
apiVersion: v1
kind: Service
metadata:
  name: "svc-ws-{workspace_id}"
  namespace: agent-platform
  labels:
    workspace_id: "{workspace_id}"
    app: k8s-agent-platform
    component: agent-service
  annotations:
    created_by: orchestrator
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
  sessionAffinity: ClientIP
  sessionAffinityConfig:
    clientIP:
      timeoutSeconds: 10800
```

---

### 2.5 Gateway Ingress（單一公開入口）

#### gateway-ingress-template.yaml

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: gateway-public-ingress
  namespace: agent-platform
  labels:
    app: k8s-agent-platform
    component: gateway-ingress
  annotations:
    kubernetes.io/ingress.class: nginx
    cert-manager.io/cluster-issuer: "{tls_issuer}"
    nginx.ingress.kubernetes.io/proxy-connect-timeout: "600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "600"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "600"
spec:
  ingressClassName: nginx
  tls:
  - hosts:
    - "api.yourdomain.com"
    secretName: "tls-api-yourdomain"
  rules:
  - host: "api.yourdomain.com"
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: gateway-service
            port:
              number: 80
```

> Gateway 在應用層完成 token 驗證與 workspace ACL 檢查，並依 `workspace_id` 內部路由到 `svc-ws-{workspace_id}`。
>
> 路由契約（建議）：
> - 對外固定入口：`https://api.yourdomain.com`
> - 控制 API：`POST /api/v1/workspaces/ensure` 回傳 `gateway_endpoint` 與 `gateway_route`
> - 業務流量：Client 呼叫 `https://api.yourdomain.com{gateway_route}`
> - Gateway 轉發：解析 token → 驗證 `workspace_members` 存取權限 → 轉發到 `svc-ws-{workspace_id}:80`
> - 回收後恢復：若 `svc-ws-{workspace_id}` 不存在，Gateway 先觸發 `ensure_session_workspace` 再重試一次轉發

### 2.6 Gateway 安全與網路建議

```yaml
# NetworkPolicy: Agent Pod 只接受 Gateway 和 Storage Service 的 ingress
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-allow-gateway-only
  namespace: agent-platform
spec:
  podSelector:
    matchLabels:
      component: agent
  policyTypes:
  - Ingress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          component: gateway
    - podSelector:
        matchLabels:
          component: storage-service
    ports:
    - protocol: TCP
      port: 8080
---
# NetworkPolicy: Orchestrator 接受 Gateway、Admin、Storage Service、Agent 的 ingress
# Agent 需要連線 Orchestrator 來報告 idle reap-self
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: orchestrator-allow-gateway-admin
  namespace: agent-platform
spec:
  podSelector:
    matchLabels:
      component: orchestrator
  policyTypes:
  - Ingress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          component: gateway
    - podSelector:
        matchLabels:
          component: admin
    - podSelector:
        matchLabels:
          component: storage-service
    - podSelector:
        matchLabels:
          component: agent
---
# NetworkPolicy: Storage Service 接受 Gateway、Orchestrator、Admin 的 ingress
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: storage-service-allow-gateway
  namespace: agent-platform
spec:
  podSelector:
    matchLabels:
      component: storage-service
  policyTypes:
  - Ingress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          component: gateway
    - podSelector:
        matchLabels:
          component: orchestrator
    - podSelector:
        matchLabels:
          component: admin
```

安全重點：
- 只在 Gateway 做外部 token 驗證；Agent 只信任來自 Gateway 和 Storage Service 的內部流量。
- Agent Pod 可主動連線 Orchestrator（用於 idle reap-self 通知），但不接受 Orchestrator 的 ingress。
- Admin Service 為 ClusterIP，前端透過 Gateway proxy（`/api/v1/admin/*`）存取，Gateway 驗證 admin token 後轉發。
- Storage Service 為 ClusterIP，前端透過 Gateway proxy（`/api/v1/workspaces/{wid}/storage/*`）存取，支援 user token + admin token 雙模式認證。Storage Service 可連線 Agent Pod（檔案操作 proxy）和 K8s API（PVC CRUD、File Operation Job）。
- Gateway 轉發時加上受信 header（如 `X-User-Id`, `X-Session-Id`），並在 Agent 端檢查來源網段/ServiceAccount。
- 對外 TLS 終止於 Ingress/Gateway；叢集內可視需求加 mTLS。
- 為 Gateway 設定 rate limit 與 request size 上限，避免大流量或惡意請求壓垮單一使用者 Pod。

---

## 3. 部署步驟

### 3.1 一次性設置

```bash
# 1. 建立 Namespace 與 RBAC
kubectl apply -f namespace.yaml

# 2. 建立 StorageClass（可選）
kubectl apply -f storage-class.yaml

# 3. 建立 Secret
kubectl apply -f db-credentials-secret.yaml

# 4. 建立 ConfigMap
kubectl apply -f orchestrator-config.yaml

# 5. 部署 Orchestrator
kubectl apply -f orchestrator-deployment.yaml

# 6. 部署 Gateway（Deployment + Service + Public Ingress）
kubectl apply -f gateway-deployment.yaml
kubectl apply -f gateway-service.yaml
kubectl apply -f gateway-ingress-template.yaml

# 7. 設置後台任務
kubectl apply -f background-job-cronjob.yaml
```

### 3.2 驗證部署

```bash
# 檢查 Namespace
kubectl get ns agent-platform

# 檢查 Orchestrator Pod
kubectl get pods -n agent-platform -l component=orchestrator

# 檢查 Gateway Pod
kubectl get pods -n agent-platform -l component=gateway

# 檢查 Gateway Service / Ingress
kubectl get svc gateway-service -n agent-platform
kubectl get ingress gateway-public-ingress -n agent-platform

# 檢查 RBAC
kubectl get serviceaccounts -n agent-platform
kubectl get clusterroles | grep orchestrator
kubectl get clusterrolebindings | grep orchestrator

# 檢查日誌
kubectl logs -n agent-platform -l component=orchestrator -f
kubectl logs -n agent-platform -l component=gateway -f
```

### 3.3 Gateway 路由驗證（建議）

```bash
# 1) 先取得 token
curl -X POST https://api.yourdomain.com/api/v1/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"user@example.com","password":"secret","grant_type":"password"}'

# 2) 建立或恢復工作區
curl -X POST https://api.yourdomain.com/api/v1/workspaces/ensure \
  -H 'Authorization: Bearer <ACCESS_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user123","session_id":"session_abc123","resource_tier":"standard"}'

# 3) 呼叫 gateway_route（示例）
curl -X POST https://api.yourdomain.com/workspaces/ws_user123/mcp/execute \
  -H 'Authorization: Bearer <ACCESS_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"execute_task","params":{"task_id":"t1","task_type":"analyze"},"id":"1"}'
```
