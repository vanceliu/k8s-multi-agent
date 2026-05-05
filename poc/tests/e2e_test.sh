#!/usr/bin/env bash
# End-to-end test for the fully containerized POC.
# Prerequisites: All services deployed via ./poc/k8s/deploy.sh
set -euo pipefail

GATEWAY="http://localhost:8000"
TOKEN="REDACTED_USER_TOKEN"
ADMIN_TOKEN="REDACTED_ADMIN_TOKEN"
USER_A="testuser1"
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

echo "=== E2E Test (Full K8s) ==="
echo ""

# ── 1. Health checks ──────────────────────────────────────────────────
echo "[1] Health checks"
gw_health=$(curl -s "$GATEWAY/health" 2>/dev/null || echo "unreachable")
check "Gateway healthy" "healthy" "$gw_health"

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

# MCP execute
mcp_resp=$(curl -s -X POST "$GATEWAY/workspaces/${WORKSPACE_A}/mcp/execute" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "X-Session-Id: $SESSION_A" \
    -H "Content-Type: application/json" \
    -d '{"method":"execute_task","params":{"task_id":"t1","task_type":"test"}}')
echo "  MCP response: $mcp_resp"
check "MCP execute returns content" "content" "$mcp_resp"

# Write file
curl -s -X POST "$GATEWAY/workspaces/${WORKSPACE_A}/mcp/execute" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "X-Session-Id: $SESSION_A" \
    -H "Content-Type: application/json" \
    -d '{"method":"write_file","params":{"path":"test.txt","content":"hello poc k8s"}}' >/dev/null

# Read file
read_resp=$(curl -s -X POST "$GATEWAY/workspaces/${WORKSPACE_A}/mcp/execute" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "X-Session-Id: $SESSION_A" \
    -H "Content-Type: application/json" \
    -d '{"method":"read_file","params":{"path":"test.txt"}}')
check "File write+read via Gateway proxy" "hello poc k8s" "$read_resp"

# List files
list_resp=$(curl -s -X POST "$GATEWAY/workspaces/${WORKSPACE_A}/mcp/execute" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "X-Session-Id: $SESSION_A" \
    -H "Content-Type: application/json" \
    -d '{"method":"list_files","params":{"path":"."}}')
check "List files includes test.txt" "test.txt" "$list_resp"

# ── 4b. Chat streaming + history consistency ─────────────────────────
echo ""
echo "[4b] Chat streaming + history consistency (normal mode)"

# Send a chat message via streaming endpoint and capture SSE events
SESSION_CHAT="sess-chat-$(date +%s)"
ensure_chat=$(curl -s -X POST "$GATEWAY/api/v1/workspaces/ensure" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$SESSION_CHAT\"}")
check "Chat session ensured" "ready" "$ensure_chat"

# Wait for pod readiness
sleep 5

# Stream a simple chat message
stream_resp=$(curl -s -N -X POST "$GATEWAY/workspaces/${WORKSPACE_A}/api/v1/chat/stream" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "X-Session-Id: $SESSION_CHAT" \
    -H "Content-Type: application/json" \
    -d "{\"message\": \"請回覆：測試成功\", \"session_id\": \"$SESSION_CHAT\"}" \
    --max-time 60 2>/dev/null)

# Verify streaming has content events
echo "$stream_resp" | grep -q "event: content" && {
    echo "  ✓ Streaming has content events"
    ((pass++)) || true
} || {
    echo "  ✗ Streaming missing content events"
    ((fail++)) || true
}

# In normal mode, streaming should NOT have tool_call/tool_result events
if echo "$stream_resp" | grep -q "event: tool_call"; then
    echo "  ✗ Normal mode streaming should not have tool_call events"
    ((fail++)) || true
else
    echo "  ✓ Normal mode streaming has no tool_call events"
    ((pass++)) || true
fi

if echo "$stream_resp" | grep -q "event: tool_result"; then
    echo "  ✗ Normal mode streaming should not have tool_result events"
    ((fail++)) || true
else
    echo "  ✓ Normal mode streaming has no tool_result events"
    ((pass++)) || true
fi

# Check stream completed
check "Stream completed" "[DONE]" "$stream_resp"

# Wait for checkpoint to be written
sleep 2

# Fetch session history
history_resp=$(curl -s "$GATEWAY/api/v1/sessions/${SESSION_CHAT}/messages" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
echo "  History: $(echo "$history_resp" | head -c 200)..."

# History should NOT contain system messages
if echo "$history_resp" | grep -q '"role":"system"'; then
    echo "  ✗ History should not contain system messages"
    ((fail++)) || true
else
    echo "  ✓ History has no system messages"
    ((pass++)) || true
fi

# History should contain human message
check "History has human message" "human" "$history_resp"

# History should contain AI response
check "History has AI response" '"role":"ai"' "$history_resp"

# History should NOT contain transfer/handoff messages in normal mode
if echo "$history_resp" | grep -q "transfer_to_"; then
    echo "  ✗ History should not contain handoff messages"
    ((fail++)) || true
else
    echo "  ✓ History has no handoff messages"
    ((pass++)) || true
fi

# In normal mode, history should NOT contain tool_calls
if echo "$history_resp" | grep -q '"tool_calls"'; then
    echo "  ✗ Normal mode history should not have tool_calls"
    ((fail++)) || true
else
    echo "  ✓ Normal mode history has no tool_calls"
    ((pass++)) || true
fi

# ── 5. Query workspace ────────────────────────────────────────────────
echo ""
echo "[5] Query workspace"
ws_resp=$(curl -s "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
echo "  Workspace: $ws_resp"
check "Workspace has session A" "$SESSION_A" "$ws_resp"
check "Workspace has session B" "$SESSION_B" "$ws_resp"

# ── 6. Idle reap ──────────────────────────────────────────────────────
echo ""
echo "[6] Idle reap test"
reap_resp=$(curl -s -X POST "$GATEWAY/api/v1/admin/reap" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"timeout_minutes": 10, "force_all": false}')
echo "  Reap (fresh sessions): $reap_resp"
check "No reap yet (sessions fresh)" "0" "$reap_resp"

echo ""
echo "  Waiting for idle timeout (sleeping 150s for 2-min timeout + reaper cycle)..."
sleep 150

# Check if reaper already cleaned up
pod_exists=$(kubectl get pod "pod-${WORKSPACE_A}" -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [ "$pod_exists" = "0" ]; then
    echo "  Background reaper already cleaned up."
    check "Pod deleted by reaper" "0" "$pod_exists"
else
    reap_resp2=$(curl -s -X POST "$GATEWAY/api/v1/admin/reap" \
        -H "Authorization: Bearer ${ADMIN_TOKEN}" \
        -H "Content-Type: application/json" \
        -d '{"timeout_minutes": 0, "force_all": true}')
    echo "  Manual reap: $reap_resp2"
    check "Workspace reaped" "$WORKSPACE_A" "$reap_resp2"
    sleep 5
    pod_exists=$(kubectl get pod "pod-${WORKSPACE_A}" -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
    check "Pod deleted after reap" "0" "$pod_exists"
fi

# PVC should still exist
pvc_exists=$(kubectl get pvc "pvc-${WORKSPACE_A}" -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
check "PVC preserved after reap" "1" "$pvc_exists"

# ── 7. Recovery — re-ensure after reap ────────────────────────────────
echo ""
echo "[7] Recovery — re-ensure workspace after reap"
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

# Verify PVC data persisted via Gateway proxy
read_resp2=$(curl -s -X POST "$GATEWAY/workspaces/${WORKSPACE_A}/mcp/execute" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "X-Session-Id: $SESSION_C" \
    -H "Content-Type: application/json" \
    -d '{"method":"read_file","params":{"path":"test.txt"}}')
check "PVC data persisted after reap+recovery" "hello poc k8s" "$read_resp2"

# ── Summary ───────────────────────────────────────────────────────────
echo ""
echo "=== Results: $pass passed, $fail failed ==="
if [ "$fail" -gt 0 ]; then
    exit 1
fi
