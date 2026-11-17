"""
Plaid routes — link token, token exchange, and transaction sync.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Optional

from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.accounts_get_request import AccountsGetRequest

from app.core.security import get_current_user
from app.core.supabase import get_supabase_admin_client
from app.services.plaid_service import get_household_id_for_user, sync_transactions, _get_plaid_client

router = APIRouter()


# =============================================================================
# HELPERS
# =============================================================================

def _safe_str(val) -> Optional[str]:
    """Convert a Plaid enum/object value to a plain string, or None."""
    if val is None:
        return None
    if hasattr(val, "value"):
        return str(val.value)
    return str(val)


def _safe_get(obj, key):
    """Get a value from either a dict or an object with attributes."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _categorize_plaid_account(
    account_type: str,
    account_subtype: Optional[str],
    account_name: str,
    official_name: Optional[str] = None,
) -> tuple[str, str, str]:
    """
    Map a Plaid account to (balance_sheet_category, balance_sheet_subcategory, balance_sheet_detail).
    Mirrors the categorize_account() logic from autofill_balance_sheet_secure.py /
    balance_sheet_account_categories.json.
    """
    name_low = (account_name or "").lower()
    sub_low  = (account_subtype or "").lower()

    if account_type == "depository":
        SUBTYPE_MAP = {
            "checking":        "CHECKING",
            "savings":         "SAVINGS",
            "money market":    "MONEY_MARKET",
            "cd":              "CD",
            "cash management": "CASH_MANAGEMENT",
        }
        detail = SUBTYPE_MAP.get(sub_low, "CHECKING")
        return "ASSETS", "CASH_AND_EQUIVALENTS", detail

    if account_type == "investment":
        SUBTYPE_MAP = {
            "401k":      "RETIREMENT_401K",
            "403b":      "RETIREMENT_401K",
            "ira":       "RETIREMENT_IRA",
            "roth":      "RETIREMENT_ROTH",
            "roth 401k": "RETIREMENT_ROTH",
            "529":       "EDUCATION_529",
            "hsa":       "HSA",
            "brokerage": "TAXABLE_BROKERAGE",
        }
        detail = SUBTYPE_MAP.get(sub_low, "TAXABLE_BROKERAGE")
        # Crypto name override
        for keyword in ("coinbase", "kraken", "gemini", "binance"):
            if keyword in name_low:
                detail = "CRYPTOCURRENCY"
                break
        return "ASSETS", "INVESTMENTS", detail

    if account_type == "credit":
        # Check both name and official_name for CC network keywords
        combined = f"{account_name or ''} {official_name or ''}".lower()
        detail = "OTHER_CC"
        CC_KEYWORDS = [
            ("american express", "AMERICAN_EXPRESS"),
            ("amex",             "AMERICAN_EXPRESS"),
            ("discover",         "DISCOVER"),
            ("capital one",      "CAPITAL_ONE"),
            ("mastercard",       "MASTERCARD"),
            ("master card",      "MASTERCARD"),
            ("visa",             "VISA"),
        ]
        for keyword, cc_detail in CC_KEYWORDS:
            if keyword in combined:
                detail = cc_detail
                break
        return "LIABILITIES", "CREDIT_CARDS", detail

    if account_type == "loan":
        SUBTYPE_MAP = {
            "mortgage":    ("LIABILITIES", "MORTGAGES", "PRIMARY_MORTGAGE"),
            "home equity": ("LIABILITIES", "MORTGAGES", "HELOC"),
            "auto":        ("LIABILITIES", "LOANS", "AUTO_LOAN"),
            "student":     ("LIABILITIES", "LOANS", "STUDENT_LOAN"),
            "personal":    ("LIABILITIES", "LOANS", "PERSONAL_LOAN"),
        }
        return SUBTYPE_MAP.get(sub_low, ("LIABILITIES", "OTHER_LIABILITIES", "OTHER"))

    # "other" type — default to OTHER_ASSETS
    return "ASSETS", "OTHER_ASSETS", "OTHER"


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.post("/create-link-token")
def create_link_token(user=Depends(get_current_user)):
    """
    Create a Plaid Link token for the authenticated user.
    The frontend passes this token to the react-plaid-link component to open
    the Plaid Link iframe.
    """
    plaid = _get_plaid_client()

    try:
        response = plaid.link_token_create(
            LinkTokenCreateRequest(
                products=[Products("transactions")],
                client_name="Wealth Tracker",
                country_codes=[CountryCode("US")],
                language="en",
                redirect_uri="https://wealth-tracker-frontend.vercel.app/oauth-callback",
                user=LinkTokenCreateRequestUser(client_user_id=str(user.id)),
                transactions=LinkTokenTransactions(days_requested=730),
            )
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create link token: {str(e)}",
        )

    return {"link_token": response["link_token"]}


class ExchangeTokenRequest(BaseModel):
    public_token: str
    institution_name: str = "Unknown"


@router.post("/exchange-token")
def exchange_token(body: ExchangeTokenRequest, user=Depends(get_current_user)):
    """
    Exchange a Plaid public_token (from Link) for a permanent access_token.
    Saves the institution and all its accounts, then runs a full transaction sync.

    Returns: { success, accounts_added, transactions_fetched, items_synced }
    """
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No household found for this user.",
        )

    supabase = get_supabase_admin_client()

    # Resolve member_id for connected_by_member_id
    member_result = (
        supabase.table("household_members")
        .select("member_id")
        .eq("user_id", str(user.id))
        .limit(1)
        .execute()
    )
    member_id = member_result.data[0]["member_id"] if member_result.data else None

    plaid = _get_plaid_client()

    # 1. Exchange public_token → access_token
    try:
        exchange_response = plaid.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=body.public_token)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Token exchange failed: {str(e)}",
        )

    access_token  = exchange_response["access_token"]
    plaid_item_id = exchange_response["item_id"]

    # 2. Fetch accounts from Plaid
    try:
        accounts_response = plaid.accounts_get(
            AccountsGetRequest(access_token=access_token)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to fetch accounts: {str(e)}",
        )

    raw_accounts = _safe_get(accounts_response, "accounts") or []

    # 3. Upsert plaid_item (idempotent on plaid_item_id)
    item_upsert = (
        supabase.table("plaid_items")
        .upsert(
            {
                "household_id":           household_id,
                "connected_by_member_id": member_id,
                "plaid_item_id":          plaid_item_id,
                "access_token":           access_token,
                "institution_name":       body.institution_name,
                "status":                 "active",
            },
            on_conflict="plaid_item_id",
        )
        .execute()
    )
    if not item_upsert.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save institution.",
        )

    db_item_id = item_upsert.data[0]["item_id"]

    # 4. Upsert accounts (idempotent on plaid_account_id)
    accounts_added = 0
    for acct in raw_accounts:
        acct_type    = _safe_str(_safe_get(acct, "type"))    or ""
        acct_subtype = _safe_str(_safe_get(acct, "subtype")) or ""
        acct_name    = _safe_str(_safe_get(acct, "name"))    or ""
        balances     = _safe_get(acct, "balances") or {}

        bal_current   = _safe_get(balances, "current")
        bal_available = _safe_get(balances, "available")
        bal_limit     = _safe_get(balances, "limit")
        currency      = _safe_str(_safe_get(balances, "iso_currency_code")) or "USD"

        acct_official = _safe_str(_safe_get(acct, "official_name")) or ""
        cat, subcat, detail = _categorize_plaid_account(acct_type, acct_subtype, acct_name, acct_official)

        row = {
            "item_id":                   db_item_id,
            "household_id":              household_id,
            "member_id":                 member_id,
            "plaid_account_id":          str(_safe_get(acct, "account_id") or ""),
            "name":                      acct_name,
            "official_name":             _safe_str(_safe_get(acct, "official_name")),
            "type":                      acct_type,
            "subtype":                   acct_subtype,
            "current_balance":           float(bal_current)   if bal_current   is not None else 0.0,
            "available_balance":         float(bal_available) if bal_available is not None else None,
            "credit_limit":              float(bal_limit)     if bal_limit     is not None else None,
            "iso_currency_code":         currency,
            "balance_sheet_category":    cat,
            "balance_sheet_subcategory": subcat,
            "balance_sheet_detail":      detail,
        }

        try:
            supabase.table("plaid_accounts").upsert(
                row, on_conflict="plaid_account_id"
            ).execute()
            accounts_added += 1
        except Exception:
            pass

    # 5. Full transaction sync for the household (includes the new institution)
    sync_result = sync_transactions(household_id)

    return {
        "success":              True,
        "accounts_added":       accounts_added,
        "transactions_fetched": sync_result.get("transactions_fetched", 0),
        "items_synced":         sync_result.get("items_synced", 0),
    }


class UpdatePlaidAccountRequest(BaseModel):
    balance_sheet_category:    Optional[str] = None
    balance_sheet_subcategory: Optional[str] = None
    balance_sheet_detail:      Optional[str] = None
    name:                      Optional[str] = None
    subtype:                   Optional[str] = None  # user-overridable investment/cash sub-type


@router.patch("/accounts/{account_id}")
def update_plaid_account(
    account_id: str,
    body: UpdatePlaidAccountRequest,
    user=Depends(get_current_user),
):
    """Update category, subcategory, detail, or name for a Plaid account."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No household found for this user.")

    supabase = get_supabase_admin_client()

    # Verify account belongs to this household
    check = (
        supabase.table("plaid_accounts")
        .select("account_id")
        .eq("account_id", account_id)
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if not check.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Account not found.")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="No fields to update.")

    result = (
        supabase.table("plaid_accounts")
        .update(updates)
        .eq("account_id", account_id)
        .execute()
    )
    return result.data[0] if result.data else {"success": True}


@router.delete("/accounts/{account_id}")
def hide_plaid_account(account_id: str, user=Depends(get_current_user)):
    """Hide a Plaid account from the balance sheet (sets is_hidden = TRUE)."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No household found for this user.")

    supabase = get_supabase_admin_client()

    # Verify account belongs to this household
    check = (
        supabase.table("plaid_accounts")
        .select("account_id")
        .eq("account_id", account_id)
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if not check.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Account not found.")

    supabase.table("plaid_accounts").update({"is_hidden": True}).eq("account_id", account_id).execute()
    return {"success": True}


class UpdateLinkTokenRequest(BaseModel):
    plaid_item_id: str


@router.post("/create-update-link-token")
def create_update_link_token(
    body: UpdateLinkTokenRequest,
    user=Depends(get_current_user),
):
    """
    Create a Plaid Link token in update mode for an existing item.
    Used when an institution's OAuth token has expired (ITEM_LOGIN_REQUIRED).
    After the user re-authenticates, the frontend calls /complete-reauth (not
    /exchange-token) so that member ownership and manual account overrides
    are preserved.
    """
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No household found.")

    supabase = get_supabase_admin_client()

    # Look up the access_token — scoped to household to prevent cross-user access.
    item_result = (
        supabase.table("plaid_items")
        .select("access_token")
        .eq("plaid_item_id", body.plaid_item_id)
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if not item_result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plaid item not found.")

    plaid = _get_plaid_client()
    try:
        response = plaid.link_token_create(
            LinkTokenCreateRequest(
                client_name="Wealth Tracker",
                country_codes=[CountryCode("US")],
                language="en",
                redirect_uri="https://wealth-tracker-frontend.vercel.app/oauth-callback",
                user=LinkTokenCreateRequestUser(client_user_id=str(user.id)),
                access_token=item_result.data[0]["access_token"],
                # No `products` field — update mode re-auth only.
            )
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create update link token: {str(e)}",
        )

    return {"link_token": response["link_token"]}


class CompleteReauthRequest(BaseModel):
    public_token: str
    plaid_item_id: str


@router.post("/complete-reauth")
def complete_reauth(body: CompleteReauthRequest, user=Depends(get_current_user)):
    """
    Finalize an OAuth re-authentication after the user completes Plaid Link
    update mode.

    Unlike /exchange-token (which is for initial connections), this endpoint:
    - Only updates access_token on the existing plaid_items row
    - Does NOT touch connected_by_member_id — preserving who originally
      connected the institution
    - Does NOT re-upsert plaid_accounts — preserving member ownership and
      any manual balance_sheet_category/detail overrides set by the user
    - Runs a full transaction sync to pick up any transactions missed while
      the token was expired
    """
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No household found.")

    plaid   = _get_plaid_client()
    supabase = get_supabase_admin_client()

    # Exchange the public_token for a new access_token.
    try:
        exchange = plaid.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=body.public_token)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Token exchange failed: {str(e)}",
        )

    access_token  = exchange["access_token"]
    plaid_item_id = exchange["item_id"]

    # Verify the item_id matches what the frontend sent (sanity check).
    if plaid_item_id != body.plaid_item_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Returned item_id does not match the requested plaid_item_id.",
        )

    # Update ONLY the access_token. Never touch connected_by_member_id or
    # any plaid_accounts fields — those must be preserved as-is.
    result = (
        supabase.table("plaid_items")
        .update({"access_token": access_token, "status": "active"})
        .eq("plaid_item_id", plaid_item_id)
        .eq("household_id", household_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Plaid item not found for this household.",
        )

    # Full transaction sync now that the token is live again.
    sync_result = sync_transactions(household_id)

    return {
        "success":         True,
        "items_synced":    sync_result.get("items_synced", 0),
        "transactions_fetched": sync_result.get("transactions_fetched", 0),
    }


@router.post("/sync")
def plaid_sync(user=Depends(get_current_user)):
    """
    Trigger a full transaction sync for the authenticated user's household.
    Fetches 2 years of transactions from Plaid, categorizes them, and upserts
    into the transactions table.
    """
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No household found for this user.",
        )

    result = sync_transactions(household_id)

    if "error" in result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result["error"],
        )

    return result
