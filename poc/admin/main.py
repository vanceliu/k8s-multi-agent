"""Admin Service — port 8090. Internal admin endpoints.

Provides user management, workspace oversight, PVC management,
and workspace member role management. All operations proxy through
Orchestrator API — Admin Service never touches K8s directly.

POC auth: static admin token (poc-admin-token-12345).
Production: JWT with admin role claim + IP whitelist.
"""

import logging

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from httpx import HTTPStatusError
from pydantic import BaseModel

from poc.admin.auth import validate_admin_token
from poc.admin.service import AdminService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("admin")

security = HTTPBearer()
svc = AdminService()

app = FastAPI(
    title="Admin Service POC",
    description="Admin token: `poc-admin-token-12345`",
)


# ── auth dependency ──────────────────────────────────────────────────

def _require_admin(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    return validate_admin_token(credentials)


# ── request models ───────────────────────────────────────────────────

class UpdateActiveRequest(BaseModel):
    is_active: bool


class ReapRequest(BaseModel):
    timeout_minutes: int = 10
    force_all: bool = False


class AddMemberRequest(BaseModel):
    user_id: str
    role: str = "member"
    granted_by: str | None = None


class UpdateMemberRoleRequest(BaseModel):
    role: str


# ── health ───────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy", "component": "admin"}


# ── User management ──────────────────────────────────────────────────

@app.get("/api/v1/admin/users")
async def list_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    is_active: bool | None = None,
    _admin: str = Depends(_require_admin),
):
    """List all users (paginated)."""
    try:
        return await svc.list_users(limit=limit, offset=offset, is_active=is_active)
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@app.get("/api/v1/admin/users/{user_id}")
async def get_user(user_id: str, _admin: str = Depends(_require_admin)):
    """Get user detail."""
    try:
        return await svc.get_user(user_id)
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@app.put("/api/v1/admin/users/{user_id}/active")
async def update_user_active(
    user_id: str, req: UpdateActiveRequest, _admin: str = Depends(_require_admin),
):
    """Activate or deactivate a user."""
    try:
        return await svc.update_user_active(user_id, req.is_active)
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


# ── Workspace management ─────────────────────────────────────────────

@app.get("/api/v1/admin/workspaces")
async def list_workspaces(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    _admin: str = Depends(_require_admin),
):
    """List all workspaces (paginated)."""
    try:
        return await svc.list_workspaces(limit=limit, offset=offset, status=status)
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@app.get("/api/v1/admin/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str, _admin: str = Depends(_require_admin)):
    """Get workspace detail."""
    try:
        return await svc.get_workspace(workspace_id)
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@app.delete("/api/v1/admin/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    _admin: str = Depends(_require_admin),
):
    """Delete a workspace (PVC + DB records + K8s resources)."""
    try:
        return await svc.delete_workspace(workspace_id, credentials.credentials)
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@app.post("/api/v1/admin/reap")
async def reap(req: ReapRequest, _admin: str = Depends(_require_admin)):
    """Manually trigger idle pod reap."""
    try:
        return await svc.reap(timeout_minutes=req.timeout_minutes, force_all=req.force_all)
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


# ── Workspace members ────────────────────────────────────────────────

@app.get("/api/v1/admin/workspaces/{workspace_id}/members")
async def list_workspace_members(
    workspace_id: str, _admin: str = Depends(_require_admin),
):
    """List workspace members."""
    try:
        return await svc.list_workspace_members(workspace_id)
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@app.post("/api/v1/admin/workspaces/{workspace_id}/members")
async def add_workspace_member(
    workspace_id: str, req: AddMemberRequest, _admin: str = Depends(_require_admin),
):
    """Add a member to a workspace."""
    try:
        return await svc.add_workspace_member(
            workspace_id, req.user_id, req.role, req.granted_by,
        )
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@app.delete("/api/v1/admin/workspaces/{workspace_id}/members/{user_id}")
async def remove_workspace_member(
    workspace_id: str, user_id: str, _admin: str = Depends(_require_admin),
):
    """Remove a member from a workspace."""
    try:
        return await svc.remove_workspace_member(workspace_id, user_id)
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@app.put("/api/v1/admin/workspaces/{workspace_id}/members/{user_id}")
async def update_workspace_member_role(
    workspace_id: str,
    user_id: str,
    req: UpdateMemberRoleRequest,
    _admin: str = Depends(_require_admin),
):
    """Update a workspace member's role."""
    try:
        return await svc.update_workspace_member_role(workspace_id, user_id, req.role)
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


# ── Pod status ───────────────────────────────────────────────────────

@app.get("/api/v1/admin/pods/status")
async def get_pods_status(_admin: str = Depends(_require_admin)):
    """Pod status overview."""
    try:
        return await svc.get_pods_status()
    except HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
