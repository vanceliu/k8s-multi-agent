const DEFAULT_BASE_URL = 'http://localhost:8000';

/**
 * Custom API error that carries HTTP status code.
 * Use `error.status` to detect specific HTTP errors (e.g. 502).
 */
export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/**
 * Helper: check if an error is a 502 (Bad Gateway / Pod unreachable).
 */
export function is502Error(error) {
  return error instanceof ApiError && error.status === 502;
}

export function createApiClient({ baseUrl = DEFAULT_BASE_URL, token } = {}) {
  const headers = () => ({
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`,
  });

  const headersWithSession = (sessionId) => ({
    ...headers(),
    ...(sessionId ? { 'X-Session-Id': sessionId } : {}),
  });

  return {
    // 1. Health check (GET /health)
    async health() {
      const res = await fetch(`${baseUrl}/health`);
      if (!res.ok) throw new ApiError(`Health check failed: ${res.status}`, res.status);
      return res.json();
    },

    // 2. Ensure workspace (POST /api/v1/workspaces/ensure)
    async ensureWorkspace({ sessionId, resourceTier = 'standard' } = {}) {
      const res = await fetch(`${baseUrl}/api/v1/workspaces/ensure`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({
          session_id: sessionId || undefined,
          resource_tier: resourceTier,
        }),
      });
      if (!res.ok) throw new ApiError(`Ensure workspace failed: ${res.status}`, res.status);
      return res.json();
    },

    // 3. Get workspace status (GET /api/v1/workspaces/{workspace_id})
    async getWorkspace(workspaceId) {
      const res = await fetch(`${baseUrl}/api/v1/workspaces/${workspaceId}`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Get workspace failed: ${res.status}`, res.status);
      return res.json();
    },

    // 4. Chat via Channel (POST /api/v1/chat) — 推薦
    async channelChat({ message, sessionId }) {
      const res = await fetch(`${baseUrl}/api/v1/chat`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({ message, session_id: sessionId }),
      });
      if (!res.ok) throw new ApiError(`Channel chat failed: ${res.status}`, res.status);
      return res.json();
    },

    // 9. Chat sync — direct proxy (POST /workspaces/{wid}/api/v1/chat)
    async chat(workspaceId, { message, sessionId }) {
      const res = await fetch(`${baseUrl}/workspaces/${workspaceId}/api/v1/chat`, {
        method: 'POST',
        headers: headersWithSession(sessionId),
        body: JSON.stringify({ message, session_id: sessionId }),
      });
      if (!res.ok) throw new ApiError(`Chat failed: ${res.status}`, res.status);
      return res.json();
    },

    // 10. Chat stream SSE — direct proxy (POST /workspaces/{wid}/api/v1/chat/stream)
    //
    // Supports SSE event types (方案 A):
    //   event: content    → AI text chunk (default if no event: field)
    //   event: tool_call  → data is JSON: {"name":"...", "args":{...}}
    //   event: tool_result → data is JSON: {"tool_name":"...", "content":"..."}
    //   data: [DONE]      → stream end
    //
    // Callbacks:
    //   onChunk(text)           — content text chunk
    //   onToolCall(data)        — tool call JSON object
    //   onToolResult(data)      — tool result JSON object
    //   onDone()                — stream finished
    //   onThinking()            — SSE comment ": thinking" received (AI is thinking)
    //   onFile(data)             — file event: {"path":"...","type":"image|document","name":"..."}
    //
    // Backward compatible: if no event: field, treats data as content text.
    async chatStream(workspaceId, { message, sessionId }, onChunk, onDone, { onToolCall, onToolResult, onThinking, onFile } = {}) {
      const res = await fetch(`${baseUrl}/workspaces/${workspaceId}/api/v1/chat/stream`, {
        method: 'POST',
        headers: headersWithSession(sessionId),
        body: JSON.stringify({ message, session_id: sessionId }),
      });
      if (!res.ok) throw new ApiError(`Chat stream failed: ${res.status}`, res.status);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      // Parse SSE: collect event type + data lines, dispatch on double newline
      let currentEvent = null; // current "event:" value
      let currentData = '';    // accumulated "data:" lines

      const dispatchEvent = () => {
        const data = currentData;
        const eventType = currentEvent || 'content';
        currentEvent = null;
        currentData = '';

        if (!data && eventType === 'content') return;

        if (data === '[DONE]') {
          onDone?.();
          return 'done';
        }

        if (eventType === 'tool_call') {
          try { onToolCall?.(JSON.parse(data)); } catch { onToolCall?.({ raw: data }); }
        } else if (eventType === 'tool_result') {
          try { onToolResult?.(JSON.parse(data)); } catch { onToolResult?.({ raw: data }); }
        } else if (eventType === 'file') {
          try { onFile?.(JSON.parse(data)); } catch { onFile?.({ raw: data }); }
        } else {
          // content event — data may be JSON {"content": "..."} or plain text
          let text = data;
          try {
            const parsed = JSON.parse(data);
            if (parsed && typeof parsed.content === 'string') {
              text = parsed.content;
            }
          } catch {
            // not JSON, use as-is
          }
          if (text) onChunk?.(text);
        }
        return null;
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (line === '') {
            // Empty line = end of SSE event block
            if (currentData || currentEvent) {
              if (dispatchEvent() === 'done') return;
            }
          } else if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim();
          } else if (line.startsWith('event:')) {
            currentEvent = line.slice(6).trim();
          } else if (line.startsWith('data: ')) {
            currentData += (currentData ? '\n' : '') + line.slice(6);
          } else if (line.startsWith('data:')) {
            currentData += (currentData ? '\n' : '') + line.slice(5);
          } else if (line.startsWith(':')) {
            // SSE comment (e.g. ": thinking") — ignore per spec, notify if thinking
            if (line.trim() === ': thinking') onThinking?.();
          } else {
            // Raw line without prefix (non-standard backend format) — treat as content
            currentData += (currentData ? '\n' : '') + line;
          }
        }
      }

      // Flush remaining
      if (currentData || currentEvent) {
        if (dispatchEvent() === 'done') return;
      }
      if (buffer.trim()) {
        currentData = buffer;
        if (dispatchEvent() === 'done') return;
      }

      onDone?.();
    },

    // 11. MCP execute (POST /workspaces/{wid}/mcp/execute)
    async mcpExecute(workspaceId, { method, params, id = '1' }) {
      const res = await fetch(`${baseUrl}/workspaces/${workspaceId}/mcp/execute`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({
          jsonrpc: '2.0',
          method,
          params,
          id,
        }),
      });
      if (!res.ok) throw new ApiError(`MCP execute failed: ${res.status}`, res.status);
      return res.json();
    },

    // 12. Upload file (POST /workspaces/{wid}/api/v1/files/upload?path=...)
    async uploadFile(workspaceId, file, { path = '' } = {}) {
      const formData = new FormData();
      formData.append('file', file);
      const query = path ? `?path=${encodeURIComponent(path)}` : '';
      const res = await fetch(`${baseUrl}/workspaces/${workspaceId}/api/v1/files/upload${query}`, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token}` },
        body: formData,
      });
      if (!res.ok) throw new ApiError(`Upload file failed: ${res.status}`, res.status);
      return res.json();
    },

    // 13. Download file (GET /workspaces/{wid}/api/v1/files/download?path=...)
    async downloadFile(workspaceId, filePath) {
      const res = await fetch(
        `${baseUrl}/workspaces/${workspaceId}/api/v1/files/download?path=${encodeURIComponent(filePath)}`,
        { headers: { 'Authorization': `Bearer ${token}` } },
      );
      if (!res.ok) throw new ApiError(`Download file failed: ${res.status}`, res.status);
      return res.blob();
    },

    // 14. List files (GET /workspaces/{wid}/api/v1/files/list?path=...)
    async listFiles(workspaceId, { path = '' } = {}) {
      const query = path ? `?path=${encodeURIComponent(path)}` : '';
      const res = await fetch(`${baseUrl}/workspaces/${workspaceId}/api/v1/files/list${query}`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`List files failed: ${res.status}`, res.status);
      return res.json();
    },

    // 15. Delete file or directory (DELETE /workspaces/{wid}/api/v1/files/delete?path=...)
    async deleteFile(workspaceId, filePath) {
      const url = `${baseUrl}/workspaces/${workspaceId}/api/v1/files/delete?path=${encodeURIComponent(filePath)}`;
      // Try DELETE first, fallback to POST if backend returns 405
      let res = await fetch(url, {
        method: 'DELETE',
        headers: { 'Authorization': `Bearer ${token}` },
      });
      if (res.status === 405) {
        res = await fetch(url, {
          method: 'POST',
          headers: headers(),
          body: JSON.stringify({ path: filePath }),
        });
      }
      if (!res.ok) throw new ApiError(`Delete file failed: ${res.status}`, res.status);
      return res.json();
    },

    // 16. Create directory (POST /workspaces/{wid}/api/v1/files/mkdir?path=...)
    async mkdir(workspaceId, dirPath) {
      const res = await fetch(
        `${baseUrl}/workspaces/${workspaceId}/api/v1/files/mkdir?path=${encodeURIComponent(dirPath)}`,
        {
          method: 'POST',
          headers: { 'Authorization': `Bearer ${token}` },
        },
      );
      if (!res.ok) throw new ApiError(`Create directory failed: ${res.status}`, res.status);
      return res.json();
    },

    // 17. Admin reap (POST /api/v1/admin/reap)
    async adminReap() {
      const res = await fetch(`${baseUrl}/api/v1/admin/reap`, {
        method: 'POST',
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Admin reap failed: ${res.status}`, res.status);
      return res.json();
    },

    // 5. List sessions (GET /api/v1/sessions)
    async listSessions() {
      const res = await fetch(`${baseUrl}/api/v1/sessions`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`List sessions failed: ${res.status}`, res.status);
      return res.json();
    },

    // 6. Get session messages (GET /api/v1/sessions/{session_id}/messages)
    async getSessionMessages(sessionId) {
      const res = await fetch(`${baseUrl}/api/v1/sessions/${sessionId}/messages`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Get session messages failed: ${res.status}`, res.status);
      return res.json();
    },

    // 7. Get session history (GET /api/v1/sessions/{session_id}/history)
    async getSessionHistory(sessionId, { limit = 50 } = {}) {
      const res = await fetch(`${baseUrl}/api/v1/sessions/${sessionId}/history?limit=${limit}`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Get session history failed: ${res.status}`, res.status);
      return res.json();
    },

    // 8. Delete session (DELETE /api/v1/sessions/{session_id})
    async deleteSession(sessionId) {
      const res = await fetch(`${baseUrl}/api/v1/sessions/${sessionId}`, {
        method: 'DELETE',
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Delete session failed: ${res.status}`, res.status);
      return res.json();
    },

    // 18. Admin channels (GET /api/v1/admin/channels)
    async adminChannels() {
      const res = await fetch(`${baseUrl}/api/v1/admin/channels`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Admin channels failed: ${res.status}`, res.status);
      return res.json();
    },

    // 38. Init binding (POST /api/v1/users/bindings/init)
    async initBinding(platform) {
      const res = await fetch(`${baseUrl}/api/v1/users/bindings/init`, {
        method: 'POST',
        headers: headers(),
        body: JSON.stringify({ platform }),
      });
      if (!res.ok) throw new ApiError(`Init binding failed: ${res.status}`, res.status);
      return res.json();
    },

    // 39. List bindings (GET /api/v1/users/bindings)
    async getBindings() {
      const res = await fetch(`${baseUrl}/api/v1/users/bindings`, {
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Get bindings failed: ${res.status}`, res.status);
      return res.json();
    },

    // 40. Unbind (DELETE /api/v1/users/bindings/{platform})
    async unbind(platform) {
      const res = await fetch(`${baseUrl}/api/v1/users/bindings/${platform}`, {
        method: 'DELETE',
        headers: headers(),
      });
      if (!res.ok) throw new ApiError(`Unbind failed: ${res.status}`, res.status);
      return res.json();
    },

    // 43. LIFF bind account (POST /api/v1/liff/bindaccount)
    async liffBindAccount({ username, password, lineUserId, lineDisplayName }) {
      const res = await fetch(`${baseUrl}/api/v1/liff/bindaccount`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username,
          password,
          line_user_id: lineUserId,
          line_display_name: lineDisplayName,
        }),
      });
      if (!res.ok) throw new ApiError(`LIFF bind failed: ${res.status}`, res.status);
      return res.json();
    },
  };
}
