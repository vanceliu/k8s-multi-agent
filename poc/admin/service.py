"""Admin service — proxies to Orchestrator API for K8s/DB operations."""

import logging

import httpx

from poc.utils.config import ORCHESTRATOR_HOST, STORAGE_SERVICE_HOST

logger = logging.getLogger("admin.service")

# Shared httpx client timeout
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class AdminService:
    def __init__(self, orchestrator_url: str = ORCHESTRATOR_HOST, storage_url: str = STORAGE_SERVICE_HOST):
        self.orchestrator_url = orchestrator_url
        self.storage_url = storage_url

    # ── helpers ───────────────────────────────────────────────────

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{self.orchestrator_url}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, json: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(f"{self.orchestrator_url}{path}", json=json)
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, json: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.put(f"{self.orchestrator_url}{path}", json=json)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> dict:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(f"{self.orchestrator_url}{path}")
        resp.raise_for_status()
        return resp.json()

    # ── User management ──────────────────────────────────────────

    async def list_users(
        self, limit: int = 50, offset: int = 0, is_active: bool | None = None,
    ) -> dict:
        params = {"limit": limit, "offset": offset}
        if is_active is not None:
            params["is_active"] = is_active
        return await self._get("/api/v1/orchestrator/users", params=params)

    async def get_user(self, user_id: str) -> dict:
        return await self._get(f"/api/v1/orchestrator/users/{user_id}")

    async def update_user_active(self, user_id: str, is_active: bool) -> dict:
        return await self._put(
            f"/api/v1/orchestrator/users/{user_id}/active",
            json={"is_active": is_active},
        )

    # ── Workspace management ─────────────────────────────────────

    async def list_workspaces(
        self, limit: int = 50, offset: int = 0, status: str | None = None,
    ) -> dict:
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return await self._get("/api/v1/orchestrator/workspaces", params=params)

    async def get_workspace(self, workspace_id: str) -> dict:
        return await self._get(f"/api/v1/orchestrator/workspaces/{workspace_id}")

    async def delete_workspace(self, workspace_id: str, admin_token: str) -> dict:
        """Delete workspace: Storage Service (PVC) → Orchestrator (DB + K8s)."""
        errors = []

        # 1. Delete PVC via Storage Service
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.delete(
                    f"{self.storage_url}/api/v1/workspaces/{workspace_id}/storage",
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                resp.raise_for_status()
            logger.info("Deleted PVC for workspace %s via Storage Service", workspace_id)
        except Exception as e:
            logger.warning("Storage delete for %s failed (continuing): %s", workspace_id, e)
            errors.append(f"storage: {e}")

        # 2. Delete DB records + K8s resources via Orchestrator
        result = await self._delete(f"/api/v1/orchestrator/workspaces/{workspace_id}")

        if errors:
            result["warnings"] = errors
        return result

    async def reap(
        self, timeout_minutes: int = 10, force_all: bool = False,
    ) -> dict:
        return await self._post(
            "/api/v1/orchestrator/reap",
            json={"timeout_minutes": timeout_minutes, "force_all": force_all},
        )

    # ── Workspace members ────────────────────────────────────────

    async def list_workspace_members(self, workspace_id: str) -> dict:
        return await self._get(f"/api/v1/orchestrator/workspaces/{workspace_id}/members")

    async def add_workspace_member(
        self, workspace_id: str, user_id: str, role: str = "member", granted_by: str | None = None,
    ) -> dict:
        return await self._post(
            f"/api/v1/orchestrator/workspaces/{workspace_id}/members",
            json={"user_id": user_id, "role": role, "granted_by": granted_by},
        )

    async def remove_workspace_member(self, workspace_id: str, user_id: str) -> dict:
        return await self._delete(
            f"/api/v1/orchestrator/workspaces/{workspace_id}/members/{user_id}",
        )

    async def update_workspace_member_role(
        self, workspace_id: str, user_id: str, role: str,
    ) -> dict:
        return await self._put(
            f"/api/v1/orchestrator/workspaces/{workspace_id}/members/{user_id}",
            json={"role": role},
        )

    # ── Pod status ───────────────────────────────────────────────

    async def get_pods_status(self) -> dict:
        return await self._get("/api/v1/orchestrator/pods/status")
