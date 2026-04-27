import { useState, useCallback, useEffect } from 'react';
import { useApp } from '../context/AppContext';

// --- SVG Icons (inline, no emoji) ---
const ChevronRight = ({ className = '' }) => (
  <svg className={className} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="9 18 15 12 9 6" />
  </svg>
);
const ChevronDown = ({ className = '' }) => (
  <svg className={className} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="6 9 12 15 18 9" />
  </svg>
);
const FolderIcon = () => (
  <svg className="w-4 h-4 text-yellow-500 shrink-0" viewBox="0 0 24 24" fill="currentColor">
    <path d="M2 6a2 2 0 012-2h5l2 2h9a2 2 0 012 2v10a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
  </svg>
);
const FolderOpenIcon = () => (
  <svg className="w-4 h-4 text-yellow-500 shrink-0" viewBox="0 0 24 24" fill="currentColor">
    <path d="M2 6a2 2 0 012-2h5l2 2h9a2 2 0 012 2v1H7.5a2 2 0 00-1.8 1.1L2 18V6z" />
    <path d="M5.5 11h15.2a1 1 0 01.97 1.24l-2 8A1 1 0 0118.7 21H3.3a1 1 0 01-.97-1.24l2.2-7.52A1 1 0 015.5 11z" />
  </svg>
);
const FileIcon = () => (
  <svg className="w-4 h-4 text-gray-400 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
    <polyline points="14 2 14 8 20 8" />
  </svg>
);
const RefreshIcon = () => (
  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="23 4 23 10 17 10" />
    <path d="M20.49 15a9 9 0 11-2.12-9.36L23 10" />
  </svg>
);
const PlusIcon = () => (
  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
  </svg>
);
const CloseIcon = () => (
  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);
const HomeIcon = () => (
  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z" />
  </svg>
);

const UploadIcon = () => (
  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="16 16 12 12 8 16" /><line x1="12" y1="12" x2="12" y2="21" />
    <path d="M20.39 18.39A5 5 0 0018 9h-1.26A8 8 0 103 16.3" />
  </svg>
);
const DownloadIcon = () => (
  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
  </svg>
);
const TrashIcon = () => (
  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" />
    <path d="M10 11v6" /><path d="M14 11v6" /><path d="M9 6V4a1 1 0 011-1h4a1 1 0 011 1v2" />
  </svg>
);
const FolderPlusIcon = () => (
  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 2h9a2 2 0 012 2z" />
    <line x1="12" y1="11" x2="12" y2="17" /><line x1="9" y1="14" x2="15" y2="14" />
  </svg>
);

// Helper: backend may return 'dir' or 'directory' for folders
const isDirType = (type) => type === 'directory' || type === 'dir';

// --- Breadcrumb ---
function Breadcrumb({ currentPath, onNavigate }) {
  const segments = currentPath ? currentPath.split('/').filter(Boolean) : [];

  return (
    <nav className="flex items-center gap-1 text-sm min-h-[36px] px-3 py-1.5 bg-gray-50 dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 overflow-x-auto" aria-label="檔案路徑">
      <button
        onClick={() => onNavigate('')}
        className="flex items-center gap-1 text-gray-500 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 cursor-pointer transition-colors duration-150 shrink-0"
        title="根目錄"
      >
        <HomeIcon />
      </button>
      {segments.map((seg, i) => {
        const pathUpTo = segments.slice(0, i + 1).join('/');
        const isLast = i === segments.length - 1;
        return (
          <span key={pathUpTo} className="flex items-center gap-1 shrink-0">
            <span className="text-gray-300 dark:text-gray-600">/</span>
            {isLast ? (
              <span className="text-gray-800 dark:text-gray-200 font-medium">{seg}</span>
            ) : (
              <button
                onClick={() => onNavigate(pathUpTo)}
                className="text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300 hover:underline cursor-pointer transition-colors duration-150"
              >
                {seg}
              </button>
            )}
          </span>
        );
      })}
    </nav>
  );
}

// --- Tree Node (recursive) ---
function TreeNode({ name, path, isDir, depth, expanded, selectedPath, treeData, onToggleDir, onSelectItem }) {
  const isSelected = selectedPath === path;
  const isExpanded = expanded[path];
  const children = treeData[path];

  return (
    <div>
      <button
        onClick={() => isDir ? onToggleDir(path) : onSelectItem(path, false)}
        className={`
          w-full flex items-center gap-1.5 px-2 py-1 text-left text-sm cursor-pointer
          transition-colors duration-150 border-l-2
          ${isSelected ? 'bg-blue-50 dark:bg-blue-900/30 border-blue-500 text-blue-700 dark:text-blue-400' : 'border-transparent hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-300'}
        `}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
        title={path}
      >
        {isDir ? (
          <>
            {isExpanded ? <ChevronDown className="shrink-0 text-gray-400" /> : <ChevronRight className="shrink-0 text-gray-400" />}
            {isExpanded ? <FolderOpenIcon /> : <FolderIcon />}
          </>
        ) : (
          <>
            <span className="w-4 shrink-0" />
            <FileIcon />
          </>
        )}
        <span className="truncate">{name}</span>
      </button>

      {isDir && isExpanded && children && (
        <div>
          {children
            .sort((a, b) => {
              if (isDirType(a.type) && !isDirType(b.type)) return -1;
              if (!isDirType(a.type) && isDirType(b.type)) return 1;
              return a.name.localeCompare(b.name);
            })
            .map((child) => {
              const childPath = path ? `${path}/${child.name}` : child.name;
              return (
                <TreeNode
                  key={childPath}
                  name={child.name}
                  path={childPath}
                  isDir={isDirType(child.type)}
                  depth={depth + 1}
                  expanded={expanded}
                  selectedPath={selectedPath}
                  treeData={treeData}
                  onToggleDir={onToggleDir}
                  onSelectItem={onSelectItem}
                />
              );
            })}
        </div>
      )}
    </div>
  );
}

// --- Main FileManager ---
export default function FileManager() {
  const { api, workspaceId, withAutoRecover, recovering } = useApp();

  // Tree state: { [dirPath]: fileEntry[] }
  const [treeData, setTreeData] = useState({});
  const [expanded, setExpanded] = useState({});
  const [selectedPath, setSelectedPath] = useState('');
  const [selectedIsDir, setSelectedIsDir] = useState(true);

  // Right panel
  const [rightFiles, setRightFiles] = useState([]);
  const [fileContent, setFileContent] = useState(null);

  // Write file
  const [showWritePanel, setShowWritePanel] = useState(false);
  const [writePath, setWritePath] = useState('');
  const [writeContent, setWriteContent] = useState('');
  const [writeResult, setWriteResult] = useState(null);

  // Loading / error
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // Upload state
  const [uploading, setUploading] = useState(false);

  // Create folder state
  const [showMkdirPanel, setShowMkdirPanel] = useState(false);
  const [mkdirName, setMkdirName] = useState('');

  // Root directories to auto-load
  const ROOT_DIRS = ['data', 'memories', 'skills', 'sessions'];

  const listFiles = useCallback(async (dirPath) => {
    try {
      const data = await withAutoRecover(() =>
        api.listFiles(workspaceId, { path: dirPath || '' })
      );
      return data.files || [];
    } catch {
      // Fallback: MCP list_files
      const data = await withAutoRecover(() =>
        api.mcpExecute(workspaceId, {
          method: 'list_files',
          params: { path: dirPath || '.' },
        })
      );
      if (data.error) throw new Error(data.error.message);
      return data.result?.files || [];
    }
  }, [api, workspaceId, withAutoRecover]);

  const readFile = useCallback(async (filePath) => {
    const data = await withAutoRecover(() =>
      api.mcpExecute(workspaceId, {
        method: 'read_file',
        params: { path: filePath },
      })
    );
    if (data.error) throw new Error(data.error.message);
    return data.result;
  }, [api, workspaceId, withAutoRecover]);

  // Load root on mount
  useEffect(() => {
    if (!workspaceId) return;
    loadRoot();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  const loadRoot = async () => {
    setLoading(true);
    setError('');
    try {
      const rootFiles = await listFiles('.');
      setTreeData((prev) => ({ ...prev, '': rootFiles }));

      // Auto-expand known root dirs
      const dirs = rootFiles.filter((f) => isDirType(f.type) && ROOT_DIRS.includes(f.name));
      const newTree = { '': rootFiles };
      const newExpanded = {};

      await Promise.allSettled(
        dirs.map(async (d) => {
          try {
            const children = await listFiles(d.name);
            newTree[d.name] = children;
            newExpanded[d.name] = true;
          } catch { /* skip */ }
        })
      );

      setTreeData((prev) => ({ ...prev, ...newTree }));
      setExpanded((prev) => ({ ...prev, ...newExpanded }));

      // Show root listing on right
      setRightFiles(rootFiles);
      setSelectedPath('');
      setSelectedIsDir(true);
      setFileContent(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleToggleDir = async (dirPath) => {
    const isCurrentlyExpanded = expanded[dirPath];

    if (isCurrentlyExpanded) {
      // Collapse
      setExpanded((prev) => ({ ...prev, [dirPath]: false }));
    } else {
      // Expand & load children if not cached
      setExpanded((prev) => ({ ...prev, [dirPath]: true }));

      if (!treeData[dirPath]) {
        setLoading(true);
        setError('');
        try {
          const children = await listFiles(dirPath);
          setTreeData((prev) => ({ ...prev, [dirPath]: children }));
        } catch (e) {
          setError(e.message);
        } finally {
          setLoading(false);
        }
      }
    }

    // Select this dir and show its contents on right
    setSelectedPath(dirPath);
    setSelectedIsDir(true);
    setFileContent(null);

    const children = treeData[dirPath];
    if (children) {
      setRightFiles(children);
    } else {
      // Will be set after load
      try {
        const files = await listFiles(dirPath);
        setRightFiles(files);
        setTreeData((prev) => ({ ...prev, [dirPath]: files }));
      } catch { /* already handled */ }
    }
  };

  const handleSelectItem = async (itemPath, isDir) => {
    setSelectedPath(itemPath);
    setSelectedIsDir(isDir);
    setError('');

    if (isDir) {
      setFileContent(null);
      try {
        const children = await listFiles(itemPath);
        setRightFiles(children);
        setTreeData((prev) => ({ ...prev, [itemPath]: children }));
        setExpanded((prev) => ({ ...prev, [itemPath]: true }));
      } catch (e) {
        setError(e.message);
      }
    } else {
      // Read file
      setLoading(true);
      try {
        const result = await readFile(itemPath);
        setFileContent({ path: itemPath, ...result });
        setRightFiles([]);
      } catch (e) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    }
  };

  const handleBreadcrumbNavigate = (path) => {
    if (path === '') {
      // Root
      setSelectedPath('');
      setSelectedIsDir(true);
      setFileContent(null);
      setRightFiles(treeData[''] || []);
    } else {
      handleSelectItem(path, true);
    }
  };

  const handleRightFileClick = (file) => {
    const fullPath = selectedPath ? `${selectedPath}/${file.name}` : file.name;
    const isDir = isDirType(file.type);
    handleSelectItem(fullPath, isDir);
    if (isDir) {
      setExpanded((prev) => ({ ...prev, [fullPath]: true }));
    }
  };

  const handleWriteFile = async () => {
    if (!writePath || !writeContent) return;
    setLoading(true);
    setError('');
    setWriteResult(null);
    try {
      const data = await withAutoRecover(() =>
        api.mcpExecute(workspaceId, {
          method: 'write_file',
          params: { path: writePath, content: writeContent },
        })
      );
      if (data.error) throw new Error(data.error.message);
      setWriteResult(data.result);

      // Refresh parent dir in tree
      const parentDir = writePath.includes('/') ? writePath.substring(0, writePath.lastIndexOf('/')) : '';
      try {
        const refreshed = await listFiles(parentDir || '.');
        setTreeData((prev) => ({ ...prev, [parentDir]: refreshed }));
        if (selectedPath === parentDir) setRightFiles(refreshed);
      } catch { /* best effort */ }
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleRefresh = () => {
    setTreeData({});
    setExpanded({});
    setFileContent(null);
    setRightFiles([]);
    loadRoot();
  };

  const handleUpload = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setError('');
    try {
      const targetPath = selectedIsDir ? (selectedPath || '') : (selectedPath.includes('/') ? selectedPath.substring(0, selectedPath.lastIndexOf('/')) : '');
      try {
        await withAutoRecover(() => api.uploadFile(workspaceId, file, { path: targetPath }));
      } catch {
        // Fallback: read file as text and use MCP write_file
        const text = await file.text();
        const writePath = targetPath ? `${targetPath}/${file.name}` : file.name;
        const data = await withAutoRecover(() =>
          api.mcpExecute(workspaceId, {
            method: 'write_file',
            params: { path: writePath, content: text },
          })
        );
        if (data.error) throw new Error(data.error.message);
      }
      // Refresh the target directory
      const refreshed = await listFiles(targetPath || '.');
      setTreeData((prev) => ({ ...prev, [targetPath]: refreshed }));
      if (selectedPath === targetPath || (!selectedPath && !targetPath)) setRightFiles(refreshed);
    } catch (err) {
      setError(err.message);
    } finally {
      setUploading(false);
      e.target.value = '';
    }
  };

  const handleDownload = async (filePath) => {
    setError('');
    try {
      let blob;
      try {
        blob = await withAutoRecover(() => api.downloadFile(workspaceId, filePath));
      } catch {
        // Fallback: use MCP read_file
        const data = await withAutoRecover(() =>
          api.mcpExecute(workspaceId, {
            method: 'read_file',
            params: { path: filePath },
          })
        );
        if (data.error) throw new Error(data.error.message);
        blob = new Blob([data.result?.content || ''], { type: 'text/plain' });
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filePath.split('/').pop();
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDeleteFile = async (filePath, isDir = false) => {
    const label = isDir ? '資料夾' : '檔案';
    if (!confirm(`確定要刪除${label} ${filePath} 嗎？此操作不可逆。${isDir ? '\n將遞迴刪除資料夾內所有內容。' : ''}`)) return;
    setError('');
    setLoading(true);
    try {
      try {
        await withAutoRecover(() => api.deleteFile(workspaceId, filePath));
      } catch {
        // Fallback: MCP delete_file
        const data = await withAutoRecover(() =>
          api.mcpExecute(workspaceId, {
            method: 'delete_file',
            params: { path: filePath },
          })
        );
        if (data.error) throw new Error(data.error.message);
      }

      // If we were viewing this file, clear the content view
      if (fileContent?.path === filePath) {
        setFileContent(null);
      }

      // If a directory was deleted, clean up its tree data and expanded state
      if (isDir) {
        setTreeData((prev) => {
          const next = { ...prev };
          for (const key of Object.keys(next)) {
            if (key === filePath || key.startsWith(filePath + '/')) {
              delete next[key];
            }
          }
          return next;
        });
        setExpanded((prev) => {
          const next = { ...prev };
          for (const key of Object.keys(next)) {
            if (key === filePath || key.startsWith(filePath + '/')) {
              delete next[key];
            }
          }
          return next;
        });
      }

      // Refresh parent directory
      const parentDir = filePath.includes('/') ? filePath.substring(0, filePath.lastIndexOf('/')) : '';
      const refreshed = await listFiles(parentDir || '.');
      setTreeData((prev) => ({ ...prev, [parentDir]: refreshed }));
      if (selectedPath === parentDir || (!selectedPath && !parentDir)) {
        setRightFiles(refreshed);
      }
      // If the deleted file was selected, go back to parent
      if (selectedPath === filePath) {
        setSelectedPath(parentDir);
        setSelectedIsDir(true);
        setRightFiles(refreshed);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleCreateFolder = async () => {
    if (!mkdirName.trim()) return;
    const dirPath = selectedIsDir
      ? (selectedPath ? `${selectedPath}/${mkdirName.trim()}` : mkdirName.trim())
      : (selectedPath.includes('/') ? `${selectedPath.substring(0, selectedPath.lastIndexOf('/'))}/${mkdirName.trim()}` : mkdirName.trim());
    setError('');
    setLoading(true);
    try {
      await withAutoRecover(() => api.mkdir(workspaceId, dirPath));
      setMkdirName('');
      setShowMkdirPanel(false);

      // Refresh parent directory
      const parentDir = dirPath.includes('/') ? dirPath.substring(0, dirPath.lastIndexOf('/')) : '';
      const refreshed = await listFiles(parentDir || '.');
      setTreeData((prev) => ({ ...prev, [parentDir]: refreshed }));
      if (selectedPath === parentDir || (!selectedPath && !parentDir)) {
        setRightFiles(refreshed);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const formatSize = (bytes) => {
    if (bytes == null) return '-';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const rootChildren = treeData[''] || [];

  return (
    <div className="flex flex-col h-[600px] border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden bg-white dark:bg-gray-800">
      {/* Top bar: breadcrumb + actions */}
      <div className="flex items-center justify-between border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800">
        <Breadcrumb currentPath={selectedPath} onNavigate={handleBreadcrumbNavigate} />
        <div className="flex items-center gap-1 px-2 shrink-0">
          {loading && (
            <svg className="w-4 h-4 animate-spin text-blue-500" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
          )}
          <label
            className={`p-1.5 text-gray-500 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30 rounded cursor-pointer transition-colors duration-150 ${uploading ? 'opacity-40 pointer-events-none' : ''}`}
            title="上傳檔案"
          >
            <UploadIcon />
            <input type="file" className="hidden" onChange={handleUpload} disabled={uploading || recovering} />
          </label>
          <button
            onClick={() => { setShowWritePanel(!showWritePanel); setWriteResult(null); }}
            className="p-1.5 text-gray-500 dark:text-gray-400 hover:text-green-600 dark:hover:text-green-400 hover:bg-green-50 dark:hover:bg-green-900/30 rounded cursor-pointer transition-colors duration-150"
            title="新增檔案"
          >
            <PlusIcon />
          </button>
          <button
            onClick={() => { setShowMkdirPanel(!showMkdirPanel); setMkdirName(''); }}
            className="p-1.5 text-gray-500 dark:text-gray-400 hover:text-yellow-600 dark:hover:text-yellow-400 hover:bg-yellow-50 dark:hover:bg-yellow-900/30 rounded cursor-pointer transition-colors duration-150"
            title="新增資料夾"
          >
            <FolderPlusIcon />
          </button>
          <button
            onClick={handleRefresh}
            disabled={loading || recovering}
            className="p-1.5 text-gray-500 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30 rounded cursor-pointer transition-colors duration-150 disabled:opacity-40"
            title="重新整理"
          >
            <RefreshIcon />
          </button>
        </div>
      </div>

      {/* Recovery banner */}
      {recovering && (
        <div className="flex items-center gap-2 px-3 py-1.5 bg-yellow-50 dark:bg-yellow-900/30 border-b border-yellow-200 dark:border-yellow-800 text-xs text-yellow-800 dark:text-yellow-200">
          <svg className="h-3.5 w-3.5 animate-spin" viewBox="0 0 24 24" fill="none">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
          正在恢復工作區...
        </div>
      )}

      {/* Error banner */}
      {error && (
        <div className="flex items-center justify-between px-3 py-1.5 bg-red-50 dark:bg-red-900/30 border-b border-red-200 dark:border-red-800 text-xs text-red-700 dark:text-red-400">
          <span className="truncate">{error}</span>
          <button onClick={() => setError('')} className="shrink-0 cursor-pointer p-0.5 hover:text-red-900 transition-colors duration-150">
            <CloseIcon />
          </button>
        </div>
      )}

      {/* Write file panel (collapsible) */}
      {showWritePanel && (
        <div className="border-b border-gray-200 dark:border-gray-700 bg-green-50/50 dark:bg-green-900/20 p-3 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">寫入檔案</span>
            <button onClick={() => setShowWritePanel(false)} className="cursor-pointer text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 transition-colors duration-150">
              <CloseIcon />
            </button>
          </div>
          <input
            type="text"
            value={writePath}
            onChange={(e) => setWritePath(e.target.value)}
            placeholder="檔案路徑 (e.g. data/notes.txt)"
            className="block w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 dark:text-gray-200 px-2.5 py-1.5 text-sm font-mono focus:border-green-500 focus:ring-1 focus:ring-green-500 focus:outline-none"
          />
          <textarea
            value={writeContent}
            onChange={(e) => setWriteContent(e.target.value)}
            placeholder="檔案內容..."
            rows={3}
            className="block w-full resize-none rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 dark:text-gray-200 px-2.5 py-1.5 text-sm focus:border-green-500 focus:ring-1 focus:ring-green-500 focus:outline-none"
          />
          <div className="flex items-center gap-2">
            <button
              onClick={handleWriteFile}
              disabled={loading || recovering || !writePath || !writeContent}
              className="rounded bg-green-600 px-3 py-1.5 text-sm text-white hover:bg-green-700 disabled:opacity-50 cursor-pointer transition-colors duration-150"
            >
              寫入
            </button>
            {writeResult && (
              <span className="text-xs text-green-700 dark:text-green-400">
                已寫入 {writeResult.path} ({writeResult.bytes_written} bytes)
              </span>
            )}
          </div>
        </div>
      )}

      {/* Create folder panel (collapsible) */}
      {showMkdirPanel && (
        <div className="border-b border-gray-200 dark:border-gray-700 bg-yellow-50/50 dark:bg-yellow-900/20 p-3 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
              新增資料夾{selectedIsDir && selectedPath ? ` (於 ${selectedPath}/)` : ''}
            </span>
            <button onClick={() => setShowMkdirPanel(false)} className="cursor-pointer text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300 transition-colors duration-150">
              <CloseIcon />
            </button>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={mkdirName}
              onChange={(e) => setMkdirName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleCreateFolder()}
              placeholder="資料夾名稱"
              className="flex-1 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 dark:text-gray-200 px-2.5 py-1.5 text-sm font-mono focus:border-yellow-500 focus:ring-1 focus:ring-yellow-500 focus:outline-none"
            />
            <button
              onClick={handleCreateFolder}
              disabled={loading || recovering || !mkdirName.trim()}
              className="rounded bg-yellow-600 px-3 py-1.5 text-sm text-white hover:bg-yellow-700 disabled:opacity-50 cursor-pointer transition-colors duration-150"
            >
              建立
            </button>
          </div>
        </div>
      )}

      {/* Main content: sidebar + right panel */}
      <div className="flex flex-1 min-h-0">
        {/* Left sidebar: tree */}
        <div className="w-56 shrink-0 border-r border-gray-200 dark:border-gray-700 overflow-y-auto bg-gray-50/50 dark:bg-gray-800/50">
          {rootChildren.length === 0 && !loading && (
            <div className="p-3 text-xs text-gray-400 dark:text-gray-500 text-center">尚無檔案</div>
          )}
          {rootChildren
            .sort((a, b) => {
              if (isDirType(a.type) && !isDirType(b.type)) return -1;
              if (!isDirType(a.type) && isDirType(b.type)) return 1;
              return a.name.localeCompare(b.name);
            })
            .map((item) => {
              const itemPath = item.name;
              return (
                <TreeNode
                  key={itemPath}
                  name={item.name}
                  path={itemPath}
                  isDir={isDirType(item.type)}
                  depth={0}
                  expanded={expanded}
                  selectedPath={selectedPath}
                  treeData={treeData}
                  onToggleDir={handleToggleDir}
                  onSelectItem={handleSelectItem}
                />
              );
            })}
        </div>

        {/* Right panel: file list or file content */}
        <div className="flex-1 overflow-y-auto">
          {/* File content view */}
          {fileContent && (
            <div className="h-full flex flex-col">
              <div className="flex items-center justify-between px-4 py-2 border-b border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-800/50">
                <div className="flex items-center gap-2 text-sm">
                  <FileIcon />
                  <span className="font-mono text-gray-700 dark:text-gray-300">{fileContent.path}</span>
                  <span className="text-gray-400 dark:text-gray-500">({formatSize(fileContent.size_bytes)})</span>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => handleDownload(fileContent.path)}
                    className="flex items-center gap-1 rounded px-2 py-1 text-xs text-gray-500 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30 cursor-pointer transition-colors duration-150"
                    title="下載檔案"
                  >
                    <DownloadIcon />
                    <span>下載</span>
                  </button>
                  <button
                    onClick={() => handleDeleteFile(fileContent.path)}
                    disabled={loading || recovering}
                    className="flex items-center gap-1 rounded px-2 py-1 text-xs text-gray-500 dark:text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30 cursor-pointer transition-colors duration-150 disabled:opacity-40"
                    title="刪除檔案"
                  >
                    <TrashIcon />
                    <span>刪除</span>
                  </button>
                </div>
              </div>
              <pre className="flex-1 p-4 text-sm font-mono text-gray-800 dark:text-gray-200 overflow-auto whitespace-pre-wrap leading-relaxed bg-white dark:bg-gray-800">
                {fileContent.content}
              </pre>
            </div>
          )}

          {/* Directory listing view */}
          {!fileContent && selectedIsDir && (
            <div className="h-full">
              {rightFiles.length === 0 && !loading ? (
                <div className="flex items-center justify-center h-full text-sm text-gray-400 dark:text-gray-500">
                  此資料夾為空
                </div>
              ) : (
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 dark:bg-gray-800 sticky top-0">
                    <tr>
                      <th className="px-4 py-2 text-left font-medium text-gray-500 dark:text-gray-400 text-xs uppercase tracking-wider">名稱</th>
                      <th className="px-4 py-2 text-right font-medium text-gray-500 dark:text-gray-400 text-xs uppercase tracking-wider w-24">大小</th>
                      <th className="px-4 py-2 text-center font-medium text-gray-500 dark:text-gray-400 text-xs uppercase tracking-wider w-16">操作</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-50 dark:divide-gray-700">
                    {rightFiles
                      .sort((a, b) => {
                        if (isDirType(a.type) && !isDirType(b.type)) return -1;
                        if (!isDirType(a.type) && isDirType(b.type)) return 1;
                        return a.name.localeCompare(b.name);
                      })
                      .map((f) => {
                        const fullPath = selectedPath ? `${selectedPath}/${f.name}` : f.name;
                        return (
                        <tr
                          key={f.name}
                          onClick={() => handleRightFileClick(f)}
                          className="hover:bg-blue-50 dark:hover:bg-blue-900/20 cursor-pointer transition-colors duration-150"
                        >
                          <td className="px-4 py-2">
                            <div className="flex items-center gap-2">
                              {isDirType(f.type) ? <FolderIcon /> : <FileIcon />}
                              <span className={isDirType(f.type) ? 'text-gray-800 dark:text-gray-200 font-medium' : 'text-gray-700 dark:text-gray-300 font-mono'}>
                                {f.name}
                              </span>
                            </div>
                          </td>
                          <td className="px-4 py-2 text-right text-gray-400 dark:text-gray-500 font-mono text-xs">
                            {!isDirType(f.type) ? formatSize(f.size) : '-'}
                          </td>
                          <td className="px-4 py-2 text-center">
                            <div className="flex items-center justify-center gap-1">
                              {!isDirType(f.type) && (
                                <button
                                  onClick={(e) => { e.stopPropagation(); handleDownload(fullPath); }}
                                  className="p-1 text-gray-400 dark:text-gray-500 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30 rounded cursor-pointer transition-colors duration-150"
                                  title="下載"
                                >
                                  <DownloadIcon />
                                </button>
                              )}
                              <button
                                onClick={(e) => { e.stopPropagation(); handleDeleteFile(fullPath, isDirType(f.type)); }}
                                disabled={loading || recovering}
                                className="p-1 text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/30 rounded cursor-pointer transition-colors duration-150 disabled:opacity-40"
                                title={isDirType(f.type) ? '刪除資料夾' : '刪除'}
                              >
                                <TrashIcon />
                              </button>
                            </div>
                          </td>
                        </tr>
                        );
                      })}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
