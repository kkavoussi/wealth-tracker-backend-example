"""
SnapTrade Service
Handles user registration, connection portal URLs, account sync,
and the brokerage reference list (used for institution routing).
"""
import datetime
from snaptrade_client import SnapTrade
from app.core.config import settings
from app.core.supabase import get_supabase_admin_client


# ---------------------------------------------------------------------------
# Module-level brokerage cache (refreshed at startup + every 24h)
# ---------------------------------------------------------------------------
_brokerage_cache: list[dict] | None = None
_brokerage_cache_at: datetime.datetime | None = None
_CACHE_TTL_HOURS = 24


def _get_client() -> SnapTrade:
    return SnapTrade(
        client_id=settings.snaptrade_client_id,
        consumer_key=settings.snaptrade_consumer_key,
    )


# ---------------------------------------------------------------------------
# Brokerage list (institution picker)
# ---------------------------------------------------------------------------

def get_brokerages() -> list[dict]:
    """
    Return SnapTrade's full brokerage list (cached 24h).
    Each entry: {slug, name, display_name, logo_url, enabled}
    """
    global _brokerage_cache, _brokerage_cache_at
    now = datetime.datetime.utcnow()
    if (
        _brokerage_cache is None
        or _brokerage_cache_at is None
        or (now - _brokerage_cache_at).total_seconds() > _CACHE_TTL_HOURS * 3600
    ):
        snaptrade = _get_client()
        response = snaptrade.reference_data.list_all_brokerages()
        _brokerage_cache = sorted(
            [
                {
                    "slug":         b.get("slug"),
                    "name":         b.get("name"),
                    "display_name": b.get("display_name") or b.get("name"),
                    "logo_url":     b.get("aws_s3_square_logo_url") or b.get("aws_s3_logo_url"),
                    "enabled":      b.get("enabled", False),
                }
                for b in (response.body or [])
                if b.get("enabled") and not b.get("maintenance_mode")
            ],
            key=lambda b: (b["display_name"] or "").lower(),
        )
        _brokerage_cache_at = now
    return _brokerage_cache


# ---------------------------------------------------------------------------
# User registration
# ---------------------------------------------------------------------------

def ensure_snaptrade_user(household_id: str) -> dict:
    """
    Return the snaptrade_users creds for this household, registering with
    SnapTrade if this household hasn't connected before.
    """
    supabase = get_supabase_admin_client()
    existing = (
        supabase.table("snaptrade_users")
        .select("snaptrade_user_id, snaptrade_user_secret")
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]

    snaptrade = _get_client()
    user_id = f"wt_{household_id}"
    response = snaptrade.authentication.register_snap_trade_user(
        body={"userId": user_id}
    )
    user_secret = response.body["userSecret"]

    supabase.table("snaptrade_users").insert({
        "household_id":           household_id,
        "snaptrade_user_id":      user_id,
        "snaptrade_user_secret":  user_secret,
    }).execute()

    return {"snaptrade_user_id": user_id, "snaptrade_user_secret": user_secret}


# ---------------------------------------------------------------------------
# Connection portal
# ---------------------------------------------------------------------------

def get_connection_portal_url(household_id: str, broker_slug: str | None = None) -> str:
    """
    Returns the SnapTrade portal URL (valid 5 minutes).
    Pass broker_slug to pre-select the institution (e.g. "FIDELITY").
    """
    snaptrade = _get_client()
    creds = ensure_snaptrade_user(household_id)

    body: dict = {"connectionType": "read"}
    if broker_slug:
        body["broker"] = broker_slug

    response = snaptrade.authentication.login_snap_trade_user(
        user_id=creds["snaptrade_user_id"],
        user_secret=creds["snaptrade_user_secret"],
        body=body,
    )
    return response.body["redirectURI"]


# ---------------------------------------------------------------------------
# Account sync
# ---------------------------------------------------------------------------

def sync_snaptrade_accounts(household_id: str, member_id: str | None = None) -> dict:
    """
    Fetch all accounts for this household's SnapTrade user and upsert into
    snaptrade_accounts. Uses list_user_accounts (SDK v10.x API).
    Returns {"accounts_synced": N}.

    member_id ownership rules:
      - Existing accounts: preserve whatever member_id is already stored in DB.
        This ensures a sync triggered by either spouse never re-assigns the
        other person's accounts.
      - New accounts (first time seen): assign the member_id of whoever
        triggered the sync, since they are the one who just connected it.
    """
    snaptrade = _get_client()
    supabase  = get_supabase_admin_client()
    creds     = ensure_snaptrade_user(household_id)

    acct_response = snaptrade.account_information.list_user_accounts(
        user_id=creds["snaptrade_user_id"],
        user_secret=creds["snaptrade_user_secret"],
    )
    accounts = acct_response.body or []

    # Build a map of snaptrade_account_id → existing row data from DB so we can
    # preserve user-editable fields on re-sync: member_id (ownership), name (user
    # may rename), and balance_sheet_subcategory (user may reclassify account type).
    existing_result = (
        supabase.table("snaptrade_accounts")
        .select("snaptrade_account_id, member_id, name, balance_sheet_subcategory")
        .eq("household_id", household_id)
        .execute()
    )
    existing_map: dict[str, dict] = {
        r["snaptrade_account_id"]: r
        for r in (existing_result.data or [])
    }

    rows = []
    for acct in accounts:
        acct_id = acct.get("id")
        if not acct_id:
            continue

        # balance is an AccountBalance object with a "total" Amount sub-object
        try:
            balance_obj = acct.get("balance") or {}
            total_obj   = balance_obj.get("total") or {}
            balance_amt = total_obj.get("amount")
            currency    = total_obj.get("currency") or "USD"
        except Exception:
            balance_amt = None
            currency    = "USD"

        institution = acct.get("institution_name") or ""
        auth_id     = str(acct.get("brokerage_authorization") or "")

        # SDK v10 raw_type is empty — use account name + institution for type detection
        acct_name_str = acct.get("name") or acct.get("number") or ""
        bs_cat, bs_sub, bs_det = _map_account_type("INVESTMENT", "", acct_name_str, institution)

        # For existing accounts: preserve member_id, name, and balance_sheet_subcategory
        # so that user edits (rename, reclassify) survive a sync.
        # For new accounts: compute everything fresh from the API data.
        acct_id_str = str(acct_id)
        if acct_id_str in existing_map:
            existing = existing_map[acct_id_str]
            assigned_member_id = existing.get("member_id")
            display_name   = existing.get("name") or _build_display_name(institution, acct_name_str)
            bs_sub_to_use  = existing.get("balance_sheet_subcategory") or bs_sub
        else:
            assigned_member_id = member_id
            display_name   = _build_display_name(institution, acct_name_str)
            bs_sub_to_use  = bs_sub

        rows.append({
            "household_id":               household_id,
            "member_id":                  assigned_member_id,
            "snaptrade_account_id":       acct_id_str,
            "brokerage_authorization_id": auth_id,
            "institution_name":           institution,
            "name":                       display_name,
            "account_number":             acct.get("number"),
            "account_category":           "INVESTMENT",
            "raw_type":                   "",
            "currency":                   currency,
            "current_balance":            balance_amt,
            "balance_sheet_category":     bs_cat,
            "balance_sheet_subcategory":  bs_sub_to_use,
            "balance_sheet_detail":       bs_det,
            "updated_at":                 datetime.datetime.utcnow().isoformat(),
        })

    if rows:
        supabase.table("snaptrade_accounts").upsert(
            rows, on_conflict="snaptrade_account_id"
        ).execute()

    supabase.table("snaptrade_users").update({
        "last_synced_at": datetime.datetime.utcnow().isoformat()
    }).eq("household_id", household_id).execute()

    return {"accounts_synced": len(rows)}


_GENERIC_NAMES = {"default", ""}

def _build_display_name(institution: str, acct_name: str) -> str:
    """
    Build a human-readable account name that mirrors Plaid's convention of
    including the institution context in the name field.
      - "Fidelity Investments" + "Rollover IRA"  → "Fidelity Investments — Rollover IRA"
      - "Coinbase"             + "Default"        → "Coinbase"
      - ""                    + "Rollover IRA"    → "Rollover IRA"
    """
    name = acct_name.strip()
    inst = institution.strip()
    if not inst:
        return name or "Investment Account"
    if name.lower() in _GENERIC_NAMES:
        return inst
    return f"{inst} — {name}"


_CRYPTO_INSTITUTIONS = {"COINBASE", "KRAKEN", "GEMINI", "BINANCE", "CRYPTO.COM", "BITFINEX"}

def _map_account_type(account_category: str, raw_type: str, name: str = "", institution: str = "") -> tuple[str, str, str]:
    # Check raw_type + account name for retirement/savings type keywords
    check = (raw_type + " " + name).upper()
    if "401K" in check or "401(K)" in check:  return "INVESTMENTS", "retirement_401k",    "Retirement"
    if "ROTH" in check:                        return "INVESTMENTS", "retirement_roth",    "Retirement"
    if "IRA"  in check:                        return "INVESTMENTS", "retirement_ira",     "Retirement"
    if "529"  in check:                        return "INVESTMENTS", "education_529",      "Education"
    if "HSA"  in check:                        return "INVESTMENTS", "hsa",                "Health Savings"
    # Detect crypto exchanges by institution name
    inst = institution.upper()
    if any(x in inst for x in _CRYPTO_INSTITUTIONS):
        return "INVESTMENTS", "cryptocurrency", "Cryptocurrency"
    if account_category == "DEPOSIT":          return "CASH",        "checking",           "Checking"
    if account_category == "LOC":              return "LIABILITIES", "credit_card_manual", "Line of Credit"
    return "INVESTMENTS", "taxable_brokerage", "Brokerage"
