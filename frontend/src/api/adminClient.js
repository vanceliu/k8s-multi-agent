import { ApiError } from './client';

/**
 * Admin Service + Storage Service API client factory.
 * Admin Service 透過 Gateway proxy（/api/v1/admin/*）統一存取。
 * Storage Service 透過 Gateway proxy（/api/v1/workspaces/storage/*、/api/v1/workspaces/{wid}/storage/*）存取。
 *
 * @param {Object} options
 * @param {string} options.baseUrl - Gateway base URL (與一般 API 相同)
 * @param {string} options.token - Admin Bearer token (env var POC_ADMIN_TOKEN)
 */
export function createAdminApiClient({ baseUrl, token } = {}) {
  const headers = () => ({
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`,
  });

  const buildQuery = (params) => {
    const entries = Object.entries(params).filter(([, v]) => v != null);
    if (!entries.length) return '';
    return '?' + new URLSearchParams(entries).toString();
  };

  return {
    // 17. 列出所有使用者 (GET /api/v1/admin/users)
    async listUsers({ limit = 50, offset = 0, isActive } = {}) {
      const query = buildQuery({ limit, offset, is_active: isActive });
      const res = await fetch(`${baseUrl}/api/v1/admin/users${query}`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`List users failed: ${res.status}`, res.status);
      return res.json();
    },

    // 18. 使用者詳情 (GET /api/v1/admin/users/{user_id})
    async getUser(userId) {
      const res = await fetch(`${baseUrl}/api/v1/admin/users/${userId}`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Get user failed: ${res.status}`, res.status);
      return res.json();
    },

    // 19. 啟用/停用使用者 (PUT /api/v1/admin/users/{user_id}/active)
    async setUserActive(userId, isActive) {
      const res = await fetch(`${baseUrl}/api/v1/admin/users/${userId}/active`, {
        method: 'PUT',
        headers: headers(),
        body: JSON.stringify({ is_active: isActive }),
      });
      if (!res.ok) throw new ApiError(`Set user active failed: ${res.status}`, res.status);
      return res.json();
    },

    // 20. 列出所有工作區 (GET /api/v1/admin/workspaces)
    async listWorkspaces({ limit = 50, offset = 0, status } = {}) {
      const query = buildQuery({ limit, offset, status });
      const res = await fetch(`${baseUrl}/api/v1/admin/workspaces${query}`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`List workspaces failed: ${res.status}`, res.status);
      return res.json();
    },

    // 21. 工作區詳情 (GET /api/v1/admin/workspaces/{workspace_id})
    async getWorkspace(workspaceId) {
      const res = await fetch(`${baseUrl}/api/v1/admin/workspaces/${workspaceId}`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Get workspace failed: ${res.status}`, res.status);
      return res.json();
    },

    // 22. 手動觸發 Pod 回收 (POST /api/v1/admin/reap)
    async reap({ timeoutMinutes, forceAll = false } = {}) {
      const res = await fetch(`${baseUrl}/api/v1/admin/reap`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({
          timeout_minutes: timeoutMinutes,
          force_all: forceAll,
        }),
      });
      if (!res.ok) throw new ApiError(`Reap failed: ${res.status}`, res.status);
      return res.json();
    },

    // 23. 列出工作區成員 (GET /api/v1/admin/workspaces/{wid}/members)
    async listMembers(workspaceId) {
      const res = await fetch(`${baseUrl}/api/v1/admin/workspaces/${workspaceId}/members`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`List members failed: ${res.status}`, res.status);
      return res.json();
    },

    // 24. 新增工作區成員 (POST /api/v1/admin/workspaces/{wid}/members)
    async addMember(workspaceId, { userId, role = 'member', grantedBy } = {}) {
      const res = await fetch(`${baseUrl}/api/v1/admin/workspaces/${workspaceId}/members`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({
          user_id: userId,
          role,
          granted_by: grantedBy,
        }),
      });
      if (!res.ok) throw new ApiError(`Add member failed: ${res.status}`, res.status);
      return res.json();
    },

    // 25. 更新成員角色 (PUT /api/v1/admin/workspaces/{wid}/members/{uid})
    async updateMemberRole(workspaceId, userId, role) {
      const res = await fetch(`${baseUrl}/api/v1/admin/workspaces/${workspaceId}/members/${userId}`, {
        method: 'PUT',
        headers: headers(),
        body: JSON.stringify({ role }),
      });
      if (!res.ok) throw new ApiError(`Update member role failed: ${res.status}`, res.status);
      return res.json();
    },

    // 26. 移除工作區成員 (DELETE /api/v1/admin/workspaces/{wid}/members/{uid})
    async removeMember(workspaceId, userId) {
      const res = await fetch(`${baseUrl}/api/v1/admin/workspaces/${workspaceId}/members/${userId}`, {
        method: 'DELETE',
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Remove member failed: ${res.status}`, res.status);
      return res.json();
    },

    // 27. 刪除工作區 (DELETE /api/v1/admin/workspaces/{workspace_id})
    async deleteWorkspace(workspaceId) {
      const res = await fetch(`${baseUrl}/api/v1/admin/workspaces/${workspaceId}`, {
        method: 'DELETE',
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Delete workspace failed: ${res.status}`, res.status);
      return res.json();
    },

    // 28. Pod 狀態總覽 (GET /api/v1/admin/pods/status)
    async getPodsStatus() {
      const res = await fetch(`${baseUrl}/api/v1/admin/pods/status`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Get pods status failed: ${res.status}`, res.status);
      return res.json();
    },

    // ─── Storage Service API（透過 Gateway proxy /api/v1/workspaces/storage/*）───

    // 29. 建立 Workspace (POST /api/v1/workspaces/storage) — admin only, group workspace
    async createWorkspace({ workspaceId, workspaceType = 'group', sizeGb = 1, displayName, ownerUserId } = {}) {
      const res = await fetch(`${baseUrl}/api/v1/workspaces/storage`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({
          workspace_id: workspaceId,
          workspace_type: workspaceType,
          size_gb: sizeGb,
          display_name: displayName || undefined,
          owner_user_id: ownerUserId || undefined,
        }),
      });
      if (!res.ok) throw new ApiError(`Create workspace failed: ${res.status}`, res.status);
      return res.json();
    },

    // 30. 確保 Workspace Storage 存在 (POST /api/v1/workspaces/storage/ensure) — Orchestrator
    async ensureStorage({ workspaceId, sizeGb = 1 } = {}) {
      const res = await fetch(`${baseUrl}/api/v1/workspaces/storage/ensure`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({
          workspace_id: workspaceId,
          size_gb: sizeGb,
        }),
      });
      if (!res.ok) throw new ApiError(`Ensure storage failed: ${res.status}`, res.status);
      return res.json();
    },

    // 31. 修改 Workspace 顯示名稱 (PUT /api/v1/workspaces/{wid}/rename)
    async renameWorkspace(workspaceId, displayName) {
      const res = await fetch(`${baseUrl}/api/v1/workspaces/${workspaceId}/rename`, {
        method: 'PUT',
        headers: headers(),
        body: JSON.stringify({ display_name: displayName }),
      });
      if (!res.ok) throw new ApiError(`Rename workspace failed: ${res.status}`, res.status);
      return res.json();
    },

    // 32. 列出 Workspace 檔案 (GET /api/v1/workspaces/{wid}/storage/files)
    async listStorageFiles(workspaceId, path = '') {
      const query = path ? `?path=${encodeURIComponent(path)}` : '';
      const res = await fetch(`${baseUrl}/api/v1/workspaces/${workspaceId}/storage/files${query}`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`List storage files failed: ${res.status}`, res.status);
      return res.json();
    },

    // 33. 上傳檔案到 Workspace (POST /api/v1/workspaces/{wid}/storage/files/upload)
    async uploadStorageFile(workspaceId, file, path = '') {
      const query = path ? `?path=${encodeURIComponent(path)}` : '';
      const formData = new FormData();
      formData.append('file', file);
      const res = await fetch(`${baseUrl}/api/v1/workspaces/${workspaceId}/storage/files/upload${query}`, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token}` },
        body: formData,
      });
      if (!res.ok) throw new ApiError(`Upload storage file failed: ${res.status}`, res.status);
      return res.json();
    },

    // 34. 從 Workspace 下載檔案 (GET /api/v1/workspaces/{wid}/storage/files/download)
    async downloadStorageFile(workspaceId, path) {
      const res = await fetch(
        `${baseUrl}/api/v1/workspaces/${workspaceId}/storage/files/download?path=${encodeURIComponent(path)}`,
        { headers: { 'Authorization': `Bearer ${token}` } },
      );
      if (!res.ok) throw new ApiError(`Download storage file failed: ${res.status}`, res.status);
      return res.blob();
    },

    // 35. 刪除 Workspace 檔案或目錄 (DELETE /api/v1/workspaces/{wid}/storage/files)
    async deleteStorageFile(workspaceId, path) {
      const res = await fetch(
        `${baseUrl}/api/v1/workspaces/${workspaceId}/storage/files?path=${encodeURIComponent(path)}`,
        { method: 'DELETE', headers: headers() },
      );
      if (!res.ok) throw new ApiError(`Delete storage file failed: ${res.status}`, res.status);
      return res.json();
    },

    // 36. 建立 Workspace 目錄 (POST /api/v1/workspaces/{wid}/storage/files/mkdir)
    async mkdirStorage(workspaceId, path) {
      const res = await fetch(
        `${baseUrl}/api/v1/workspaces/${workspaceId}/storage/files/mkdir?path=${encodeURIComponent(path)}`,
        { method: 'POST', headers: headers() },
      );
      if (!res.ok) throw new ApiError(`Create storage dir failed: ${res.status}`, res.status);
      return res.json();
    },

    // 37. Workspace 存取權限 (GET /api/v1/workspaces/{wid}/storage/access)
    async getStorageAccess(workspaceId) {
      const res = await fetch(`${baseUrl}/api/v1/workspaces/${workspaceId}/storage/access`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Get storage access failed: ${res.status}`, res.status);
      return res.json();
    },

    // ─── Gateway Admin API（需要 admin token）───

    // 18. Channel 狀態 (GET /api/v1/admin/channels) — Gateway 端點，但需 admin token
    async adminChannels() {
      const res = await fetch(`${baseUrl}/api/v1/admin/channels`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Admin channels failed: ${res.status}`, res.status);
      return res.json();
    },

  };
}
