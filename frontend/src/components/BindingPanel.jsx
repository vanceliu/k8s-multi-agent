import { useState, useEffect, useRef, useCallback } from 'react';
import { useApp } from '../context/AppContext';

const PLATFORMS = [
  { key: 'line', label: 'LINE', icon: '💬', color: 'bg-green-500' },
  { key: 'slack', label: 'Slack', icon: '📨', color: 'bg-purple-600' },
  { key: 'teams', label: 'Teams', icon: '👥', color: 'bg-blue-600' },
];

function PlatformCard({ platform, binding, onBind, onUnbind }) {
  const [verifying, setVerifying] = useState(false);
  const [code, setCode] = useState('');
  const [expiresAt, setExpiresAt] = useState(null);
  const [countdown, setCountdown] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const timerRef = useRef(null);
  const pollRef = useRef(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  useEffect(() => {
    if (!expiresAt) return;
    timerRef.current = setInterval(() => {
      const remaining = Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
      setCountdown(remaining);
      if (remaining <= 0) {
        clearInterval(timerRef.current);
        if (pollRef.current) clearInterval(pollRef.current);
        setVerifying(false);
        setCode('');
        setError('驗證碼已過期，請重新綁定');
      }
    }, 1000);
    return () => clearInterval(timerRef.current);
  }, [expiresAt]);

  const handleBind = async () => {
    setLoading(true);
    setError('');
    try {
      const data = await onBind(platform.key);
      setCode(data.code);
      setExpiresAt(Date.now() + data.expires_in * 1000);
      setCountdown(data.expires_in);
      setVerifying(true);

      pollRef.current = setInterval(async () => {
        const found = await onBind.__poll(platform.key);
        if (found) {
          clearInterval(pollRef.current);
          setVerifying(false);
          setCode('');
        }
      }, 3000);
    } catch (e) {
      if (e.status === 409) {
        setError('此平台已有綁定帳號，請先解綁再重新綁定');
      } else {
        setError(e.message || '綁定失敗');
      }
    } finally {
      setLoading(false);
    }
  };

  const handleUnbind = async () => {
    if (!confirm(`確定要解除 ${platform.label} 綁定嗎？`)) return;
    setLoading(true);
    setError('');
    try {
      await onUnbind(platform.key);
    } catch (e) {
      setError(e.message || '解綁失敗');
    } finally {
      setLoading(false);
    }
  };

  const formatCountdown = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 p-4">
      <div className="flex items-center gap-3 mb-3">
        <span className={`inline-flex h-8 w-8 items-center justify-center rounded-lg text-white text-sm ${platform.color}`}>
          {platform.icon}
        </span>
        <span className="font-medium text-gray-900 dark:text-gray-100">{platform.label}</span>
        {binding && binding.status === 'active' && (
          <span className="ml-auto inline-flex items-center gap-1 text-xs text-green-600 dark:text-green-400">
            <span className="h-1.5 w-1.5 rounded-full bg-green-500" />
            已綁定
          </span>
        )}
        {binding && binding.status === 'inactive' && (
          <span className="ml-auto inline-flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
            <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
            連線中斷
          </span>
        )}
      </div>

      {error && (
        <p className="mb-3 text-xs text-red-600 dark:text-red-400">{error}</p>
      )}

      {!binding && !verifying && (
        <button
          onClick={handleBind}
          disabled={loading}
          className="w-full rounded-md bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {loading ? '處理中...' : `綁定 ${platform.label}`}
        </button>
      )}

      {verifying && (
        <div className="space-y-2">
          <div className="rounded-md bg-gray-50 dark:bg-gray-900 p-3 text-center">
            <p className="text-xs text-gray-500 dark:text-gray-400 mb-1">驗證碼</p>
            <p className="text-2xl font-mono font-bold tracking-widest text-gray-900 dark:text-gray-100">{code}</p>
          </div>
          <p className="text-xs text-gray-500 dark:text-gray-400 text-center">
            請在 {platform.label} 官方帳號中輸入此驗證碼
          </p>
          <p className="text-xs text-center text-amber-600 dark:text-amber-400">
            ⏱ {formatCountdown(countdown)}
          </p>
        </div>
      )}

      {binding && (
        <div className="space-y-3">
          <div className="text-sm text-gray-700 dark:text-gray-300">
            <p>{binding.display_name || binding.platform_uid}</p>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
              綁定時間：{new Date(binding.bound_at).toLocaleDateString('zh-TW')}
            </p>
          </div>
          {binding.status === 'inactive' && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              連線已中斷（使用者封鎖或刪除官方帳號），通知將改為上線時推送
            </p>
          )}
          <button
            onClick={handleUnbind}
            disabled={loading}
            className="w-full rounded-md border border-red-300 dark:border-red-700 px-3 py-2 text-sm font-medium text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50 transition-colors"
          >
            {loading ? '處理中...' : '解除綁定'}
          </button>
        </div>
      )}
    </div>
  );
}

export default function BindingPanel() {
  const { api } = useApp();
  const [bindings, setBindings] = useState([]);
  const [loading, setLoading] = useState(true);

  const loadBindings = useCallback(async () => {
    try {
      const data = await api.getBindings();
      setBindings(data.bindings || []);
    } catch {
      setBindings([]);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    loadBindings();
  }, [loadBindings]);

  const handleBind = async (platform) => {
    const data = await api.initBinding(platform);
    return data;
  };

  handleBind.__poll = async (platform) => {
    const data = await api.getBindings();
    const found = (data.bindings || []).find(
      (b) => b.platform === platform && b.status === 'active'
    );
    if (found) {
      setBindings(data.bindings);
      return true;
    }
    return false;
  };

  const handleUnbind = async (platform) => {
    await api.unbind(platform);
    setBindings((prev) => prev.filter((b) => b.platform !== platform));
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-blue-600 border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="space-y-4 overflow-y-auto">
      <div>
        <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">帳號綁定</h2>
        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
          綁定外部 IM 平台後，可直接在該平台與 Agent 對話並接收通知
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {PLATFORMS.map((platform) => (
          <PlatformCard
            key={platform.key}
            platform={platform}
            binding={bindings.find((b) => b.platform === platform.key)}
            onBind={handleBind}
            onUnbind={handleUnbind}
          />
        ))}
      </div>
    </div>
  );
}
