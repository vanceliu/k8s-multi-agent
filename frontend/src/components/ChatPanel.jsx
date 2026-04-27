import { useState, useRef, useEffect, useCallback, useId } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useApp } from '../context/AppContext';

/** Renders a mermaid diagram from source code (lazy-loads mermaid) */
function MermaidDiagram({ chart }) {
  const containerRef = useRef(null);
  const id = `mermaid-${useId().replace(/:/g, '')}`;
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!containerRef.current || !chart.trim()) return;
    let cancelled = false;
    (async () => {
      try {
        const { default: mermaid } = await import('mermaid');
        mermaid.initialize({ startOnLoad: false, theme: 'default', securityLevel: 'loose' });
        const { svg } = await mermaid.render(id, chart.trim());
        if (!cancelled && containerRef.current) {
          containerRef.current.innerHTML = svg;
        }
      } catch (e) {
        if (!cancelled) setError(e.message || 'Mermaid render failed');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [chart, id]);

  if (error) {
    return (
      <div className="rounded border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/30 p-2 text-xs text-red-600 dark:text-red-400">
        <div className="font-medium">Mermaid 渲染失敗</div>
        <pre className="mt-1 whitespace-pre-wrap">{error}</pre>
      </div>
    );
  }

  return (
    <div>
      {loading && <div className="text-xs text-gray-400 animate-pulse">載入圖表中...</div>}
      <div ref={containerRef} className="my-2 overflow-auto" />
    </div>
  );
}

/** Authenticated image — fetches with Bearer token, renders as blob URL */
function AuthImage({ src, alt, token, className }) {
  const [blobUrl, setBlobUrl] = useState(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let revoke;
    (async () => {
      try {
        const res = await fetch(src, { headers: { 'Authorization': `Bearer ${token}` } });
        if (!res.ok) throw new Error(res.status);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        revoke = url;
        setBlobUrl(url);
      } catch {
        setError(true);
      }
    })();
    return () => { if (revoke) URL.revokeObjectURL(revoke); };
  }, [src, token]);

  if (error) return <div className="text-xs text-gray-400">圖片載入失敗</div>;
  if (!blobUrl) return <div className="text-xs text-gray-400 animate-pulse">載入圖片中...</div>;
  return <img src={blobUrl} alt={alt} className={className} />;
}

/** Authenticated download — fetches with Bearer token, triggers browser download */
function AuthDownloadLink({ url, fileName, token, className, children }) {
  const handleClick = async (e) => {
    e.preventDefault();
    try {
      const res = await fetch(url, { headers: { 'Authorization': `Bearer ${token}` } });
      if (!res.ok) throw new Error(res.status);
      const blob = await res.blob();
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = fileName;
      a.click();
      URL.revokeObjectURL(blobUrl);
    } catch {
      alert(`下載失敗: ${fileName}`);
    }
  };

  return (
    <a href="#" onClick={handleClick} className={className}>
      {children}
    </a>
  );
}

/** Collapsible system message — shows 2 lines by default, expandable */
function CollapsibleSystemMsg({ content }) {
  const [expanded, setExpanded] = useState(false);
  const [needsCollapse, setNeedsCollapse] = useState(false);
  const textRef = useRef(null);

  useEffect(() => {
    const el = textRef.current;
    if (el) {
      // Check if content exceeds 2 lines (~2.5em at text-xs line-height)
      setNeedsCollapse(el.scrollHeight > 40);
    }
  }, [content]);

  return (
    <div className="max-w-[80%]">
      <div
        ref={textRef}
        className={`whitespace-pre-wrap break-all ${!expanded && needsCollapse ? 'line-clamp-2' : ''}`}
      >
        {content}
      </div>
      {needsCollapse && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="mt-1 text-yellow-600 hover:text-yellow-800 cursor-pointer text-[10px] transition-colors duration-150"
        >
          {expanded ? '收合' : '展開全部'}
        </button>
      )}
    </div>
  );
}

/**
 * Chat modes:
 * - channel:  POST /api/v1/chat (推薦，自動 ensure workspace)
 * - sync:     POST /workspaces/{wid}/api/v1/chat (需要 workspace)
 * - stream:   POST /workspaces/{wid}/api/v1/chat/stream (需要 workspace，SSE)
 */
const CHAT_MODES = [
  { key: 'stream', label: '串流 SSE（推薦）', needsWorkspace: true },
  { key: 'channel', label: 'Channel', needsWorkspace: false },
  { key: 'sync', label: '同步（Direct Proxy）', needsWorkspace: true },
];

/** Convert API messages (human/ai/tool) to chat UI messages */
function apiMessagesToChat(apiMessages, { baseUrl, workspaceId } = {}) {
  if (!apiMessages || !apiMessages.length) return [];
  const result = [];
  for (const msg of apiMessages) {
    if (msg.role === 'human') {
      result.push({ role: 'user', content: msg.content });
    } else if (msg.role === 'ai') {
      if (msg.content) {
        result.push({ role: 'assistant', content: msg.content });
      }
      if (msg.tool_calls && msg.tool_calls.length > 0) {
        result.push({
          role: 'system',
          content: `🔧 使用工具: ${msg.tool_calls.map((t) => `${t.name}(${JSON.stringify(t.args)})`).join('\n')}`,
        });
      }
    } else if (msg.role === 'tool') {
      result.push({
        role: 'system',
        content: `📎 ${msg.tool_name}: ${msg.content}`,
      });
      // Render files attached to tool results (images inline, documents as links)
      if (msg.files && msg.files.length > 0 && baseUrl && workspaceId) {
        for (const f of msg.files) {
          const downloadUrl = `${baseUrl}/workspaces/${workspaceId}/api/v1/files/download?path=${encodeURIComponent(f.path)}`;
          result.push({
            role: 'file',
            fileType: f.type,
            fileName: f.name,
            filePath: f.path,
            downloadUrl,
          });
        }
      }
    }
  }
  return result;
}

export default function ChatPanel() {
  const { api, baseUrl, token, workspaceId, setWorkspaceId, workspaceIdRef, sessionId, setSessionId, withAutoRecover, recoverWorkspace, recovering, workspaceReady, setWorkspaceReady } = useApp();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [chatMode, setChatMode] = useState('stream');
  const messagesEndRef = useRef(null);
  const isComposingRef = useRef(false);

  // Session sidebar
  const [sessions, setSessions] = useState(null);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const currentMode = CHAT_MODES.find((m) => m.key === chatMode);
  const needsWorkspace = currentMode?.needsWorkspace ?? false;

  // Load session list
  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const data = await api.listSessions();
      setSessions(data);
    } catch {
      // silently fail — sidebar is optional
    } finally {
      setSessionsLoading(false);
    }
  }, [api]);

  // Auto-load sessions on mount
  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  // Switch to a session and load its history
  const switchSession = async (sid) => {
    if (sid === sessionId && messages.length > 0) return; // already active
    setSessionId(sid);
    setMessages([]);
    setHistoryLoading(true);
    try {
      const data = await withAutoRecover(() => api.getSessionMessages(sid));
      const chatMsgs = apiMessagesToChat(data.messages, { baseUrl, workspaceId: workspaceIdRef.current });
      setMessages(chatMsgs);
    } catch (e) {
      if (e.status !== 404) {
        setMessages([{ role: 'system', content: `載入對話歷史失敗: ${e.message}` }]);
      }
      // 404 = new session, no history — that's fine
    } finally {
      setHistoryLoading(false);
    }
  };

  // Create new session
  const createNewSession = () => {
    const newId = `sess-${crypto.randomUUID().slice(0, 8)}`;
    setSessionId(newId);
    setMessages([]);
    // Add the new session to the sidebar immediately (optimistic)
    setSessions((prev) => {
      const existing = prev?.sessions || [];
      return {
        ...prev,
        sessions: [
          { session_id: newId, pod_status: 'running', created_at: new Date().toISOString(), last_active_at: new Date().toISOString(), terminated_at: null },
          ...existing,
        ],
        total: (prev?.total || 0) + 1,
      };
    });
  };

  // Delete session
  const deleteSession = async (sid, e) => {
    e.stopPropagation(); // prevent switching to this session
    if (!confirm(`確定要刪除 Session「${sid}」嗎？\n此操作不可逆，對話歷史將永久刪除。`)) return;
    try {
      await api.deleteSession(sid);
      // If we deleted the active session, create a new one
      if (sid === sessionId) {
        createNewSession();
      }
      loadSessions();
    } catch (err) {
      alert(`刪除失敗: ${err.message}`);
    }
  };

  // Format time for sidebar
  const formatTime = (ts) => {
    if (!ts) return '';
    const d = new Date(ts);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) {
      return d.toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit' });
    }
    return d.toLocaleDateString('zh-TW', { month: 'short', day: 'numeric' });
  };

  /**
   * Core send logic.
   * All API calls are wrapped with withAutoRecover — on 502, it auto-calls
   * ensureWorkspace then retries once.
   * For stream/sync modes, auto-ensures workspace if not yet created.
   */
  /**
   * Ensure workspace is available, return workspace_id.
   * Wraps recoverWorkspace with user-friendly error messages.
   */
  const ensureWorkspaceForChat = async () => {
    try {
      const recovered = await recoverWorkspace();
      return recovered.workspace_id;
    } catch (e) {
      if (e.message?.includes('Failed to fetch') || e.name === 'TypeError') {
        throw new Error('無法連線到 Gateway，請確認連線設定是否正確');
      }
      if (e.status === 503) {
        throw new Error('Gateway 服務尚未就緒 (503)，請稍後再試');
      }
      if (e.message?.includes('逾時')) {
        throw new Error('工作區啟動逾時，Pod 可能正在初始化，請稍後再試');
      }
      throw new Error(`工作區建立失敗: ${e.message}`);
    }
  };

  const doSend = async (messageText) => {
    if (!messageText.trim() || loading) return;

    const userMsg = { role: 'user', content: messageText };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');

    setLoading(true);

    try {
      // Auto-ensure workspace for direct proxy modes (stream/sync)
      if (needsWorkspace && !workspaceIdRef.current) {
        await ensureWorkspaceForChat();
      }

      if (chatMode === 'channel') {
        const data = await withAutoRecover(() =>
          api.channelChat({ message: messageText, sessionId })
        );
        setMessages((prev) => [...prev, { role: 'assistant', content: data.content }]);

        if (data.tool_calls && data.tool_calls.length > 0) {
          setMessages((prev) => [
            ...prev,
            {
              role: 'system',
              content: `🔧 使用工具: ${data.tool_calls.map((t) => t.name || t).join(', ')}`,
            },
          ]);
        }
      } else if (chatMode === 'stream') {
        let accumulated = '';
        let inToolPhase = false;
        // Each stream round gets a unique _streamId so content updates never touch previous assistant messages
        let currentStreamId = `s-${Date.now()}`;

        await withAutoRecover(() =>
          api.chatStream(
            workspaceIdRef.current,
            { message: messageText, sessionId },
            (chunk) => {
              setThinking(false);
              if (inToolPhase) {
                // Transitioning from tool phase back to content — start new assistant block
                inToolPhase = false;
                accumulated = '';
                currentStreamId = `s-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
              }
              accumulated += chunk;
              const text = accumulated;
              const sid = currentStreamId;
              setMessages((prev) => {
                // Find existing assistant message with this stream ID
                const idx = prev.findIndex((m) => m.role === 'assistant' && m._streamId === sid);
                if (idx >= 0) {
                  const updated = [...prev];
                  updated[idx] = { ...updated[idx], content: text };
                  return updated;
                }
                // First chunk for this stream round — append new assistant message
                return [...prev, { role: 'assistant', content: text, _streamId: sid }];
              });
            },
            () => {},
            {
              onToolCall: (tc) => {
                inToolPhase = true;
                const callContent = `🔧 ${tc.name || tc.raw || 'unknown'}${tc.args ? `(${JSON.stringify(tc.args)})` : ''}`;
                setMessages((prev) => [...prev, { role: 'system', type: 'tool_call', content: callContent }]);
              },
              onToolResult: (tr) => {
                const resultContent = `📎 ${tr.tool_name || 'tool'}: ${tr.content || tr.raw || ''}`;
                setMessages((prev) => [...prev, { role: 'system', type: 'tool_result', content: resultContent }]);
              },
              onThinking: () => { setThinking(true); },
              onFile: (fileData) => {
                const downloadUrl = `${baseUrl}/workspaces/${workspaceIdRef.current}/api/v1/files/download?path=${encodeURIComponent(fileData.path)}`;
                setMessages((prev) => [...prev, {
                  role: 'file',
                  fileType: fileData.type,
                  fileName: fileData.name,
                  filePath: fileData.path,
                  downloadUrl,
                }]);
              },
            }
          )
        );

        // Stream ended — clear thinking
        setThinking(false);

        // Stream 結束，刷新 session 列表
      } else {
        const data = await withAutoRecover(() =>
          api.chat(workspaceIdRef.current, {
            message: messageText,
            sessionId,
          })
        );
        setMessages((prev) => [...prev, { role: 'assistant', content: data.content }]);
      }

      // Refresh session list after sending
      loadSessions();
    } catch (e) {
      // Classify errors with user-friendly messages
      let errorMsg;
      if (e.message?.includes('Failed to fetch') || e.name === 'TypeError') {
        errorMsg = '無法連線到 Gateway，請確認連線設定是否正確';
      } else if (e.status === 502) {
        errorMsg = '無法連線到 Agent Pod (502)，工作區可能尚未就緒，請稍後再試';
      } else if (e.status === 503) {
        errorMsg = 'Channel service 未初始化或 Web channel 不可用 (503)';
      } else if (e.status === 408) {
        errorMsg = '請求超時 (408)，請稍後再試';
      } else {
        errorMsg = e.message;
      }
      setMessages((prev) => [...prev, { role: 'error', content: errorMsg }]);
    } finally {
      setLoading(false);
    }
  };

  const sendMessage = () => doSend(input);

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && !isComposingRef.current) {
      e.preventDefault();
      sendMessage();
    }
  };

  const handleCompositionStart = () => {
    isComposingRef.current = true;
  };

  const handleCompositionEnd = () => {
    isComposingRef.current = false;
  };

  const canSend = true; // auto-ensure handles workspace creation

  return (
    <div className="flex h-full min-h-0 gap-0">
      {/* Session Sidebar — hidden on mobile, collapsible on desktop */}
      <div className={`hidden flex-col border-r border-gray-200 dark:border-gray-700 transition-all sm:flex ${sidebarCollapsed ? 'w-10' : 'w-44 lg:w-56'}`}>
        {/* Sidebar header */}
        <div className="flex items-center justify-between border-b border-gray-100 dark:border-gray-700 px-2 py-2">
          {!sidebarCollapsed && (
            <span className="text-xs font-medium text-gray-600 dark:text-gray-400">Sessions</span>
          )}
          <button
            onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
            className="rounded p-1 text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 hover:text-gray-600 dark:hover:text-gray-300"
            title={sidebarCollapsed ? '展開' : '收合'}
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              {sidebarCollapsed ? (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 5l7 7-7 7M5 5l7 7-7 7" />
              ) : (
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 19l-7-7 7-7m8 14l-7-7 7-7" />
              )}
            </svg>
          </button>
        </div>

        {!sidebarCollapsed && (
          <>
            {/* New session button */}
            <div className="border-b border-gray-100 dark:border-gray-700 p-2">
              <button
                onClick={createNewSession}
                className="flex w-full items-center gap-1 rounded bg-blue-50 dark:bg-blue-900/30 px-2 py-1.5 text-xs text-blue-600 dark:text-blue-400 hover:bg-blue-100 dark:hover:bg-blue-900/50"
              >
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                </svg>
                新對話
              </button>
            </div>

            {/* Session list */}
            <div className="flex-1 overflow-y-auto">
              {sessionsLoading && (
                <div className="p-2 text-xs text-gray-400 text-center">載入中...</div>
              )}

              {sessions?.sessions?.map((s) => (
                <div
                  key={s.session_id}
                  onClick={() => switchSession(s.session_id)}
                  className={`group flex w-full cursor-pointer flex-col gap-0.5 border-b border-gray-50 dark:border-gray-700 px-3 py-2 text-left transition-colors ${
                    s.session_id === sessionId
                      ? 'bg-blue-50 dark:bg-blue-900/30 border-l-2 border-l-blue-500'
                      : 'hover:bg-gray-50 dark:hover:bg-gray-700'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="truncate text-xs font-mono text-gray-700 dark:text-gray-300">
                      {s.session_id}
                    </span>
                    <div className="flex items-center gap-1">
                      <button
                        onClick={(e) => deleteSession(s.session_id, e)}
                        className="hidden rounded p-0.5 text-gray-300 dark:text-gray-600 hover:bg-red-50 dark:hover:bg-red-900/30 hover:text-red-500 group-hover:block"
                        title="刪除 Session"
                      >
                        <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </button>
                      <span className={`h-1.5 w-1.5 flex-shrink-0 rounded-full ${
                        s.pod_status === 'running' ? 'bg-green-500' :
                        s.pod_status === 'pending' ? 'bg-yellow-500' : 'bg-gray-300'
                      }`} />
                    </div>
                  </div>
                  <span className="text-[10px] text-gray-400 dark:text-gray-500">
                    {formatTime(s.last_active_at)}
                  </span>
                </div>
              ))}

              {sessions && sessions.sessions?.length === 0 && (
                <div className="p-3 text-xs text-gray-400 text-center">尚無 Session</div>
              )}

              {/* Refresh button */}
              <div className="p-2">
                <button
                  onClick={loadSessions}
                  disabled={sessionsLoading}
                  className="w-full rounded border border-gray-200 dark:border-gray-700 px-2 py-1 text-[10px] text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
                >
                  重新整理
                </button>
              </div>
            </div>
          </>
        )}
      </div>

      {/* Chat Area */}
      <div className="flex min-h-0 flex-1 flex-col pl-0 sm:pl-3 lg:pl-4">
        {/* Header */}
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2 sm:mb-3">
          <div className="flex items-center gap-2 sm:gap-3">
            <h2 className="text-base font-semibold sm:text-lg dark:text-gray-100">AI 對話</h2>
            <span className="hidden rounded bg-gray-100 dark:bg-gray-700 px-2 py-0.5 text-xs font-mono text-gray-500 dark:text-gray-400 sm:inline">
              {sessionId}
            </span>
          </div>
          <div className="flex items-center gap-2 sm:gap-3">
            <label className="hidden text-sm text-gray-600 dark:text-gray-400 sm:inline">模式：</label>
            <select
              value={chatMode}
              onChange={(e) => setChatMode(e.target.value)}
              className="rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-2 py-1 text-sm dark:text-gray-200 focus:border-blue-500 focus:outline-none"
            >
              {CHAT_MODES.map((mode) => (
                <option
                  key={mode.key}
                  value={mode.key}
                >
                  {mode.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Channel 模式提示 */}
        {chatMode === 'channel' && (
          <div className="mb-3 rounded border border-blue-100 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/30 px-3 py-2 text-xs text-blue-700 dark:text-blue-300">
            Channel 模式：自動管理工作區，支援 <code>/new</code>（重置對話）、<code>/status</code>（查狀態）、<code>/help</code>（說明）指令
          </div>
        )}

        {/* Messages */}
        <div className="min-h-0 flex-1 overflow-y-auto rounded border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-2 space-y-3 sm:p-4 sm:space-y-4">
          {recovering && (
            <div className="sticky top-0 z-10 flex items-center gap-2 rounded bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 px-4 py-2 text-sm text-yellow-800 dark:text-yellow-200">
              <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              正在恢復工作區，請稍候...
            </div>
          )}

          {historyLoading && (
            <div className="flex items-center justify-center gap-2 py-8 text-sm text-gray-400">
              <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              載入對話歷史...
            </div>
          )}

          {messages.length === 0 && !recovering && !historyLoading && (
            <div className="text-center text-sm text-gray-400 dark:text-gray-500 mt-20">
              輸入訊息開始對話...
            </div>
          )}

          {messages.map((msg, i) => (
            <div
              key={i}
              className={`flex ${
                msg.role === 'user' ? 'justify-end' :
                msg.role === 'system' ? 'justify-center' :
                'justify-start'
              }`}
            >
              {msg.role === 'file' ? (
                <div className="max-w-[80%] rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 px-4 py-2 text-sm dark:text-gray-200">
                  {msg.fileType === 'image' ? (
                    <div className="space-y-1">
                      <AuthImage
                        src={msg.downloadUrl}
                        alt={msg.fileName}
                        token={token}
                        className="max-h-80 rounded border border-gray-200"
                      />
                      <AuthDownloadLink
                        url={msg.downloadUrl}
                        fileName={msg.fileName}
                        token={token}
                        className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
                      >
                        <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                        </svg>
                        {msg.fileName}
                      </AuthDownloadLink>
                    </div>
                  ) : (
                    <AuthDownloadLink
                      url={msg.downloadUrl}
                      fileName={msg.fileName}
                      token={token}
                      className="inline-flex items-center gap-2 text-blue-600 hover:underline"
                    >
                      <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
                      </svg>
                      {msg.fileName}
                    </AuthDownloadLink>
                  )}
                </div>
              ) : (
              <div
                className={`max-w-[80%] rounded-lg px-4 py-2 text-sm ${
                  msg.role === 'user'
                    ? 'bg-blue-600 text-white'
                    : msg.role === 'error'
                    ? 'bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-400 border border-red-200 dark:border-red-800'
                    : msg.role === 'system'
                    ? 'bg-yellow-50 dark:bg-yellow-900/30 text-yellow-800 dark:text-yellow-200 border border-yellow-200 dark:border-yellow-800 text-xs italic'
                    : 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-200'
                }`}
              >
                {msg.role === 'assistant' ? (
                  <div className="prose prose-sm dark:prose-invert max-w-none prose-table:border-collapse prose-th:border prose-th:border-gray-300 dark:prose-th:border-gray-600 prose-th:px-3 prose-th:py-1 prose-th:bg-gray-50 dark:prose-th:bg-gray-700 prose-td:border prose-td:border-gray-300 dark:prose-td:border-gray-600 prose-td:px-3 prose-td:py-1 prose-code:before:content-none prose-code:after:content-none prose-code:bg-gray-200 dark:prose-code:bg-gray-600 prose-code:px-1 prose-code:rounded">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        code({ className, children, ...props }) {
                          const match = /language-mermaid/.test(className || '');
                          if (match) {
                            return <MermaidDiagram chart={String(children).replace(/\n$/, '')} />;
                          }
                          return <code className={className} {...props}>{children}</code>;
                        },
                      }}
                    >{msg.content || '...'}</ReactMarkdown>
                  </div>
                ) : msg.role === 'system' ? (
                  <CollapsibleSystemMsg content={msg.content} />
                ) : (
                  <span className="whitespace-pre-wrap">{msg.content}</span>
                )}
              </div>
              )}
            </div>
          ))}
          {thinking && (
            <div className="flex justify-start">
              <div className="rounded-lg bg-gray-100 dark:bg-gray-700 px-4 py-2 text-sm text-gray-500 dark:text-gray-400 italic flex items-center gap-2">
                <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                思考中...
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="mt-2 flex gap-2 sm:mt-3">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onCompositionStart={handleCompositionStart}
            onCompositionEnd={handleCompositionEnd}
            placeholder={chatMode === 'channel'
              ? '輸入訊息... (支援 /new /status /help 指令)'
              : '輸入訊息... (Enter 送出, Shift+Enter 換行)'}
            rows={2}
            className="flex-1 resize-none rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-2 py-1.5 text-sm dark:text-gray-200 focus:border-blue-500 focus:outline-none sm:px-3 sm:py-2"
          />
          <button
            onClick={sendMessage}
            disabled={loading || recovering || !input.trim() || !canSend}
            className="self-end rounded bg-blue-600 px-4 py-1.5 text-sm text-white hover:bg-blue-700 disabled:opacity-50 sm:px-6 sm:py-2"
          >
            {recovering ? '恢復中...' : loading ? '回應中...' : '送出'}
          </button>
        </div>
      </div>
    </div>
  );
}
