#!/usr/bin/env bash
# Deploy POC — all services run inside K8s (kind + Podman).
# Usage: ./poc/k8s/deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
NAMESPACE="agent-platform"
CLUSTER_NAME="agent-poc"

export KIND_EXPERIMENTAL_PROVIDER=podman

echo "=== POC Full K8s Deploy ==="

# 1. Create kind cluster (skip if exists)
echo "[1/7] Ensuring kind cluster..."
if kind get clusters 2>/dev/null | grep -q "$CLUSTER_NAME"; then
    echo "  Cluster '$CLUSTER_NAME' already exists, skipping."
else
    kind create cluster --name "$CLUSTER_NAME" --config "$SCRIPT_DIR/kind-config.yaml"
fi
kubectl config use-context "kind-$CLUSTER_NAME"

# 2. Apply namespace + RBAC
echo "[2/7] Applying namespace & RBAC..."
kubectl apply -f "$SCRIPT_DIR/namespace-rbac.yaml"

# 3. Build images
echo "[3/7] Building images..."
docker build -t k8s-agent-runtime:latest -f "$PROJECT_ROOT/poc/docker/Dockerfile.agent" "$PROJECT_ROOT"
docker build -t k8s-orchestrator:latest -f "$PROJECT_ROOT/poc/docker/Dockerfile.orchestrator" "$PROJECT_ROOT"
docker build -t k8s-gateway:latest -f "$PROJECT_ROOT/poc/docker/Dockerfile.gateway" "$PROJECT_ROOT"
docker build -t k8s-admin:latest -f "$PROJECT_ROOT/poc/docker/Dockerfile.admin" "$PROJECT_ROOT"
docker build -t k8s-storage-service:latest -f "$PROJECT_ROOT/poc/docker/Dockerfile.storage" "$PROJECT_ROOT"

# 3.5. Clean up dangling (untagged) images from previous builds
echo "[3.5/7] Cleaning up dangling images..."
dangling=$(docker images -f "dangling=true" -q)
if [ -n "$dangling" ]; then
    docker rmi $dangling 2>/dev/null || true
    echo "  Removed dangling images."
else
    echo "  No dangling images found."
fi

# 4. Load images into kind
echo "[4/7] Loading images into kind..."
kind load docker-image k8s-agent-runtime:latest --name "$CLUSTER_NAME"
kind load docker-image k8s-orchestrator:latest --name "$CLUSTER_NAME"
kind load docker-image k8s-gateway:latest --name "$CLUSTER_NAME"
kind load docker-image k8s-admin:latest --name "$CLUSTER_NAME"
kind load docker-image k8s-storage-service:latest --name "$CLUSTER_NAME"

# 5. Deploy services
echo "[5/7] Deploying Orchestrator, Gateway, Admin & Storage Service..."
kubectl apply -f "$SCRIPT_DIR/orchestrator.yaml"
kubectl apply -f "$SCRIPT_DIR/gateway.yaml"
kubectl apply -f "$SCRIPT_DIR/admin.yaml"
kubectl apply -f "$SCRIPT_DIR/storage-service.yaml"

# Wait for pods
echo "  Waiting for Orchestrator..."
kubectl wait --for=condition=ready pod -l component=orchestrator -n "$NAMESPACE" --timeout=120s
echo "  Waiting for Gateway..."
kubectl wait --for=condition=ready pod -l component=gateway -n "$NAMESPACE" --timeout=120s
echo "  Waiting for Admin..."
kubectl wait --for=condition=ready pod -l component=admin -n "$NAMESPACE" --timeout=120s
echo "  Waiting for Storage Service..."
kubectl wait --for=condition=ready pod -l component=storage-service -n "$NAMESPACE" --timeout=120s

# 6. Verify
echo "[6/7] Verifying..."
echo ""
kubectl get pods,svc -n "$NAMESPACE"
echo ""
echo "=== Deploy complete ==="
echo ""
echo "Gateway is accessible at: http://localhost:8000"
echo "Admin Service is internal (ClusterIP on port 8090)"
echo "Storage Service is internal (ClusterIP on port 8091)"
echo ""
echo "Test with:"
echo "  curl -s http://localhost:8000/health"
echo ""
echo "  curl -s -X POST http://localhost:8000/api/v1/workspaces/ensure \\"
echo "    -H 'Authorization: Bearer \$POC_STATIC_TOKEN:testuser1' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"session_id\": \"sess-001\"}'"
echo ""
echo "E2E test:"
echo "  bash poc/tests/e2e_test.sh"
