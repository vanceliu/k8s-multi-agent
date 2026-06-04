import { useState, useEffect, useRef } from 'react';

const LIFF_ID = '2010184377-zdxRpWV3';
const LIFF_SDK_URL = 'https://static.line-scdn.net/liff/edge/2/sdk.js';
const GATEWAY_BASE_URL = import.meta.env.VITE_GATEWAY_URL || 'https://openclaw-line.darfonenergy-platform.com';

function loadLiffSdk() {
  return new Promise((resolve, reject) => {
    if (window.liff) {
      resolve(window.liff);
      return;
    }
    const script = document.createElement('script');
    script.src = LIFF_SDK_URL;
    script.onload = () => resolve(window.liff);
    script.onerror = () => reject(new Error('LIFF SDK 載入失敗'));
    document.head.appendChild(script);
  });
}

export default function LiffBindPage() {
  const [status, setStatus] = useState('loading');
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const profileRef = useRef(null);
  const liffRef = useRef(null);

  useEffect(() => {
    let cancelled = false;

    async function init() {
      try {
        const liff = await loadLiffSdk();
        liffRef.current = liff;
        await liff.init({ liffId: LIFF_ID });

        if (!liff.isLoggedIn()) {
          liff.login({ redirectUri: window.location.href });
          return;
        }

        const profile = await liff.getProfile();
        profileRef.current = profile;

        if (!cancelled) setStatus('ready');
      } catch (e) {
        if (!cancelled) {
          setStatus('error');
          setError('LIFF 初始化失敗：' + e.message);
        }
      }
    }

    init();
    return () => { cancelled = true; };
  }, []);

  const handleSubmit = async (e) => {
    e.preventDefault();

    if (!username.trim() || !password.trim()) {
      setError('請填寫帳號和密碼');
      return;
    }

    if (!profileRef.current) {
      setError('無法取得 LINE 資料，請重新開啟');
      return;
    }

    setSubmitting(true);
    setError('');

    try {
      const resp = await fetch(`${GATEWAY_BASE_URL}/api/v1/liff/bindaccount`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: username.trim(),
          password: password.trim(),
          line_user_id: profileRef.current.userId,
          line_display_name: profileRef.current.displayName,
        }),
      });

      const data = await resp.json();

      if (resp.ok && data.success) {
        setSuccess(`綁定成功！已連結帳號 ${data.username}`);
        setStatus('success');
        setTimeout(() => {
          if (liffRef.current) liffRef.current.closeWindow();
        }, 2000);
      } else {
        setError(data.error || '綁定失敗，請確認帳號密碼');
      }
    } catch {
      setError('網路錯誤，請稍後再試');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-100 flex items-start justify-center pt-8 px-4">
      <div className="w-full max-w-sm bg-white rounded-xl shadow-sm p-6">
        <h2 className="text-lg font-bold text-center text-gray-900 mb-6">帳號綁定</h2>

        {status === 'loading' && (
          <div className="flex flex-col items-center gap-3 py-8">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-green-500 border-t-transparent" />
            <p className="text-sm text-gray-500">初始化中...</p>
          </div>
        )}

        {status === 'error' && !success && (
          <p className="text-center text-sm text-red-600 py-4">{error}</p>
        )}

        {status === 'ready' && (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm text-gray-600 mb-1">帳號（User ID）</label>
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="請輸入帳號"
                className="w-full px-3 py-3 border border-gray-300 rounded-lg text-base focus:outline-none focus:ring-2 focus:ring-green-500 focus:border-transparent"
                autoComplete="username"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-600 mb-1">密碼</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="請輸入密碼"
                className="w-full px-3 py-3 border border-gray-300 rounded-lg text-base focus:outline-none focus:ring-2 focus:ring-green-500 focus:border-transparent"
                autoComplete="current-password"
              />
            </div>

            {error && (
              <p className="text-sm text-red-600">{error}</p>
            )}

            <button
              type="submit"
              disabled={submitting}
              className="w-full py-3 bg-[#06C755] text-white font-medium rounded-lg text-base hover:bg-[#05b34c] disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
            >
              {submitting ? '綁定中...' : '確認綁定'}
            </button>
          </form>
        )}

        {status === 'success' && (
          <div className="text-center py-6">
            <div className="inline-flex items-center justify-center h-12 w-12 rounded-full bg-green-100 mb-3">
              <svg className="h-6 w-6 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
              </svg>
            </div>
            <p className="text-sm text-green-600 font-medium">{success}</p>
            <p className="text-xs text-gray-400 mt-2">頁面將自動關閉...</p>
          </div>
        )}
      </div>
    </div>
  );
}
