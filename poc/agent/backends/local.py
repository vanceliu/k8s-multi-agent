"""Local filesystem backend — PVC-backed replacement for S3Backend.

Interface mirrors doc 06 S3Backend so switching to S3 later only requires
swapping this class. All paths are relative to the workspace root.

Session-aware access control:
- Write allowed: own session folder, workspace-level data/ and memories/
- Read-only: other session folders
- Blocked: path traversal outside workspace
"""

import fnmatch
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Workspace-level directories that all sessions can read and write
_SHARED_WRITABLE_DIRS = {"data", "memories"}


class LocalBackend:
    """Filesystem backend backed by PVC-mounted local directory.

    Drop-in replacement for S3Backend. All operations are synchronous
    (local disk), wrapped in async interface for consistency.
    """

    def __init__(self, workspace_path: str, session_id: str | None = None):
        self.root = Path(workspace_path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id

    def _resolve(self, path: str) -> Path:
        """Resolve relative path to absolute, ensuring it stays within workspace."""
        clean = path.lstrip("/")
        resolved = (self.root / clean).resolve()
        if not str(resolved).startswith(str(self.root.resolve())):
            raise ValueError(f"Path traversal detected: {path}")
        return resolved

    def _check_write_permission(self, path: str) -> None:
        """Check if the current session has write permission to the given path.

        Write rules:
        - Own session folder (sessions/{self.session_id}/*): allowed
        - Shared workspace dirs (data/, memories/): allowed
        - Other session folders (sessions/{other_id}/*): denied
        - Other workspace-level paths (skills/, etc.): allowed
        """
        if not self.session_id:
            return  # No session context — unrestricted (backward compat)

        clean = path.lstrip("/")
        parts = clean.split("/")

        if len(parts) >= 2 and parts[0] == "sessions":
            target_session = parts[1]
            if target_session != self.session_id:
                raise PermissionError(
                    f"Session {self.session_id} cannot write to session {target_session}'s folder"
                )

    # ── Read ──────────────────────────────────────────────────────

    async def read_file(self, path: str) -> str:
        target = self._resolve(path)
        return target.read_text(encoding="utf-8")

    async def read_file_bytes(self, path: str) -> bytes:
        target = self._resolve(path)
        return target.read_bytes()

    # ── Write ─────────────────────────────────────────────────────

    async def write_file(self, path: str, content: str) -> None:
        self._check_write_permission(path)
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    async def write_file_bytes(self, path: str, data: bytes) -> None:
        self._check_write_permission(path)
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    # ── Edit ──────────────────────────────────────────────────────

    async def edit_file(self, path: str, old_str: str, new_str: str) -> str:
        self._check_write_permission(path)
        content = await self.read_file(path)
        if old_str not in content:
            raise ValueError(f"String not found in {path}")
        new_content = content.replace(old_str, new_str, 1)
        await self.write_file(path, new_content)
        return f"Replaced 1 occurrence in {path}"

    # ── List / Delete ─────────────────────────────────────────────

    async def ls(self, path: str = "") -> list[dict[str, Any]]:
        target = self._resolve(path) if path else self.root
        if not target.is_dir():
            return []
        items = []
        for p in sorted(target.iterdir()):
            if p.is_dir():
                items.append({"name": p.name, "type": "dir"})
            else:
                items.append({
                    "name": p.name,
                    "type": "file",
                    "size": p.stat().st_size,
                    "last_modified": p.stat().st_mtime,
                })
        return items

    async def delete_path(self, path: str) -> str:
        """Delete a file or directory. Returns 'file', 'dir', or 'not_found'."""
        self._check_write_permission(path)
        target = self._resolve(path)
        if target.is_file():
            target.unlink()
            return "file"
        elif target.is_dir():
            import shutil
            shutil.rmtree(target)
            return "dir"
        return "not_found"

    async def delete_file(self, path: str) -> None:
        self._check_write_permission(path)
        target = self._resolve(path)
        if target.is_file():
            target.unlink()

    async def create_directory(self, path: str) -> None:
        self._check_write_permission(path)
        target = self._resolve(path)
        target.mkdir(parents=True, exist_ok=True)

    async def file_exists(self, path: str) -> bool:
        return self._resolve(path).exists()

    # ── Glob / Grep ───────────────────────────────────────────────

    async def glob(self, pattern: str, path: str = "") -> list[str]:
        base = self._resolve(path) if path else self.root
        if not base.is_dir():
            return []
        results = []
        for p in base.rglob("*"):
            if p.is_file():
                rel = str(p.relative_to(base))
                if fnmatch.fnmatch(rel, pattern):
                    results.append(rel)
        return results

    async def grep(
        self, pattern: str, path: str = "", ignore_case: bool = False,
    ) -> list[dict[str, Any]]:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
        files = await self.glob("*", path)
        results = []
        for f in files[:50]:
            try:
                content = await self.read_file(f if not path else f"{path}/{f}")
                for i, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        results.append({"file": f, "line": i, "content": line})
            except Exception:
                continue
        return results

    # ── Lifecycle ─────────────────────────────────────────────────

    async def close(self) -> None:
        """No-op for local backend. S3Backend would close aioboto3 session."""
        pass
