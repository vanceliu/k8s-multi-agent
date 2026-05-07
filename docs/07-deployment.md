# 07 部署指南

本文件提供完整的本地開發與生產部署流程。

## 0. POC 快速部署（已驗證）

POC 將 Gateway、Orchestrator、Agent 三個元件全部容器化部署於 K8s 叢集內。

### 0.1 前置要求

```bash
# macOS
brew install python@3.13
brew install kubectl
brew install kind
brew install podman   # 或 docker

# 驗證
python3 --version     # Python 3.13+
kubectl version --client
kind version
podman version        # 或 docker version
```

### 0.2 一鍵部署

```bash
bash poc/k8s/deploy.sh
```

此腳本會自動：
1. 建立 kind 叢集（含 NodePort mapping，Gateway 對外 :8000）
2. 部署 Namespace + RBAC
3. 構建三個 Docker 鏡像（Agent、Orchestrator、Gateway）
4. 載入鏡像到 kind 叢集
5. 部署 Orchestrator（Deployment + ClusterIP Service）
6. 部署 Gateway（Deployment + NodePort Service）

### 0.3 驗證

```bash
# 健康檢查
curl -s http://localhost:8000/health

# 建立工作區
curl -s -X POST http://localhost:8000/api/v1/workspaces/ensure \
  -H "Authorization: Bearer $POC_STATIC_TOKEN:testuser1" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "sess-001"}'

# 檢查 K8s 資源
kubectl get pods,svc -n agent-platform

# 自動化 E2E 測試
bash poc/tests/e2e_test.sh
```

### 0.4 清理

```bash
KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name agent-poc
```

---

## 1. 本地開發環境（Production 規劃）

### 1.1 前置要求

```bash
# macOS
brew install python@3.13
brew install postgresql
brew install kubectl
brew install minikube
brew install docker
brew install helm

# 驗證版本
python --version        # Python 3.13+
pg_config --version     # PostgreSQL 14+
kubectl version --client
minikube version
docker --version
helm version
```

### 1.2 初始化本地 K8s 集群

```bash
# 啟動 minikube
minikube start --cpus=4 --memory=8192 --driver=docker

# 驗證
kubectl cluster-info
kubectl get nodes

# 啟用 ingress 外掛
minikube addons enable ingress

# 配置 local Docker registry（可選）
minikube addons enable registry
```

### 1.3 啟動 PostgreSQL

```bash
# 使用 Docker
docker run -d \
  --name postgres-dev \
  -e POSTGRES_USER=agent_user \
  -e POSTGRES_PASSWORD=agent_password \
  -e POSTGRES_DB=agent_platform_db \
  -p 5432:5432 \
  postgres:14

# 或使用 Homebrew
brew services start postgresql

# 建立資料庫
psql -U postgres << EOF
CREATE USER agent_user WITH PASSWORD 'agent_password';
CREATE DATABASE agent_platform_db OWNER agent_user;
EOF
```

### 1.4 設置 Python 虛擬環境

```bash
cd /Users/vanceliu/Documents/Private/Project/GitHub/dbt-openclaw

# 建立虛擬環境
python -m venv .venv

# 啟用
source .venv/bin/activate

# 安裝依賴
pip install -r requirements.txt
pip install -r requirements-dev.txt  # 開發依賴

# 執行資料庫遷移
alembic upgrade head
```

### 1.5 環境變數配置

```bash
# .env.local
export ENVIRONMENT=development
export LOG_LEVEL=debug
export DATABASE_URL="postgresql://agent_user:agent_password@localhost:5432/agent_platform_db"
export SQLALCHEMY_ECHO=true

export K8S_NAMESPACE=agent-platform
export K8S_CONTEXT=minikube

export AGENT_IMAGE=k8s-agent-runtime:latest
export ORCHESTRATOR_URL=http://localhost:8080

export IDLE_TIMEOUT_MINUTES=5  # 開發時設短一點
export REAP_CHECK_INTERVAL_SECONDS=30

# CWA 台灣氣象署開放資料 API（Agent research_agent 使用）
export CWA_API_BASE="https://opendata.cwa.gov.tw/api/v1/rest/datastore"
export CWA_API_KEY="your-cwa-api-key"  # 至 https://opendata.cwa.gov.tw 申請

# Agent SSE Display Mode（控制 tool_call/tool_result 事件可見性）
# "normal": 僅 content/file/error；"full_history": 額外輸出 tool_call/tool_result
export AGENT_DISPLAY_MODE="normal"

export DOMAIN_SUFFIX=localhost
export API_PORT=8080
export MCP_PORT=8080
```

### 1.6 本地服務啟動

#### 啟動 Orchestrator

```bash
# 終端 1
source .venv/bin/activate
export $(cat .env.local | xargs)
python -m app.orchestrator.main
```

#### 啟動 API Gateway

```bash
# 終端 2
source .venv/bin/activate
export $(cat .env.local | xargs)
python -m app.api.gateway
```

#### 驗證本地運行

```bash
# 測試 Orchestrator
curl http://localhost:8080/health

# 測試 API Gateway
curl http://localhost:8000/api/v1/health
```

---

## 2. 容器構建

### 2.1 構建 Orchestrator 鏡像

```bash
# 建立 Dockerfile
cat > docker/Dockerfile.orchestrator << 'EOF'
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    git curl jq postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/app/
COPY docker/entrypoint-orchestrator.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s \
    CMD curl -f http://localhost:8080/health || exit 1

EXPOSE 8080 8081

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app.orchestrator.main"]
EOF

# 構建
docker build \
  -f docker/Dockerfile.orchestrator \
  -t your-registry/k8s-agent-orchestrator:latest \
  .

# 推送（若使用遠端倉庫）
docker push your-registry/k8s-agent-orchestrator:latest
```

### 2.2 構建 Agent 鏡像

> **注意**：Dockerfile.agent 使用 `requirements.txt` 管理 Python 依賴，新增套件時更新 `requirements.txt` 即可。

```bash
# 構建
docker build \
  -f docker/Dockerfile.agent \
  -t your-registry/k8s-agent-runtime:latest \
  .

# 推送
docker push your-registry/k8s-agent-runtime:latest
```

### 2.3 本地 minikube 鏡像載入

```bash
# 若使用 minikube 內建 registry
eval $(minikube docker-env)
docker build -f docker/Dockerfile.orchestrator -t k8s-agent-orchestrator:latest .
docker build -f docker/Dockerfile.agent -t k8s-agent-runtime:latest .

# 或使用 docker push 到 minikube registry
minikube docker-env
docker tag k8s-agent-orchestrator:latest localhost:5000/k8s-agent-orchestrator:latest
docker push localhost:5000/k8s-agent-orchestrator:latest
```

---

## 3. Kubernetes 部署（開發環境）

### 3.1 一鍵部署腳本

```bash
#!/bin/bash
# scripts/deploy-dev.sh

set -e

NAMESPACE=agent-platform
CONTEXT=minikube

echo "=== Deploying to $CONTEXT ===="

# 1. 建立 Namespace
kubectl create namespace $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -

# 2. 建立 Secret
kubectl create secret generic db-credentials \
  --from-literal=url="postgresql://agent_user:agent_password@postgres:5432/agent_platform_db" \
  -n $NAMESPACE \
  --dry-run=client -o yaml | kubectl apply -f -

# 3. 應用資源
kubectl apply -f infra/k8s/namespace.yaml
kubectl apply -f infra/k8s/rbac.yaml                    # 含 batch/jobs 權限（session cleanup Job 需要）
kubectl apply -f infra/k8s/orchestrator-deployment.yaml
kubectl apply -f infra/k8s/gateway-deployment.yaml

# 4. 等待 Pod 就緒
echo "Waiting for Orchestrator to be ready..."
kubectl wait --for=condition=ready pod \
  -l component=orchestrator \
  -n $NAMESPACE \
  --timeout=300s

# 5. 埠轉發（本地開發）
kubectl port-forward -n $NAMESPACE \
  svc/orchestrator 8080:80 &

echo "✓ Deployment complete"
echo "Orchestrator: http://localhost:8080"
```

執行：
```bash
chmod +x scripts/deploy-dev.sh
./scripts/deploy-dev.sh
```

### 3.2 驗證部署

```bash
# 檢查 Pod 狀態
kubectl get pods -n agent-platform

# 查看日誌
kubectl logs -n agent-platform -l component=orchestrator -f

# 檢查 Service
kubectl get svc -n agent-platform

# 測試 API
kubectl port-forward -n agent-platform svc/orchestrator 8080:80
curl http://localhost:8080/health
```

---

## 4. Helm 部署（生產環境）

### 4.1 Helm Chart 結構

```
helm/k8s-agent-platform/
├── Chart.yaml
├── values.yaml
├── values-dev.yaml
├── values-prod.yaml
└── templates/
    ├── namespace.yaml
    ├── rbac.yaml
    ├── secret.yaml
    ├── configmap.yaml
    ├── orchestrator-deployment.yaml
    ├── orchestrator-service.yaml
    ├── background-job.yaml
    └── ingress.yaml
```

### 4.2 部署命令

```bash
# 開發環境
helm install k8s-agent-platform ./helm/k8s-agent-platform \
  --namespace agent-platform \
  --create-namespace \
  -f helm/values-dev.yaml

# 生產環境
helm install k8s-agent-platform ./helm/k8s-agent-platform \
  --namespace agent-platform \
  --create-namespace \
  -f helm/values-prod.yaml

# 升級
helm upgrade k8s-agent-platform ./helm/k8s-agent-platform \
  -f helm/values-prod.yaml

# 回滾
helm rollback k8s-agent-platform 1
```

---

## 5. 資料庫遷移

### 5.1 初始化資料庫

```bash
# 初次部署
alembic upgrade head

# 建立新遷移
alembic revision --autogenerate -m "add users table"

# 查看遷移歷史
alembic history

# 特定版本回滾
alembic downgrade -1
```

### 5.2 備份與恢復

```bash
# 備份
pg_dump -h localhost -U agent_user agent_platform_db > backup.sql

# 恢復
psql -h localhost -U agent_user agent_platform_db < backup.sql

# 使用 K8s 持久化備份
kubectl exec -it postgres-pod -n agent-platform -- \
  pg_dump -U agent_user agent_platform_db | gzip > db-backup.sql.gz
```

---

## 6. 監控與日誌

### 6.1 啟用 Prometheus 監控

```bash
# 安裝 kube-prometheus-stack（可選）
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace

# 驗證
kubectl get pods -n monitoring
```

### 6.2 查看日誌

```bash
# Orchestrator 日誌
kubectl logs -n agent-platform -l component=orchestrator -f

# Agent Pod 日誌
kubectl logs -n agent-platform -l component=agent pod-ws-user123

# 上一個 Pod 的日誌（若已重啟）
kubectl logs -n agent-platform --previous pod-ws-user123
```

### 6.3 設置日誌聚合

```bash
# 使用 ELK Stack（可選）
helm install elasticsearch elastic/elasticsearch -n logging --create-namespace
helm install logstash elastic/logstash -n logging
helm install kibana elastic/kibana -n logging

# 配置 Fluentd 轉發
kubectl apply -f infra/k8s/fluentd-daemonset.yaml
```

---

## 7. 故障排查

### 7.1 常見問題

#### Pod 無法啟動

```bash
# 檢查事件
kubectl describe pod pod-ws-user123 -n agent-platform

# 檢查容器日誌
kubectl logs pod-ws-user123 -n agent-platform

# 檢查 S3 存取
aws s3 ls s3://agent-workspace-data/users/user123/

# 檢查存儲（僅 DB PVC）
kubectl get pv
```

#### 數據庫連線失敗

```bash
# 檢查 Secret
kubectl get secret db-credentials -n agent-platform -o yaml

# 驗證連線
kubectl run -it --rm debug --image=postgres:14 -- \
  psql postgresql://agent_user:agent_password@postgres:5432/agent_platform_db
```

#### Ingress 無法路由

```bash
# 檢查 Ingress
kubectl get ingress -n agent-platform
kubectl describe ingress gateway-public-ingress -n agent-platform

# 檢查 DNS（Gateway 單一入口）
nslookup api.yourdomain.com
# 應指向 ingress-nginx 的外部 IP

# 檢查 ingress-nginx 狀態
kubectl get pods -n ingress-nginx
```

### 7.2 除錯命令

```bash
# 進入 Pod
kubectl exec -it pod-ws-user123 -n agent-platform -- /bin/bash

# 埠轉發進行本地測試
kubectl port-forward pod-ws-user123 8080:8080 -n agent-platform

# 檢查資源使用情況
kubectl top nodes
kubectl top pods -n agent-platform

# 查看事件
kubectl get events -n agent-platform --sort-by='.lastTimestamp'
```

---

## 8. 清理與維護

### 8.1 清理資源

```bash
# 刪除單個 Pod
kubectl delete pod pod-ws-user123 -n agent-platform

# 刪除整個 Deployment
kubectl delete deployment orchestrator -n agent-platform

# 刪除 Namespace（會清理該命名空間內所有資源）
kubectl delete namespace agent-platform

# 刪除 S3 工作區資料（注意：會永久刪除使用者資料）
aws s3 rm s3://agent-workspaces-$ACCOUNT_ID/users/user123/ --recursive
```

### 8.2 定期維護

```bash
# 清理已完成的 Job
kubectl delete job -n agent-platform --field-selector status.successful=1

# 清理已終止的 Pod
kubectl delete pods -n agent-platform --field-selector=status.phase=Failed

# 備份定期執行
0 2 * * * /path/to/scripts/backup.sh >> /var/log/backup.log 2>&1
```

---

## 9. 性能調優

### 9.1 Resource 配置建議

```yaml
# 開發環境
orchestrator:
  requests:
    cpu: 100m
    memory: 256Mi
  limits:
    cpu: 500m
    memory: 1Gi

# 生產環境
orchestrator:
  requests:
    cpu: 500m
    memory: 1Gi
  limits:
    cpu: 2000m
    memory: 4Gi
```

### 9.2 資料庫優化

```sql
-- 建立索引
CREATE INDEX idx_sessions_last_active ON sessions(last_active_at);
CREATE INDEX idx_workspaces_status ON workspaces(status);
CREATE INDEX idx_activity_logs_timestamp ON activity_logs(timestamp DESC);

-- 分析查詢性能
EXPLAIN ANALYZE SELECT * FROM sessions WHERE last_active_at < now() - interval '30 minutes';
```

### 9.3 Kubernetes 調度優化

```yaml
# Pod Affinity（Pod 傾向調度到同一節點）
affinity:
  podAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        labelSelector:
          matchExpressions:
          - key: component
            operator: In
            values:
            - agent
        topologyKey: kubernetes.io/hostname

# Node Affinity（Pod 傾向調度到特定節點）
affinity:
  nodeAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      nodeSelector:
        node-role: compute
```

---

## 10. 生產環境檢查清單

- [ ] 資料庫備份策略已配置
- [ ] 監控與告警已設置
- [ ] 日誌聚合已運行
- [ ] RBAC 權限最小化
- [ ] Secret 已使用加密存儲（如 HashiCorp Vault）
- [ ] TLS 憑證已配置（Let's Encrypt 或企業 CA）
- [ ] Ingress rate limiting 已啟用
- [ ] Pod 資源限制已設置
- [ ] PDB (PodDisruptionBudget) 已配置
- [ ] 災難恢復計劃已制定與測試
- [ ] 負載測試已執行
- [ ] 安全掃描已完成（SAST/DAST）


