#!/usr/bin/env bash
# End-to-end test for the fully containerized POC (including Admin Service).
# Prerequisites: All services deployed via ./poc/k8s/deploy.sh
set -euo pipefail

GATEWAY="http://localhost:8000"
TOKEN="poc-test-token-12345"
ADMIN_TOKEN="poc-admin-token-12345"
USER_A="testuser1"
USER_B="testuser2"
WORKSPACE_A="ws-${USER_A}"
SESSION_A="sess-a-$(date +%s)"
SESSION_B="sess-b-$(date +%s)"
NAMESPACE="agent-platform"

pass=0
fail=0

check() {
    local desc="$1" expected="$2" actual="$3"
    if echo "$actual" | grep -q "$expected"; then
        echo "  ✓ $desc"
        ((pass++)) || true
    else
        echo "  ✗ $desc (expected '$expected', got '$actual')"
        ((fail++)) || true
    fi
}

# Admin Service is ClusterIP — proxy via kubectl port-forward
ADMIN_PF_PID=""
cleanup() {
    if [ -n "$ADMIN_PF_PID" ]; then
        kill "$ADMIN_PF_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "=== E2E Test (Full K8s + Admin Service) ==="
echo ""

# Start port-forward to admin service
echo "[0] Setting up admin port-forward..."
kubectl port-forward svc/admin -n "$NAMESPACE" 8090:80 &>/dev/null &
ADMIN_PF_PID=$!
ADMIN="http://localhost:8090"
# Wait until admin responds
for i in $(seq 1 15); do
    if curl -s "$ADMIN/health" &>/dev/null; then break; fi
    sleep 1
done

# ── 1. Health checks ──────────────────────────────────────────────────
echo "[1] Health checks"
gw_health=$(curl -s "$GATEWAY/health" 2>/dev/null || echo "unreachable")
check "Gateway healthy" "healthy" "$gw_health"

admin_health=$(curl -s "$ADMIN/health" 2>/dev/null || echo "unreachable")
check "Admin healthy" "healthy" "$admin_health"

# ── 2. Ensure workspace (first login) ────────────────────────────────
echo ""
echo "[2] Ensure workspace — first login (user=$USER_A, session=$SESSION_A)"
resp=$(curl -s -X POST "$GATEWAY/api/v1/workspaces/ensure" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$SESSION_A\"}")
echo "  Response: $resp"
check "workspace created" "$WORKSPACE_A" "$resp"
check "status ready" "ready" "$resp"

# Wait for agent pod to be running
echo "  Waiting for agent pod..."
for i in $(seq 1 30); do
    pod_phase=$(kubectl get pod "pod-${WORKSPACE_A}" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
    if [ "$pod_phase" = "Running" ]; then break; fi
    sleep 2
done
check "Agent Pod running" "Running" "$pod_phase"

# Wait extra for readiness probe
sleep 5

# ── 3. Multi-session: second session same user ────────────────────────
echo ""
echo "[3] Multi-session — second session (user=$USER_A, session=$SESSION_B)"
resp2=$(curl -s -X POST "$GATEWAY/api/v1/workspaces/ensure" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$SESSION_B\"}")
echo "  Response: $resp2"
check "same workspace reused" "$WORKSPACE_A" "$resp2"

pod_count=$(kubectl get pods -n "$NAMESPACE" -l "workspace_id=${WORKSPACE_A}" --no-headers 2>/dev/null | wc -l | tr -d ' ')
check "Still 1 agent pod" "1" "$pod_count"

# ── 4. Proxy to Agent Pod via Gateway ─────────────────────────────────
echo ""
echo "[4] Proxy requests to Agent Pod via Gateway"

# MCP execute (execute_task goes through LLM, verify we get a result back)
mcp_resp=$(curl -s -X POST "$GATEWAY/workspaces/${WORKSPACE_A}/mcp/execute" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "X-Session-Id: $SESSION_A" \
    -H "Content-Type: application/json" \
    -d '{"method":"execute_task","params":{"task_id":"t1","task_type":"test"}}')
echo "  MCP response: ${mcp_resp:0:120}..."
check "MCP execute returns result" "result" "$mcp_resp"

# Write file (to data/ which is shared across sessions)
curl -s -X POST "$GATEWAY/workspaces/${WORKSPACE_A}/mcp/execute" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "X-Session-Id: $SESSION_A" \
    -H "Content-Type: application/json" \
    -d '{"method":"write_file","params":{"path":"data/test.txt","content":"hello poc k8s"}}' >/dev/null

# Read file
read_resp=$(curl -s -X POST "$GATEWAY/workspaces/${WORKSPACE_A}/mcp/execute" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "X-Session-Id: $SESSION_A" \
    -H "Content-Type: application/json" \
    -d '{"method":"read_file","params":{"path":"data/test.txt"}}')
check "File write+read via Gateway proxy" "hello poc k8s" "$read_resp"

# List files
list_resp=$(curl -s -X POST "$GATEWAY/workspaces/${WORKSPACE_A}/mcp/execute" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "X-Session-Id: $SESSION_A" \
    -H "Content-Type: application/json" \
    -d '{"method":"list_files","params":{"path":"data"}}')
check "List files includes test.txt" "test.txt" "$list_resp"

# ── 5. Query workspace ────────────────────────────────────────────────
echo ""
echo "[5] Query workspace"
ws_resp=$(curl -s "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
echo "  Workspace: $ws_resp"
check "Workspace has session A" "$SESSION_A" "$ws_resp"
check "Workspace has session B" "$SESSION_B" "$ws_resp"

# ── 6. List sessions ─────────────────────────────────────────────────
echo ""
echo "[6] List user sessions"
sess_resp=$(curl -s "$GATEWAY/api/v1/sessions" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
check "Sessions list has session A" "$SESSION_A" "$sess_resp"
check "Sessions list has session B" "$SESSION_B" "$sess_resp"

# ══════════════════════════════════════════════════════════════════════
# Admin Service Tests
# ══════════════════════════════════════════════════════════════════════
echo ""
echo "══════════════════════════════════════════"
echo "  Admin Service Tests"
echo "══════════════════════════════════════════"

# ── 7. Admin auth ─────────────────────────────────────────────────────
echo ""
echo "[7] Admin auth validation"
bad_auth=$(curl -s -o /dev/null -w "%{http_code}" "$ADMIN/api/v1/admin/users" \
    -H "Authorization: Bearer wrong-token" 2>/dev/null)
check "Bad admin token returns 403" "403" "$bad_auth"

# ── 8. Admin: list users ──────────────────────────────────────────────
echo ""
echo "[8] Admin: list users"
users_resp=$(curl -s "$ADMIN/api/v1/admin/users" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
echo "  Users: $users_resp"
check "Users list contains testuser1" "$USER_A" "$users_resp"
check "Users list has total field" "total" "$users_resp"

# ── 9. Admin: get user detail ─────────────────────────────────────────
echo ""
echo "[9] Admin: get user detail"
user_detail=$(curl -s "$ADMIN/api/v1/admin/users/${USER_A}" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
echo "  User detail: $user_detail"
check "User detail has user_id" "$USER_A" "$user_detail"
check "User detail has workspace" "workspace" "$user_detail"
check "User detail has total_sessions" "total_sessions" "$user_detail"

# ── 10. Admin: list workspaces ────────────────────────────────────────
echo ""
echo "[10] Admin: list workspaces"
ws_list=$(curl -s "$ADMIN/api/v1/admin/workspaces" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
echo "  Workspaces: $ws_list"
check "Workspaces list contains ws-testuser1" "$WORKSPACE_A" "$ws_list"
check "Workspaces list has total field" "total" "$ws_list"

# ── 11. Admin: get workspace detail ───────────────────────────────────
echo ""
echo "[11] Admin: get workspace detail"
ws_detail=$(curl -s "$ADMIN/api/v1/admin/workspaces/${WORKSPACE_A}" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
echo "  Workspace detail: $ws_detail"
check "Workspace detail has workspace_id" "$WORKSPACE_A" "$ws_detail"
check "Workspace detail has sessions" "sessions" "$ws_detail"

# ── 12. Admin: pods status ────────────────────────────────────────────
echo ""
echo "[12] Admin: pods status"
pods_resp=$(curl -s "$ADMIN/api/v1/admin/pods/status" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
echo "  Pods status: $pods_resp"
check "Pods status has summary" "summary" "$pods_resp"
check "Pods status has running_pods" "running_pods" "$pods_resp"

# ── 13. Admin: workspace members ──────────────────────────────────────
echo ""
echo "[13] Admin: workspace members"

# Create second user first (ensure workspace for user B)
curl -s -X POST "$GATEWAY/api/v1/workspaces/ensure" \
    -H "Authorization: Bearer ${TOKEN}:${USER_B}" \
    -H "Content-Type: application/json" \
    -d '{"session_id": "sess-tmp"}' >/dev/null

# Wait for user B's pod
sleep 5

# List members (should be empty initially for POC — no auto-add owner)
members_resp=$(curl -s "$ADMIN/api/v1/admin/workspaces/${WORKSPACE_A}/members" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
echo "  Members (initial): $members_resp"
check "Members response has workspace_id" "$WORKSPACE_A" "$members_resp"

# Add member
add_resp=$(curl -s -X POST "$ADMIN/api/v1/admin/workspaces/${WORKSPACE_A}/members" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"${USER_B}\", \"role\": \"member\", \"granted_by\": \"${USER_A}\"}")
echo "  Add member: $add_resp"
check "Member added" "$USER_B" "$add_resp"
check "Member role is member" "member" "$add_resp"

# List members again
members_resp2=$(curl -s "$ADMIN/api/v1/admin/workspaces/${WORKSPACE_A}/members" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
echo "  Members (after add): $members_resp2"
check "Members list contains user B" "$USER_B" "$members_resp2"

# Update role
update_resp=$(curl -s -X PUT "$ADMIN/api/v1/admin/workspaces/${WORKSPACE_A}/members/${USER_B}" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"role": "admin"}')
echo "  Update role: $update_resp"
check "Role updated to admin" "admin" "$update_resp"

# Remove member
remove_resp=$(curl -s -X DELETE "$ADMIN/api/v1/admin/workspaces/${WORKSPACE_A}/members/${USER_B}" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
echo "  Remove member: $remove_resp"
check "Member removed" "removed" "$remove_resp"

# Verify removed
members_resp3=$(curl -s "$ADMIN/api/v1/admin/workspaces/${WORKSPACE_A}/members" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
check "Members list no longer has user B" "members" "$members_resp3"

# ── 14. Admin: user activate/deactivate ───────────────────────────────
echo ""
echo "[14] Admin: user activate/deactivate"
deactivate_resp=$(curl -s -X PUT "$ADMIN/api/v1/admin/users/${USER_B}/active" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"is_active": false}')
echo "  Deactivate: $deactivate_resp"
check "User deactivated" "false" "$deactivate_resp"

# Re-activate
activate_resp=$(curl -s -X PUT "$ADMIN/api/v1/admin/users/${USER_B}/active" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"is_active": true}')
check "User re-activated" "true" "$activate_resp"

# ── 15. Admin: reap (no idle pods) ────────────────────────────────────
echo ""
echo "[15] Admin: reap (no idle pods expected)"
reap_resp=$(curl -s -X POST "$ADMIN/api/v1/admin/reap" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"timeout_minutes": 10, "force_all": false}')
echo "  Reap: $reap_resp"
check "No pods reaped (sessions fresh)" "0" "$reap_resp"

# ── 16. Admin: force reap all ─────────────────────────────────────────
echo ""
echo "[16] Admin: force reap all pods"
force_reap_resp=$(curl -s -X POST "$ADMIN/api/v1/admin/reap" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"timeout_minutes": 0, "force_all": true}')
echo "  Force reap: $force_reap_resp"
check "Force reap returned workspaces" "workspaces_reaped" "$force_reap_resp"

sleep 5

# Verify pods deleted
agent_pods=$(kubectl get pods -n "$NAMESPACE" -l component=agent --no-headers 2>/dev/null | wc -l | tr -d ' ')
check "All agent pods deleted after force reap" "0" "$agent_pods"

# PVCs should still exist
pvc_exists=$(kubectl get pvc -n "$NAMESPACE" -l "workspace_id=${WORKSPACE_A}" --no-headers 2>/dev/null | wc -l | tr -d ' ')
check "PVC preserved after reap" "1" "$pvc_exists"

# ── 17. Recovery — re-ensure after reap ───────────────────────────────
echo ""
echo "[17] Recovery — re-ensure workspace after force reap"
SESSION_C="sess-c-$(date +%s)"
resp3=$(curl -s -X POST "$GATEWAY/api/v1/workspaces/ensure" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$SESSION_C\"}")
echo "  Response: $resp3"
check "Workspace recovered" "$WORKSPACE_A" "$resp3"

echo "  Waiting for recovered pod..."
for i in $(seq 1 30); do
    pod_phase=$(kubectl get pod "pod-${WORKSPACE_A}" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
    if [ "$pod_phase" = "Running" ]; then break; fi
    sleep 2
done
check "Recovered pod running" "Running" "$pod_phase"

# Wait for readiness
sleep 5

# Verify PVC data persisted
read_resp2=$(curl -s -X POST "$GATEWAY/workspaces/${WORKSPACE_A}/mcp/execute" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "X-Session-Id: $SESSION_C" \
    -H "Content-Type: application/json" \
    -d '{"method":"read_file","params":{"path":"data/test.txt"}}')
check "PVC data persisted after reap+recovery" "hello poc k8s" "$read_resp2"

# ── Summary ───────────────────────────────────────────────────────────
echo ""
echo "=== Results: $pass passed, $fail failed ==="
if [ "$fail" -gt 0 ]; then
    exit 1
fi
