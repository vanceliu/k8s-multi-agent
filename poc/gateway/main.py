"""API Gateway — port 8000. Token validation + IM channel abstraction + proxy.

Integrates the channel abstraction layer (inspired by deer-flow).
Chat requests go through WebChannel → MessageBus → ChannelManager → Agent Pod.
MCP/proxy requests go directly to Agent Pod via route table.
"""

import json as _json
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Depends, HTTPException, Request, Response, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from poc.gateway.channels.service import (
    get_channel_service,
    start_channel_service,
    stop_channel_service,
)
from poc.utils.config import ADMIN_STATIC_TOKEN, ORCHESTRATOR_HOST, STORAGE_SERVICE_HOST, STATIC_TOKEN

# Admin Service internal URL (ClusterIP)
ADMIN_SERVICE_HOST = os.environ.get(
    "ADMIN_SERVICE_HOST",
    "http://admin.agent-platform.svc.cluster.local",
)

# Storage Service internal URL (ClusterIP)
STORAGE_SERVICE_URL = os.environ.get("STORAGE_SERVICE_HOST", STORAGE_SERVICE_HOST)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("gateway")

security = HTTPBearer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start channel service (MessageBus + ChannelManager + WebChannel)
    svc = await start_channel_service(
        orchestrator_url=ORCHESTRATOR_HOST,
        channels_config={"web": {"enabled": True}},
    )
    logger.info("API Gateway started, orchestrator=%s, channels=%s", ORCHESTRATOR_HOST, svc.get_status())
    yield
    await stop_channel_service()


app = FastAPI(
    title="API Gateway POC",
    lifespan=lifespan,
    description="Token format: `$POC_STATIC_TOKEN:{user_id}`",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── auth helper ───────────────────────────────────────────────────────

def _validate_token(credentials: HTTPAuthorizationCredentials) -> str:
    """Returns user_id from token. POC token format: {STATIC_TOKEN}:{user_id}"""
    token = credentials.credentials
    if ":" not in token:
        raise HTTPException(status_code=401, detail="Invalid token format, expected token:user_id")
    prefix, user_id = token.rsplit(":", 1)
    if prefix != STATIC_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user_id


def _validate_admin_token(credentials: HTTPAuthorizationCredentials) -> str:
    """Validate admin token. POC: static token. Production: JWT with admin role."""
    token = credentials.credentials
    if token != ADMIN_STATIC_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    return "admin"


def _get_route_table() -> dict[str, str]:
    """Get route table from ChannelManager."""
    svc = get_channel_service()
    if svc:
        return svc.manager.get_route_table()
    return {}


# ── request models ────────────────────────────────────────────────────

class EnsureRequest(BaseModel):
    session_id: str | None = None
    resource_tier: str = "standard"


class ChatRequest(BaseModel):
    message: str
    session_id: str


# ── routes ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    svc = get_channel_service()
    return {
        "status": "healthy",
        "component": "gateway",
        "channels": svc.get_status() if svc else {},
    }


@app.post("/api/v1/workspaces/ensure")
async def ensure_workspace(
    req: EnsureRequest,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    user_id = _validate_token(credentials)

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_HOST}/api/v1/orchestrator/ensure",
            json={"user_id": user_id, "session_id": req.session_id, "resource_tier": req.resource_tier},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    data = resp.json()
    workspace_id = data["workspace_id"]

    # Sync route to ChannelManager
    svc = get_channel_service()
    if svc:
        svc.manager.set_route(workspace_id, data.get("service_endpoint", ""))
    logger.info("Route cached: %s -> %s", workspace_id, data.get("service_endpoint", ""))

    return {
        "workspace_id": data["workspace_id"],
        "user_id": data["user_id"],
        "session_id": data["session_id"],
        "gateway_endpoint": "http://localhost:8000",
        "gateway_route": f"/workspaces/{data['workspace_id']}",
        "status": data["status"],
    }


# ── Storage Service proxy (must be before /workspaces/{workspace_id}) ─

def _validate_any_token(credentials: HTTPAuthorizationCredentials) -> str:
    """Validate user or admin token. Returns user_id or 'admin'."""
    token = credentials.credentials
    if token == ADMIN_STATIC_TOKEN:
        return "admin"
    if ":" not in token:
        raise HTTPException(status_code=401, detail="Invalid token format")
    prefix, user_id = token.rsplit(":", 1)
    if prefix != STATIC_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user_id


@app.get("/api/v1/workspaces/storage")
async def proxy_storage_list(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """List workspace storage. Proxied to Storage Service."""
    _validate_any_token(credentials)
    headers = {
        "Authorization": f"Bearer {credentials.credentials}",
    }
    params = dict(request.query_params)
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.get(
                f"{STORAGE_SERVICE_URL}/api/v1/workspaces/storage",
                headers=headers, params=params,
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach Storage Service")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.post("/api/v1/workspaces/storage")
async def proxy_storage_create(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Create a new workspace + PVC. Admin only. Proxied to Storage Service."""
    _validate_any_token(credentials)
    body = await request.body()
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{STORAGE_SERVICE_URL}/api/v1/workspaces/storage",
                content=body,
                headers={
                    "Authorization": f"Bearer {credentials.credentials}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach Storage Service")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.post("/api/v1/workspaces/storage/ensure")
async def proxy_storage_ensure(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Ensure workspace storage. Called by Orchestrator."""
    _validate_any_token(credentials)
    body = await request.body()
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{STORAGE_SERVICE_URL}/api/v1/workspaces/storage/ensure",
                content=body,
                headers={
                    "Authorization": f"Bearer {credentials.credentials}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach Storage Service")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.put("/api/v1/workspaces/{workspace_id}/rename")
async def proxy_workspace_rename(
    workspace_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Rename workspace display_name. Proxied to Storage Service."""
    _validate_any_token(credentials)
    body = await request.body()
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.put(
                f"{STORAGE_SERVICE_URL}/api/v1/workspaces/{workspace_id}/rename",
                content=body,
                headers={
                    "Authorization": f"Bearer {credentials.credentials}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach Storage Service")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/v1/workspaces/{workspace_id}/storage/files")
async def proxy_storage_file_list(
    workspace_id: str,
    path: str = Query("", description="相對於 workspace 的目錄路徑"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """List files in workspace storage. Proxied to Storage Service."""
    _validate_any_token(credentials)
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.get(
                f"{STORAGE_SERVICE_URL}/api/v1/workspaces/{workspace_id}/storage/files",
                params={"path": path},
                headers={"Authorization": f"Bearer {credentials.credentials}"},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach Storage Service")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.post("/api/v1/workspaces/{workspace_id}/storage/files/upload")
async def proxy_storage_file_upload(
    workspace_id: str,
    file: UploadFile = File(...),
    path: str = Query("", description="相對於 workspace 的目標目錄"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Proxy workspace file upload to Storage Service."""
    _validate_any_token(credentials)
    content = await file.read()
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{STORAGE_SERVICE_URL}/api/v1/workspaces/{workspace_id}/storage/files/upload",
                files={"file": (file.filename, content, file.content_type or "application/octet-stream")},
                params={"path": path},
                headers={"Authorization": f"Bearer {credentials.credentials}"},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach Storage Service")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/v1/workspaces/{workspace_id}/storage/files/download")
async def proxy_storage_file_download(
    workspace_id: str,
    path: str = Query(..., description="相對於 workspace 的檔案路徑"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Proxy workspace file download from Storage Service."""
    _validate_any_token(credentials)
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.get(
                f"{STORAGE_SERVICE_URL}/api/v1/workspaces/{workspace_id}/storage/files/download",
                params={"path": path},
                headers={"Authorization": f"Bearer {credentials.credentials}"},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach Storage Service")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/octet-stream"),
        headers={"Content-Disposition": resp.headers.get("content-disposition", "")},
    )


@app.delete("/api/v1/workspaces/{workspace_id}/storage/files")
async def proxy_storage_file_delete(
    workspace_id: str,
    path: str = Query(..., description="相對於 workspace 的檔案或目錄路徑"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Proxy file delete to Storage Service."""
    _validate_any_token(credentials)
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.delete(
                f"{STORAGE_SERVICE_URL}/api/v1/workspaces/{workspace_id}/storage/files",
                params={"path": path},
                headers={"Authorization": f"Bearer {credentials.credentials}"},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach Storage Service")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.post("/api/v1/workspaces/{workspace_id}/storage/files/mkdir")
async def proxy_storage_file_mkdir(
    workspace_id: str,
    path: str = Query(..., description="相對於 workspace 的目錄路徑"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Proxy mkdir to Storage Service."""
    _validate_any_token(credentials)
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"{STORAGE_SERVICE_URL}/api/v1/workspaces/{workspace_id}/storage/files/mkdir",
                params={"path": path},
                headers={"Authorization": f"Bearer {credentials.credentials}"},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach Storage Service")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/v1/workspaces/{workspace_id}/storage/access")
async def proxy_storage_access(
    workspace_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Get workspace storage access info. Proxied to Storage Service."""
    _validate_any_token(credentials)
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.get(
                f"{STORAGE_SERVICE_URL}/api/v1/workspaces/{workspace_id}/storage/access",
                headers={"Authorization": f"Bearer {credentials.credentials}"},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach Storage Service")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


# ── Workspace queries (after storage routes) ─────────────────────────

@app.get("/api/v1/workspaces/{workspace_id}")
async def get_workspace(
    workspace_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    _validate_token(credentials)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_HOST}/api/v1/orchestrator/workspaces/{workspace_id}",
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/v1/sessions")
async def list_user_sessions(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """List all sessions for the authenticated user."""
    user_id = _validate_token(credentials)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_HOST}/api/v1/orchestrator/users/{user_id}/sessions",
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/v1/sessions/{session_id}/history")
async def get_session_history(
    session_id: str,
    limit: int = 50,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Get session details + activity log history."""
    _validate_token(credentials)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_HOST}/api/v1/orchestrator/sessions/{session_id}/history",
            params={"limit": limit},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/api/v1/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Get conversation message history for a session (from Orchestrator → PostgreSQL checkpointer)."""
    _validate_token(credentials)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_HOST}/api/v1/orchestrator/sessions/{session_id}/messages",
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.delete("/api/v1/sessions/{session_id}")
async def delete_session(
    session_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Delete a session and all related data (conversation history, activity logs)."""
    _validate_token(credentials)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(
            f"{ORCHESTRATOR_HOST}/api/v1/orchestrator/sessions/{session_id}",
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


# ── Chat via Channel abstraction ──────────────────────────────────────

@app.post("/api/v1/chat")
async def chat_via_channel(
    req: ChatRequest,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Chat endpoint routed through the IM channel abstraction layer.

    This is the recommended endpoint for frontend integration.
    Messages flow: WebChannel → MessageBus → ChannelManager → Agent Pod.
    """
    user_id = _validate_token(credentials)

    svc = get_channel_service()
    if not svc:
        raise HTTPException(status_code=503, detail="Channel service not initialized")

    web_channel = svc.get_channel("web")
    if not web_channel:
        raise HTTPException(status_code=503, detail="Web channel not available")

    from poc.gateway.channels.adapters.web import WebChannel
    assert isinstance(web_channel, WebChannel)

    result = await web_channel.handle_chat_request(
        user_id=user_id,
        message=req.message,
        session_id=req.session_id,
    )
    return result


# ── File Upload / Download proxy ──────────────────────────────────────

def _resolve_agent_endpoint(workspace_id: str) -> str:
    """Resolve agent endpoint from route table, raise 502 if not found."""
    route_table = _get_route_table()
    endpoint = route_table.get(workspace_id)
    if not endpoint:
        raise HTTPException(
            status_code=502,
            detail=f"No route for workspace {workspace_id}. Call /api/v1/workspaces/ensure first.",
        )
    return endpoint


@app.post("/workspaces/{workspace_id}/api/v1/files/upload")
async def proxy_file_upload(
    workspace_id: str,
    file: UploadFile = File(...),
    path: str = Query("", description="相對於 workspace 的目標目錄"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Proxy file upload to Agent Pod."""
    _validate_token(credentials)
    endpoint = _resolve_agent_endpoint(workspace_id)

    content = await file.read()
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(
                f"http://{endpoint}/api/v1/files/upload",
                files={"file": (file.filename, content, file.content_type or "application/octet-stream")},
                params={"path": path},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach agent pod")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/workspaces/{workspace_id}/api/v1/files/download")
async def proxy_file_download(
    workspace_id: str,
    path: str = Query(..., description="相對於 workspace 的檔案路徑"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Proxy file download from Agent Pod."""
    _validate_token(credentials)
    endpoint = _resolve_agent_endpoint(workspace_id)

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.get(
                f"http://{endpoint}/api/v1/files/download",
                params={"path": path},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach agent pod")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/octet-stream"),
        headers={"Content-Disposition": resp.headers.get("content-disposition", "")},
    )


@app.delete("/workspaces/{workspace_id}/api/v1/files/delete")
async def proxy_file_delete(
    workspace_id: str,
    path: str = Query(..., description="相對於 workspace 的檔案或目錄路徑"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Proxy file/directory delete to Agent Pod."""
    _validate_token(credentials)
    endpoint = _resolve_agent_endpoint(workspace_id)

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.delete(
                f"http://{endpoint}/api/v1/files/delete",
                params={"path": path},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach agent pod")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.post("/workspaces/{workspace_id}/api/v1/files/mkdir")
async def proxy_file_mkdir(
    workspace_id: str,
    path: str = Query(..., description="相對於 workspace 的目錄路徑"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Proxy directory creation to Agent Pod."""
    _validate_token(credentials)
    endpoint = _resolve_agent_endpoint(workspace_id)

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                f"http://{endpoint}/api/v1/files/mkdir",
                params={"path": path},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach agent pod")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/workspaces/{workspace_id}/api/v1/files/list")
async def proxy_file_list(
    workspace_id: str,
    path: str = Query("", description="相對於 workspace 的目錄路徑"),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Proxy file list from Agent Pod."""
    _validate_token(credentials)
    endpoint = _resolve_agent_endpoint(workspace_id)

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"http://{endpoint}/api/v1/files/list",
                params={"path": path},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach agent pod")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


# ── Direct proxy to Agent Pod ─────────────────────────────────────────

@app.post("/workspaces/{workspace_id}/{path:path}")
async def proxy_to_agent(
    workspace_id: str,
    path: str,
    body: dict,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Direct proxy to Agent Pod via K8s internal service.

    Supports all agent endpoints:
    - `mcp/execute` — MCP file operations
    - `api/v1/chat` — Direct chat (bypasses channel layer)
    - `api/v1/chat/stream` — Streaming chat
    """
    user_id = _validate_token(credentials)

    # Ensure session exists in DB for chat requests
    session_id = request.headers.get("X-Session-Id", "") or body.get("session_id", "")
    if session_id and "chat" in path:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(
                    f"{ORCHESTRATOR_HOST}/api/v1/orchestrator/ensure",
                    json={"user_id": user_id, "session_id": session_id},
                )
            except Exception:
                logger.warning("Failed to ensure session for %s/%s", user_id, session_id)
    elif session_id:
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                await client.post(
                    f"{ORCHESTRATOR_HOST}/api/v1/orchestrator/mark-activity",
                    json={"user_id": user_id, "session_id": session_id},
                )
            except Exception:
                logger.warning("Failed to mark activity for %s", user_id)

    # resolve agent endpoint
    route_table = _get_route_table()
    endpoint = route_table.get(workspace_id)
    if not endpoint:
        raise HTTPException(
            status_code=502,
            detail=f"No route for workspace {workspace_id}. Call /api/v1/workspaces/ensure first.",
        )

    agent_url = f"http://{endpoint}/{path}"
    raw_body = _json.dumps(body).encode("utf-8")

    # SSE streaming proxy for chat/stream endpoint
    if path.endswith("chat/stream"):
        async def stream_proxy():
            stream_timeout = httpx.Timeout(180.0, connect=10.0)
            async with httpx.AsyncClient(timeout=stream_timeout) as client:
                async with client.stream(
                    "POST",
                    url=agent_url,
                    content=raw_body,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk

        return StreamingResponse(
            stream_proxy(),
            media_type="text/event-stream",
        )

    # Normal proxy for other endpoints
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(
                url=agent_url,
                content=raw_body,
                headers={"Content-Type": request.headers.get("content-type", "application/json")},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail=f"Cannot reach agent at {agent_url}")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


# ── Admin ─────────────────────────────────────────────────────────────

@app.get("/api/v1/admin/channels")
async def channel_status(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get status of all IM channels (Gateway-local data)."""
    _validate_admin_token(credentials)
    svc = get_channel_service()
    if not svc:
        return {"status": "not_initialized"}
    return svc.get_status()


@app.api_route("/api/v1/admin/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_to_admin(
    path: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """Proxy all /api/v1/admin/* requests to Admin Service.

    Gateway validates admin token, then forwards with the same token
    so Admin Service can also validate independently.
    """
    _validate_admin_token(credentials)

    target_url = f"{ADMIN_SERVICE_HOST}/api/v1/admin/{path}"
    method = request.method
    headers = {
        "Authorization": f"Bearer {credentials.credentials}",
        "Content-Type": request.headers.get("content-type", "application/json"),
    }

    # Forward query params
    params = dict(request.query_params)

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            if method in ("POST", "PUT"):
                body = await request.body()
                resp = await client.request(
                    method, target_url, content=body, headers=headers, params=params,
                )
            else:
                resp = await client.request(
                    method, target_url, headers=headers, params=params,
                )
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot reach Admin Service")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
