"""
Transaction endpoints — user-initiated re-categorization.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.core.security import get_current_user
from app.core.supabase import get_supabase_admin_client
from app.services.plaid_service import get_household_id_for_user

router = APIRouter()

# Categories whose transactions are excluded from expenses
_EXCLUDE_CATEGORIES = {"INCOME", "TRANSFERS"}


class UpdateTransactionRequest(BaseModel):
    category:      str
    subcategory:   str
    detail:        Optional[str] = None
    merchant_name: Optional[str] = None  # user-visible display name (None = clear override)


@router.patch("/{transaction_id}")
def update_transaction(
    transaction_id: str,
    body: UpdateTransactionRequest,
    user=Depends(get_current_user),
):
    """Re-categorize a single transaction. Writes categorization_source='user_override'
    so that subsequent Plaid syncs never overwrite the user's choice."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=404, detail="No household found for this user.")

    supabase = get_supabase_admin_client()

    # Verify ownership
    check = (
        supabase.table("transactions")
        .select("transaction_id")
        .eq("transaction_id", transaction_id)
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if not check.data:
        raise HTTPException(status_code=404, detail="Transaction not found.")

    display = f"{body.category.replace('_', ' ').title()} > {body.subcategory.replace('_', ' ').title()}"
    if body.detail:
        display += f" > {body.detail.replace('_', ' ').title()}"

    exclude = body.category in _EXCLUDE_CATEGORIES

    supabase.table("transactions").update({
        "merchant_name":             body.merchant_name,
        "category":                  body.category,
        "subcategory":               body.subcategory,
        "detail":                    body.detail,
        "category_display_name":     display,
        "exclude_from_expenses":     exclude,
        "categorization_source":     "user_override",
        "categorization_confidence": "HIGH",
        "flagged_for_review":        False,
        "flag_reason":               None,
    }).eq("transaction_id", transaction_id).eq("household_id", household_id).execute()

    return {"success": True}
