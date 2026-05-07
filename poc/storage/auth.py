"""Storage Service authentication — dual mode: user token + admin token.

User token format: $POC_STATIC_TOKEN:{user_id} → (user_id, "user")
Admin token format: $POC_ADMIN_TOKEN → ("admin", "admin")
"""

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from poc.utils.config import ADMIN_STATIC_TOKEN, STATIC_TOKEN


def validate_token(credentials: HTTPAuthorizationCredentials) -> tuple[str, str]:
    """Validate token and return (user_id, role).

    Returns:
        ("admin", "admin") for admin tokens
        (user_id, "user") for user tokens
    """
    token = credentials.credentials

    # Admin token
    if token == ADMIN_STATIC_TOKEN:
        return ("admin", "admin")

    # User token: {STATIC_TOKEN}:{user_id}
    if ":" not in token:
        raise HTTPException(status_code=401, detail="Invalid token format")
    prefix, user_id = token.rsplit(":", 1)
    if prefix != STATIC_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return (user_id, "user")
