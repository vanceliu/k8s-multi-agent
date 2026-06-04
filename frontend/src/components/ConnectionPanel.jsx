import { useState } from 'react';
import { useApp } from '../context/AppContext';

export default function ConnectionPanel() {
  const {
    baseUrl, setBaseUrl, token, setToken,
    connected, setConnected, api, resetConnection,
    adminToken, setAdminToken,
    adminConnected, setAdminConnected, adminApi,
  } = useApp();

  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // Admin section toggle
  const [showAdmin, setShowAdmin] = useState(false);
  const [adminStatus, setAdminStatus] = useState(null);
  const [adminLoading, setAdminLoading] = useState(false);
  const [adminError, setAdminError] = useState('');

  const handleConnect = async () => {
    setLoading(true);
    setError('');
    setStatus(null);
    try {
      const data = await api.health();
      setStatus(data);
      setConnected(true);
    } catch (e) {
      setError(e.message);
      setConnected(false);
    } finally {
      setLoading(false);
    }
  };

  const handleAdminConnect = async () => {
    setAdminLoading(true);
    setAdminError('');
    setAdminStatus(null);
    try {
      // Use listUsers as a health check for Admin Service
      const data = await adminApi.listUsers({ limit: 1 });
      setAdminStatus(data);
      setAdminConnected(true);
    } catch (e) {
      setAdminError(`Admin Service 連線失敗: ${e.message}`);
      setAdminConnected(false);
    } finally {
      setAdminLoading(false);
    }
  };

  const handleDisconnect = () => {
    resetConnection();
    setStatus(null);
    setAdminStatus(null);
    setAdminError('');
  };

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold dark:text-gray-100">Gateway 連線設定</h2>

      <div className="grid grid-cols-1 gap-3">
        <label className="block">
          <span className="text-sm text-gray-600 dark:text-gray-400">Base URL</span>
          <input
            type="text"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            disabled={connected}
            className="mt-1 block w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm dark:text-gray-200 focus:border-blue-500 focus:outline-none disabled:bg-gray-100 dark:disabled:bg-gray-600"
          />
        </label>

        <label className="block">
          <span className="text-sm text-gray-600 dark:text-gray-400">Bearer Token</span>
          <input
            type="text"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            disabled={connected}
            className="mt-1 block w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm font-mono dark:text-gray-200 focus:border-blue-500 focus:outline-none disabled:bg-gray-100 dark:disabled:bg-gray-600"
          />
        </label>
      </div>

      <div className="flex items-center gap-3">
        {!connected ? (
          <button
            onClick={handleConnect}
            disabled={loading}
            className="rounded bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {loading ? '連線中...' : '測試連線'}
          </button>
        ) : (
          <button
            onClick={handleDisconnect}
            className="rounded bg-gray-500 px-4 py-2 text-sm text-white hover:bg-gray-600"
          >
            中斷連線
          </button>
        )}

        {connected && (
          <span className="inline-flex items-center gap-1 text-sm text-green-600 dark:text-green-400">
            <span className="h-2 w-2 rounded-full bg-green-500" />
            已連線
          </span>
        )}
      </div>

      {error && (
        <div className="rounded bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-400">{error}</div>
      )}

      {status && (
        <pre className="rounded bg-gray-100 dark:bg-gray-700 p-3 text-xs dark:text-gray-300">{JSON.stringify(status, null, 2)}</pre>
      )}

      {/* Admin Service Section */}
      <div className="border-t pt-4">
        <button
          onClick={() => setShowAdmin(!showAdmin)}
          className="flex items-center gap-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100"
        >
          <span className={`transition-transform ${showAdmin ? 'rotate-90' : ''}`}>&#9654;</span>
          Admin Service 設定（選填）
          {adminConnected && (
            <span className="inline-flex items-center rounded-full bg-amber-100 dark:bg-amber-900 px-2 py-0.5 text-xs font-medium text-amber-800 dark:text-amber-200">
              已連線
            </span>
          )}
        </button>

        {showAdmin && (
          <div className="mt-3 space-y-3 rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 p-4">
            <p className="text-xs text-gray-500 dark:text-gray-400">
              填入 Admin Token 後，將透過 Gateway proxy 啟用管理員功能（使用者管理、工作區管理、Pod/PVC 管理）。
            </p>

            <label className="block">
              <span className="text-sm text-gray-600 dark:text-gray-400">Admin Token</span>
              <input
                type="text"
                value={adminToken}
                onChange={(e) => setAdminToken(e.target.value)}
                disabled={adminConnected}
                placeholder="poc-admin-token-12345"
                className="mt-1 block w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm font-mono dark:text-gray-200 focus:border-blue-500 focus:outline-none disabled:bg-gray-100 dark:disabled:bg-gray-600"
              />
            </label>

            <div className="flex items-center gap-3">
              {!adminConnected ? (
                <button
                  onClick={handleAdminConnect}
                  disabled={adminLoading}
                  className="rounded bg-amber-600 px-4 py-2 text-sm text-white hover:bg-amber-700 disabled:opacity-50"
                >
                  {adminLoading ? '連線中...' : '連線 Admin Service'}
                </button>
              ) : (
                <button
                  onClick={() => {
                    setAdminConnected(false);
                    setAdminStatus(null);
                    setAdminError('');
                  }}
                  className="rounded bg-gray-500 px-4 py-2 text-sm text-white hover:bg-gray-600"
                >
                  中斷 Admin 連線
                </button>
              )}
            </div>

            {adminError && (
              <div className="rounded bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-400">{adminError}</div>
            )}

            {adminStatus && (
              <div className="rounded bg-green-50 dark:bg-green-900/30 p-3 text-sm text-green-700 dark:text-green-400">
                Admin Service 已連線，共 {adminStatus.total} 位使用者
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
