import { createContext, useContext, useState, useMemo, useCallback, useRef } from 'react';
import { createApiClient, is502Error } from '../api/client';
import { createAdminApiClient } from '../api/adminClient';

const AppContext = createContext(null);

/** Max polling attempts when waiting for workspace to become ready */
const MAX_RECOVER_POLL_ATTEMPTS = 15;
/** Polling interval (ms) when waiting for workspace recovery */
const RECOVER_POLL_INTERVAL = 2000;

export function AppProvider({ children }) {
  const [baseUrl, setBaseUrl] = useState('http://localhost:8000');
  const [token, setToken] = useState('REDACTED_USER_TOKEN:testuser1');
  const [workspaceId, setWorkspaceId] = useState('');
  const [sessionId, setSessionId] = useState(() => `sess-${crypto.randomUUID().slice(0, 8)}`);
  const [connected, setConnected] = useState(false);
  const [workspaceReady, setWorkspaceReady] = useState(false);
  const [recovering, setRecovering] = useState(false);

  // Dark mode — persisted in localStorage
  const [darkMode, setDarkMode] = useState(() => {
    const saved = localStorage.getItem('dbt-openclaw-dark-mode');
    if (saved !== null) return saved === 'true';
    return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false;
  });

  const toggleDarkMode = useCallback(() => {
    setDarkMode((prev) => {
      const next = !prev;
      localStorage.setItem('dbt-openclaw-dark-mode', String(next));
      return next;
    });
  }, []);

  // Admin Service state (shares Gateway baseUrl, only needs separate token)
  const [adminToken, setAdminToken] = useState('REDACTED_ADMIN_TOKEN');
  const [adminConnected, setAdminConnected] = useState(false);
  const isAdmin = adminConnected;

  // Prevent concurrent recovery attempts
  const recoverPromiseRef = useRef(null);
  // Keep a ref to workspaceId so closures inside withAutoRecover always see the latest value
  const workspaceIdRef = useRef('');
  const setWorkspaceIdWithRef = useCallback((val) => {
    setWorkspaceId(val);
    workspaceIdRef.current = val;
  }, []);

  const api = useMemo(() => createApiClient({ baseUrl, token }), [baseUrl, token]);
  const adminApi = useMemo(
    () => createAdminApiClient({ baseUrl, token: adminToken }),
    [baseUrl, adminToken],
  );

  const resetConnection = useCallback(() => {
    setConnected(false);
    setWorkspaceReady(false);
    setWorkspaceIdWithRef('');
    setAdminConnected(false);
  }, [setWorkspaceIdWithRef]);

  /**
   * Recover workspace on 502 error.
   * Calls ensureWorkspace → polls until ready → updates state.
   * Returns the recovered workspace data or throws if recovery fails.
   * Deduplicates concurrent calls so only one recovery runs at a time.
   */
  const recoverWorkspace = useCallback(async () => {
    // If a recovery is already in progress, reuse the same promise
    if (recoverPromiseRef.current) {
      return recoverPromiseRef.current;
    }

    const doRecover = async () => {
      setRecovering(true);
      setWorkspaceReady(false);

      try {
        const data = await api.ensureWorkspace({ sessionId });

        setWorkspaceIdWithRef(data.workspace_id);

        if (data.status === 'ready') {
          setWorkspaceReady(true);
          return data;
        }

        // Poll until workspace is ready
        for (let attempt = 0; attempt < MAX_RECOVER_POLL_ATTEMPTS; attempt++) {
          await new Promise((r) => setTimeout(r, RECOVER_POLL_INTERVAL));

          try {
            const status = await api.getWorkspace(data.workspace_id);
            if (status.status === 'active') {
              setWorkspaceReady(true);
              return { ...data, status: 'ready' };
            }
          } catch {
            // Ignore transient polling errors, keep trying
          }
        }

        throw new Error('工作區恢復逾時，請稍後再試');
      } finally {
        setRecovering(false);
        recoverPromiseRef.current = null;
      }
    };

    recoverPromiseRef.current = doRecover();
    return recoverPromiseRef.current;
  }, [api, sessionId]);

  /**
   * Universal 502 auto-recovery wrapper.
   * Wraps any async operation — if it throws a 502 or a network error
   * (TypeError from fetch, typically meaning Pod unreachable),
   * calls ensureWorkspace to recover the pod, then retries the operation once.
   *
   * Usage: const result = await withAutoRecover(() => api.someCall(...));
   */
  const withAutoRecover = useCallback(async (operation) => {
    try {
      return await operation();
    } catch (e) {
      if (is502Error(e) || e.name === 'TypeError') {
        await recoverWorkspace();
        return await operation();
      }
      throw e;
    }
  }, [recoverWorkspace]);

  return (
    <AppContext.Provider value={{
      baseUrl, setBaseUrl,
      token, setToken,
      workspaceId, setWorkspaceId: setWorkspaceIdWithRef, workspaceIdRef,
      sessionId, setSessionId,
      connected, setConnected,
      workspaceReady, setWorkspaceReady,
      recovering,
      api,
      resetConnection,
      recoverWorkspace,
      withAutoRecover,
      // Dark mode
      darkMode, toggleDarkMode,
      // Admin (shares Gateway baseUrl via proxy)
      adminToken, setAdminToken,
      adminConnected, setAdminConnected,
      isAdmin,
      adminApi,
    }}>
      {children}
    </AppContext.Provider>
  );
}

export function useApp() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useApp must be used within AppProvider');
  return ctx;
}
