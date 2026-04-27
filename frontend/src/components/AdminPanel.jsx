import { useState, useEffect, useCallback } from 'react';
import { useApp } from '../context/AppContext';

const ADMIN_TABS = [
  { key: 'users', label: '使用者管理' },
  { key: 'workspaces', label: '工作區管理' },
  { key: 'pods', label: 'Pod 狀態' },
  { key: 'channels', label: 'Channel 狀態' },
  { key: 'reap', label: 'Pod 回收' },
];

export default function AdminPanel() {
  const { adminApi } = useApp();
  const [activeTab, setActiveTab] = useState('users');

  return (
    <div className="flex h-full flex-col space-y-4 overflow-hidden">
      <h2 className="shrink-0 text-lg font-semibold dark:text-gray-100">管理員面板</h2>

      {/* Sub-tabs */}
      <nav className="shrink-0 flex gap-1 overflow-x-auto border-b border-gray-200 dark:border-gray-700">
        {ADMIN_TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`whitespace-nowrap px-3 py-1.5 text-xs font-medium border-b-2 transition-colors ${
              activeTab === tab.key
                ? 'border-amber-600 text-amber-700 dark:text-amber-400'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-500'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      {/* Content */}
      <div className="flex-1 overflow-auto">
        {activeTab === 'users' && <UsersSection adminApi={adminApi} />}
        {activeTab === 'workspaces' && <WorkspacesSection adminApi={adminApi} />}
        {activeTab === 'pods' && <PodsSection adminApi={adminApi} />}
        {activeTab === 'channels' && <ChannelsSection adminApi={adminApi} />}
        {activeTab === 'reap' && <ReapSection adminApi={adminApi} />}
      </div>
    </div>
  );
}

/* ─── Users ─── */
function UsersSection({ adminApi }) {
  const [users, setUsers] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [actionLoading, setActionLoading] = useState('');

  const fetchUsers = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await adminApi.listUsers({ limit: 100 });
      setUsers(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [adminApi]);

  useEffect(() => { fetchUsers(); }, [fetchUsers]);

  const toggleActive = async (userId, currentActive) => {
    setActionLoading(userId);
    try {
      await adminApi.setUserActive(userId, !currentActive);
      await fetchUsers();
    } catch (e) {
      setError(e.message);
    } finally {
      setActionLoading('');
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm text-gray-500 dark:text-gray-400">
          {users ? `共 ${users.total} 位使用者` : ''}
        </span>
        <button onClick={fetchUsers} disabled={loading} className="text-xs text-blue-600 dark:text-blue-400 hover:underline disabled:opacity-50">
          {loading ? '載入中...' : '重新整理'}
        </button>
      </div>

      {error && <div className="rounded bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-400">{error}</div>}

      {users?.users?.length > 0 && (
        <div className="overflow-auto rounded border border-gray-200 dark:border-gray-700">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 dark:bg-gray-800 text-left text-xs text-gray-500 dark:text-gray-400">
              <tr>
                <th className="px-3 py-2">User ID</th>
                <th className="px-3 py-2">Email</th>
                <th className="px-3 py-2">認證方式</th>
                <th className="px-3 py-2">狀態</th>
                <th className="px-3 py-2">建立時間</th>
                <th className="px-3 py-2">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
              {users.users.map((u) => (
                <tr key={u.user_id} className="hover:bg-gray-50 dark:hover:bg-gray-700">
                  <td className="px-3 py-2 font-mono text-xs">{u.user_id}</td>
                  <td className="px-3 py-2 text-xs">{u.email || '-'}</td>
                  <td className="px-3 py-2 text-xs">{u.auth_provider}</td>
                  <td className="px-3 py-2">
                    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
                      u.is_active ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400' : 'bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-400'
                    }`}>
                      {u.is_active ? '啟用' : '停用'}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-xs text-gray-500">
                    {u.created_at ? new Date(u.created_at).toLocaleString('zh-TW') : '-'}
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => toggleActive(u.user_id, u.is_active)}
                      disabled={actionLoading === u.user_id}
                      className={`rounded px-2 py-1 text-xs text-white disabled:opacity-50 ${
                        u.is_active ? 'bg-red-500 hover:bg-red-600' : 'bg-green-500 hover:bg-green-600'
                      }`}
                    >
                      {actionLoading === u.user_id ? '...' : u.is_active ? '停用' : '啟用'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ─── Workspaces ─── */
function WorkspacesSection({ adminApi }) {
  const [workspaces, setWorkspaces] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [expandedWs, setExpandedWs] = useState(null);
  const [members, setMembers] = useState(null);

  // Rename
  const [renaming, setRenaming] = useState(null);
  const [renameValue, setRenameValue] = useState('');

  // Workspace delete
  const [deletingWorkspace, setDeletingWorkspace] = useState('');

  // File browser
  const [fileBrowser, setFileBrowser] = useState(null); // { wsId, path, files, mode }
  const [fileLoading, setFileLoading] = useState(false);
  const [fileUploading, setFileUploading] = useState(false);
  const [mkdirPath, setMkdirPath] = useState('');
  const [showMkdir, setShowMkdir] = useState(false);

  // Access
  const [accessData, setAccessData] = useState(null);

  // Create workspace
  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState({ workspaceId: '', sizeGb: 1, displayName: '', ownerUserId: '' });
  const [creating, setCreating] = useState(false);

  const fetchWorkspaces = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await adminApi.listWorkspaces({ limit: 100, status: statusFilter || undefined });
      setWorkspaces(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [adminApi, statusFilter]);

  useEffect(() => { fetchWorkspaces(); }, [fetchWorkspaces]);

  const toggleMembers = async (wsId) => {
    if (expandedWs === wsId) {
      setExpandedWs(null);
      setMembers(null);
      return;
    }
    try {
      const data = await adminApi.listMembers(wsId);
      setMembers(data);
      setExpandedWs(wsId);
    } catch (e) {
      setError(e.message);
    }
  };

  // ─── Create workspace ───
  const handleCreate = async () => {
    if (!createForm.workspaceId) { setError('請輸入 Workspace ID'); return; }
    setCreating(true);
    setError('');
    try {
      await adminApi.createWorkspace(createForm);
      setShowCreate(false);
      setCreateForm({ workspaceId: '', sizeGb: 1, displayName: '', ownerUserId: '' });
      await fetchWorkspaces();
    } catch (e) {
      setError(e.message);
    } finally {
      setCreating(false);
    }
  };

  // ─── Rename ───
  const handleRename = async (wsId) => {
    if (!renameValue.trim()) return;
    setError('');
    try {
      await adminApi.renameWorkspace(wsId, renameValue.trim());
      setRenaming(null);
      await fetchWorkspaces();
    } catch (e) {
      setError(e.message);
    }
  };

  // ─── Delete workspace (#29) ───
  const handleDeleteWorkspace = async (wsId) => {
    if (!confirm(`確定要刪除工作區「${wsId}」？將同時刪除 PVC、DB 記錄和 K8s 資源，此操作不可逆。`)) return;
    setDeletingWorkspace(wsId);
    try {
      await adminApi.deleteWorkspace(wsId);
      if (fileBrowser?.wsId === wsId) setFileBrowser(null);
      if (accessData?.workspace_id === wsId) setAccessData(null);
      if (expandedWs === wsId) { setExpandedWs(null); setMembers(null); }
      await fetchWorkspaces();
    } catch (e) {
      setError(e.message);
    } finally {
      setDeletingWorkspace('');
    }
  };

  // ─── File browser ───
  const openFiles = async (wsId, path = '') => {
    setFileLoading(true);
    setError('');
    setAccessData(null);
    try {
      const data = await adminApi.listStorageFiles(wsId, path);
      setFileBrowser({ wsId, path: path || '', files: data.files, mode: data.mode, total: data.total });
    } catch (e) {
      setError(e.message);
    } finally {
      setFileLoading(false);
    }
  };

  const handleFileUpload = async (e) => {
    const file = e.target.files?.[0];
    if (!file || !fileBrowser) return;
    setFileUploading(true);
    setError('');
    try {
      await adminApi.uploadStorageFile(fileBrowser.wsId, file, fileBrowser.path);
      await openFiles(fileBrowser.wsId, fileBrowser.path);
    } catch (err) {
      setError(err.message);
    } finally {
      setFileUploading(false);
      e.target.value = '';
    }
  };

  const handleFileDownload = async (fileName) => {
    if (!fileBrowser) return;
    const filePath = fileBrowser.path ? `${fileBrowser.path}/${fileName}` : fileName;
    try {
      const blob = await adminApi.downloadStorageFile(fileBrowser.wsId, filePath);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = fileName;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e.message);
    }
  };

  const handleFileDelete = async (fileName, type) => {
    if (!fileBrowser) return;
    const filePath = fileBrowser.path ? `${fileBrowser.path}/${fileName}` : fileName;
    const label = type === 'dir' ? '目錄' : '檔案';
    if (!confirm(`確定要刪除${label}「${filePath}」？`)) return;
    try {
      await adminApi.deleteStorageFile(fileBrowser.wsId, filePath);
      await openFiles(fileBrowser.wsId, fileBrowser.path);
    } catch (e) {
      setError(e.message);
    }
  };

  const handleMkdir = async () => {
    if (!mkdirPath.trim() || !fileBrowser) return;
    const fullPath = fileBrowser.path ? `${fileBrowser.path}/${mkdirPath.trim()}` : mkdirPath.trim();
    try {
      await adminApi.mkdirStorage(fileBrowser.wsId, fullPath);
      setMkdirPath('');
      setShowMkdir(false);
      await openFiles(fileBrowser.wsId, fileBrowser.path);
    } catch (e) {
      setError(e.message);
    }
  };

  const navigateUp = () => {
    if (!fileBrowser?.path) return;
    const parts = fileBrowser.path.split('/').filter(Boolean);
    parts.pop();
    openFiles(fileBrowser.wsId, parts.join('/'));
  };

  // ─── Access ───
  const openAccess = async (wsId) => {
    if (accessData?.workspace_id === wsId) { setAccessData(null); return; }
    setError('');
    setFileBrowser(null);
    try {
      const data = await adminApi.getStorageAccess(wsId);
      setAccessData(data);
    } catch (e) {
      setError(e.message);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm text-gray-500">
            {workspaces ? `共 ${workspaces.total} 個工作區` : ''}
          </span>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="rounded border border-gray-300 px-2 py-1 text-xs"
          >
            <option value="">全部</option>
            <option value="active">Active</option>
            <option value="idle">Idle</option>
          </select>
        </div>
        <div className="flex gap-2">
          <button onClick={() => setShowCreate(!showCreate)} className="rounded bg-blue-600 px-2 py-1 text-xs text-white hover:bg-blue-700">
            建立工作區
          </button>
          <button onClick={fetchWorkspaces} disabled={loading} className="text-xs text-blue-600 dark:text-blue-400 hover:underline disabled:opacity-50">
            {loading ? '載入中...' : '重新整理'}
          </button>
        </div>
      </div>

      {error && <div className="rounded bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-400">{error}</div>}

      {/* Create workspace form */}
      {showCreate && (
        <div className="rounded border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/30 p-3 space-y-2">
          <h4 className="text-sm font-medium text-blue-800 dark:text-blue-300">建立 Group 工作區</h4>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <input
              placeholder="Workspace ID *"
              value={createForm.workspaceId}
              onChange={(e) => setCreateForm({ ...createForm, workspaceId: e.target.value })}
              className="rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 dark:text-gray-200 px-2 py-1 text-xs focus:border-blue-500 focus:outline-none"
            />
            <input
              type="number" min={1} placeholder="容量 (GB)"
              value={createForm.sizeGb}
              onChange={(e) => setCreateForm({ ...createForm, sizeGb: Number(e.target.value) })}
              className="rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 dark:text-gray-200 px-2 py-1 text-xs focus:border-blue-500 focus:outline-none"
            />
            <input
              placeholder="顯示名稱"
              value={createForm.displayName}
              onChange={(e) => setCreateForm({ ...createForm, displayName: e.target.value })}
              className="rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 dark:text-gray-200 px-2 py-1 text-xs focus:border-blue-500 focus:outline-none"
            />
            <input
              placeholder="Owner User ID"
              value={createForm.ownerUserId}
              onChange={(e) => setCreateForm({ ...createForm, ownerUserId: e.target.value })}
              className="rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 dark:text-gray-200 px-2 py-1 text-xs focus:border-blue-500 focus:outline-none"
            />
          </div>
          <div className="flex gap-2">
            <button onClick={handleCreate} disabled={creating} className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50">
              {creating ? '建立中...' : '建立'}
            </button>
            <button onClick={() => setShowCreate(false)} className="text-xs text-gray-500 dark:text-gray-400 hover:underline">取消</button>
          </div>
        </div>
      )}

      {workspaces?.workspaces?.length > 0 && (
        <div className="overflow-auto rounded border border-gray-200 dark:border-gray-700">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 dark:bg-gray-800 text-left text-xs text-gray-500 dark:text-gray-400">
              <tr>
                <th className="px-3 py-2">Workspace ID</th>
                <th className="px-3 py-2">顯示名稱</th>
                <th className="px-3 py-2">User</th>
                <th className="px-3 py-2">狀態</th>
                <th className="px-3 py-2">Tier</th>
                <th className="px-3 py-2">Sessions</th>
                <th className="px-3 py-2">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
              {workspaces.workspaces.map((ws) => (
                <tr key={ws.workspace_id} className="hover:bg-gray-50 dark:hover:bg-gray-700">
                  <td className="px-3 py-2 font-mono text-xs">{ws.workspace_id}</td>
                  <td className="px-3 py-2 text-xs">
                    {renaming === ws.workspace_id ? (
                      <span className="flex items-center gap-1">
                        <input
                          value={renameValue}
                          onChange={(e) => setRenameValue(e.target.value)}
                          onKeyDown={(e) => e.key === 'Enter' && handleRename(ws.workspace_id)}
                          className="w-28 rounded border border-gray-300 px-1 py-0.5 text-xs focus:border-blue-500 focus:outline-none"
                          autoFocus
                        />
                        <button onClick={() => handleRename(ws.workspace_id)} className="text-blue-600 hover:underline">OK</button>
                        <button onClick={() => setRenaming(null)} className="text-gray-400 hover:underline">X</button>
                      </span>
                    ) : (
                      <span
                        className="cursor-pointer hover:text-blue-600"
                        onClick={() => { setRenaming(ws.workspace_id); setRenameValue(ws.display_name || ''); }}
                        title="點擊修改顯示名稱"
                      >
                        {ws.display_name || '-'}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-xs">{ws.user_id}</td>
                  <td className="px-3 py-2">
                    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
                      ws.status === 'active' ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
                    }`}>
                      {ws.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-xs">{ws.resource_tier}</td>
                  <td className="px-3 py-2 text-xs">{ws.active_sessions ?? '-'}</td>
                  <td className="px-3 py-2">
                    <div className="flex gap-1 flex-wrap">
                      <button onClick={() => toggleMembers(ws.workspace_id)} className="rounded bg-gray-100 dark:bg-gray-700 px-2 py-1 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600">
                        {expandedWs === ws.workspace_id ? '收合成員' : '成員'}
                      </button>
                      <button onClick={() => openFiles(ws.workspace_id)} className="rounded bg-gray-100 dark:bg-gray-700 px-2 py-1 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600">
                        檔案
                      </button>
                      <button onClick={() => openAccess(ws.workspace_id)} className="rounded bg-gray-100 dark:bg-gray-700 px-2 py-1 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600">
                        權限
                      </button>
                      <button
                        onClick={() => handleDeleteWorkspace(ws.workspace_id)}
                        disabled={deletingWorkspace === ws.workspace_id}
                        className="rounded bg-red-600 px-2 py-1 text-xs text-white hover:bg-red-700 disabled:opacity-50"
                      >
                        {deletingWorkspace === ws.workspace_id ? '...' : '刪除工作區'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Members detail */}
      {expandedWs && members && (
        <MembersPanel
          adminApi={adminApi}
          workspaceId={expandedWs}
          members={members}
          onClose={() => { setExpandedWs(null); setMembers(null); }}
          onRefresh={async () => {
            const data = await adminApi.listMembers(expandedWs);
            setMembers(data);
          }}
          setError={setError}
        />
      )}

      {/* File browser panel */}
      {fileBrowser && (
        <div className="rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 p-3 space-y-2">
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-medium">
              {fileBrowser.wsId}
              <span className="ml-2 font-mono text-xs text-gray-500 dark:text-gray-400">/{fileBrowser.path}</span>
              {fileBrowser.mode && (
                <span className={`ml-2 inline-flex items-center rounded-full px-2 py-0.5 text-xs ${
                  fileBrowser.mode === 'online' ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400' : 'bg-orange-100 text-orange-700'
                }`}>
                  {fileBrowser.mode}
                </span>
              )}
            </h4>
            <div className="flex gap-2 items-center">
              <button onClick={() => setShowMkdir(!showMkdir)} className="rounded bg-gray-200 dark:bg-gray-700 px-2 py-1 text-xs dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600">
                新增目錄
              </button>
              <label className={`rounded bg-blue-600 px-2 py-1 text-xs text-white hover:bg-blue-700 cursor-pointer ${fileUploading ? 'opacity-50 pointer-events-none' : ''}`}>
                {fileUploading ? '上傳中...' : '上傳檔案'}
                <input type="file" className="hidden" onChange={handleFileUpload} disabled={fileUploading} />
              </label>
              <button onClick={() => setFileBrowser(null)} className="text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300">關閉</button>
            </div>
          </div>

          {showMkdir && (
            <div className="flex items-center gap-2">
              <input
                placeholder="目錄名稱"
                value={mkdirPath}
                onChange={(e) => setMkdirPath(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleMkdir()}
                className="rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 dark:text-gray-200 px-2 py-1 text-xs focus:border-blue-500 focus:outline-none"
                autoFocus
              />
              <button onClick={handleMkdir} className="text-xs text-blue-600 hover:underline">建立</button>
              <button onClick={() => { setShowMkdir(false); setMkdirPath(''); }} className="text-xs text-gray-400 hover:underline">取消</button>
            </div>
          )}

          {fileLoading ? (
            <div className="text-xs text-gray-500 dark:text-gray-400">載入中...</div>
          ) : (
            <div className="overflow-auto rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
              <table className="w-full text-xs">
                <thead className="bg-gray-50 text-left text-gray-500">
                  <tr>
                    <th className="px-3 py-1.5">名稱</th>
                    <th className="px-3 py-1.5">類型</th>
                    <th className="px-3 py-1.5">大小</th>
                    <th className="px-3 py-1.5">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                  {fileBrowser.path && (
                    <tr className="hover:bg-gray-50 cursor-pointer" onClick={navigateUp}>
                      <td className="px-3 py-1.5 text-blue-600" colSpan={4}>.. (上一層)</td>
                    </tr>
                  )}
                  {fileBrowser.files?.map((f) => (
                    <tr key={f.name} className="hover:bg-gray-50 dark:hover:bg-gray-700">
                      <td className="px-3 py-1.5 font-mono">
                        {f.type === 'dir' ? (
                          <button
                            onClick={() => openFiles(fileBrowser.wsId, fileBrowser.path ? `${fileBrowser.path}/${f.name}` : f.name)}
                            className="text-blue-600 hover:underline"
                          >
                            {f.name}/
                          </button>
                        ) : f.name}
                      </td>
                      <td className="px-3 py-1.5 text-gray-500 dark:text-gray-400">{f.type}</td>
                      <td className="px-3 py-1.5 text-gray-500 dark:text-gray-400">{f.type === 'file' && f.size != null ? `${f.size} B` : '-'}</td>
                      <td className="px-3 py-1.5">
                        <div className="flex gap-1">
                          {f.type === 'file' && (
                            <button onClick={() => handleFileDownload(f.name)} className="text-blue-600 hover:underline">下載</button>
                          )}
                          <button onClick={() => handleFileDelete(f.name, f.type)} className="text-red-500 hover:underline">刪除</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {(!fileBrowser.files || fileBrowser.files.length === 0) && (
                    <tr><td colSpan={4} className="px-3 py-3 text-center text-gray-400 dark:text-gray-500">空目錄</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Access panel */}
      {accessData && (
        <div className="rounded border border-purple-200 dark:border-purple-800 bg-purple-50 dark:bg-purple-900/30 p-3 space-y-2">
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-medium text-purple-800 dark:text-purple-300">
              {accessData.workspace_id} 存取權限
            </h4>
            <button onClick={() => setAccessData(null)} className="text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300">關閉</button>
          </div>
          {accessData.members?.length > 0 ? (
            <div className="space-y-1">
              {accessData.members.map((m) => (
                <div key={m.user_id} className="flex items-center gap-3 text-xs">
                  <span className="font-mono">{m.user_id}</span>
                  <span className={`rounded-full px-2 py-0.5 font-medium ${
                    m.role === 'owner' ? 'bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-400'
                    : m.role === 'admin' ? 'bg-purple-100 dark:bg-purple-900/40 text-purple-700 dark:text-purple-400'
                    : m.role === 'member' ? 'bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-400'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
                  }`}>
                    {m.role}
                  </span>
                  {m.granted_by && <span className="text-gray-400 dark:text-gray-500">by {m.granted_by}</span>}
                  {m.granted_at && <span className="text-gray-400 dark:text-gray-500">{new Date(m.granted_at).toLocaleString('zh-TW')}</span>}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-gray-500 dark:text-gray-400">無成員資料</p>
          )}
        </div>
      )}
    </div>
  );
}

/* ─── Members Panel (sub-component for WorkspacesSection) ─── */
function MembersPanel({ adminApi, workspaceId, members, onClose, onRefresh, setError }) {
  const [users, setUsers] = useState(null);
  const [showAdd, setShowAdd] = useState(false);
  const [selectedUsers, setSelectedUsers] = useState([]);
  const [addRole, setAddRole] = useState('member');
  const [adding, setAdding] = useState(false);
  const [updatingRole, setUpdatingRole] = useState('');
  const [removing, setRemoving] = useState('');

  // Fetch all users for the picker
  const fetchUsers = useCallback(async () => {
    try {
      const data = await adminApi.listUsers({ limit: 200 });
      setUsers(data.users || []);
    } catch (e) {
      setError(e.message);
    }
  }, [adminApi, setError]);

  // Load users when add panel opens
  useEffect(() => {
    if (showAdd && !users) fetchUsers();
  }, [showAdd, users, fetchUsers]);

  const existingUserIds = new Set(members.members?.map((m) => m.user_id) || []);
  const availableUsers = users?.filter((u) => !existingUserIds.has(u.user_id) && u.is_active) || [];

  const toggleUser = (userId) => {
    setSelectedUsers((prev) =>
      prev.includes(userId) ? prev.filter((id) => id !== userId) : [...prev, userId]
    );
  };

  const handleAdd = async () => {
    if (!selectedUsers.length) return;
    setAdding(true);
    try {
      for (const userId of selectedUsers) {
        await adminApi.addMember(workspaceId, { userId, role: addRole });
      }
      setSelectedUsers([]);
      setShowAdd(false);
      await onRefresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setAdding(false);
    }
  };

  const handleUpdateRole = async (userId, newRole) => {
    setUpdatingRole(userId);
    try {
      await adminApi.updateMemberRole(workspaceId, userId, newRole);
      await onRefresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setUpdatingRole('');
    }
  };

  const handleRemove = async (userId) => {
    if (!confirm(`確定要移除成員「${userId}」？`)) return;
    setRemoving(userId);
    try {
      await adminApi.removeMember(workspaceId, userId);
      await onRefresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setRemoving('');
    }
  };

  const roleBadge = (role) => {
    const cls = role === 'owner' ? 'bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-400'
      : role === 'admin' ? 'bg-purple-100 dark:bg-purple-900/40 text-purple-700 dark:text-purple-400'
      : role === 'member' ? 'bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-400'
      : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400';
    return <span className={`rounded-full px-2 py-0.5 font-medium ${cls}`}>{role}</span>;
  };

  return (
    <div className="rounded border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/30 p-3 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium text-blue-800 dark:text-blue-300">{workspaceId} 的成員</h4>
        <div className="flex gap-2">
          <button onClick={() => setShowAdd(!showAdd)} className="rounded bg-blue-600 px-2 py-1 text-xs text-white hover:bg-blue-700">
            新增成員
          </button>
          <button onClick={onClose} className="text-xs text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300">關閉</button>
        </div>
      </div>

      {/* Add members form */}
      {showAdd && (
        <div className="rounded border border-blue-300 dark:border-blue-700 bg-white dark:bg-gray-800 p-3 space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-600 dark:text-gray-400">角色：</span>
            <select
              value={addRole}
              onChange={(e) => setAddRole(e.target.value)}
              className="rounded border border-gray-300 px-2 py-1 text-xs"
            >
              <option value="member">member</option>
              <option value="admin">admin</option>
              <option value="readonly">readonly</option>
            </select>
          </div>
          {availableUsers.length > 0 ? (
            <div className="max-h-40 overflow-auto rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800">
              {availableUsers.map((u) => (
                <label key={u.user_id} className="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-700 cursor-pointer text-xs">
                  <input
                    type="checkbox"
                    checked={selectedUsers.includes(u.user_id)}
                    onChange={() => toggleUser(u.user_id)}
                    className="rounded border-gray-300"
                  />
                  <span className="font-mono">{u.user_id}</span>
                  {u.email && <span className="text-gray-400">{u.email}</span>}
                </label>
              ))}
            </div>
          ) : (
            <p className="text-xs text-gray-500">{users ? '沒有可新增的使用者' : '載入中...'}</p>
          )}
          <div className="flex gap-2">
            <button
              onClick={handleAdd}
              disabled={adding || !selectedUsers.length}
              className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {adding ? '新增中...' : `新增 ${selectedUsers.length} 位成員`}
            </button>
            <button onClick={() => { setShowAdd(false); setSelectedUsers([]); }} className="text-xs text-gray-500 dark:text-gray-400 hover:underline">取消</button>
          </div>
        </div>
      )}

      {/* Members list */}
      {members.members?.length > 0 ? (
        <div className="space-y-1">
          {members.members.map((m) => (
            <div key={m.user_id} className="flex items-center gap-3 text-xs">
              <span className="font-mono min-w-[100px]">{m.user_id}</span>
              {m.role === 'owner' ? (
                roleBadge('owner')
              ) : (
                <select
                  value={m.role}
                  onChange={(e) => handleUpdateRole(m.user_id, e.target.value)}
                  disabled={updatingRole === m.user_id}
                  className="rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 dark:text-gray-200 px-1 py-0.5 text-xs disabled:opacity-50"
                >
                  <option value="admin">admin</option>
                  <option value="member">member</option>
                  <option value="readonly">readonly</option>
                </select>
              )}
              {m.granted_by && <span className="text-gray-400 dark:text-gray-500">by {m.granted_by}</span>}
              {m.role !== 'owner' && (
                <button
                  onClick={() => handleRemove(m.user_id)}
                  disabled={removing === m.user_id}
                  className="text-red-500 hover:underline disabled:opacity-50"
                >
                  {removing === m.user_id ? '...' : '移除'}
                </button>
              )}
            </div>
          ))}
        </div>
      ) : (
        <p className="text-xs text-gray-500 dark:text-gray-400">無成員資料</p>
      )}
    </div>
  );
}

/* ─── Pods ─── */
function PodsSection({ adminApi }) {
  const [pods, setPods] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const fetchPods = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await adminApi.getPodsStatus();
      setPods(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [adminApi]);

  useEffect(() => { fetchPods(); }, [fetchPods]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm text-gray-500 dark:text-gray-400">Pod 狀態總覽</span>
        <button onClick={fetchPods} disabled={loading} className="text-xs text-blue-600 dark:text-blue-400 hover:underline disabled:opacity-50">
          {loading ? '載入中...' : '重新整理'}
        </button>
      </div>

      {error && <div className="rounded bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-400">{error}</div>}

      {pods && (
        <>
          {/* Summary cards */}
          <div className="grid grid-cols-3 gap-3">
            <div className="rounded border border-gray-200 dark:border-gray-700 p-3 text-center">
              <div className="text-2xl font-bold text-green-600 dark:text-green-400">{pods.summary?.running ?? 0}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">Running</div>
            </div>
            <div className="rounded border border-gray-200 dark:border-gray-700 p-3 text-center">
              <div className="text-2xl font-bold text-yellow-600 dark:text-yellow-400">{pods.summary?.pending ?? 0}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">Pending</div>
            </div>
            <div className="rounded border border-gray-200 dark:border-gray-700 p-3 text-center">
              <div className="text-2xl font-bold text-gray-500 dark:text-gray-400">{pods.summary?.terminated ?? 0}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">Terminated</div>
            </div>
          </div>

          <div className="text-sm text-gray-500">
            總 Sessions: {pods.total_sessions ?? 0}
          </div>

          {/* Running pods table */}
          {pods.running_pods?.length > 0 && (
            <div className="overflow-auto rounded border border-gray-200 dark:border-gray-700">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 dark:bg-gray-800 text-left text-xs text-gray-500 dark:text-gray-400">
                  <tr>
                    <th className="px-3 py-2">Pod Name</th>
                    <th className="px-3 py-2">Workspace</th>
                    <th className="px-3 py-2">User</th>
                    <th className="px-3 py-2">Session</th>
                    <th className="px-3 py-2">最後活動</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                  {pods.running_pods.map((pod) => (
                    <tr key={pod.pod_name} className="hover:bg-gray-50 dark:hover:bg-gray-700">
                      <td className="px-3 py-2 font-mono text-xs">{pod.pod_name}</td>
                      <td className="px-3 py-2 font-mono text-xs">{pod.workspace_id}</td>
                      <td className="px-3 py-2 text-xs">{pod.user_id}</td>
                      <td className="px-3 py-2 font-mono text-xs">{pod.session_id}</td>
                      <td className="px-3 py-2 text-xs text-gray-500">
                        {pod.last_active_at ? new Date(pod.last_active_at).toLocaleString('zh-TW') : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/* ─── Channels (Gateway API #18, requires admin token) ─── */
function ChannelsSection({ adminApi }) {
  const [channelStatus, setChannelStatus] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const fetchChannels = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await adminApi.adminChannels();
      setChannelStatus(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [adminApi]);

  useEffect(() => { fetchChannels(); }, [fetchChannels]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm text-gray-500 dark:text-gray-400">Channel 狀態</span>
        <button onClick={fetchChannels} disabled={loading} className="text-xs text-blue-600 dark:text-blue-400 hover:underline disabled:opacity-50">
          {loading ? '載入中...' : '重新整理'}
        </button>
      </div>

      {error && <div className="rounded bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-400">{error}</div>}

      {channelStatus && (
        <div className="space-y-2">
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div className="rounded border border-gray-200 dark:border-gray-700 p-3">
              <span className="text-gray-500 dark:text-gray-400">Manager 狀態</span>
              <div className={`mt-1 font-medium ${channelStatus.manager_running ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}>
                {channelStatus.manager_running ? '運行中' : '已停止'}
              </div>
            </div>
            <div className="rounded border border-gray-200 dark:border-gray-700 p-3">
              <span className="text-gray-500 dark:text-gray-400">Chat→Workspace 映射數</span>
              <div className="mt-1 font-medium">{channelStatus.store_mappings}</div>
            </div>
            <div className="rounded border border-gray-200 dark:border-gray-700 p-3">
              <span className="text-gray-500 dark:text-gray-400">待處理 Inbound 訊息</span>
              <div className="mt-1 font-medium">{channelStatus.inbound_pending}</div>
            </div>
            <div className="rounded border border-gray-200 dark:border-gray-700 p-3">
              <span className="text-gray-500 dark:text-gray-400">Channels</span>
              <div className="mt-1 space-y-1">
                {channelStatus.channels && Object.entries(channelStatus.channels).map(([name, info]) => (
                  <div key={name} className="flex items-center gap-2 text-xs">
                    <span className={`h-2 w-2 rounded-full ${info.running ? 'bg-green-500' : 'bg-red-500'}`} />
                    <span className="font-mono">{name}</span>
                    <span className="text-gray-400 dark:text-gray-500">{info.running ? '運行中' : '已停止'}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
          <details className="text-xs">
            <summary className="cursor-pointer text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200">原始 JSON</summary>
            <pre className="mt-1 rounded bg-gray-100 dark:bg-gray-700 p-3 dark:text-gray-300 overflow-auto">
              {JSON.stringify(channelStatus, null, 2)}
            </pre>
          </details>
        </div>
      )}
    </div>
  );
}

/* ─── Reap ─── */
function ReapSection({ adminApi }) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const [timeoutMinutes, setTimeoutMinutes] = useState(10);
  const [forceAll, setForceAll] = useState(false);

  const handleReap = async () => {
    if (forceAll && !confirm('確定要強制回收所有 Pod（含活躍中的）？')) return;
    setLoading(true);
    setError('');
    setResult(null);
    try {
      const data = await adminApi.reap({ timeoutMinutes, forceAll });
      setResult(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="rounded border border-yellow-200 dark:border-yellow-800 bg-yellow-50 dark:bg-yellow-900/30 p-3 text-sm text-yellow-800 dark:text-yellow-200">
        此操作會回收閒置的 workspace Pod。強制回收會影響所有使用者，請謹慎使用。
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="text-sm text-gray-600 dark:text-gray-400">閒置超時（分鐘）</span>
          <input
            type="number"
            value={timeoutMinutes}
            onChange={(e) => setTimeoutMinutes(Number(e.target.value))}
            min={1}
            className="mt-1 block w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 dark:text-gray-200 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none"
          />
        </label>

        <label className="flex items-center gap-2 self-end pb-2">
          <input
            type="checkbox"
            checked={forceAll}
            onChange={(e) => setForceAll(e.target.checked)}
            className="rounded border-gray-300"
          />
          <span className="text-sm text-red-600 dark:text-red-400 font-medium">強制回收所有 Pod</span>
        </label>
      </div>

      <button
        onClick={handleReap}
        disabled={loading}
        className={`rounded px-4 py-2 text-sm text-white disabled:opacity-50 ${
          forceAll ? 'bg-red-600 hover:bg-red-700' : 'bg-amber-600 hover:bg-amber-700'
        }`}
      >
        {loading ? '執行中...' : forceAll ? '強制回收所有 Pod' : '回收閒置 Pod'}
      </button>

      {error && <div className="rounded bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-400">{error}</div>}

      {result && (
        <div className="space-y-2">
          <div className="text-sm">
            已回收 <span className="font-semibold">{result.total_reaped}</span> 個工作區
          </div>
          <pre className="rounded bg-gray-100 dark:bg-gray-700 p-3 text-xs dark:text-gray-300 overflow-auto">
            {JSON.stringify(result, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
