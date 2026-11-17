"""
Auth routes — minimal for now.
"""
from fastapi import APIRouter, Depends
from app.core.security import get_current_user
from app.models.auth import UserResponse

router = APIRouter()


@router.get("/me", response_model=UserResponse)
def get_me(user=Depends(get_current_user)):
    """Returns the current authenticated user's info. Used to verify the JWT round-trip."""
    return UserResponse(user_id=str(user.id), email=user.email)
