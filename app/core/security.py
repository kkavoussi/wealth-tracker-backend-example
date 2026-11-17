"""
FastAPI dependency — verifies Supabase JWTs on protected routes.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.supabase import get_supabase_admin_client

security = HTTPBearer()


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Extracts the Bearer token from the Authorization header,
    verifies it with Supabase, and returns the user object.

    Raises 401 if the token is missing, invalid, or expired.
    """
    token = credentials.credentials
    try:
        response = get_supabase_admin_client().auth.get_user(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    if response.user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    return response.user
