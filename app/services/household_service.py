"""
Business logic for household creation and member invites.
"""
import secrets
import string
import datetime
from fastapi import HTTPException, status
from app.core.supabase import get_supabase_admin_client
from app.models.households import HouseholdSetupResponse, InviteResponse, JoinResponse


def get_member_id_for_user(user_id: str) -> str | None:
    """Return the member_id for an authenticated user, or None if not found."""
    supabase = get_supabase_admin_client()
    result = (
        supabase.table("household_members")
        .select("member_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return result.data[0]["member_id"] if result.data else None


_CODE_ALPHABET = string.ascii_letters + string.digits


def create_invite(
    household_id: str,
    created_by_user_id: str,
    frontend_url: str,
) -> InviteResponse:
    """Generate a one-time invite code valid for 7 days."""
    supabase = get_supabase_admin_client()
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    expires_at = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
    ).isoformat()

    supabase.table("household_invites").insert({
        "household_id": household_id,
        "created_by":   created_by_user_id,
        "code":         code,
        "expires_at":   expires_at,
    }).execute()

    return InviteResponse(
        code=code,
        invite_url=f"{frontend_url}/signup?invite={code}",
        expires_at=expires_at,
    )


def join_household(user_id: str, code: str, display_name: str) -> JoinResponse:
    """Validate invite code and add the user to the household."""
    supabase = get_supabase_admin_client()

    # Check user isn't already a member
    existing = (
        supabase.table("household_members")
        .select("member_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        # Idempotent: already a member — return existing record
        m = existing.data[0]
        hm = (
            supabase.table("household_members")
            .select("member_id, household_id, role")
            .eq("member_id", m["member_id"])
            .single()
            .execute()
        )
        return JoinResponse(
            household_id=hm.data["household_id"],
            member_id=hm.data["member_id"],
            role=hm.data["role"],
        )

    # Validate the invite code
    invite_result = (
        supabase.table("household_invites")
        .select("id, household_id, expires_at, used_at")
        .eq("code", code)
        .limit(1)
        .execute()
    )
    if not invite_result.data:
        raise HTTPException(status_code=404, detail="Invite code not found.")
    invite = invite_result.data[0]

    if invite["used_at"] is not None:
        raise HTTPException(status_code=400, detail="This invite has already been used.")

    now = datetime.datetime.now(datetime.timezone.utc)
    expires_at = datetime.datetime.fromisoformat(invite["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
    if now > expires_at:
        raise HTTPException(status_code=400, detail="This invite has expired.")

    household_id = invite["household_id"]

    # Add member
    member_result = (
        supabase.table("household_members")
        .insert({
            "household_id": household_id,
            "user_id":      user_id,
            "role":         "member",
            "display_name": display_name,
            "joined_at":    now.isoformat(),
        })
        .execute()
    )
    if not member_result.data:
        raise HTTPException(status_code=500, detail="Failed to join household.")
    member = member_result.data[0]

    # Mark invite used
    supabase.table("household_invites").update({
        "used_at": now.isoformat(),
        "used_by": user_id,
    }).eq("id", invite["id"]).execute()

    return JoinResponse(
        household_id=household_id,
        member_id=member["member_id"],
        role="member",
    )


def create_household_for_user(
    user_id: str,
    household_name: str,
    display_name: str,
) -> HouseholdSetupResponse:
    """
    Creates a household and an admin member record for a new user.
    Idempotent: if a member record already exists for this user_id, returns it.

    Uses the admin Supabase client to bypass RLS.
    """
    supabase = get_supabase_admin_client()

    # Check if this user is already a member of a household
    existing = (
        supabase.table("household_members")
        .select("member_id, household_id, role")
        .eq("user_id", user_id)
        .execute()
    )
    if existing.data:
        member = existing.data[0]
        return HouseholdSetupResponse(
            household_id=member["household_id"],
            member_id=member["member_id"],
            role=member["role"],
        )

    # Create the household
    household_result = (
        supabase.table("households")
        .insert({"name": household_name})
        .execute()
    )
    if not household_result.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create household",
        )
    household = household_result.data[0]

    # Create the admin member record
    member_result = (
        supabase.table("household_members")
        .insert({
            "household_id": household["household_id"],
            "user_id": user_id,
            "role": "admin",
            "display_name": display_name,
        })
        .execute()
    )
    if not member_result.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create member record",
        )
    member = member_result.data[0]

    return HouseholdSetupResponse(
        household_id=household["household_id"],
        member_id=member["member_id"],
        role=member["role"],
    )
