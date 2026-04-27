#!/usr/bin/env bash
# End-to-end test for Workspace Storage Service.
# Prerequisites: All services deployed via ./poc/k8s/deploy.sh
set -euo pipefail

GATEWAY="http://localhost:8000"
TOKEN="REDACTED_USER_TOKEN"
ADMIN_TOKEN="REDACTED_ADMIN_TOKEN"
USER_A="testuser1"
USER_B="testuser2"
WORKSPACE_A="ws-${USER_A}"
SESSION_A="sess-storage-$(date +%s)"
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

check_status() {
    local desc="$1" expected="$2" actual="$3"
    if [ "$actual" = "$expected" ]; then
        echo "  ✓ $desc"
        ((pass++)) || true
    else
        echo "  ✗ $desc (expected HTTP $expected, got $actual)"
        ((fail++)) || true
    fi
}

# Port-forward to Storage Service for direct testing
STORAGE_PF_PID=""
cleanup() {
    if [ -n "$STORAGE_PF_PID" ]; then
        kill "$STORAGE_PF_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "=== E2E Test (Workspace Storage Service) ==="
echo ""

# Start port-forward to Storage Service
echo "[0] Setting up Storage Service port-forward..."
kubectl port-forward svc/storage-service -n "$NAMESPACE" 8091:80 &>/dev/null &
STORAGE_PF_PID=$!
STORAGE_SVC="http://localhost:8091"
for i in $(seq 1 15); do
    if curl -s "$STORAGE_SVC/health" &>/dev/null; then break; fi
    sleep 1
done

# ── 1. Health check ──────────────────────────────────────────────────
echo "[1] Health check"
storage_health=$(curl -s "$STORAGE_SVC/health" 2>/dev/null || echo "unreachable")
check "Storage Service healthy" "healthy" "$storage_health"

# ── 2. Ensure workspace (creates PVC via Orchestrator → Storage Service) ─
echo ""
echo "[2] Ensure workspace to create PVC"
resp=$(curl -s -X POST "$GATEWAY/api/v1/workspaces/ensure" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$SESSION_A\"}")
echo "  Response: $resp"
check "workspace created" "$WORKSPACE_A" "$resp"

# Wait for agent pod
echo "  Waiting for agent pod..."
for i in $(seq 1 30); do
    pod_phase=$(kubectl get pod "pod-${WORKSPACE_A}" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
    if [ "$pod_phase" = "Running" ]; then break; fi
    sleep 2
done
check "Agent Pod running" "Running" "$pod_phase"
sleep 5

# ── 3. Workspace storage list (admin) ────────────────────────────────
echo ""
echo "[3] Workspace storage list (admin via Gateway)"
storage_resp=$(curl -s "$GATEWAY/api/v1/workspaces/storage" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
echo "  Storage: $storage_resp"
check "Storage list contains workspace" "$WORKSPACE_A" "$storage_resp"
check "Storage list has total field" "total" "$storage_resp"

# ── 4. Workspace storage list (user — needs workspace_members entry) ─
echo ""
echo "[4] Workspace storage list (user)"

# Ensure user B exists
curl -s -X POST "$GATEWAY/api/v1/workspaces/ensure" \
    -H "Authorization: Bearer ${TOKEN}:${USER_B}" \
    -H "Content-Type: application/json" \
    -d '{"session_id": "sess-tmp-storage"}' >/dev/null
sleep 3

# Add user A as owner of their own workspace (for storage access)
curl -s -X POST "$GATEWAY/api/v1/admin/workspaces/${WORKSPACE_A}/members" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"${USER_A}\", \"role\": \"owner\", \"granted_by\": \"${USER_A}\"}" >/dev/null

# User A should see their workspace storage
user_storage=$(curl -s "$GATEWAY/api/v1/workspaces/storage" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
echo "  User storage: $user_storage"
check "User sees own workspace" "$WORKSPACE_A" "$user_storage"

# ── 5. Workspace rename ──────────────────────────────────────────────
echo ""
echo "[5] Workspace rename (display_name)"
rename_resp=$(curl -s -X PUT "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/rename" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"display_name": "TestUser1 Workspace"}')
echo "  Rename: $rename_resp"
check "Rename returns display_name" "TestUser1 Workspace" "$rename_resp"

# Verify in list
storage_resp2=$(curl -s "$GATEWAY/api/v1/workspaces/storage" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
check "Storage list shows display_name" "TestUser1 Workspace" "$storage_resp2"

# ── 6. File operations (online mode — Pod running) ───────────────────
echo ""
echo "[6] File operations (online mode)"

# mkdir
mkdir_resp=$(curl -s -X POST "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files/mkdir?path=test-dir" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
echo "  mkdir: $mkdir_resp"
check "mkdir success" "created" "$mkdir_resp"
check "mkdir online mode" "online" "$mkdir_resp"

# upload
upload_resp=$(curl -s -X POST "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files/upload?path=test-dir" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -F "file=@/dev/stdin;filename=hello.txt" <<< "hello from storage service")
echo "  upload: $upload_resp"
check "upload success" "uploaded" "$upload_resp"

# list
list_resp=$(curl -s "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files?path=test-dir" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
echo "  list: $list_resp"
check "list contains hello.txt" "hello.txt" "$list_resp"

# download
dl_resp=$(curl -s "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files/download?path=test-dir/hello.txt" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
check "download content matches" "hello from storage service" "$dl_resp"

# delete file
del_resp=$(curl -s -X DELETE "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files?path=test-dir/hello.txt" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
echo "  delete: $del_resp"
check "delete success" "deleted" "$del_resp"

# delete directory
del_dir_resp=$(curl -s -X DELETE "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files?path=test-dir" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
check "delete dir success" "deleted" "$del_dir_resp"

# ── 7. Storage access info ───────────────────────────────────────────
echo ""
echo "[7] Storage access info"
access_resp=$(curl -s "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/access" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")
echo "  Access: $access_resp"
check "Access info has workspace_id" "$WORKSPACE_A" "$access_resp"
check "Access info has members" "members" "$access_resp"
check "Access info contains owner" "owner" "$access_resp"

# ── 8. Permission checks ────────────────────────────────────────────
echo ""
echo "[8] Permission checks"

# Add user B as readonly
curl -s -X POST "$GATEWAY/api/v1/admin/workspaces/${WORKSPACE_A}/members" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"${USER_B}\", \"role\": \"readonly\", \"granted_by\": \"${USER_A}\"}" >/dev/null

# Readonly user can list files
ro_list=$(curl -s -o /dev/null -w "%{http_code}" \
    "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files" \
    -H "Authorization: Bearer ${TOKEN}:${USER_B}")
check_status "Readonly user can list files" "200" "$ro_list"

# Readonly user cannot upload (should get 403)
ro_upload=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files/upload" \
    -H "Authorization: Bearer ${TOKEN}:${USER_B}" \
    -F "file=@/dev/stdin;filename=blocked.txt" <<< "should fail")
check_status "Readonly user cannot upload" "403" "$ro_upload"

# Readonly user cannot mkdir (should get 403)
ro_mkdir=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files/mkdir?path=blocked-dir" \
    -H "Authorization: Bearer ${TOKEN}:${USER_B}")
check_status "Readonly user cannot mkdir" "403" "$ro_mkdir"

# Clean up: remove user B from workspace
curl -s -X DELETE "$GATEWAY/api/v1/admin/workspaces/${WORKSPACE_A}/members/${USER_B}" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" >/dev/null

# ── 9. Force reap + offline file operations ──────────────────────────
echo ""
echo "[9] Offline file operations (Pod not running)"

# Write a file first while Pod is online
curl -s -X POST "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files/mkdir?path=offline-test" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" >/dev/null
curl -s -X POST "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files/upload?path=offline-test" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -F "file=@/dev/stdin;filename=persist.txt" <<< "survive reap" >/dev/null

# Force reap to stop the Pod
echo "  Force reaping all pods..."
curl -s -X POST "$GATEWAY/api/v1/admin/reap" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"timeout_minutes": 0, "force_all": true}' >/dev/null
sleep 5

# Verify Pod is gone
pod_exists=$(kubectl get pod "pod-${WORKSPACE_A}" -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
check "Agent Pod deleted" "0" "$pod_exists"

# PVC should still exist
pvc_exists=$(kubectl get pvc "pvc-${WORKSPACE_A}" -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
check "PVC preserved" "1" "$pvc_exists"

# File list (offline mode via Job)
offline_list=$(curl -s "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files?path=offline-test" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
echo "  Offline list: $offline_list"
check "Offline list contains persist.txt" "persist.txt" "$offline_list"
check "Offline mode indicator" "offline" "$offline_list"

# File mkdir (offline)
offline_mkdir=$(curl -s -X POST "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files/mkdir?path=offline-test/subdir" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
echo "  Offline mkdir: $offline_mkdir"
check "Offline mkdir success" "created" "$offline_mkdir"
check "Offline mkdir mode" "offline" "$offline_mkdir"

# File delete (offline)
offline_del=$(curl -s -X DELETE "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files?path=offline-test/subdir" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
echo "  Offline delete: $offline_del"
check "Offline delete success" "deleted" "$offline_del"

# ── 10. Recovery — re-ensure after reap ──────────────────────────────
echo ""
echo "[10] Recovery — re-ensure workspace"
SESSION_R="sess-recovery-$(date +%s)"
resp_r=$(curl -s -X POST "$GATEWAY/api/v1/workspaces/ensure" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}" \
    -H "Content-Type: application/json" \
    -d "{\"session_id\": \"$SESSION_R\"}")
check "Workspace recovered" "$WORKSPACE_A" "$resp_r"

echo "  Waiting for recovered pod..."
for i in $(seq 1 30); do
    pod_phase=$(kubectl get pod "pod-${WORKSPACE_A}" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
    if [ "$pod_phase" = "Running" ]; then break; fi
    sleep 2
done
check "Recovered pod running" "Running" "$pod_phase"
sleep 5

# Verify data persisted through reap
persist_resp=$(curl -s "$GATEWAY/api/v1/workspaces/${WORKSPACE_A}/storage/files?path=offline-test" \
    -H "Authorization: Bearer ${TOKEN}:${USER_A}")
check "Data persisted after reap+recovery" "persist.txt" "$persist_resp"
check "Back to online mode" "online" "$persist_resp"

# ── 11. Auth validation ──────────────────────────────────────────────
echo ""
echo "[11] Auth validation"
bad_auth=$(curl -s -o /dev/null -w "%{http_code}" "$GATEWAY/api/v1/workspaces/storage" \
    -H "Authorization: Bearer wrong-token" 2>/dev/null)
check_status "Bad token returns 401/403" "401" "$bad_auth"

# ── Summary ──────────────────────────────────────────────────────────
echo ""
echo "=== Storage Service E2E Results: $pass passed, $fail failed ==="
if [ "$fail" -gt 0 ]; then
    exit 1
fi
