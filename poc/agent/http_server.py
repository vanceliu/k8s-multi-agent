"""FastAPI HTTP layer for Agent container.

Mirrors doc 06 §4.2: /health, /readiness, /shutdown, /api/v1/chat,
/api/v1/chat/stream, /mcp/execute, /api/v1/agent/status,
/api/v1/files/upload, /api/v1/files/download, /api/v1/files/delete.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, UploadFile, File, Query, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from poc.agent.runtime import DeepAgentsRuntime

# ── File detection for SSE file events ────────────────────────────
_FILE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",  # images
    ".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".txt",  # documents
)
_FILE_PATTERN = re.compile(
    r"""(?:^|[\s'"/：:])"""              # boundary before filename (incl. fullwidth colon)
    r"""((?:[\w./-]+/)?"""              # optional path prefix
    r"""[\w.-]+"""                       # filename stem
    r"""(?:"""
    + "|".join(re.escape(ext) for ext in _FILE_EXTENSIONS)
    + r"""))"""                          # known extension
    r"""(?=[\s'",;:）)\]}\n]|$)""",     # boundary after filename (incl. fullwidth paren)
    re.IGNORECASE,
)


def _extract_files(text: str) -> list[str]:
    """Extract file paths with known extensions from tool output text."""
    return list(dict.fromkeys(_FILE_PATTERN.findall(text)))

# Global activity timestamp (read by idle checker in main.py)
_last_active_at: datetime = datetime.now(timezone.utc)


def get_last_active_at() -> datetime:
    return _last_active_at


def create_app(runtime: DeepAgentsRuntime) -> FastAPI:
    """Build FastAPI application wired to the given runtime."""
    app = FastAPI(title="Deep Agent Container", version="2.0")

    # ── Middleware: track activity ─────────────────────────────────

    @app.middleware("http")
    async def track_activity(request: Request, call_next):
        global _last_active_at
        skip_paths = {"/health", "/readiness", "/shutdown"}
        if request.url.path not in skip_paths:
            _last_active_at = datetime.now(timezone.utc)
        return await call_next(request)

    # ── K8s Probes ────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        return {
            "status": "healthy" if not runtime._shutting_down else "shutting_down",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workspace_id": runtime.workspace_id,
            "pod_name": runtime.pod_name,
            "idle_seconds": int(
                (datetime.now(timezone.utc) - _last_active_at).total_seconds()
            ),
        }

    @app.get("/readiness")
    async def readiness():
        if runtime._shutting_down:
            return {"status": "not_ready", "reason": "shutting_down"}

        backend_ok = runtime.backend is not None
        model_ok = runtime._model is not None
        return {
            "status": "ready" if backend_ok else "not_ready",
            "dependencies": {
                "backend": "initialized" if backend_ok else "missing",
                "agent": "initialized" if model_ok else "stub_mode",
            },
        }

    # ── Graceful shutdown (preStop hook) ──────────────────────────

    @app.get("/shutdown")
    async def shutdown_endpoint():
        """K8s preStop hook. Marks agent as shutting down, drains requests."""
        import asyncio

        runtime._shutting_down = True

        # Wait for in-flight requests (max 10s), excluding this request
        if runtime._active_requests > 0:
            try:
                await asyncio.wait_for(runtime._drain_event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass

        # Save state
        if runtime.backend:
            import json
            try:
                await runtime.backend.write_file(
                    "last_shutdown.json",
                    json.dumps({
                        "workspace_id": runtime.workspace_id,
                        "pod_name": runtime.pod_name,
                        "shutdown_at": datetime.now(timezone.utc).isoformat(),
                        "reason": "prestop_hook",
                    }),
                )
            except Exception:
                pass

        return {"status": "shutdown_complete"}

    # ── Chat API ──────────────────────────────────────────────────

    class ChatRequest(BaseModel):
        message: str
        session_id: str

    class ChatResponse(BaseModel):
        content: str
        session_id: str
        tool_calls: list[dict[str, Any]] = []

    @app.post("/api/v1/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest):
        """Synchronous chat endpoint."""
        result = await runtime.invoke(
            message=req.message,
            session_id=req.session_id,
        )
        return ChatResponse(**result)

    @app.post("/api/v1/chat/stream")
    async def chat_stream(req: ChatRequest):
        """Streaming chat endpoint (SSE) — token-by-token.

        Event types:
        - event: content      — AI response text chunk (token-by-token)
        - event: tool_call    — AI requesting a tool call
        - event: tool_result  — tool execution result
        - event: file         — file produced by tool (image, document, etc.)
        - event: error        — agent error (recursion limit, LLM failure, etc.)
        - data: [DONE]        — stream complete
        """
        import json

        async def generate():
            try:
                async for event in runtime.invoke_stream(
                    message=req.message,
                    session_id=req.session_id,
                ):
                    event_type = event.get("type", "")

                    if event_type == "content":
                        data = json.dumps({"content": event["content"]}, ensure_ascii=False)
                        yield f"event: content\ndata: {data}\n\n"

                    elif event_type == "thinking":
                        yield ": thinking\n\n"

                    elif event_type == "tool_call":
                        data = json.dumps(
                            {"name": event["name"], "args": event.get("args", {})},
                            ensure_ascii=False,
                        )
                        yield f"event: tool_call\ndata: {data}\n\n"

                    elif event_type == "tool_result":
                        data = json.dumps(
                            {"tool_name": event.get("tool_name", ""), "content": event.get("content", "")},
                            ensure_ascii=False,
                        )
                        yield f"event: tool_result\ndata: {data}\n\n"

                    # File detection from tool output (emitted in both normal and full_history modes)
                    if event_type == "tool_output_for_files":
                        files = _extract_files(event.get("content", ""))
                        ws_prefix = runtime.config.workspace_path.rstrip("/") + "/"
                        for filepath in files:
                            # Strip absolute workspace path to relative
                            if filepath.startswith(ws_prefix):
                                filepath = filepath[len(ws_prefix):]
                            elif filepath.startswith("/"):
                                # Unknown absolute path, skip
                                continue
                            if not filepath.startswith("sessions/"):
                                filepath = f"sessions/{req.session_id}/{filepath}"
                            ext = Path(filepath).suffix.lower()
                            if ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"):
                                file_type = "image"
                            else:
                                file_type = "document"
                            fdata = json.dumps(
                                {"path": filepath, "type": file_type, "name": Path(filepath).name},
                                ensure_ascii=False,
                            )
                            yield f"event: file\ndata: {fdata}\n\n"

                    elif event_type == "error":
                        data = json.dumps({"error": event["error"]}, ensure_ascii=False)
                        yield f"event: error\ndata: {data}\n\n"

                    elif event_type == "compaction_start":
                        data = json.dumps(
                            {
                                "session_id": event.get("session_id", req.session_id),
                                "before_tokens": event.get("before_tokens", 0),
                                "message_count": event.get("message_count", 0),
                            },
                            ensure_ascii=False,
                        )
                        yield f"event: compaction_start\ndata: {data}\n\n"

                    elif event_type == "compaction_memory_flush":
                        data = json.dumps(
                            {
                                "session_id": event.get("session_id", req.session_id),
                                "stored": event.get("stored", 0),
                            },
                            ensure_ascii=False,
                        )
                        yield f"event: compaction_memory_flush\ndata: {data}\n\n"

                    elif event_type == "compaction_complete":
                        data = json.dumps(
                            {
                                "session_id": event.get("session_id", req.session_id),
                                "before_tokens": event.get("before_tokens", 0),
                                "after_tokens": event.get("after_tokens", 0),
                                "message_count": event.get("message_count", 0),
                                "memories_flushed": event.get("memories_flushed", 0),
                            },
                            ensure_ascii=False,
                        )
                        yield f"event: compaction_complete\ndata: {data}\n\n"

            except Exception as e:
                err_name = type(e).__name__
                if "Recursion" in err_name:
                    err_msg = "Agent 處理步驟已達上限，已基於目前結果回覆。"
                else:
                    err_msg = f"Agent 處理發生錯誤：{err_name}"
                logger.warning("SSE stream error: %s: %s", err_name, e)
                data = json.dumps({"error": err_msg}, ensure_ascii=False)
                yield f"event: error\ndata: {data}\n\n"

            yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
        )

    # ── MCP compat (backward compatible) ──────────────────────────

    class MCPRequest(BaseModel):
        jsonrpc: str = "2.0"
        method: str
        params: dict[str, Any] = {}
        id: str | None = None

    class MCPResponse(BaseModel):
        jsonrpc: str = "2.0"
        result: Any | None = None
        error: dict[str, Any] | None = None
        id: str | None = None

    @app.post("/mcp/execute", response_model=MCPResponse)
    async def mcp_execute(req: MCPRequest):
        """MCP compat: maps MCP methods to agent chat or direct file ops."""
        if runtime._shutting_down:
            return MCPResponse(
                error={"code": -2, "message": "Agent is shutting down"},
                id=req.id,
            )

        try:
            # Direct file operations (bypass LLM for efficiency)
            if req.method == "list_files":
                path = req.params.get("path", "")
                items = await runtime.backend.ls(path)
                return MCPResponse(
                    result={"files": items, "total": len(items)},
                    id=req.id,
                )
            elif req.method == "read_file":
                path = req.params.get("path", "")
                content = await runtime.backend.read_file(path)
                size = len(content.encode("utf-8"))
                return MCPResponse(
                    result={"content": content, "size_bytes": size},
                    id=req.id,
                )
            elif req.method == "write_file":
                path = req.params.get("path", "")
                content = req.params.get("content", "")
                await runtime.backend.write_file(path, content)
                return MCPResponse(
                    result={"path": path, "bytes_written": len(content)},
                    id=req.id,
                )
            elif req.method == "execute_task":
                # Route through agent
                prompt = _mcp_to_prompt(req.method, req.params)
                result = await runtime.invoke(
                    message=prompt,
                    session_id=f"mcp-{req.id or 'default'}",
                )
                return MCPResponse(
                    result={"content": result["content"]},
                    id=req.id,
                )
            else:
                return MCPResponse(
                    error={"code": -1, "message": f"Unknown method: {req.method}"},
                    id=req.id,
                )
        except Exception as e:
            return MCPResponse(
                error={"code": -1, "message": str(e)},
                id=req.id,
            )

    def _mcp_to_prompt(method: str, params: dict) -> str:
        if method == "execute_task":
            return (
                f"請執行以下任務：{params.get('task_type', 'general')}。"
                f"參數：{params.get('parameters', {})}"
            )
        return str(params)

    # ── Agent status ──────────────────────────────────────────────

    @app.get("/api/v1/agent/status")
    async def agent_status():
        return {
            "workspace_id": runtime.workspace_id,
            "pod_name": runtime.pod_name,
            "model": runtime.config.model_name,
            "model_provider": runtime.config.model_provider,
            "agent_ready": runtime._model is not None,
            "backend_ready": runtime.backend is not None,
            "status": "ready" if not runtime._shutting_down else "shutting_down",
        }

    # ── File Upload / Download ───────────────────────────────────

    @app.post("/api/v1/files/upload")
    async def upload_file(
        file: UploadFile = File(...),
        path: str = Query("", description="相對於 workspace 的目標目錄，例如 data/ 或 sessions/sess-001/"),
    ):
        """Upload a file to the workspace PVC.

        The file is saved to {workspace_path}/{path}/{filename}.
        Path traversal outside workspace is blocked by LocalBackend.
        """
        if not runtime.backend:
            raise HTTPException(status_code=503, detail="Backend not initialized")

        target_path = f"{path.rstrip('/')}/{file.filename}".lstrip("/") if path else file.filename
        content = await file.read()

        try:
            await runtime.backend.write_file_bytes(target_path, content)
            return {
                "status": "uploaded",
                "path": target_path,
                "size_bytes": len(content),
                "filename": file.filename,
            }
        except (ValueError, PermissionError) as e:
            raise HTTPException(status_code=403, detail=str(e))

    @app.get("/api/v1/files/download")
    async def download_file(
        path: str = Query(..., description="相對於 workspace 的檔案路徑，例如 sessions/sess-001/report.pdf"),
    ):
        """Download a file from the workspace PVC.

        Path traversal outside workspace is blocked by LocalBackend._resolve().
        """
        if not runtime.backend:
            raise HTTPException(status_code=503, detail="Backend not initialized")

        try:
            resolved = runtime.backend._resolve(path)
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))

        if not resolved.is_file():
            raise HTTPException(status_code=404, detail=f"File not found: {path}")

        return FileResponse(
            path=str(resolved),
            filename=resolved.name,
            media_type="application/octet-stream",
        )

    @app.delete("/api/v1/files/delete")
    async def delete_path(
        path: str = Query(..., description="相對於 workspace 的檔案或目錄路徑"),
    ):
        """Delete a file or directory from the workspace PVC.

        Write permission and path traversal are checked by LocalBackend.
        """
        if not runtime.backend:
            raise HTTPException(status_code=503, detail="Backend not initialized")

        try:
            resolved = runtime.backend._resolve(path)
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))

        if not resolved.exists():
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")

        try:
            kind = await runtime.backend.delete_path(path)
            return {"status": "deleted", "path": path, "type": kind}
        except (ValueError, PermissionError) as e:
            raise HTTPException(status_code=403, detail=str(e))

    @app.post("/api/v1/files/mkdir")
    async def create_directory(
        path: str = Query(..., description="相對於 workspace 的目錄路徑，例如 sessions/sess-001/output"),
    ):
        """Create a directory in the workspace PVC.

        Write permission and path traversal are checked by LocalBackend.
        """
        if not runtime.backend:
            raise HTTPException(status_code=503, detail="Backend not initialized")

        try:
            await runtime.backend.create_directory(path)
            return {"status": "created", "path": path}
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))

    @app.get("/api/v1/files/list")
    async def list_files(
        path: str = Query("", description="相對於 workspace 的目錄路徑"),
    ):
        """List files in a workspace directory."""
        if not runtime.backend:
            raise HTTPException(status_code=503, detail="Backend not initialized")

        try:
            items = await runtime.backend.ls(path)
            return {"path": path, "files": items, "total": len(items)}
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))

    return app
