"""Storage Service — business logic for workspace storage CRUD, file operations, access control.

Dual-mode file operations:
  - Pod online  → proxy to Agent Pod /api/v1/files/*
  - Pod offline → K8s Job mounts PVC and operates directly
"""

import base64
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from poc.db.models import Workspace, WorkspaceMember
from poc.storage.k8s_client import StorageK8sClient
from poc.utils.config import DEFAULT_PVC_SIZE_GB, PVC_FILE_SIZE_LIMIT_MB

logger = logging.getLogger("storage.service")

_ROLE_PRIORITY = {"owner": 4, "admin": 3, "member": 2, "readonly": 1}
_MIN_ROLE_WRITE = "member"
_MIN_ROLE_ADMIN = "admin"
_MIN_ROLE_READ = "readonly"

_PROXY_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class StorageService:
    def __init__(self):
        self.k8s = StorageK8sClient()

    # ── Access control helpers ───────────────────────────────────────

    async def _get_workspace(
        self, db: AsyncSession, workspace_id: str,
    ) -> Workspace | None:
        result = await db.execute(
            select(Workspace).where(Workspace.workspace_id == workspace_id)
        )
        return result.scalar_one_or_none()

    async def _check_access(
        self,
        db: AsyncSession,
        workspace_id: str,
        user_id: str,
        role: str,
        min_role: str = "readonly",
    ) -> Workspace:
        """Check user access to workspace storage. Returns Workspace or raises."""
        ws = await self._get_workspace(db, workspace_id)
        if not ws:
            raise PermissionError(f"Workspace {workspace_id} not found")

        if role == "admin":
            return ws

        result = await db.execute(
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace_id)
            .where(WorkspaceMember.user_id == user_id)
        )
        member = result.scalar_one_or_none()
        if not member:
            raise PermissionError(f"User {user_id} has no access to workspace {workspace_id}")

        if _ROLE_PRIORITY.get(member.role, 0) < _ROLE_PRIORITY.get(min_role, 0):
            raise PermissionError(
                f"User {user_id} role '{member.role}' insufficient, need '{min_role}'+",
            )

        return ws

    # ── Routing helper ───────────────────────────────────────────────

    def _is_pod_online(self, workspace_id: str) -> bool:
        pod_name = f"pod-{workspace_id}"
        return self.k8s.pod_is_running(pod_name)

    def _get_agent_endpoint(self, workspace_id: str) -> str | None:
        service_name = f"svc-{workspace_id}"
        return self.k8s.get_service_endpoint(service_name)

    @staticmethod
    def _pvc_name(workspace_id: str) -> str:
        return f"pvc-{workspace_id}"

    # ── Workspace storage list ───────────────────────────────────────

    async def list_workspaces_storage(
        self,
        db: AsyncSession,
        user_id: str,
        role: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """List workspace storage. Admin sees all; user sees only accessible ones."""
        k8s_pvcs = self.k8s.list_pvcs_detail()

        if role == "admin":
            accessible_ws_ids = None
        else:
            result = await db.execute(
                select(WorkspaceMember.workspace_id)
                .where(WorkspaceMember.user_id == user_id)
            )
            accessible_ws_ids = {row[0] for row in result.all()}

        items = []
        for pvc in k8s_pvcs:
            ws_id = pvc["workspace_id"]
            if not ws_id:
                continue
            if accessible_ws_ids is not None and ws_id not in accessible_ws_ids:
                continue

            ws_result = await db.execute(
                select(Workspace.display_name, Workspace.workspace_type)
                .where(Workspace.workspace_id == ws_id)
            )
            ws_row = ws_result.one_or_none()

            items.append({
                "workspace_id": ws_id,
                "display_name": ws_row[0] if ws_row else None,
                "workspace_type": ws_row[1] if ws_row else "personal",
                "capacity": pvc["capacity"],
                "status": pvc["status"],
                "storage_class": pvc["storage_class"],
                "created_at": pvc["created_at"],
            })

        total = len(items)
        items = items[offset:offset + limit]
        return {"workspaces": items, "total": total}

    # ── Create workspace (Admin) ───────────────────────────────────────

    async def create_workspace(
        self,
        db: AsyncSession,
        workspace_id: str,
        workspace_type: str = "group",
        size_gb: int = DEFAULT_PVC_SIZE_GB,
        display_name: str | None = None,
        owner_user_id: str | None = None,
    ) -> dict:
        """Admin creates a new workspace + PVC.

        - workspace_type: team / company (personal is auto-created on user login)
        - owner_user_id: optional, must be an existing user
        """
        if workspace_type != "group":
            raise ValueError("workspace_type must be 'group' (personal is auto-created on user login)")

        # Check workspace_id uniqueness
        existing = await self._get_workspace(db, workspace_id)
        if existing:
            raise ValueError(f"Workspace {workspace_id} already exists")

        # Validate owner if provided
        if owner_user_id:
            from poc.db.models import User
            user_result = await db.execute(
                select(User).where(User.user_id == owner_user_id)
            )
            if not user_result.scalar_one_or_none():
                raise ValueError(f"User {owner_user_id} not found")

        pvc_name = self._pvc_name(workspace_id)

        # Create K8s PVC (POC: RWO since kind single-node; Production: RWX for group)
        self.k8s.create_pvc(workspace_id, pvc_name, size_gb)

        # Create DB record (group workspace: user_id=null, owner via workspace_members)
        ws = Workspace(
            workspace_id=workspace_id,
            user_id=None,
            workspace_type=workspace_type,
            pvc_name=pvc_name,
            display_name=display_name,
            pvc_size_gb=size_gb,
            status="idle",
        )
        db.add(ws)
        await db.flush()

        # Add owner to workspace_members if specified
        if owner_user_id:
            db.add(WorkspaceMember(
                workspace_id=workspace_id,
                user_id=owner_user_id,
                role="owner",
                granted_by=owner_user_id,
            ))

        await db.commit()

        return {
            "workspace_id": workspace_id,
            "workspace_type": workspace_type,
            "display_name": display_name,
            "status": "created",
        }

    # ── Workspace storage ensure (called by Orchestrator) ────────────

    async def ensure_storage(
        self,
        db: AsyncSession,
        workspace_id: str,
        size_gb: int = DEFAULT_PVC_SIZE_GB,
    ) -> dict:
        """Ensure PVC exists for a workspace. Called during workspace ensure flow."""
        pvc_name = self._pvc_name(workspace_id)

        if self.k8s.pvc_exists(pvc_name):
            return {"workspace_id": workspace_id, "status": "exists"}

        ws = await self._get_workspace(db, workspace_id)
        if not ws:
            raise ValueError(f"Workspace {workspace_id} not found")

        k8s_result = self.k8s.create_pvc(workspace_id, pvc_name, size_gb)
        return {
            "workspace_id": workspace_id,
            "status": k8s_result.get("status", "Pending"),
        }

    # ── Workspace rename ─────────────────────────────────────────────

    async def rename_workspace(
        self,
        db: AsyncSession,
        workspace_id: str,
        display_name: str,
        user_id: str,
        role: str,
    ) -> dict:
        """Rename workspace display_name."""
        ws = await self._check_access(db, workspace_id, user_id, role, min_role=_MIN_ROLE_ADMIN)
        ws.display_name = display_name
        await db.commit()
        return {"workspace_id": workspace_id, "display_name": display_name}

    # ── Workspace storage delete ─────────────────────────────────────

    async def delete_storage(
        self,
        db: AsyncSession,
        workspace_id: str,
        user_id: str,
        role: str,
    ) -> dict:
        """Delete workspace PVC. Admin or workspace owner only. Pod must be offline."""
        ws = await self._check_access(db, workspace_id, user_id, role, min_role="owner")

        if self._is_pod_online(workspace_id):
            raise ValueError(
                f"Cannot delete storage for workspace {workspace_id}: Pod is still running. "
                "Reap the workspace first."
            )

        pvc_name = self._pvc_name(workspace_id)
        self.k8s.delete_pvc(pvc_name)
        return {"message": f"Storage for workspace {workspace_id} deleted"}

    # ── File operations (dual-mode routing) ──────────────────────────

    async def file_list(
        self,
        db: AsyncSession,
        workspace_id: str,
        path: str,
        user_id: str,
        role: str,
    ) -> dict:
        ws = await self._check_access(db, workspace_id, user_id, role, min_role=_MIN_ROLE_READ)
        pvc_name = self._pvc_name(workspace_id)

        if self._is_pod_online(workspace_id):
            result = await self._proxy_agent_get(
                workspace_id, "/api/v1/files/list", params={"path": path},
            )
            return {**result, "mode": "online"}

        job_name = self.k8s.create_file_operation_job(workspace_id, pvc_name, "list", path)
        log = self.k8s.wait_for_job_completion(job_name)
        files = self._parse_ls_output(log) if log and log != "DIR_NOT_FOUND" else []
        return {"path": path, "files": files, "total": len(files), "mode": "offline"}

    async def file_upload(
        self,
        db: AsyncSession,
        workspace_id: str,
        path: str,
        filename: str,
        content: bytes,
        user_id: str,
        role: str,
    ) -> dict:
        ws = await self._check_access(db, workspace_id, user_id, role, min_role=_MIN_ROLE_WRITE)
        pvc_name = self._pvc_name(workspace_id)
        target_path = f"{path.rstrip('/')}/{filename}".lstrip("/") if path else filename

        if self._is_pod_online(workspace_id):
            result = await self._proxy_agent_upload(workspace_id, target_path, filename, content)
            return {**result, "mode": "online"}

        size_mb = len(content) / (1024 * 1024)
        if size_mb > PVC_FILE_SIZE_LIMIT_MB:
            raise ValueError(
                f"File too large ({size_mb:.1f}MB). "
                f"Offline mode limit: {PVC_FILE_SIZE_LIMIT_MB}MB. "
                "Start the workspace Pod for larger files."
            )

        content_b64 = base64.b64encode(content).decode("ascii")
        job_name = self.k8s.create_file_operation_job(
            workspace_id, pvc_name, "write", target_path, content_b64=content_b64,
        )
        log = self.k8s.wait_for_job_completion(job_name)
        if not log or "WRITTEN" not in log:
            raise RuntimeError(f"File upload failed: {log}")
        return {"status": "uploaded", "path": target_path, "size_bytes": len(content), "mode": "offline"}

    async def file_download(
        self,
        db: AsyncSession,
        workspace_id: str,
        path: str,
        user_id: str,
        role: str,
    ) -> tuple[bytes, str]:
        """Returns (content_bytes, mode)."""
        ws = await self._check_access(db, workspace_id, user_id, role, min_role=_MIN_ROLE_READ)
        pvc_name = self._pvc_name(workspace_id)

        if self._is_pod_online(workspace_id):
            content = await self._proxy_agent_download(workspace_id, path)
            return (content, "online")

        job_name = self.k8s.create_file_operation_job(workspace_id, pvc_name, "read", path)
        log = self.k8s.wait_for_job_completion(job_name)
        if not log or log.strip() == "FILE_NOT_FOUND":
            raise FileNotFoundError(f"File not found: {path}")
        content = base64.b64decode(log.strip())
        return (content, "offline")

    async def file_delete(
        self,
        db: AsyncSession,
        workspace_id: str,
        path: str,
        user_id: str,
        role: str,
    ) -> dict:
        ws = await self._check_access(db, workspace_id, user_id, role, min_role=_MIN_ROLE_WRITE)
        pvc_name = self._pvc_name(workspace_id)

        if self._is_pod_online(workspace_id):
            result = await self._proxy_agent_delete(workspace_id, path)
            return {**result, "mode": "online"}

        job_name = self.k8s.create_file_operation_job(workspace_id, pvc_name, "delete", path)
        log = self.k8s.wait_for_job_completion(job_name)
        if not log or "DELETED" not in log:
            raise RuntimeError(f"File delete failed: {log}")
        return {"status": "deleted", "path": path, "mode": "offline"}

    async def file_mkdir(
        self,
        db: AsyncSession,
        workspace_id: str,
        path: str,
        user_id: str,
        role: str,
    ) -> dict:
        ws = await self._check_access(db, workspace_id, user_id, role, min_role=_MIN_ROLE_WRITE)
        pvc_name = self._pvc_name(workspace_id)

        if self._is_pod_online(workspace_id):
            result = await self._proxy_agent_mkdir(workspace_id, path)
            return {**result, "mode": "online"}

        job_name = self.k8s.create_file_operation_job(workspace_id, pvc_name, "mkdir", path)
        log = self.k8s.wait_for_job_completion(job_name)
        if not log or "CREATED" not in log:
            raise RuntimeError(f"Mkdir failed: {log}")
        return {"status": "created", "path": path, "mode": "offline"}

    # ── Access info ──────────────────────────────────────────────────

    async def get_access_info(
        self,
        db: AsyncSession,
        workspace_id: str,
        user_id: str,
        role: str,
    ) -> dict:
        ws = await self._check_access(db, workspace_id, user_id, role, min_role=_MIN_ROLE_ADMIN)

        result = await db.execute(
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace_id)
        )
        members = result.scalars().all()

        return {
            "workspace_id": workspace_id,
            "members": [
                {
                    "user_id": m.user_id,
                    "role": m.role,
                    "granted_by": m.granted_by,
                    "granted_at": m.granted_at.isoformat() if m.granted_at else None,
                }
                for m in members
            ],
        }

    # ── Agent Pod proxy helpers ──────────────────────────────────────

    async def _proxy_agent_get(
        self, workspace_id: str, path: str, params: dict | None = None,
    ) -> dict:
        endpoint = self._get_agent_endpoint(workspace_id)
        if not endpoint:
            raise RuntimeError(f"No service endpoint for workspace {workspace_id}")
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as c:
            resp = await c.get(f"http://{endpoint}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def _proxy_agent_upload(
        self, workspace_id: str, target_path: str, filename: str, content: bytes,
    ) -> dict:
        endpoint = self._get_agent_endpoint(workspace_id)
        if not endpoint:
            raise RuntimeError(f"No service endpoint for workspace {workspace_id}")
        parts = target_path.rsplit("/", 1)
        upload_path = parts[0] if len(parts) > 1 else ""
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as c:
            resp = await c.post(
                f"http://{endpoint}/api/v1/files/upload",
                files={"file": (filename, content, "application/octet-stream")},
                params={"path": upload_path},
            )
        resp.raise_for_status()
        return resp.json()

    async def _proxy_agent_download(self, workspace_id: str, path: str) -> bytes:
        endpoint = self._get_agent_endpoint(workspace_id)
        if not endpoint:
            raise RuntimeError(f"No service endpoint for workspace {workspace_id}")
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as c:
            resp = await c.get(
                f"http://{endpoint}/api/v1/files/download",
                params={"path": path},
            )
        resp.raise_for_status()
        return resp.content

    async def _proxy_agent_delete(self, workspace_id: str, path: str) -> dict:
        endpoint = self._get_agent_endpoint(workspace_id)
        if not endpoint:
            raise RuntimeError(f"No service endpoint for workspace {workspace_id}")
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as c:
            resp = await c.delete(
                f"http://{endpoint}/api/v1/files/delete",
                params={"path": path},
            )
        resp.raise_for_status()
        return resp.json()

    async def _proxy_agent_mkdir(self, workspace_id: str, path: str) -> dict:
        endpoint = self._get_agent_endpoint(workspace_id)
        if not endpoint:
            raise RuntimeError(f"No service endpoint for workspace {workspace_id}")
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as c:
            resp = await c.post(
                f"http://{endpoint}/api/v1/files/mkdir",
                params={"path": path},
            )
        resp.raise_for_status()
        return resp.json()

    # ── Parsing helpers ──────────────────────────────────────────────

    @staticmethod
    def _parse_ls_output(log: str) -> list[dict]:
        """Parse `ls -la` output (busybox compatible) into file list.

        BusyBox format: perms links owner group size month day time name
        GNU format:     perms links owner group size month day time name
        """
        files = []
        for line in log.strip().splitlines():
            if line.startswith("total ") or line.endswith(" .") or line.endswith(" .."):
                continue
            # Split into at most 9 parts (busybox: perms links owner group size mon day time name)
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            perms = parts[0]
            size_str = parts[4]
            name = parts[8]
            is_dir = perms.startswith("d")
            entry = {"name": name, "type": "dir" if is_dir else "file"}
            if not is_dir:
                try:
                    entry["size"] = int(size_str)
                except ValueError:
                    pass
            files.append(entry)
        return files
