"""
Household routes.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from app.core.config import settings
from app.core.security import get_current_user
from app.core.supabase import get_supabase_admin_client
from app.models.households import (
    HouseholdSetupRequest, HouseholdSetupResponse, HouseholdMember,
    InviteResponse, JoinRequest, JoinResponse, HouseholdInfo,
)
from app.services.household_service import (
    create_household_for_user, create_invite, join_household,
)
from app.services.plaid_service import get_household_id_for_user

router = APIRouter()


@router.get("/members", response_model=List[HouseholdMember])
def get_members(user=Depends(get_current_user)):
    """Return all members of the authenticated user's household."""
    hid = get_household_id_for_user(str(user.id))
    if not hid:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No household found for this user.",
        )
    supabase = get_supabase_admin_client()
    result = (
        supabase.table("household_members")
        .select("member_id, display_name, role")
        .eq("household_id", hid)
        .execute()
    )
    return result.data or []


@router.post("/invite", response_model=InviteResponse)
def create_household_invite(user=Depends(get_current_user)):
    """Generate a one-time invite link for a new household member."""
    hid = get_household_id_for_user(str(user.id))
    if not hid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No household found for this user.")
    return create_invite(hid, str(user.id), settings.frontend_url)


@router.post("/join", response_model=JoinResponse)
def join_household_route(body: JoinRequest, user=Depends(get_current_user)):
    """Accept an invite code and add the user to the household."""
    return join_household(str(user.id), body.code, body.display_name)


@router.get("/me", response_model=HouseholdInfo)
def get_household_info(user=Depends(get_current_user)):
    """Return the household name for the authenticated user."""
    hid = get_household_id_for_user(str(user.id))
    if not hid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No household found for this user.")
    supabase = get_supabase_admin_client()
    h = (
        supabase.table("households")
        .select("household_id, name")
        .eq("household_id", hid)
        .single()
        .execute()
    )
    return HouseholdInfo(
        household_id=h.data["household_id"],
        household_name=h.data["name"],
    )


@router.post("/setup", response_model=HouseholdSetupResponse)
def setup_household(request: HouseholdSetupRequest, user=Depends(get_current_user)):
    """
    Creates a household + admin member record for a newly signed-up user.
    Idempotent — returns the existing record if this user already has one.
    Called from the frontend immediately after email confirmation.
    """
    return create_household_for_user(
        user_id=str(user.id),
        household_name=request.household_name,
        display_name=request.display_name,
    )
