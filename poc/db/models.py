"""SQLAlchemy ORM models — 5 tables (workspace-centric) per design doc 03."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


def _utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(512))
    auth_provider = Column(String(50), default="local")
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    workspaces = relationship("Workspace", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    memberships = relationship("WorkspaceMember", back_populates="user", foreign_keys="WorkspaceMember.user_id", cascade="all, delete-orphan")


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(String(255), unique=True, nullable=False)
    user_id = Column(String(255), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True, index=True)
    workspace_type = Column(String(50), default="personal", nullable=False)  # personal / group
    pvc_name = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=True)
    pvc_size_gb = Column(Integer, default=1, nullable=False)
    resource_tier = Column(String(50), default="standard", nullable=False)
    status = Column(String(50), default="active", nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    last_reap_at = Column(DateTime(timezone=True))

    user = relationship("User", back_populates="workspaces")
    sessions = relationship("Session", back_populates="workspace", cascade="all, delete-orphan")
    members = relationship("WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan")


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(255), unique=True, nullable=False, index=True)
    user_id = Column(String(255), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    workspace_id = Column(String(255), ForeignKey("workspaces.workspace_id", ondelete="CASCADE"))
    pod_name = Column(String(255))
    service_name = Column(String(255))
    pod_status = Column(String(50))
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_active_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    terminated_at = Column(DateTime(timezone=True))

    user = relationship("User", back_populates="sessions")
    workspace = relationship("Workspace", back_populates="sessions")


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    activity_id = Column(String(255), unique=True, nullable=False)
    user_id = Column(String(255), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    session_id = Column(String(255), ForeignKey("sessions.session_id", ondelete="SET NULL"))
    workspace_id = Column(String(255), ForeignKey("workspaces.workspace_id", ondelete="SET NULL"))
    activity_type = Column(String(100), nullable=False, index=True)
    action = Column(String(255))
    resource_type = Column(String(100))
    resource_id = Column(String(255))
    http_method = Column(String(10))
    http_endpoint = Column(String(512))
    status_code = Column(Integer)
    error_message = Column(Text)
    extra_metadata = Column("metadata", JSON)
    timestamp = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)


class WorkspaceMember(Base):
    """工作區成員存取控制。個人 workspace 建立時自動加入 owner；group workspace 支援多成員。"""
    __tablename__ = "workspace_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(
        String(255),
        ForeignKey("workspaces.workspace_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        String(255),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(50), nullable=False, default="member")  # owner/admin/member/readonly
    granted_by = Column(String(255), ForeignKey("users.user_id"))
    granted_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    workspace = relationship("Workspace", back_populates="members")
    user = relationship("User", back_populates="memberships", foreign_keys=[user_id])

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
    )


class PodState(Base):
    __tablename__ = "pod_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pod_name = Column(String(255), unique=True, nullable=False)
    user_id = Column(String(255), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    workspace_id = Column(String(255), ForeignKey("workspaces.workspace_id", ondelete="CASCADE"))
    desired_state = Column(String(50), nullable=False)
    desired_resources_cpu = Column(String(20))
    desired_resources_memory = Column(String(20))
    actual_state = Column(String(50))
    restart_count = Column(Integer, default=0)
    last_error_message = Column(Text)
    last_state_update_at = Column(DateTime(timezone=True))
    sync_status = Column(String(50), default="synced", index=True)
