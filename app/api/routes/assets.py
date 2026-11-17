"""
Manual asset routes — add and manage manually-entered assets and liabilities.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.security import get_current_user
from app.core.supabase import get_supabase_admin_client
from app.services.plaid_service import get_household_id_for_user

router = APIRouter()


# asset_type → (balance_sheet_category, balance_sheet_subcategory, balance_sheet_detail)
MANUAL_TYPE_MAP: dict[str, tuple[str, str, str]] = {
    # ── Assets: Cash & Equivalents ──────────────────────────────────────────
    "cash_other":           ("ASSETS", "CASH_AND_EQUIVALENTS", "OTHER"),       # backward compat
    "checking":             ("ASSETS", "CASH_AND_EQUIVALENTS", "CHECKING"),
    "savings":              ("ASSETS", "CASH_AND_EQUIVALENTS", "SAVINGS"),
    "money_market":         ("ASSETS", "CASH_AND_EQUIVALENTS", "MONEY_MARKET"),
    "cash_management":      ("ASSETS", "CASH_AND_EQUIVALENTS", "CASH_MANAGEMENT"),
    "cd":                   ("ASSETS", "CASH_AND_EQUIVALENTS", "CD"),
    # ── Assets: Investments ─────────────────────────────────────────────────
    "taxable_brokerage":    ("ASSETS", "INVESTMENTS", "TAXABLE_BROKERAGE"),
    "retirement_401k":      ("ASSETS", "INVESTMENTS", "RETIREMENT_401K"),
    "retirement_ira":       ("ASSETS", "INVESTMENTS", "RETIREMENT_IRA"),
    "retirement_roth":      ("ASSETS", "INVESTMENTS", "RETIREMENT_ROTH"),
    "education_529":        ("ASSETS", "INVESTMENTS", "EDUCATION_529"),
    "hsa":                  ("ASSETS", "INVESTMENTS", "HSA"),
    "cryptocurrency":       ("ASSETS", "INVESTMENTS", "CRYPTOCURRENCY"),
    "stock_options":        ("ASSETS", "INVESTMENTS", "STOCK_OPTIONS"),
    "espp":                 ("ASSETS", "INVESTMENTS", "ESPP"),
    # ── Assets: Real Estate ─────────────────────────────────────────────────
    "real_estate":          ("ASSETS", "REAL_ESTATE", "OTHER"),                # backward compat
    "primary_residence":    ("ASSETS", "REAL_ESTATE", "PRIMARY_RESIDENCE"),
    "investment_property":  ("ASSETS", "REAL_ESTATE", "INVESTMENT_PROPERTY"),
    "vacation_property":    ("ASSETS", "REAL_ESTATE", "VACATION_PROPERTY"),
    "land":                 ("ASSETS", "REAL_ESTATE", "LAND"),
    # ── Assets: Other ───────────────────────────────────────────────────────
    "vehicle":              ("ASSETS", "OTHER_ASSETS", "VEHICLE"),
    "personal_property":    ("ASSETS", "OTHER_ASSETS", "PERSONAL_PROPERTY"),
    "business_assets":      ("ASSETS", "OTHER_ASSETS", "BUSINESS_ASSETS"),
    "other_asset":          ("ASSETS", "OTHER_ASSETS", "OTHER"),
    # ── Liabilities: Loans ──────────────────────────────────────────────────
    "personal_loan":        ("LIABILITIES", "LOANS", "PERSONAL_LOAN"),
    "auto_loan":            ("LIABILITIES", "LOANS", "AUTO_LOAN"),
    "student_loan":         ("LIABILITIES", "LOANS", "STUDENT_LOAN"),
    "margin_loan":          ("LIABILITIES", "LOANS", "MARGIN_LOAN"),
    "401k_loan":            ("LIABILITIES", "LOANS", "401K_LOAN"),
    # ── Liabilities: Mortgages ──────────────────────────────────────────────
    "mortgage_manual":      ("LIABILITIES", "MORTGAGES", "PRIMARY_MORTGAGE"),
    "investment_mortgage":  ("LIABILITIES", "MORTGAGES", "INVESTMENT_MORTGAGE"),
    "heloc":                ("LIABILITIES", "MORTGAGES", "HELOC"),
    "home_equity_loan":     ("LIABILITIES", "MORTGAGES", "HOME_EQUITY_LOAN"),
    # ── Liabilities: Credit Cards (detail set by user via CC network picker) ─
    "credit_card_manual":   ("LIABILITIES", "CREDIT_CARDS", "OTHER_CC"),
    # ── Liabilities: Other ──────────────────────────────────────────────────
    "other_liability":      ("LIABILITIES", "OTHER_LIABILITIES", "OTHER"),
    "medical_debt":         ("LIABILITIES", "OTHER_LIABILITIES", "MEDICAL_DEBT"),
    "tax_debt":             ("LIABILITIES", "OTHER_LIABILITIES", "TAX_DEBT"),
    "family_loan":          ("LIABILITIES", "OTHER_LIABILITIES", "FAMILY_LOANS"),
}

# Legacy alias — subcategory only (backward compat for any callers)
ASSET_TYPE_SUBCATEGORY = {k: v[1] for k, v in MANUAL_TYPE_MAP.items()}

VALID_ASSET_TYPES = set(MANUAL_TYPE_MAP.keys())


class ManualAssetRequest(BaseModel):
    name: str
    asset_type: str
    current_value: float
    description: Optional[str] = None
    detail: Optional[str] = None   # CC network override (e.g. MASTERCARD, VISA)


@router.post("/manual")
def add_manual_asset(body: ManualAssetRequest, user=Depends(get_current_user)):
    """
    Add a manually-entered asset or liability to the household's balance sheet.
    Returns the created row.
    """
    if body.asset_type not in VALID_ASSET_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid asset_type '{body.asset_type}'. Must be one of: {sorted(VALID_ASSET_TYPES)}",
        )

    if body.current_value < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="current_value must be >= 0",
        )

    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No household found for this user.",
        )

    supabase = get_supabase_admin_client()

    # Resolve member_id
    member_result = (
        supabase.table("household_members")
        .select("member_id")
        .eq("user_id", str(user.id))
        .limit(1)
        .execute()
    )
    member_id = member_result.data[0]["member_id"] if member_result.data else None

    category, subcategory, default_detail = MANUAL_TYPE_MAP[body.asset_type]
    # Use caller-supplied detail (e.g. CC network) if provided, else use the default
    detail = body.detail if body.detail else default_detail

    result = (
        supabase.table("manual_assets")
        .insert({
            "household_id":              household_id,
            "member_id":                 member_id,
            "name":                      body.name,
            "asset_type":                body.asset_type,
            "current_value":             body.current_value,
            "description":               body.description,
            "balance_sheet_category":    category,
            "balance_sheet_subcategory": subcategory,
            "balance_sheet_detail":      detail,
        })
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save asset.",
        )

    return result.data[0]


class UpdateManualAssetRequest(BaseModel):
    name:          Optional[str]   = None
    asset_type:    Optional[str]   = None
    current_value: Optional[float] = None
    description:   Optional[str]   = None
    detail:        Optional[str]   = None   # CC network override


@router.patch("/manual/{asset_id}")
def update_manual_asset(
    asset_id: str,
    body: UpdateManualAssetRequest,
    user=Depends(get_current_user),
):
    """Update a manually-entered asset's name, type, value, description, or detail."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No household found for this user.")

    supabase = get_supabase_admin_client()

    check = (
        supabase.table("manual_assets")
        .select("asset_id, asset_type")
        .eq("asset_id", asset_id)
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if not check.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Asset not found.")

    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.current_value is not None:
        if body.current_value < 0:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="current_value must be >= 0")
        updates["current_value"] = body.current_value
    if body.description is not None:
        updates["description"] = body.description
    if body.asset_type is not None:
        if body.asset_type not in VALID_ASSET_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid asset_type '{body.asset_type}'. Must be one of: {sorted(VALID_ASSET_TYPES)}",
            )
        cat, subcat, default_detail = MANUAL_TYPE_MAP[body.asset_type]
        updates["asset_type"]                = body.asset_type
        updates["balance_sheet_category"]    = cat
        updates["balance_sheet_subcategory"] = subcat
        # Use caller-supplied detail if provided; otherwise use the type's default
        updates["balance_sheet_detail"] = body.detail if body.detail else default_detail
    elif body.detail is not None:
        # Allow updating detail alone (e.g. changing CC network without changing type)
        updates["balance_sheet_detail"] = body.detail

    if not updates:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="No fields to update.")

    result = (
        supabase.table("manual_assets")
        .update(updates)
        .eq("asset_id", asset_id)
        .execute()
    )
    return result.data[0] if result.data else {"success": True}


@router.delete("/manual/{asset_id}")
def hide_manual_asset(asset_id: str, user=Depends(get_current_user)):
    """Hide a manual asset/liability from the balance sheet (sets is_hidden = TRUE)."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No household found for this user.")

    supabase = get_supabase_admin_client()

    check = (
        supabase.table("manual_assets")
        .select("asset_id")
        .eq("asset_id", asset_id)
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if not check.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Asset not found.")

    supabase.table("manual_assets").update({"is_hidden": True}).eq("asset_id", asset_id).execute()
    return {"success": True}
