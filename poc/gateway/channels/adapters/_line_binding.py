"""LINE binding DB operations.

Provides functions for the LINEChannel adapter to resolve, create,
deactivate, reactivate, and remove user bindings.
"""

import logging
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select, update, delete, and_

from poc.db.models import UserBinding, BindingVerification, User
from poc.db.session import async_session

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Resolve / Query ──────────────────────────────────────────────


async def resolve_binding(platform: str, platform_uid: str) -> dict[str, Any] | None:
    """Look up user binding by platform UID. Returns dict or None."""
    async with async_session() as session:
        result = await session.execute(
            select(UserBinding).where(
                and_(
                    UserBinding.platform == platform,
                    UserBinding.platform_uid == platform_uid,
                )
            )
        )
        binding = result.scalar_one_or_none()
        if not binding:
            return None
        return {
            "user_id": binding.user_id,
            "platform": binding.platform,
            "platform_uid": binding.platform_uid,
            "display_name": binding.display_name,
            "status": binding.status,
        }


async def get_user_binding(user_id: str, platform: str) -> dict[str, Any] | None:
    """Look up user binding by user_id + platform."""
    async with async_session() as session:
        result = await session.execute(
            select(UserBinding).where(
                and_(
                    UserBinding.user_id == user_id,
                    UserBinding.platform == platform,
                )
            )
        )
        binding = result.scalar_one_or_none()
        if not binding:
            return None
        return {
            "user_id": binding.user_id,
            "platform": binding.platform,
            "platform_uid": binding.platform_uid,
            "display_name": binding.display_name,
            "status": binding.status,
            "bound_at": binding.bound_at.isoformat() if binding.bound_at else None,
        }


async def list_user_bindings(user_id: str) -> list[dict[str, Any]]:
    """List all bindings for a user."""
    async with async_session() as session:
        result = await session.execute(
            select(UserBinding).where(UserBinding.user_id == user_id)
        )
        bindings = result.scalars().all()
        return [
            {
                "platform": b.platform,
                "platform_uid": b.platform_uid,
                "display_name": b.display_name,
                "status": b.status,
                "bound_at": b.bound_at.isoformat() if b.bound_at else None,
            }
            for b in bindings
        ]


# ── Binding lifecycle ────────────────────────────────────────────


async def deactivate_binding(platform: str, platform_uid: str) -> bool:
    """Mark binding as inactive (unfollow event)."""
    async with async_session() as session:
        result = await session.execute(
            update(UserBinding)
            .where(
                and_(
                    UserBinding.platform == platform,
                    UserBinding.platform_uid == platform_uid,
                )
            )
            .values(status="inactive", updated_at=_utcnow())
        )
        await session.commit()
        return result.rowcount > 0


async def reactivate_binding(platform: str, platform_uid: str) -> bool:
    """Reactivate binding (follow event after previous unfollow)."""
    async with async_session() as session:
        result = await session.execute(
            update(UserBinding)
            .where(
                and_(
                    UserBinding.platform == platform,
                    UserBinding.platform_uid == platform_uid,
                    UserBinding.status == "inactive",
                )
            )
            .values(status="active", updated_at=_utcnow())
        )
        await session.commit()
        return result.rowcount > 0


async def unbind(platform: str, platform_uid: str) -> bool:
    """Remove binding entirely (user-initiated unbind)."""
    async with async_session() as session:
        result = await session.execute(
            delete(UserBinding).where(
                and_(
                    UserBinding.platform == platform,
                    UserBinding.platform_uid == platform_uid,
                )
            )
        )
        await session.commit()
        return result.rowcount > 0


async def unbind_by_user(user_id: str, platform: str) -> bool:
    """Remove binding by user_id + platform (Web-initiated unbind)."""
    async with async_session() as session:
        result = await session.execute(
            delete(UserBinding).where(
                and_(
                    UserBinding.user_id == user_id,
                    UserBinding.platform == platform,
                )
            )
        )
        await session.commit()
        return result.rowcount > 0


# ── Verification code flow ───────────────────────────────────────


def _generate_code() -> str:
    """Generate a 6-digit verification code."""
    return "".join(random.choices(string.digits, k=6))


async def create_verification(user_id: str, platform: str) -> dict[str, Any]:
    """Create a verification code for binding initiation.

    Returns {"code": "123456", "expires_in": 300} or {"error": "..."}.
    """
    async with async_session() as session:
        # Check if user already has an active binding for this platform
        existing = await session.execute(
            select(UserBinding).where(
                and_(
                    UserBinding.user_id == user_id,
                    UserBinding.platform == platform,
                    UserBinding.status == "active",
                )
            )
        )
        if existing.scalar_one_or_none():
            return {"error": "already_bound", "message": "此平台已有綁定帳號，請先解綁再重新綁定"}

        # Invalidate any existing unused verifications for this user+platform
        await session.execute(
            update(BindingVerification)
            .where(
                and_(
                    BindingVerification.user_id == user_id,
                    BindingVerification.platform == platform,
                    BindingVerification.used == False,
                )
            )
            .values(used=True)
        )

        # Create new verification
        code = _generate_code()
        expires_at = _utcnow() + timedelta(minutes=5)
        verification = BindingVerification(
            user_id=user_id,
            platform=platform,
            code=code,
            expires_at=expires_at,
        )
        session.add(verification)
        await session.commit()

        return {"code": code, "expires_in": 300}


async def verify_and_bind(
    platform: str,
    platform_uid: str,
    code: str,
    display_name: str = "",
) -> dict[str, Any]:
    """Verify code and create binding. Returns {"success": True, ...} or {"success": False, "error": ...}."""
    async with async_session() as session:
        now = _utcnow()

        # Find matching unused, non-expired verification
        result = await session.execute(
            select(BindingVerification).where(
                and_(
                    BindingVerification.platform == platform,
                    BindingVerification.code == code,
                    BindingVerification.used == False,
                    BindingVerification.expires_at > now,
                )
            )
        )
        verification = result.scalar_one_or_none()

        if not verification:
            return {"success": False, "error": "驗證碼錯誤或已過期"}

        user_id = verification.user_id

        # Check if this platform_uid is already bound to another user
        existing_binding = await session.execute(
            select(UserBinding).where(
                and_(
                    UserBinding.platform == platform,
                    UserBinding.platform_uid == platform_uid,
                )
            )
        )
        if existing_binding.scalar_one_or_none():
            return {"success": False, "error": "此 LINE 帳號已綁定其他使用者"}

        # Check if user already has a binding for this platform
        user_binding = await session.execute(
            select(UserBinding).where(
                and_(
                    UserBinding.user_id == user_id,
                    UserBinding.platform == platform,
                )
            )
        )
        if user_binding.scalar_one_or_none():
            return {"success": False, "error": "此帳號已有綁定的 LINE，請先解綁"}

        # Mark verification as used
        verification.used = True

        # Create binding
        binding = UserBinding(
            user_id=user_id,
            platform=platform,
            platform_uid=platform_uid,
            display_name=display_name,
            status="active",
        )
        session.add(binding)
        await session.commit()

        # Get username for response
        user_result = await session.execute(
            select(User.username).where(User.user_id == user_id)
        )
        username = user_result.scalar_one_or_none() or user_id

        return {
            "success": True,
            "user_id": user_id,
            "username": username,
            "platform": platform,
            "platform_uid": platform_uid,
        }


# ── LINE API helpers ─────────────────────────────────────────────


async def get_line_profile(line_uid: str, access_token: str) -> dict[str, Any] | None:
    """Fetch LINE user profile."""
    if not access_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"https://api.line.me/v2/bot/profile/{line_uid}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 200:
                return resp.json()
            return None
    except Exception:
        logger.warning("Failed to get LINE profile for uid=%s", line_uid)
        return None


async def verify_and_bind_direct(
    user_id: str,
    platform: str,
    platform_uid: str,
    display_name: str = "",
) -> dict[str, Any]:
    """Directly create binding without verification code (for LIFF flow).

    Returns {"success": True, ...} or {"success": False, "error": ...}.
    """
    async with async_session() as session:
        # Check if this platform_uid is already bound to another user
        existing_binding = await session.execute(
            select(UserBinding).where(
                and_(
                    UserBinding.platform == platform,
                    UserBinding.platform_uid == platform_uid,
                )
            )
        )
        if existing_binding.scalar_one_or_none():
            return {"success": False, "error": "此 LINE 帳號已綁定其他使用者"}

        # Check if user already has a binding for this platform
        user_binding = await session.execute(
            select(UserBinding).where(
                and_(
                    UserBinding.user_id == user_id,
                    UserBinding.platform == platform,
                )
            )
        )
        if user_binding.scalar_one_or_none():
            return {"success": False, "error": "此帳號已有綁定的 LINE，請先解綁"}

        # Create binding
        binding = UserBinding(
            user_id=user_id,
            platform=platform,
            platform_uid=platform_uid,
            display_name=display_name,
            status="active",
        )
        session.add(binding)
        await session.commit()

        return {
            "success": True,
            "user_id": user_id,
            "platform": platform,
            "platform_uid": platform_uid,
        }
