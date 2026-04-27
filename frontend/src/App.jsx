import { useState, useEffect } from 'react';
import { AppProvider, useApp } from './context/AppContext';
import ConnectionPanel from './components/ConnectionPanel';
import WorkspacePanel from './components/WorkspacePanel';
import ChatPanel from './components/ChatPanel';
import FileManager from './components/FileManager';
import AdminPanel from './components/AdminPanel';

const TABS = [
  { key: 'connection', label: '連線設定', requiresConnection: false },
  { key: 'workspace', label: '工作區', requiresConnection: true },
  { key: 'chat', label: 'AI 對話', requiresConnection: true },
  { key: 'files', label: '檔案管理', requiresWorkspace: true },
  { key: 'admin', label: '管理員', requiresAdmin: true },
];

function AppContent() {
  const { connected, workspaceReady, isAdmin, darkMode, toggleDarkMode } = useApp();
  const [activeTab, setActiveTab] = useState('connection');

  // Sync dark class on <html> so Tailwind dark: variants work globally
  useEffect(() => {
    document.documentElement.classList.toggle('dark', darkMode);
  }, [darkMode]);

  const isTabEnabled = (tab) => {
    if (tab.requiresAdmin) return isAdmin;
    if (tab.requiresWorkspace) return workspaceReady;
    if (tab.requiresConnection) return connected;
    return true;
  };

  const visibleTabs = TABS.filter((tab) => !tab.requiresAdmin || isAdmin);

  return (
    <div className="flex h-screen flex-col bg-gray-50 dark:bg-gray-900">
      {/* Header */}
      <header className="shrink-0 border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-sm">
        <div className="mx-auto max-w-7xl px-3 py-3 sm:px-4 sm:py-4">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-lg font-bold text-gray-900 dark:text-gray-100 sm:text-xl">DBT OpenClaw UI</h1>
              <p className="hidden text-xs text-gray-500 dark:text-gray-400 mt-0.5 sm:block">Deep Agent Workspace Demo</p>
            </div>
            <div className="flex items-center gap-2 text-xs sm:gap-3">
              {/* Dark mode toggle */}
              <button
                onClick={toggleDarkMode}
                className="rounded-lg p-1.5 text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700 transition-colors"
                title={darkMode ? '切換亮色模式' : '切換暗色模式'}
              >
                {darkMode ? (
                  <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
                  </svg>
                ) : (
                  <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
                  </svg>
                )}
              </button>
              {isAdmin && (
                <span className="inline-flex items-center rounded-full bg-amber-100 dark:bg-amber-900 px-2 py-0.5 text-xs font-medium text-amber-800 dark:text-amber-200">
                  Admin
                </span>
              )}
              <span className={`inline-flex items-center gap-1 ${connected ? 'text-green-600 dark:text-green-400' : 'text-gray-400 dark:text-gray-500'}`}>
                <span className={`h-2 w-2 rounded-full ${connected ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-600'}`} />
                Gateway
              </span>
              <span className={`inline-flex items-center gap-1 ${workspaceReady ? 'text-green-600 dark:text-green-400' : 'text-gray-400 dark:text-gray-500'}`}>
                <span className={`h-2 w-2 rounded-full ${workspaceReady ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-600'}`} />
                Workspace
              </span>
            </div>
          </div>
        </div>
      </header>

      {/* Tabs */}
      <div className="shrink-0 mx-auto max-w-7xl px-3 sm:px-4">
        <nav className="flex gap-0.5 overflow-x-auto border-b border-gray-200 dark:border-gray-700 pt-2 sm:gap-1">
          {visibleTabs.map((tab) => {
            const enabled = isTabEnabled(tab);
            return (
              <button
                key={tab.key}
                onClick={() => enabled && setActiveTab(tab.key)}
                className={`whitespace-nowrap px-2.5 py-2 text-xs font-medium border-b-2 transition-colors sm:px-4 sm:text-sm ${
                  activeTab === tab.key
                    ? 'border-blue-600 text-blue-600 dark:border-blue-400 dark:text-blue-400'
                    : enabled
                    ? 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-500'
                    : 'border-transparent text-gray-300 dark:text-gray-600 cursor-not-allowed'
                }`}
              >
                {tab.label}
              </button>
            );
          })}
        </nav>
      </div>

      {/* Content */}
      <main className="mx-auto w-full max-w-7xl flex-1 overflow-hidden px-2 py-3 sm:px-4 sm:py-4 lg:py-6">
        <div className={`flex h-full flex-col rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-sm ${activeTab === 'chat' ? 'p-2 sm:p-4 lg:p-6' : 'p-4 sm:p-6'}`}>
          {activeTab === 'connection' && <ConnectionPanel />}
          {activeTab === 'workspace' && <WorkspacePanel />}
          {activeTab === 'chat' && <ChatPanel />}
          {activeTab === 'files' && <FileManager />}
          {activeTab === 'admin' && <AdminPanel />}
        </div>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AppProvider>
      <AppContent />
    </AppProvider>
  );
}
