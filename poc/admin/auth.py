"""Admin authentication — POC uses static admin token."""

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from poc.utils.config import ADMIN_STATIC_TOKEN


def validate_admin_token(credentials: HTTPAuthorizationCredentials) -> str:
    """Validate admin token. Returns 'admin' as user identity.

    POC token format: {ADMIN_STATIC_TOKEN}
    Production: JWT with admin role claim.
    """
    token = credentials.credentials
    if token != ADMIN_STATIC_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    return "admin"
