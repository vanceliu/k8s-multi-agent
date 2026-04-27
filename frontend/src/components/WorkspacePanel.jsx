import { useState } from 'react';
import { useApp } from '../context/AppContext';

export default function WorkspacePanel() {
  const { api, workspaceId, setWorkspaceId, sessionId, setSessionId, workspaceReady, setWorkspaceReady } = useApp();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [wsInfo, setWsInfo] = useState(null);
  const [resourceTier, setResourceTier] = useState('standard');

  const handleEnsure = async () => {
    setLoading(true);
    setError('');
    try {
      const data = await api.ensureWorkspace({ sessionId, resourceTier });
      setWsInfo(data);
      setWorkspaceId(data.workspace_id);

      if (data.status === 'ready') {
        setWorkspaceReady(true);
      } else {
        // Poll until ready
        const poll = setInterval(async () => {
          try {
            const status = await api.getWorkspace(data.workspace_id);
            setWsInfo(status);
            if (status.status === 'active') {
              setWorkspaceReady(true);
              clearInterval(poll);
            }
          } catch {
            clearInterval(poll);
          }
        }, 2000);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleRefreshStatus = async () => {
    if (!workspaceId) return;
    setLoading(true);
    setError('');
    try {
      const data = await api.getWorkspace(workspaceId);
      setWsInfo(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold dark:text-gray-100">工作區管理</h2>

      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className="text-sm text-gray-600 dark:text-gray-400">Session ID</span>
          <input
            type="text"
            value={sessionId}
            onChange={(e) => setSessionId(e.target.value)}
            className="mt-1 block w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm font-mono dark:text-gray-200 focus:border-blue-500 focus:outline-none"
          />
        </label>

        <label className="block">
          <span className="text-sm text-gray-600 dark:text-gray-400">Resource Tier</span>
          <select
            value={resourceTier}
            onChange={(e) => setResourceTier(e.target.value)}
            className="mt-1 block w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm dark:text-gray-200 focus:border-blue-500 focus:outline-none"
          >
            <option value="standard">Standard</option>
            <option value="premium">Premium</option>
            <option value="enterprise">Enterprise</option>
          </select>
        </label>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={handleEnsure}
          disabled={loading}
          className="rounded bg-green-600 px-4 py-2 text-sm text-white hover:bg-green-700 disabled:opacity-50"
        >
          {loading ? '處理中...' : '建立 / 恢復工作區'}
        </button>

        {workspaceId && (
          <button
            onClick={handleRefreshStatus}
            disabled={loading}
            className="rounded bg-gray-200 dark:bg-gray-700 px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600 disabled:opacity-50"
          >
            重新整理狀態
          </button>
        )}

        {workspaceReady && (
          <span className="inline-flex items-center gap-1 text-sm text-green-600 dark:text-green-400">
            <span className="h-2 w-2 rounded-full bg-green-500" />
            工作區就緒
          </span>
        )}
      </div>

      {error && (
        <div className="rounded bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-400">{error}</div>
      )}

      {wsInfo && (
        <div className="space-y-2">
          <div className="flex gap-4 text-sm">
            <span className="text-gray-500 dark:text-gray-400">Workspace ID:</span>
            <span className="font-mono dark:text-gray-200">{wsInfo.workspace_id}</span>
          </div>
          <pre className="rounded bg-gray-100 dark:bg-gray-700 p-3 text-xs dark:text-gray-300 overflow-auto">{JSON.stringify(wsInfo, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}
