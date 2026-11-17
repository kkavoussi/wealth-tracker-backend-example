"""
SnapTrade routes — brokerage list, connection portal, account sync, and account management.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from typing import Optional

from app.core.security import get_current_user
from app.core.supabase import get_supabase_admin_client
from app.services.plaid_service import get_household_id_for_user
from app.services.household_service import get_member_id_for_user
from app.services import snaptrade_service

router = APIRouter()


@router.get("/brokerages")
def list_brokerages():
    """Return the cached SnapTrade brokerage list (no auth required — public reference data)."""
    try:
        return snaptrade_service.get_brokerages()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch brokerage list: {str(e)}",
        )


class ConnectRequest(BaseModel):
    broker_slug: Optional[str] = None


@router.post("/connect")
def connect_snaptrade(body: ConnectRequest = ConnectRequest(), user=Depends(get_current_user)):
    """Register household's SnapTrade user (if needed) and return a connection portal URL."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No household found.",
        )
    try:
        portal_url = snaptrade_service.get_connection_portal_url(
            household_id, broker_slug=body.broker_slug
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate connection portal: {str(e)}",
        )
    return {"portal_url": portal_url}


@router.post("/sync")
def sync_snaptrade(user=Depends(get_current_user)):
    """Sync all SnapTrade accounts + balances. No-op if household hasn't connected SnapTrade yet."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No household found.",
        )
    supabase = get_supabase_admin_client()
    connected = (
        supabase.table("snaptrade_users")
        .select("id")
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if not connected.data:
        return {"accounts_synced": 0, "skipped": True}
    member_id = get_member_id_for_user(str(user.id))
    try:
        return snaptrade_service.sync_snaptrade_accounts(household_id, member_id=member_id)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Sync failed: {str(e)}",
        )


@router.get("/accounts")
def list_snaptrade_accounts(
    member_id: Optional[str] = Query(default=None, description="Filter by member ID; omit for combined household view"),
    user=Depends(get_current_user),
):
    """Return all non-hidden snaptrade_accounts for the authenticated user's household.
    Pass ?member_id=<uuid> to restrict to a single member's accounts."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No household found.",
        )
    supabase = get_supabase_admin_client()
    q = (
        supabase.table("snaptrade_accounts")
        .select("*")
        .eq("household_id", household_id)
        .eq("is_hidden", False)
    )
    if member_id:
        q = q.eq("member_id", member_id)
    return q.execute().data or []


class PatchSnaptradeAccountRequest(BaseModel):
    name: Optional[str] = None
    balance_sheet_category: Optional[str] = None
    balance_sheet_subcategory: Optional[str] = None
    balance_sheet_detail: Optional[str] = None


@router.patch("/accounts/{account_id}")
def patch_snaptrade_account(
    account_id: str,
    body: PatchSnaptradeAccountRequest,
    user=Depends(get_current_user),
):
    """Update display fields for a SnapTrade account (name, balance sheet classification)."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No household found.")
    supabase = get_supabase_admin_client()
    check = (
        supabase.table("snaptrade_accounts")
        .select("id")
        .eq("id", account_id)
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if not check.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found.")
    payload = body.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update.")
    supabase.table("snaptrade_accounts").update(payload).eq("id", account_id).execute()
    return {"success": True}


@router.delete("/accounts/{account_id}")
def hide_snaptrade_account(account_id: str, user=Depends(get_current_user)):
    """Hide a SnapTrade account from the balance sheet (sets is_hidden = TRUE)."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No household found.",
        )
    supabase = get_supabase_admin_client()
    check = (
        supabase.table("snaptrade_accounts")
        .select("id")
        .eq("id", account_id)
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if not check.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found.",
        )
    supabase.table("snaptrade_accounts").update({"is_hidden": True}).eq("id", account_id).execute()
    return {"success": True}
