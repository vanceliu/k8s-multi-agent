"""Orchestrator FastAPI application — port 8080.

Two-tier reap mechanism:
  1. Agent Pod self-reports idle → POST /api/v1/orchestrator/reap-self
  2. Background stale scanner (every 10 min) cleans up Pods that are
     unresponsive or in abnormal state for > 12 hours.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from poc.db.session import get_db, init_db
from poc.orchestrator.service import OrchestratorService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("orchestrator")

svc = OrchestratorService()
_stale_scanner_task: asyncio.Task | None = None

# Stale pod scanner interval (seconds) — checks for 12hr unresponsive pods
STALE_SCAN_INTERVAL_SECONDS = 600  # every 10 minutes
STALE_POD_TIMEOUT_HOURS = 12


async def _stale_pod_scanner():
    """Background task: scan for pods unresponsive or abnormal for > 12 hours."""
    from poc.db.session import async_session
    while True:
        await asyncio.sleep(STALE_SCAN_INTERVAL_SECONDS)
        try:
            async with async_session() as db:
                reaped = await svc.reap_stale_pods(db, timeout_minutes=STALE_POD_TIMEOUT_HOURS * 60)
                if reaped:
                    logger.info("Stale scanner reaped %d pod(s)", len(reaped))
                fixed = await svc._fix_orphan_active_workspaces(db)
                if fixed:
                    logger.info("Stale scanner fixed %d orphan workspace(s)", fixed)
        except Exception:
            logger.exception("Stale scanner error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    global _stale_scanner_task
    _stale_scanner_task = asyncio.create_task(_stale_pod_scanner())
    logger.info(
        "Orchestrator started (stale_scan_interval=%ds, stale_timeout=%dh)",
        STALE_SCAN_INTERVAL_SECONDS, STALE_POD_TIMEOUT_HOURS,
    )
    yield
    if _stale_scanner_task:
        _stale_scanner_task.cancel()


app = FastAPI(title="Orchestrator POC", lifespan=lifespan)


# ── request / response models ─────────────────────────────────────────

class EnsureRequest(BaseModel):
    user_id: str
    session_id: str | None = None
    resource_tier: str = "standard"


class MarkActivityRequest(BaseModel):
    user_id: str
    session_id: str


class ReapSelfRequest(BaseModel):
    workspace_id: str


class ReapRequest(BaseModel):
    dry_run: bool = False
    timeout_minutes: int = 10
    force_all: bool = False


# ── routes ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/readiness")
async def readiness():
    return {"status": "ready"}


@app.post("/api/v1/orchestrator/ensure")
async def ensure_workspace(req: EnsureRequest, db: AsyncSession = Depends(get_db)):
    result = await svc.ensure_session_workspace(
        db, req.user_id, req.session_id, req.resource_tier,
    )
    return result


@app.post("/api/v1/orchestrator/mark-activity")
async def mark_activity(req: MarkActivityRequest, db: AsyncSession = Depends(get_db)):
    await svc.mark_activity(db, req.user_id, req.session_id)
    return {"message": "Activity recorded"}


@app.post("/api/v1/orchestrator/reap-self")
async def reap_self(req: ReapSelfRequest, db: AsyncSession = Depends(get_db)):
    """Called by Agent Pod when it detects idle timeout."""
    result = await svc.reap_workspace(db, req.workspace_id, reason="agent_idle_self_report")
    return result


@app.post("/api/v1/orchestrator/reap")
async def reap(req: ReapRequest, db: AsyncSession = Depends(get_db)):
    """Admin endpoint: manually trigger pod reap.

    - Default: reap pods idle > 10 minutes
    - force_all=true: reap ALL running pods (including active ones)
    """
    reaped = await svc.reap_stale_pods(
        db, timeout_minutes=req.timeout_minutes, force_all=req.force_all,
    )
    return {"total_reaped": len(reaped), "workspaces_reaped": reaped}


@app.get("/api/v1/orchestrator/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str, db: AsyncSession = Depends(get_db)):
    ws = await svc.get_workspace(db, workspace_id)
    if not ws:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


@app.get("/api/v1/orchestrator/users/{user_id}/sessions")
async def get_user_sessions(user_id: str, db: AsyncSession = Depends(get_db)):
    """List all sessions for a user."""
    sessions = await svc.get_user_sessions(db, user_id)
    return {"user_id": user_id, "sessions": sessions, "total": len(sessions)}


@app.get("/api/v1/orchestrator/sessions/{session_id}/history")
async def get_session_history(
    session_id: str, limit: int = 50, db: AsyncSession = Depends(get_db),
):
    """Get session details + activity log history."""
    from fastapi import HTTPException
    history = await svc.get_session_history(db, session_id, limit=limit)
    if not history:
        raise HTTPException(status_code=404, detail="Session not found")
    return history


@app.delete("/api/v1/orchestrator/sessions/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a session and all related data (checkpoints, activity logs)."""
    from fastapi import HTTPException
    result = await svc.delete_session(db, session_id)
    if not result:
        raise HTTPException(status_code=404, detail="Session not found")
    return result


@app.get("/api/v1/orchestrator/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    """Get conversation history for a session directly from PostgreSQL checkpointer."""
    messages = await svc.get_session_messages(session_id)
    return {
        "session_id": session_id,
        "messages": messages,
        "total": len(messages),
    }


# ── Admin APIs (called by Admin Service) ─────────────────────────────

@app.get("/api/v1/orchestrator/workspaces")
async def list_all_workspaces(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List all workspaces (paginated). For admin use."""
    return await svc.list_all_workspaces(db, limit=limit, offset=offset, status=status)


@app.get("/api/v1/orchestrator/users")
async def list_all_users(
    limit: int = 50,
    offset: int = 0,
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List all users (paginated). For admin use."""
    return await svc.list_all_users(db, limit=limit, offset=offset, is_active=is_active)


@app.get("/api/v1/orchestrator/users/{user_id}")
async def get_user(user_id: str, db: AsyncSession = Depends(get_db)):
    """Get user detail. For admin use."""
    from fastapi import HTTPException
    result = await svc.get_user(db, user_id)
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return result


@app.put("/api/v1/orchestrator/users/{user_id}/active")
async def update_user_active(
    user_id: str, body: dict, db: AsyncSession = Depends(get_db),
):
    """Activate/deactivate a user. For admin use."""
    from fastapi import HTTPException
    is_active = body.get("is_active")
    if is_active is None:
        raise HTTPException(status_code=400, detail="is_active field required")
    result = await svc.update_user_active(db, user_id, is_active)
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return result


@app.get("/api/v1/orchestrator/pods/status")
async def get_pods_status(db: AsyncSession = Depends(get_db)):
    """Pod status overview. For admin use."""
    return await svc.get_pods_status(db)


@app.get("/api/v1/orchestrator/workspaces/{workspace_id}/members")
async def list_workspace_members(
    workspace_id: str, db: AsyncSession = Depends(get_db),
):
    """List workspace members. For admin use."""
    members = await svc.list_workspace_members(db, workspace_id)
    return {"workspace_id": workspace_id, "members": members}


class AddMemberRequest(BaseModel):
    user_id: str
    role: str = "member"
    granted_by: str | None = None


@app.post("/api/v1/orchestrator/workspaces/{workspace_id}/members")
async def add_workspace_member(
    workspace_id: str, req: AddMemberRequest, db: AsyncSession = Depends(get_db),
):
    """Add a workspace member. For admin use."""
    from fastapi import HTTPException
    try:
        return await svc.add_workspace_member(
            db, workspace_id, req.user_id, req.role, req.granted_by,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/v1/orchestrator/workspaces/{workspace_id}/members/{user_id}")
async def remove_workspace_member(
    workspace_id: str, user_id: str, db: AsyncSession = Depends(get_db),
):
    """Remove a workspace member. For admin use."""
    from fastapi import HTTPException
    try:
        return await svc.remove_workspace_member(db, workspace_id, user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class UpdateMemberRoleRequest(BaseModel):
    role: str


@app.put("/api/v1/orchestrator/workspaces/{workspace_id}/members/{user_id}")
async def update_workspace_member_role(
    workspace_id: str,
    user_id: str,
    req: UpdateMemberRoleRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update a workspace member's role. For admin use."""
    from fastapi import HTTPException
    try:
        return await svc.update_workspace_member_role(db, workspace_id, user_id, req.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/v1/orchestrator/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str, db: AsyncSession = Depends(get_db),
):
    """Delete a workspace and all related records + K8s resources. For admin use."""
    from fastapi import HTTPException
    try:
        return await svc.delete_workspace(db, workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
