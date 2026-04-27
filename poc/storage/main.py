"""Workspace Storage Service FastAPI application — port 8091.

Dedicated workspace storage management: file operations (dual-mode),
rename, delete, access control via workspace_members.

Auth: user token (own workspaces) + admin token (all workspaces).
URL pattern: /api/v1/workspaces/{workspace_id}/storage/*
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile, File
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from poc.db.session import get_db, init_db
from poc.storage.auth import validate_token
from poc.storage.service import StorageService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("storage")

security = HTTPBearer()
svc = StorageService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Storage Service started")
    yield


app = FastAPI(title="Workspace Storage Service POC", lifespan=lifespan)


# ── auth dependency ──────────────────────────────────────────────────

def _auth(credentials: HTTPAuthorizationCredentials = Depends(security)) -> tuple[str, str]:
    return validate_token(credentials)


# ── request models ───────────────────────────────────────────────────

class RenameRequest(BaseModel):
    display_name: str


class EnsureStorageRequest(BaseModel):
    workspace_id: str
    size_gb: int = 1


class CreateWorkspaceRequest(BaseModel):
    workspace_id: str
    workspace_type: str = "group"  # group (personal is auto-created)
    size_gb: int = 1
    display_name: str | None = None
    owner_user_id: str | None = None


# ── health ───────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy", "component": "storage-service"}


# ── Workspace storage list ───────────────────────────────────────────

@app.get("/api/v1/workspaces/storage")
async def list_workspaces_storage(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: tuple[str, str] = Depends(_auth),
    db: AsyncSession = Depends(get_db),
):
    """List workspace storage. Admin sees all; user sees only accessible ones."""
    user_id, role = auth
    try:
        return await svc.list_workspaces_storage(db, user_id, role, limit=limit, offset=offset)
    except Exception as e:
        logger.exception("list_workspaces_storage error")
        raise HTTPException(status_code=500, detail=str(e))


# ── Create workspace (Admin) ─────────────────────────────────────────

@app.post("/api/v1/workspaces/storage")
async def create_workspace(
    req: CreateWorkspaceRequest,
    auth: tuple[str, str] = Depends(_auth),
    db: AsyncSession = Depends(get_db),
):
    """Create a new workspace + PVC. Admin only."""
    _, role = auth
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    try:
        return await svc.create_workspace(
            db, req.workspace_id, req.workspace_type, req.size_gb,
            req.display_name, req.owner_user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Ensure storage (called by Orchestrator) ──────────────────────────

@app.post("/api/v1/workspaces/storage/ensure")
async def ensure_storage(
    req: EnsureStorageRequest,
    auth: tuple[str, str] = Depends(_auth),
    db: AsyncSession = Depends(get_db),
):
    """Ensure PVC exists for a workspace. Called by Orchestrator during ensure flow."""
    _, role = auth
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    try:
        return await svc.ensure_storage(db, req.workspace_id, req.size_gb)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Workspace rename ─────────────────────────────────────────────────

@app.put("/api/v1/workspaces/{workspace_id}/rename")
async def rename_workspace(
    workspace_id: str,
    req: RenameRequest,
    auth: tuple[str, str] = Depends(_auth),
    db: AsyncSession = Depends(get_db),
):
    """Rename workspace display_name. Admin or workspace admin+."""
    user_id, role = auth
    try:
        return await svc.rename_workspace(db, workspace_id, req.display_name, user_id, role)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


# ── Workspace storage delete ─────────────────────────────────────────

@app.delete("/api/v1/workspaces/{workspace_id}/storage")
async def delete_storage(
    workspace_id: str,
    auth: tuple[str, str] = Depends(_auth),
    db: AsyncSession = Depends(get_db),
):
    """Delete workspace PVC. Admin or workspace owner."""
    user_id, role = auth
    try:
        return await svc.delete_storage(db, workspace_id, user_id, role)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── File operations ──────────────────────────────────────────────────

@app.get("/api/v1/workspaces/{workspace_id}/storage/files")
async def file_list(
    workspace_id: str,
    path: str = Query("", description="相對於 workspace 的目錄路徑"),
    auth: tuple[str, str] = Depends(_auth),
    db: AsyncSession = Depends(get_db),
):
    """List files in workspace storage. Readonly+ access."""
    user_id, role = auth
    try:
        return await svc.file_list(db, workspace_id, path, user_id, role)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.exception("file_list error")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/workspaces/{workspace_id}/storage/files/upload")
async def file_upload(
    workspace_id: str,
    file: UploadFile = File(...),
    path: str = Query("", description="相對於 workspace 的目標目錄"),
    auth: tuple[str, str] = Depends(_auth),
    db: AsyncSession = Depends(get_db),
):
    """Upload a file to workspace storage. Member+ access."""
    user_id, role = auth
    content = await file.read()
    try:
        return await svc.file_upload(
            db, workspace_id, path, file.filename, content, user_id, role,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/workspaces/{workspace_id}/storage/files/download")
async def file_download(
    workspace_id: str,
    path: str = Query(..., description="相對於 workspace 的檔案路徑"),
    auth: tuple[str, str] = Depends(_auth),
    db: AsyncSession = Depends(get_db),
):
    """Download a file from workspace storage. Readonly+ access."""
    user_id, role = auth
    try:
        content, mode = await svc.file_download(db, workspace_id, path, user_id, role)
        filename = path.rsplit("/", 1)[-1] if "/" in path else path
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Storage-Mode": mode,
            },
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/v1/workspaces/{workspace_id}/storage/files")
async def file_delete(
    workspace_id: str,
    path: str = Query(..., description="相對於 workspace 的檔案或目錄路徑"),
    auth: tuple[str, str] = Depends(_auth),
    db: AsyncSession = Depends(get_db),
):
    """Delete a file or directory from workspace storage. Member+ access."""
    user_id, role = auth
    try:
        return await svc.file_delete(db, workspace_id, path, user_id, role)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/workspaces/{workspace_id}/storage/files/mkdir")
async def file_mkdir(
    workspace_id: str,
    path: str = Query(..., description="相對於 workspace 的目錄路徑"),
    auth: tuple[str, str] = Depends(_auth),
    db: AsyncSession = Depends(get_db),
):
    """Create a directory in workspace storage. Member+ access."""
    user_id, role = auth
    try:
        return await svc.file_mkdir(db, workspace_id, path, user_id, role)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Access info ──────────────────────────────────────────────────────

@app.get("/api/v1/workspaces/{workspace_id}/storage/access")
async def get_access_info(
    workspace_id: str,
    auth: tuple[str, str] = Depends(_auth),
    db: AsyncSession = Depends(get_db),
):
    """Get workspace storage access info (workspace members). Admin or workspace admin+."""
    user_id, role = auth
    try:
        return await svc.get_access_info(db, workspace_id, user_id, role)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8091)
