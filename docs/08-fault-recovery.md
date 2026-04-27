# 08 故障恢復

本文件詳細描述系統在各類故障情況下的診斷方法與恢復策略。

## 1. 故障分類與影響

| 故障類型 | 嚴重性 | 受影響範圍 | RTO | 恢復方式 |
|---------|------|---------|-----|---------|
| Pod 崩潰 | 中 | 單個用戶 | < 1 min | 自動重啟 |
| Node 故障 | 高 | 多個 Pod | 5-15 min | 自動遷移 + 手動確認 |
| 存儲故障 | 危急 | 所有用戶 | > 1 hour | S3 版本恢復 |
| DB 故障 | 危急 | 所有操作 | > 1 hour | Failover + 恢復備份 |
| Orchestrator 故障 | 高 | 新工作區建立 | < 5 min | 多副本自動轉移 |
| Cleanup Job 失敗 | 低 | 單個 session 殘留資料 | 手動 | 手動清理或重建 Job |
| 網路分區 | 高 | 跨AZ通訊 | 5-30 min | 手動介入 |

---

## 2. Pod 級故障

### 2.1 Pod CrashLoopBackOff

**症狀**：Pod 不斷重啟，狀態顯示 CrashLoopBackOff

**診斷**：

```bash
# 檢查 Pod 狀態
kubectl describe pod pod-ws-user123 -n agent-platform

# 查看最新日誌
kubectl logs pod-ws-user123 -n agent-platform --tail=100

# 查看前一個崩潰實例的日誌
kubectl logs pod-ws-user123 -n agent-platform --previous

# 檢查事件
kubectl get events -n agent-platform --sort-by='.lastTimestamp' | grep pod-ws-user123
```

**常見原因**：

1. **容器啟動命令錯誤**
   ```bash
   # 解決方案：修正 Dockerfile 或啟動腳本
   # 重新構建鏡像並部署
   kubectl delete pod pod-ws-user123 -n agent-platform  # Pod 會自動重建
   ```

2. **缺失依賴或模塊**
   ```bash
   # 進入容器檢查環境
   kubectl exec -it pod-ws-user123 -n agent-platform -- /bin/bash
   python -c "import required_module"  # 檢查依賴
   ```

3. **資源不足**
   ```bash
   # 檢查節點資源
   kubectl top nodes
   kubectl top pods -n agent-platform
   
   # 若 CPU/Memory 過高，調整 requests/limits
   kubectl set resources deployment orchestrator -n agent-platform \
     --requests=cpu=500m,memory=1Gi --limits=cpu=2000m,memory=4Gi
   ```

4. **S3 存取異常**
   ```bash
   # 檢查 S3 存取權限（IRSA annotation、IAM Policy）
   kubectl get sa -n agent-platform
   kubectl describe sa <agent-serviceaccount> -n agent-platform

   # 確認 IRSA annotation 是否正確
   # annotations:
   #   eks.amazonaws.com/role-arn: arn:aws:iam::<account>:role/<role-name>

   # 若 S3 存取被拒，檢查 IRSA、IAM Role、S3 Bucket Policy
   aws sts assume-role-with-web-identity \
     --role-arn arn:aws:iam::<account>:role/<role-name> \
     --role-session-name test-session \
     --web-identity-token $(cat $AWS_WEB_IDENTITY_TOKEN_FILE)

   # 測試 S3 連線
   aws s3 ls s3://<workspace-bucket>/users/user123/
   ```

**恢復步驟**：

```bash
# 1. 修正問題根源
# （見上文具體原因）

# 2. 強制刪除 Pod（觸發重建）
kubectl delete pod pod-ws-user123 -n agent-platform --grace-period=0 --force

# 3. 監控重啟情況
kubectl get pods -n agent-platform -w

# 4. 若問題持續，向資料庫標記該 Pod 異常
kubectl exec -it <postgres-pod> -- psql -U agent_user -d agent_platform_db << EOF
UPDATE pod_states SET sync_status = 'error' WHERE pod_name = 'pod-ws-user123';
EOF
```

---

### 2.2 Pod OOMKilled（記憶體不足）

**症狀**：Pod 被 kubelet 殺死，事件顯示 OOMKilled

**診斷**：

```bash
kubectl describe pod pod-ws-user123 -n agent-platform | grep -A 5 "Last State"

# 輸出例：
# Last State:     Terminated
#   Reason:       OOMKilled
```

**恢復**：

```bash
# 1. 增加記憶體限制
kubectl set resources pod pod-ws-user123 -n agent-platform \
  --limits=memory=2Gi

# 2. 或透過 Deployment 修改
kubectl patch deployment orchestrator -n agent-platform --type='json' \
  -p='[{"op": "replace", "path": "/spec/template/spec/containers/0/resources/limits/memory", "value":"2Gi"}]'

# 3. 重建 Pod
kubectl delete pod pod-ws-user123 -n agent-platform
```

---

### 2.3 Pod Pending（資源等待）

**症狀**：Pod 長時間停留在 Pending 狀態

**診斷**：

```bash
kubectl describe pod pod-ws-user123 -n agent-platform

# 典型輸出：
# Status: Pending
# Conditions:
#   Type           Status  Reason
#   ----           ------  ------
#   PodScheduled   False   Unschedulable
# 
# Events:
#   Warning  FailedScheduling  ... insufficient cpu
```

**恢復**：

```bash
# 1. 檢查節點資源
kubectl describe nodes

# 2. 若資源不足，可選方案：
#    A. 縮放工作負載
kubectl scale deployment orchestrator -n agent-platform --replicas=1

#    B. 新增節點（若使用 cloud autoscaler）
# （取決於你的部署平台，如 EKS、GKE、AKS）

#    C. 調降 Pod requests
kubectl patch pod pod-ws-user123 -n agent-platform --type='json' \
  -p='[{"op": "replace", "path": "/spec/containers/0/resources/requests/cpu", "value":"50m"}]'
```

---

## 3. 節點級故障

### 3.1 Node NotReady

**症狀**：節點狀態顯示 NotReady

**診斷**：

```bash
kubectl get nodes
# NAME       STATUS     ROLES   ...
# node-1     NotReady   worker

# 詳細信息
kubectl describe node node-1 | grep -A 10 "Conditions"

# 查看節點事件
kubectl get events --field-selector involvedObject.name=node-1 --all-namespaces
```

**常見原因**：

1. **Kubelet 故障**
   ```bash
   # SSH 進入節點（取決於你的平台）
   ssh user@node-1
   
   # 檢查 kubelet 狀態
   systemctl status kubelet
   
   # 查看 kubelet 日誌
   journalctl -u kubelet -f
   
   # 重啟 kubelet
   sudo systemctl restart kubelet
   ```

2. **網路問題**
   ```bash
   # 節點上測試網路
   ping -c 5 <master-node-ip>
   
   # 檢查 DNS
   nslookup kubernetes.default.svc.cluster.local
   ```

3. **磁碟空間不足**
   ```bash
   ssh user@node-1
   df -h  # 檢查磁碟
   du -sh /var/lib/kubelet  # 檢查 kubelet 目錄
   
   # 清理容器鏡像
   docker image prune -a
   ```

**恢復**：

```bash
# 1. 排空節點（不刪除 Pod）
kubectl cordon node-1

# 2. 逐出節點上的 Pod（重新調度）
kubectl drain node-1 --ignore-daemonsets

# 3. 修復節點（見上文）

# 4. 恢復節點
kubectl uncordon node-1

# 5. 驗證
kubectl get nodes  # 應顯示 Ready
```

---

### 3.2 Node 完全故障（離線）

**症狀**：節點無法訪問，顯示 NotReady 或 Unknown

**恢復步驟**：

```bash
# 1. 等待自動故障轉移（通常 5 分鐘）
# Kubernetes 會自動驅逐 Pod 到其他節點

# 2. 檢查 Pod 狀態
kubectl get pods -n agent-platform -o wide

# 3. 若 Pod 無法重新調度（資源不足）
# 需要手動刪除 Pod 允許重新調度
kubectl delete pod <pod-name> -n agent-platform --grace-period=0

# 4. 如果節點故障持續，刪除該節點
kubectl delete node node-1

# 5. 若使用 Cloud 部署，新節點會自動加入（autoscaler）
```

---

## 4. S3 存取故障

### 4.1 S3 存取異常

**症狀**：Agent Pod 啟動後無法讀寫 S3 資料，日誌顯示 AccessDenied 或連線逾時

**診斷**：

```bash
# 檢查 Pod 日誌中的 S3 錯誤
kubectl logs pod-ws-user123 -n agent-platform --tail=100 | grep -i "s3\|access denied\|nosuchbucket"

# 檢查 IRSA annotation（ServiceAccount 是否綁定 IAM Role）
kubectl get sa -n agent-platform
kubectl describe sa <agent-serviceaccount> -n agent-platform
# 確認 annotations 中有:
#   eks.amazonaws.com/role-arn: arn:aws:iam::<account>:role/<role-name>
```

**常見原因**：

1. **IRSA Annotation 缺失或錯誤**
   ```bash
   # 確認 ServiceAccount 的 IRSA annotation
   kubectl get sa <agent-serviceaccount> -n agent-platform -o jsonpath='{.metadata.annotations}'

   # 若缺少 annotation，補上
   kubectl annotate sa <agent-serviceaccount> -n agent-platform \
     eks.amazonaws.com/role-arn=arn:aws:iam::<account>:role/<role-name>
   ```

2. **IAM Role Trust Policy 配置錯誤**
   ```bash
   # 檢查 IAM Role 的信任政策
   aws iam get-role --role-name <role-name> --query 'Role.AssumeRolePolicyDocument'

   # 確認信任政策中包含正確的 OIDC provider 和 ServiceAccount
   # 條件字串應為:
   #   "system:serviceaccount:agent-platform:<serviceaccount-name>"
   ```

3. **S3 Bucket Policy 限制**
   ```bash
   # 檢查 Bucket Policy
   aws s3api get-bucket-policy --bucket <workspace-bucket>

   # 確認 IAM Role 有足夠權限（s3:GetObject, s3:PutObject, s3:ListBucket 等）
   aws iam get-role-policy --role-name <role-name> --policy-name <policy-name>
   ```

4. **網路連線問題（無法連接 S3 Endpoint）**
   ```bash
   # 從 Pod 內測試 S3 連線
   kubectl exec -it pod-ws-user123 -n agent-platform -- /bin/bash
   curl -I https://<workspace-bucket>.s3.<region>.amazonaws.com

   # 檢查 NAT Gateway / VPC Endpoint 是否正常
   aws ec2 describe-vpc-endpoints --filters "Name=service-name,Values=com.amazonaws.<region>.s3"
   ```

**恢復**：

```bash
# 1. 修正 IRSA / IAM / Bucket Policy 配置（見上文具體原因）

# 2. 重啟 Pod 讓新的 IAM 憑證生效
kubectl delete pod pod-ws-user123 -n agent-platform

# 3. 驗證 S3 存取
kubectl exec -it pod-ws-user123 -n agent-platform -- \
  aws s3 ls s3://<workspace-bucket>/users/user123/

# 4. 監控 Pod 日誌確認恢復
kubectl logs pod-ws-user123 -n agent-platform -f
```

---

### 4.2 S3Backend 初始化失敗

**症狀**：Pod 可正常啟動（Running），但 S3Backend 無法連接，應用日誌顯示初始化錯誤

**診斷**：

```bash
kubectl describe pod pod-ws-user123 -n agent-platform
# Pod 狀態為 Running，但應用層日誌有 S3 錯誤

# 查看應用日誌
kubectl logs pod-ws-user123 -n agent-platform --tail=200 | grep -i "s3backend\|init\|connect"
```

**常見原因**：

1. **AWS 憑證過期或無效**
   ```bash
   # 檢查 Pod 內的 AWS 環境變數
   kubectl exec -it pod-ws-user123 -n agent-platform -- env | grep AWS

   # 驗證臨時憑證是否有效
   kubectl exec -it pod-ws-user123 -n agent-platform -- \
     aws sts get-caller-identity
   ```

2. **Region 配置錯誤**
   ```bash
   # 確認 AWS_REGION 環境變數
   kubectl exec -it pod-ws-user123 -n agent-platform -- echo $AWS_REGION

   # 修正 region 配置（若不正確，更新 Deployment 環境變數）
   kubectl set env deployment/<deployment-name> AWS_REGION=ap-northeast-1 -n agent-platform
   ```

3. **Bucket 名稱錯誤**
   ```bash
   # 確認配置的 bucket 名稱是否存在
   aws s3 ls | grep <configured-bucket-name>

   # 若 bucket 不存在，需建立或修正配置
   ```

**恢復**：

```bash
# 1. 修正配置（憑證 / region / bucket 名稱，見上文具體原因）

# 2. 重啟 Pod 讓 S3Backend 重新初始化
kubectl delete pod pod-ws-user123 -n agent-platform

# 3. 驗證 S3Backend 正常運作
kubectl logs pod-ws-user123 -n agent-platform -f | grep -i "s3backend.*initialized"

# 4. 若問題仍存在，檢查 VPC 網路設定
# 確認 Pod 可存取 S3 endpoint（透過 NAT Gateway 或 VPC Endpoint）
```

> **注意**：S3 為無容量限制的物件存儲，無需像 PVC 擔心磁碟空間不足。
> 建議啟用 S3 Bucket Versioning 以支援資料誤刪恢復，並搭配 S3 Lifecycle Policy 管理儲存成本。

---

## 5. 資料庫故障

### 5.1 DB 連線失敗

**症狀**：Orchestrator Pod 日誌顯示 "psycopg2.OperationalError: could not connect to server"

**診斷**：

```bash
# 1. 檢查 DB Pod 狀態
kubectl get pods -n agent-platform -l component=postgres

# 2. 檢查 DB 服務
kubectl get svc -n agent-platform postgres

# 3. 測試連線
kubectl run -it --rm psql-test --image=postgres:14 -- \
  psql -h postgres.agent-platform.svc.cluster.local -U agent_user -d agent_platform_db -c "SELECT 1"
```

**恢復**：

```bash
# 1. 檢查 DB Pod 日誌
kubectl logs -n agent-platform postgres-0

# 2. 若 DB Pod 崩潰，嘗試重啟
kubectl delete pod postgres-0 -n agent-platform

# 3. 若使用 Helm，檢查 release
helm status postgresql -n agent-platform

# 4. 若 DB 資料損壞，從備份恢復
kubectl exec -it postgres-0 -n agent-platform -- \
  psql -U agent_user agent_platform_db < /backup/db-backup.sql
```

### 5.2 DB 磁碟滿

**症狀**：寫入失敗，日誌顯示 "disk full"

**恢復**：

```bash
# 1. 進入 DB Pod
kubectl exec -it postgres-0 -n agent-platform -- /bin/bash

# 2. 檢查存儲
df -h

# 3. 清理舊日誌
psql -U agent_user -d agent_platform_db << EOF
-- 刪除 N 天前的活動日誌
DELETE FROM activity_logs WHERE timestamp < now() - interval '90 days';
VACUUM;
EOF

# 4. 擴展 DB PVC（注意：此為資料庫本身的 PVC，非 workspace 資料）
kubectl patch pvc postgres-pvc -n agent-platform \
  -p '{"spec":{"resources":{"requests":{"storage":"50Gi"}}}}'
```

---

## 6. Orchestrator 故障

### 6.1 Orchestrator Pod 崩潰

**症狀**：無法建立新工作區，API 無響應

**診斷**：

```bash
kubectl get pods -n agent-platform -l component=orchestrator
kubectl logs -n agent-platform -l component=orchestrator --tail=50
```

**恢復**：

```bash
# 1. Orchestrator 是多副本部署，應自動轉移
# 檢查其他副本
kubectl get pods -n agent-platform -l component=orchestrator

# 2. 若所有副本都故障，檢查根本原因
kubectl describe pod <orchestrator-pod> -n agent-platform

# 3. 若是 DB 連線問題（見上文 5.1）

# 4. 若是程式代碼問題，需要修復並重新部署
git pull origin main
docker build -f docker/Dockerfile.orchestrator -t your-registry/k8s-agent-orchestrator:latest .
docker push your-registry/k8s-agent-orchestrator:latest

kubectl set image deployment/orchestrator \
  orchestrator=your-registry/k8s-agent-orchestrator:latest \
  -n agent-platform
```

---

### 6.2 Session Cleanup Job 失敗

**症狀**：刪除 session 後，PVC 上的 `sessions/{session_id}/` 資料夾未被清除

**診斷**：

```bash
# 檢查 cleanup Job 狀態
kubectl get jobs -n agent-platform -l component=session-cleanup

# 查看失敗的 Job 日誌
kubectl logs job/cleanup-{session_id_prefix}-{timestamp} -n agent-platform

# 檢查 RBAC 權限（Orchestrator 需要 batch/jobs 權限）
kubectl auth can-i create jobs --as=system:serviceaccount:agent-platform:orchestrator-sa -n agent-platform
```

**恢復**：

```bash
# 1. 若 RBAC 缺少 batch/jobs 權限
kubectl apply -f infra/k8s/rbac.yaml

# 2. 手動清理殘留的 session 資料夾
kubectl run cleanup-manual --rm -it --image=busybox \
  --overrides='{"spec":{"containers":[{"name":"cleanup","image":"busybox","command":["sh","-c","rm -rf /workspace/{workspace_id}/sessions/{session_id}"],"volumeMounts":[{"name":"ws","mountPath":"/workspace/{workspace_id}"}]}],"volumes":[{"name":"ws","persistentVolumeClaim":{"claimName":"pvc-{workspace_id}"}}]}}' \
  -n agent-platform

# 3. 清理失敗的 Job 資源
kubectl delete jobs -n agent-platform -l component=session-cleanup --field-selector status.successful=0
```

---

## 7. 網路故障

### 7.1 Pod 內無法訪問外部服務

**症狀**：Agent Pod 無法呼叫外部 API（如 LLM 服務）

**診斷**：

```bash
# 進入 Pod
kubectl exec -it pod-ws-user123 -n agent-platform -- /bin/bash

# 測試網路連接
ping 8.8.8.8
curl https://api.openai.com/v1/models
```

**恢復**：

```bash
# 1. 檢查 NetworkPolicy
kubectl get networkpolicy -n agent-platform
kubectl describe networkpolicy <name> -n agent-platform

# 2. 若過於限制，調整規則
kubectl delete networkpolicy <name> -n agent-platform

# 3. 檢查 DNS
kubectl get svc -n kube-system -l k8s-app=kube-dns
kubectl logs -n kube-system -l k8s-app=kube-dns

# 4. 測試 DNS 解析
kubectl exec -it pod-ws-user123 -n agent-platform -- nslookup kubernetes.default
```

---

## 8. 資料恢復流程

### 8.1 從備份恢復 DB

```bash
# 1. 準備備份檔案
# 備份應存儲在外部（如 S3、GCS）

# 2. 下載備份
aws s3 cp s3://backup-bucket/db-backup.sql.gz .
gunzip db-backup.sql.gz

# 3. 恢復到新 DB
kubectl exec -it postgres-0 -n agent-platform -- \
  psql -U agent_user -d agent_platform_db < db-backup.sql

# 4. 驗證資料
kubectl exec -it postgres-0 -n agent-platform -- \
  psql -U agent_user -d agent_platform_db -c "SELECT COUNT(*) FROM users;"
```

### 8.2 從 S3 版本控制恢復

```bash
# S3 Bucket 應啟用 Versioning 以支援誤刪恢復

# 1. 列出物件的所有版本
aws s3api list-object-versions \
  --bucket <workspace-bucket> \
  --prefix "users/user123/"

# 2. 查看特定物件的版本歷史
aws s3api list-object-versions \
  --bucket <workspace-bucket> \
  --prefix "users/user123/session-data.json"

# 3. 恢復到特定版本（下載舊版本覆蓋）
aws s3api get-object \
  --bucket <workspace-bucket> \
  --key "users/user123/session-data.json" \
  --version-id "<version-id>" \
  /tmp/restored-session-data.json

aws s3 cp /tmp/restored-session-data.json \
  s3://<workspace-bucket>/users/user123/session-data.json

# 4. 驗證恢復結果
aws s3 ls s3://<workspace-bucket>/users/user123/

# 若使用 S3 跨區域複製（Cross-Region Replication）進行災難恢復
# 5. 從備援區域的 Bucket 複製資料
aws s3 sync s3://<backup-bucket-in-other-region>/users/user123/ \
  s3://<workspace-bucket>/users/user123/ \
  --source-region <backup-region>
```

---

## 9. 故障轉移與高可用

### 9.1 Orchestrator 故障轉移

**設置多副本 + Leader Election**：

```yaml
# orchestrator-deployment.yaml
spec:
  replicas: 3  # 奇數個副本
  
  # 使用 leader election
  env:
  - name: ORCHESTRATOR_LEADER_ELECTION_ENABLED
    value: "true"
  - name: ORCHESTRATOR_LEADER_ELECTION_NAMESPACE
    value: agent-platform
  - name: ORCHESTRATOR_LEADER_ELECTION_NAME
    value: orchestrator-leader
```

當一個副本故障時，其他副本自動選舉新 leader。

### 9.2 資料庫故障轉移

使用 PostgreSQL HA（高可用）部署：

```bash
# 部署 PostgreSQL 主從複製
helm install postgresql bitnami/postgresql \
  --set primary.replication.enabled=true \
  --set primary.replication.slaveReplicas=2 \
  -n agent-platform

# 設置自動故障轉移（使用 patroni）
helm install postgres-operator postgres-operator/postgres-operator \
  -n agent-platform
```

---

## 10. 災難恢復計劃 (DRP)

### 10.1 RTO / RPO 目標

```
Scenario                    RTO         RPO
────────────────────────────────────────────
Pod 重啟                  < 1 min      N/A
Node 故障                 5-15 min     < 5 min
DB 故障                   30-60 min    < 5 min
整個集群故障              2-4 hours    < 1 hour
多 AZ 故障                > 1 day      取決於備份策略
```

### 10.2 備份策略

```bash
# 每日全量備份 + 每小時增量
0 2 * * * /scripts/backup-full.sh
0 * * * * /scripts/backup-incremental.sh

# 備份保留時間
  - 日備份：30 天
  - 周備份：13 周
  - 月備份：12 月

# 備份存儲位置
  主: 本地 K8s 存儲
  副: AWS S3 (跨 region)
```

### 10.3 定期演習

```bash
# 每月進行一次全恢復演習
1. 在隔離環境恢復所有系統
2. 驗證應用功能
3. 記錄恢復時間與問題
4. 更新文檔與流程
```

---

## 11. 監控與告警

### 11.1 關鍵指標

```yaml
告警規則:
  - Pod crash 率 > 10%/hour → Page
  - Pod pending 時間 > 30 min → Alert
  - S3 存取錯誤率 > 1% → Alert
  - DB 連線數 > 80% → Alert
  - API 延遲 > 1s → Alert (P99)
  - 工作區建立失敗率 > 5% → Alert
```

### 11.2 日誌與追蹤

```bash
# 啟用分佈式追蹤（OpenTelemetry）
helm install opentelemetry-collector open-telemetry/opentelemetry-collector \
  -n monitoring

# 結構化日誌
所有日誌使用 JSON 格式，包含：
  - timestamp
  - level (INFO/WARN/ERROR)
  - component (orchestrator/agent/api)
  - user_id
  - session_id
  - message
  - error_stack (若適用)
```


