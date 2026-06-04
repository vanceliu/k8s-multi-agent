"""Orchestrator service — workspace lifecycle management."""

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from poc.db.models import ActivityLog, PodState, Session, User, Workspace, WorkspaceMember
from poc.orchestrator.k8s_client import K8sClient
from poc.utils.config import ADMIN_STATIC_TOKEN, AGENT_DISPLAY_MODE, DEFAULT_PVC_SIZE_GB, STORAGE_SERVICE_HOST

logger = logging.getLogger("orchestrator.service")

# ── File detection (shared logic with agent http_server) ──────────
_FILE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".txt",
)
_FILE_PATTERN = re.compile(
    r"""(?:^|[\s'"/：:])"""
    r"""((?:[\w./-]+/)?"""
    r"""[\w.-]+"""
    r"""(?:"""
    + "|".join(re.escape(ext) for ext in _FILE_EXTENSIONS)
    + r"""))"""
    r"""(?=[\s'",;:）)\]}\n]|$)""",
    re.IGNORECASE,
)


def _extract_files(text: str, session_id: str | None = None) -> list[dict]:
    """Extract file info from tool output text."""
    raw = list(dict.fromkeys(_FILE_PATTERN.findall(text)))
    result = []
    for filepath in raw:
        # Strip absolute paths — find the `sessions/` segment if present
        sessions_idx = filepath.find("sessions/")
        if sessions_idx > 0:
            filepath = filepath[sessions_idx:]
        elif filepath.startswith("/"):
            # Unknown absolute path, skip
            continue

        if session_id and not filepath.startswith("sessions/"):
            filepath = f"sessions/{session_id}/{filepath}"
        ext = Path(filepath).suffix.lower()
        file_type = "image" if ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp") else "document"
        result.append({"path": filepath, "type": file_type, "name": Path(filepath).name})
    return result

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@host.docker.internal:5432/claw_data",
)

# Convert SQLAlchemy-style URL to psycopg-style for checkpointer
_PSYCOPG_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


class OrchestratorService:
    def __init__(self):
        self.k8s = K8sClient()
        self._checkpointer = None
        self._pg_conn = None

    # ── helpers (workspace-centric naming) ──────────────────────────

    @staticmethod
    def _pod_name(workspace_id: str) -> str:
        return f"pod-{workspace_id}"

    @staticmethod
    def _service_name(workspace_id: str) -> str:
        return f"svc-{workspace_id}"

    @staticmethod
    def _pvc_name(workspace_id: str) -> str:
        return f"pvc-{workspace_id}"

    @staticmethod
    def _workspace_id(user_id: str) -> str:
        return f"ws-{user_id}"

    async def _ensure_pvc(
        self, workspace_id: str, pvc_name: str, size_gb: int,
    ) -> None:
        """Delegate PVC creation to Storage Service via HTTP."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{STORAGE_SERVICE_HOST}/api/v1/workspaces/storage/ensure",
                    json={
                        "workspace_id": workspace_id,
                        "size_gb": size_gb,
                    },
                    headers={"Authorization": f"Bearer {ADMIN_STATIC_TOKEN}"},
                )
                # 400 = already exists, which is fine
                if resp.status_code not in (200, 400):
                    logger.error("Storage Service create failed: %s %s", resp.status_code, resp.text)
        except httpx.ConnectError:
            # Fallback: Storage Service unavailable, skip (PVC may already exist)
            logger.warning("Storage Service unreachable, skipping PVC ensure for %s", pvc_name)

    async def _get_shared_workspaces(
        self, db: AsyncSession, user_id: str,
    ) -> list[dict]:
        """Get all non-personal workspaces the user has access to via workspace_members."""
        from poc.db.models import WorkspaceMember
        result = await db.execute(
            select(WorkspaceMember.workspace_id, WorkspaceMember.role)
            .where(WorkspaceMember.user_id == user_id)
        )
        memberships = result.all()

        shared = []
        for ws_id, role in memberships:
            # Skip personal workspace (already mounted as primary)
            ws_result = await db.execute(
                select(Workspace.workspace_type)
                .where(Workspace.workspace_id == ws_id)
            )
            ws_type = ws_result.scalar_one_or_none()
            if ws_type and ws_type != "personal":
                shared.append({
                    "workspace_id": ws_id,
                    "pvc_name": self._pvc_name(ws_id),
                    "role": role,
                })
        return shared

    # ── ensure user exists ────────────────────────────────────────────

    async def _ensure_user(self, db: AsyncSession, user_id: str) -> User:
        result = await db.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            user = User(
                user_id=user_id,
                username=user_id,
                email=f"{user_id}@poc.local",
                auth_provider="static_token",
            )
            db.add(user)
            await db.flush()
        return user

    # ── core: ensure_session_workspace ────────────────────────────────

    async def ensure_session_workspace(
        self,
        db: AsyncSession,
        user_id: str,
        session_id: str | None = None,
        resource_tier: str = "standard",
    ) -> dict:
        await self._ensure_user(db, user_id)

        session_id = session_id or str(uuid.uuid4())
        workspace_id = self._workspace_id(user_id)
        pvc_name = self._pvc_name(workspace_id)
        pod_name = self._pod_name(workspace_id)
        service_name = self._service_name(workspace_id)

        # 1) workspace record (personal workspace)
        result = await db.execute(
            select(Workspace)
            .where(Workspace.user_id == user_id)
            .where(Workspace.workspace_type == "personal")
        )
        workspace = result.scalar_one_or_none()
        if not workspace:
            workspace = Workspace(
                workspace_id=workspace_id,
                user_id=user_id,
                workspace_type="personal",
                pvc_name=pvc_name,
                pvc_size_gb=DEFAULT_PVC_SIZE_GB,
                resource_tier=resource_tier,
                status="active",
            )
            db.add(workspace)
            await db.flush()
            # Add owner to workspace_members
            db.add(WorkspaceMember(
                workspace_id=workspace_id,
                user_id=user_id,
                role="owner",
                granted_by=user_id,
            ))
            await db.flush()

        # 2) PVC — delegate to Storage Service
        await self._ensure_pvc(workspace_id, pvc_name, workspace.pvc_size_gb)

        # 2b) Shared workspaces (group) — query workspace_members
        shared_workspaces = await self._get_shared_workspaces(db, user_id)

        # 3) Pod + Service
        pod_created = False
        if not self.k8s.pod_exists(pod_name):
            self.k8s.create_pod(workspace_id, pod_name, pvc_name, shared_workspaces)
            pod_created = True
        if not self.k8s.service_exists(service_name):
            self.k8s.create_service(workspace_id, service_name)

        # 4) wait for Pod readiness (non-blocking for existing pods)
        pod_ready = True
        if pod_created:
            pod_ready = self.k8s.wait_for_pod_ready(pod_name)

        # 5) session record
        result = await db.execute(select(Session).where(Session.session_id == session_id))
        session = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if not session:
            session = Session(
                session_id=session_id,
                user_id=user_id,
                workspace_id=workspace_id,
                pod_name=pod_name,
                service_name=service_name,
                pod_status="running" if pod_ready else "pending",
                last_active_at=now,
            )
            db.add(session)
            await db.flush()
        else:
            session.pod_name = pod_name
            session.service_name = service_name
            session.pod_status = "running" if pod_ready else "pending"
            session.last_active_at = now

        # 6) update workspace status
        workspace.status = "active"
        workspace.updated_at = now

        # 7) pod_states
        if pod_created:
            result2 = await db.execute(select(PodState).where(PodState.pod_name == pod_name))
            ps = result2.scalar_one_or_none()
            if ps:
                ps.desired_state = "running"
                ps.actual_state = "pending" if not pod_ready else "running"
                ps.last_state_update_at = now
                ps.sync_status = "synced"
            else:
                db.add(PodState(
                    pod_name=pod_name,
                    workspace_id=workspace_id, desired_state="running",
                    actual_state="pending" if not pod_ready else "running",
                    last_state_update_at=now,
                ))

        # 8) activity log
        db.add(ActivityLog(
            activity_id=str(uuid.uuid4()),
            user_id=user_id, session_id=session_id,
            workspace_id=workspace_id,
            activity_type="workspace_ensured",
            action="create" if pod_created else "reuse",
            resource_type="pod", resource_id=pod_name,
            timestamp=now,
        ))

        await db.commit()

        return {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "session_id": session_id,
            "pod_name": pod_name,
            "service_name": service_name,
            "service_endpoint": f"{service_name}.{self.k8s.ns}.svc.cluster.local",
            "status": "ready" if pod_ready else "pending",
        }

    # ── mark_activity ─────────────────────────────────────────────────

    async def mark_activity(
        self, db: AsyncSession, user_id: str, session_id: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        await db.execute(
            update(Session)
            .where(Session.session_id == session_id)
            .values(last_active_at=now)
        )
        await db.commit()

    # ── reap_workspace (called by Agent self-report) ──────────────────

    async def reap_workspace(
        self, db: AsyncSession, workspace_id: str, reason: str = "agent_idle_self_report",
    ) -> dict:
        """Reap a workspace. Called when Agent Pod reports idle."""
        pod_name = self._pod_name(workspace_id)
        service_name = self._service_name(workspace_id)
        now = datetime.now(timezone.utc)

        # Look up user_id from workspace
        ws_result = await db.execute(
            select(Workspace.user_id).where(Workspace.workspace_id == workspace_id)
        )
        user_id = ws_result.scalar_one_or_none() or "system"

        # Delete K8s resources (Pod + Service, keep PVC for data persistence)
        self.k8s.delete_pod(pod_name)
        self.k8s.delete_service(service_name)

        # Update DB
        await db.execute(
            update(Session)
            .where(Session.workspace_id == workspace_id)
            .where(Session.pod_status == "running")
            .values(pod_status="terminated", terminated_at=now)
        )
        await db.execute(
            update(Workspace)
            .where(Workspace.workspace_id == workspace_id)
            .values(status="idle", last_reap_at=now, updated_at=now)
        )

        db.add(ActivityLog(
            activity_id=str(uuid.uuid4()),
            user_id=user_id,
            workspace_id=workspace_id, activity_type="pod_reaped",
            action=reason, resource_type="pod",
            resource_id=pod_name, timestamp=now,
        ))

        await db.commit()
        logger.info("Reaped workspace %s (reason=%s)", workspace_id, reason)

        return {"workspace_id": workspace_id, "pod_name": pod_name, "reason": reason}

    # ── reap_stale_pods (background scanner for unresponsive pods) ─────

    async def reap_stale_pods(
        self, db: AsyncSession, timeout_minutes: int = 720, force_all: bool = False,
    ) -> list[dict]:
        """Scan for pods that are idle or unresponsive.

        Args:
            timeout_minutes: Reap pods idle longer than this (default 720 = 12h).
            force_all: If True, reap ALL running pods regardless of idle time.
        """
        from datetime import timedelta

        if force_all:
            cutoff = datetime.now(timezone.utc)
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)

        result = await db.execute(
            select(Session.workspace_id, Session.pod_name, Session.service_name)
            .where(Session.pod_status == "running")
            .where(Session.last_active_at < cutoff)
        )
        stale_rows = result.all()

        reaped = []
        checked_workspaces: set[str] = set()

        for workspace_id, pod_name, service_name in stale_rows:
            if workspace_id in checked_workspaces:
                continue
            checked_workspaces.add(workspace_id)

            # For scheduled scanner: also check if pod is actually healthy
            if not force_all:
                pod_running = self.k8s.pod_is_running(pod_name)
                if pod_running:
                    continue

            result_dict = await self.reap_workspace(
                db, workspace_id,
                reason="admin_force_reap" if force_all else "stale_pod_scanner",
            )
            reaped.append(result_dict)

        return reaped

    async def _fix_orphan_active_workspaces(self, db: AsyncSession) -> int:
        """Fix workspaces stuck in 'active' with no running session and no K8s Pod.

        Single DB query + single K8s list call, O(1) API calls regardless of workspace count.
        Returns the number of workspaces fixed.
        """
        from sqlalchemy import exists, and_

        # Find workspaces that are 'active' but have no session with pod_status='running'
        has_running_session = (
            exists()
            .where(and_(
                Session.workspace_id == Workspace.workspace_id,
                Session.pod_status == "running",
            ))
        )
        result = await db.execute(
            select(Workspace.workspace_id)
            .where(Workspace.status == "active")
            .where(~has_running_session)
        )
        candidates = [row[0] for row in result.all()]

        if not candidates:
            return 0

        # Single K8s API call to get all running pods
        running_pods = self.k8s.list_pod_names()

        now = datetime.now(timezone.utc)
        fixed = 0
        for ws_id in candidates:
            pod_name = self._pod_name(ws_id)
            if pod_name not in running_pods:
                await db.execute(
                    update(Workspace)
                    .where(Workspace.workspace_id == ws_id)
                    .values(status="idle", updated_at=now)
                )
                fixed += 1

        if fixed:
            await db.commit()
            logger.info("Fixed %d orphan active workspace(s): %s → idle", fixed,
                        [ws for ws in candidates if self._pod_name(ws) not in running_pods])

        return fixed

    # ── get workspace info ────────────────────────────────────────────

    async def get_workspace(self, db: AsyncSession, workspace_id: str) -> dict | None:
        result = await db.execute(select(Workspace).where(Workspace.workspace_id == workspace_id))
        ws = result.scalar_one_or_none()
        if not ws:
            return None

        sessions_result = await db.execute(
            select(Session).where(Session.workspace_id == workspace_id)
        )
        sessions = sessions_result.scalars().all()

        return {
            "workspace_id": ws.workspace_id,
            "user_id": ws.user_id,
            "status": ws.status,
            "sessions": [
                {
                    "session_id": s.session_id,
                    "pod_status": s.pod_status,
                    "last_active_at": s.last_active_at.isoformat() if s.last_active_at else None,
                }
                for s in sessions
            ],
        }

    # ── get user sessions ─────────────────────────────────────────

    async def get_user_sessions(self, db: AsyncSession, user_id: str) -> list[dict]:
        """List all sessions for a user."""
        result = await db.execute(
            select(Session)
            .where(Session.user_id == user_id)
            .order_by(Session.created_at.desc())
        )
        sessions = result.scalars().all()

        return [
            {
                "session_id": s.session_id,
                "workspace_id": s.workspace_id,
                "pod_status": s.pod_status,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "last_active_at": s.last_active_at.isoformat() if s.last_active_at else None,
                "terminated_at": s.terminated_at.isoformat() if s.terminated_at else None,
            }
            for s in sessions
        ]

    # ── get session activity history ──────────────────────────────

    async def get_session_history(
        self, db: AsyncSession, session_id: str, limit: int = 50,
    ) -> dict | None:
        """Get session details + activity log history."""
        result = await db.execute(
            select(Session).where(Session.session_id == session_id)
        )
        session = result.scalar_one_or_none()
        if not session:
            return None

        logs_result = await db.execute(
            select(ActivityLog)
            .where(ActivityLog.session_id == session_id)
            .order_by(ActivityLog.timestamp.desc())
            .limit(limit)
        )
        logs = logs_result.scalars().all()

        return {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "workspace_id": session.workspace_id,
            "pod_name": session.pod_name,
            "pod_status": session.pod_status,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "last_active_at": session.last_active_at.isoformat() if session.last_active_at else None,
            "terminated_at": session.terminated_at.isoformat() if session.terminated_at else None,
            "activity_logs": [
                {
                    "activity_id": log.activity_id,
                    "activity_type": log.activity_type,
                    "action": log.action,
                    "resource_type": log.resource_type,
                    "resource_id": log.resource_id,
                    "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                    "metadata": log.extra_metadata,
                }
                for log in logs
            ],
        }

    # ── delete session (with full cleanup) ────────────────────────

    async def delete_session(self, db: AsyncSession, session_id: str) -> dict | None:
        """Delete a session and all related data:
        - Session folder on PVC (via K8s cleanup Job)
        - LangGraph checkpoint data (conversation history)
        - Activity logs
        - Session record
        """
        result = await db.execute(
            select(Session).where(Session.session_id == session_id)
        )
        session = result.scalar_one_or_none()
        if not session:
            return None

        user_id = session.user_id
        workspace_id = session.workspace_id

        # 0. Delete session folder on PVC via K8s cleanup Job
        pvc_name = self._pvc_name(workspace_id)
        try:
            job_name = self.k8s.create_cleanup_job(workspace_id, pvc_name, session_id)
            logger.info("Cleanup Job %s created for session %s", job_name, session_id)
        except Exception:
            logger.exception("Failed to create cleanup Job for session %s", session_id)

        # 1. Delete LangGraph checkpoint data (thread_id = session_id)
        checkpoint_tables = [
            "checkpoint_writes",
            "checkpoint_blobs",
            "checkpoints",
        ]
        for table in checkpoint_tables:
            try:
                await db.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = :tid"),
                    {"tid": session_id},
                )
            except Exception:
                # Table may not exist if checkpointer was never used
                logger.debug("Skipping checkpoint cleanup for table %s", table)

        # 2. Delete activity logs for this session
        await db.execute(
            delete(ActivityLog).where(ActivityLog.session_id == session_id)
        )

        # 3. Delete session record
        await db.execute(
            delete(Session).where(Session.session_id == session_id)
        )

        await db.commit()
        logger.info("Deleted session %s (user=%s, workspace=%s)", session_id, user_id, workspace_id)

        return {
            "session_id": session_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "deleted": True,
        }

    # ── checkpointer (lazy init) ─────────────────────────────────

    async def _ensure_checkpointer(self):
        """Lazy-init AsyncPostgresSaver for reading conversation history."""
        if self._checkpointer is not None:
            return self._checkpointer

        try:
            from psycopg import AsyncConnection
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            conn = await AsyncConnection.connect(
                _PSYCOPG_URL, autocommit=True, prepare_threshold=0,
            )
            checkpointer = AsyncPostgresSaver(conn)
            self._checkpointer = checkpointer
            self._pg_conn = conn
            logger.info("Orchestrator checkpointer initialized (read-only, for messages)")
            return checkpointer
        except Exception as e:
            logger.warning("Failed to init checkpointer: %s", e)
            return None

    # ── get session messages (from checkpointer) ─────────────────

    async def get_session_messages(self, session_id: str) -> list[dict]:
        """Read conversation messages directly from PostgreSQL checkpointer.

        Filtering behavior depends on AGENT_DISPLAY_MODE:
          - "normal": only human + AI content (no tool_call/tool_result)
          - "full_history": human + AI content + tool_call + tool_result + files

        Both modes always filter out:
          - system messages (session context prompt)
          - handoff messages (transfer_* / transfer_back_to_*)
          - sub-agent internal AI reasoning
        """
        checkpointer = await self._ensure_checkpointer()
        if checkpointer is None:
            return []

        show_tools = AGENT_DISPLAY_MODE == "full_history"

        # Sub-agent node names to filter out
        _SUB_AGENT_NAMES = {"research_agent", "code_agent"}

        try:
            config = {"configurable": {"thread_id": session_id}}
            checkpoint_tuple = await checkpointer.aget_tuple(config)
            if not checkpoint_tuple or not checkpoint_tuple.checkpoint:
                return []

            channel_values = checkpoint_tuple.checkpoint.get("channel_values", {})
            messages = channel_values.get("messages", [])

            result = []
            for msg in messages:
                role = getattr(msg, "type", "unknown")
                content = getattr(msg, "content", "")
                msg_name = getattr(msg, "name", None)

                # Always skip system messages
                if role == "system":
                    continue

                # Always skip empty tool results
                if role == "tool" and not content:
                    continue

                # Always skip sub-agent AI messages (internal reasoning)
                if role == "ai" and msg_name in _SUB_AGENT_NAMES:
                    continue

                # Always skip handoff tool messages (transfer_*)
                if role == "tool" and msg_name and msg_name.startswith("transfer_"):
                    continue

                # Handle AI messages with tool_calls
                tool_calls = getattr(msg, "tool_calls", None)
                if role == "ai" and tool_calls:
                    # Filter out handoff tool calls
                    real_calls = [
                        tc for tc in tool_calls
                        if not tc.get("name", "").startswith("transfer_")
                    ]
                    # AI message with only handoff calls and no content — skip entirely
                    if not real_calls and not content:
                        continue
                    tool_calls = real_calls if real_calls else None

                # In normal mode, skip tool-related messages but extract files
                if not show_tools:
                    if role == "tool":
                        # Still extract files and attach to the last AI entry
                        files = _extract_files(content, session_id=session_id)
                        if files and result:
                            for prev in reversed(result):
                                if prev["role"] == "ai":
                                    prev.setdefault("files", []).extend(files)
                                    break
                        continue
                    # Strip tool_calls from AI messages (keep content only)
                    tool_calls = None

                entry = {
                    "role": role,
                    "content": content,
                }
                if tool_calls:
                    entry["tool_calls"] = [
                        {"name": tc["name"], "args": tc.get("args", {})}
                        for tc in tool_calls
                    ]
                if role == "tool" and show_tools:
                    entry["tool_name"] = msg_name
                    files = _extract_files(content, session_id=session_id)
                    if files:
                        entry["files"] = files

                # Deduplicate consecutive identical human messages
                if role == "human" and result:
                    last = result[-1]
                    if last["role"] == "human" and last["content"] == content:
                        continue

                result.append(entry)
            return result
        except Exception as e:
            logger.warning("Failed to get messages for session %s: %s", session_id, e)
            return []

    # ── admin: list all workspaces (paginated) ───────────────────

    async def list_all_workspaces(
        self,
        db: AsyncSession,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
    ) -> dict:
        """List all workspaces with session info, for admin use."""
        query = select(Workspace)
        if status:
            query = query.where(Workspace.status == status)
        query = query.order_by(Workspace.updated_at.desc()).offset(offset).limit(limit)

        result = await db.execute(query)
        workspaces = result.scalars().all()

        # Count total
        from sqlalchemy import func
        count_query = select(func.count(Workspace.id))
        if status:
            count_query = count_query.where(Workspace.status == status)
        total = (await db.execute(count_query)).scalar() or 0

        items = []
        for ws in workspaces:
            # Get active session count
            sess_result = await db.execute(
                select(func.count(Session.id))
                .where(Session.workspace_id == ws.workspace_id)
                .where(Session.pod_status == "running")
            )
            active_sessions = sess_result.scalar() or 0

            items.append({
                "workspace_id": ws.workspace_id,
                "display_name": ws.display_name,
                "user_id": ws.user_id,
                "workspace_type": ws.workspace_type,
                "resource_tier": ws.resource_tier,
                "status": ws.status,
                "active_sessions": active_sessions,
                "created_at": ws.created_at.isoformat() if ws.created_at else None,
                "updated_at": ws.updated_at.isoformat() if ws.updated_at else None,
                "last_reap_at": ws.last_reap_at.isoformat() if ws.last_reap_at else None,
            })

        return {"total": total, "workspaces": items, "limit": limit, "offset": offset}

    # ── admin: list all users (paginated) ────────────────────────

    async def list_all_users(
        self,
        db: AsyncSession,
        limit: int = 50,
        offset: int = 0,
        is_active: bool | None = None,
    ) -> dict:
        """List all users, for admin use."""
        from sqlalchemy import func

        query = select(User)
        if is_active is not None:
            query = query.where(User.is_active == is_active)
        query = query.order_by(User.created_at.desc()).offset(offset).limit(limit)

        result = await db.execute(query)
        users = result.scalars().all()

        count_query = select(func.count(User.id))
        if is_active is not None:
            count_query = count_query.where(User.is_active == is_active)
        total = (await db.execute(count_query)).scalar() or 0

        items = []
        for u in users:
            items.append({
                "user_id": u.user_id,
                "username": u.username,
                "email": u.email,
                "auth_provider": u.auth_provider,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "updated_at": u.updated_at.isoformat() if u.updated_at else None,
            })

        return {"total": total, "users": items, "limit": limit, "offset": offset}

    # ── admin: get user detail ───────────────────────────────────

    async def get_user(self, db: AsyncSession, user_id: str) -> dict | None:
        result = await db.execute(select(User).where(User.user_id == user_id))
        u = result.scalar_one_or_none()
        if not u:
            return None

        # Get workspace info
        ws_result = await db.execute(select(Workspace).where(Workspace.user_id == user_id))
        ws = ws_result.scalar_one_or_none()

        # Get session count
        from sqlalchemy import func
        sess_count = (await db.execute(
            select(func.count(Session.id)).where(Session.user_id == user_id)
        )).scalar() or 0

        return {
            "user_id": u.user_id,
            "username": u.username,
            "email": u.email,
            "auth_provider": u.auth_provider,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "updated_at": u.updated_at.isoformat() if u.updated_at else None,
            "workspace": {
                "workspace_id": ws.workspace_id,
                "status": ws.status,
            } if ws else None,
            "total_sessions": sess_count,
        }

    # ── admin: update user active status ─────────────────────────

    async def update_user_active(
        self, db: AsyncSession, user_id: str, is_active: bool,
    ) -> dict | None:
        result = await db.execute(select(User).where(User.user_id == user_id))
        u = result.scalar_one_or_none()
        if not u:
            return None

        u.is_active = is_active
        await db.commit()
        return {"user_id": u.user_id, "is_active": u.is_active}

    # ── admin: pods status overview ──────────────────────────────

    async def get_pods_status(self, db: AsyncSession) -> dict:
        """Get overview of all pod states — queries K8s for actual running pods."""
        from kubernetes.client.exceptions import ApiException

        # 1. Get actual pods from K8s
        running_pods = []
        pending_pods = []
        try:
            pods = self.k8s.core.list_namespaced_pod(
                self.k8s.ns,
                label_selector="component=agent,app=k8s-agent-platform",
            )
            for pod in pods.items:
                phase = pod.status.phase
                workspace_id = pod.metadata.labels.get("workspace_id", "")
                pod_info = {
                    "pod_name": pod.metadata.name,
                    "workspace_id": workspace_id,
                    "phase": phase,
                    "created_at": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
                }

                # Enrich with DB info (owner user_id, active sessions)
                ws_result = await db.execute(
                    select(Workspace.user_id).where(Workspace.workspace_id == workspace_id)
                )
                user_id = ws_result.scalar_one_or_none() or ""
                pod_info["user_id"] = user_id

                from sqlalchemy import func
                active_sessions = (await db.execute(
                    select(func.count(Session.id))
                    .where(Session.workspace_id == workspace_id)
                    .where(Session.pod_status == "running")
                )).scalar() or 0
                pod_info["active_sessions"] = active_sessions

                # Latest activity
                latest = await db.execute(
                    select(Session.last_active_at)
                    .where(Session.workspace_id == workspace_id)
                    .order_by(Session.last_active_at.desc())
                    .limit(1)
                )
                last_active = latest.scalar_one_or_none()
                pod_info["last_active_at"] = last_active.isoformat() if last_active else None

                if phase == "Running":
                    running_pods.append(pod_info)
                elif phase == "Pending":
                    pending_pods.append(pod_info)
        except ApiException as e:
            logger.warning("Failed to list pods from K8s: %s", e)

        return {
            "summary": {
                "running": len(running_pods),
                "pending": len(pending_pods),
            },
            "running_pods": running_pods,
            "pending_pods": pending_pods,
        }

    # ── admin: workspace members ─────────────────────────────────

    async def list_workspace_members(
        self, db: AsyncSession, workspace_id: str,
    ) -> list[dict]:
        """List all members of a workspace."""
        from poc.db.models import WorkspaceMember
        result = await db.execute(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
        )
        members = result.scalars().all()
        return [
            {
                "workspace_id": m.workspace_id,
                "user_id": m.user_id,
                "role": m.role,
                "granted_by": m.granted_by,
                "granted_at": m.granted_at.isoformat() if m.granted_at else None,
            }
            for m in members
        ]

    async def add_workspace_member(
        self,
        db: AsyncSession,
        workspace_id: str,
        user_id: str,
        role: str = "member",
        granted_by: str | None = None,
    ) -> dict:
        """Add a member to a workspace."""
        from poc.db.models import WorkspaceMember

        # Check workspace exists
        ws = await db.execute(select(Workspace).where(Workspace.workspace_id == workspace_id))
        if not ws.scalar_one_or_none():
            raise ValueError(f"Workspace {workspace_id} not found")

        # Check user exists
        u = await db.execute(select(User).where(User.user_id == user_id))
        if not u.scalar_one_or_none():
            raise ValueError(f"User {user_id} not found")

        # Check if already a member
        existing = await db.execute(
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace_id)
            .where(WorkspaceMember.user_id == user_id)
        )
        if existing.scalar_one_or_none():
            raise ValueError(f"User {user_id} is already a member of {workspace_id}")

        member = WorkspaceMember(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            granted_by=granted_by,
        )
        db.add(member)
        await db.commit()
        return {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "role": role,
            "granted_by": granted_by,
        }

    async def remove_workspace_member(
        self, db: AsyncSession, workspace_id: str, user_id: str,
    ) -> dict:
        """Remove a member from a workspace. Cannot remove owner."""
        from poc.db.models import WorkspaceMember

        result = await db.execute(
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace_id)
            .where(WorkspaceMember.user_id == user_id)
        )
        member = result.scalar_one_or_none()
        if not member:
            raise ValueError(f"Member {user_id} not found in {workspace_id}")
        if member.role == "owner":
            raise ValueError("Cannot remove workspace owner")

        await db.execute(
            delete(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace_id)
            .where(WorkspaceMember.user_id == user_id)
        )
        await db.commit()
        return {"workspace_id": workspace_id, "user_id": user_id, "removed": True}

    async def update_workspace_member_role(
        self, db: AsyncSession, workspace_id: str, user_id: str, role: str,
    ) -> dict:
        """Update a member's role. Cannot change owner role."""
        from poc.db.models import WorkspaceMember

        result = await db.execute(
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == workspace_id)
            .where(WorkspaceMember.user_id == user_id)
        )
        member = result.scalar_one_or_none()
        if not member:
            raise ValueError(f"Member {user_id} not found in {workspace_id}")
        if member.role == "owner":
            raise ValueError("Cannot change owner role")
        if role not in ("admin", "member", "readonly"):
            raise ValueError(f"Invalid role: {role}")

        member.role = role
        await db.commit()
        return {"workspace_id": workspace_id, "user_id": user_id, "role": role}

    async def delete_workspace(self, db: AsyncSession, workspace_id: str) -> dict:
        """Delete a workspace and all related DB records + K8s resources."""
        result = await db.execute(
            select(Workspace).where(Workspace.workspace_id == workspace_id)
        )
        ws = result.scalar_one_or_none()
        if not ws:
            raise ValueError(f"Workspace {workspace_id} not found")

        # Delete K8s Pod + Service (ignore if not found)
        pod_name = f"pod-{workspace_id}"
        svc_name = f"svc-{workspace_id}"
        try:
            self.k8s.delete_pod(pod_name)
            logger.info("Deleted Pod %s", pod_name)
        except Exception as e:
            logger.warning("Pod %s delete skipped: %s", pod_name, e)
        try:
            self.k8s.delete_service(svc_name)
            logger.info("Deleted Service %s", svc_name)
        except Exception as e:
            logger.warning("Service %s delete skipped: %s", svc_name, e)

        # Delete DB records (order: activity_logs, pod_states, sessions, members, workspace)
        await db.execute(
            delete(ActivityLog).where(ActivityLog.workspace_id == workspace_id)
        )
        await db.execute(
            delete(PodState).where(PodState.workspace_id == workspace_id)
        )
        await db.execute(
            delete(Session).where(Session.workspace_id == workspace_id)
        )
        await db.execute(
            delete(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
        )
        await db.execute(
            delete(Workspace).where(Workspace.workspace_id == workspace_id)
        )
        await db.commit()
        logger.info("Deleted workspace %s and all related records", workspace_id)
        return {"workspace_id": workspace_id, "deleted": True}
