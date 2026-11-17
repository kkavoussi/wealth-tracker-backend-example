"""
Plaid Service
Fetches transactions and account balances from Plaid using access tokens
stored in the Supabase plaid_items table, then upserts into the transactions
and plaid_accounts tables.
"""

import datetime
import json
from plaid.api import plaid_api
from plaid import ApiClient, Configuration
from plaid.exceptions import ApiException
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from plaid.model.accounts_get_request import AccountsGetRequest

from app.core.config import settings
from app.core.supabase import get_supabase_admin_client
from app.services.categorization_service import (
    find_cross_account_matches,
    categorize_transaction,
)


def _get_plaid_client():
    host = (
        "https://sandbox.plaid.com"
        if settings.plaid_env == "sandbox"
        else "https://production.plaid.com"
    )
    secret = (
        settings.plaid_sandbox_secret
        if settings.plaid_env == "sandbox"
        else settings.plaid_secret
    )
    config = Configuration(
        host=host,
        api_key={"clientId": settings.plaid_client_id, "secret": secret},
    )
    return plaid_api.PlaidApi(ApiClient(config))


def get_household_id_for_user(user_id: str) -> str | None:
    """Return the household_id for an authenticated user, or None if not found."""
    supabase = get_supabase_admin_client()
    result = (
        supabase.table("household_members")
        .select("household_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["household_id"]
    return None


def _safe_str(val) -> str | None:
    """Convert a Plaid enum/object value to a plain string, or None."""
    if val is None:
        return None
    if hasattr(val, "value"):
        return str(val.value)
    return str(val)


def _fetch_all_transactions(plaid_client, access_token: str, start_date, end_date) -> list:
    """Fetch all transactions for a Plaid item, handling pagination."""
    all_txns = []
    offset = 0
    batch_size = 500

    while True:
        request = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=TransactionsGetRequestOptions(count=batch_size, offset=offset),
        )
        response = plaid_client.transactions_get(request)
        batch = response["transactions"]
        all_txns.extend(batch)
        if len(all_txns) >= response["total_transactions"]:
            break
        offset += len(batch)

    return all_txns


def _plaid_txn_to_dict(txn, plaid_account_id_to_db: dict, household_id: str) -> dict | None:
    """
    Map a Plaid transaction object to a dict matching our transactions table schema.
    Returns None if the account is unknown (not yet seeded).
    """
    plaid_account_id = _safe_str(txn.get("account_id") if isinstance(txn, dict) else txn.account_id)
    if plaid_account_id not in plaid_account_id_to_db:
        return None

    db_account_id = plaid_account_id_to_db[plaid_account_id]

    def get(key):
        if isinstance(txn, dict):
            return txn.get(key)
        return getattr(txn, key, None)

    # Plaid amount convention: positive = outflow (debit), negative = inflow (credit)
    amount = float(get("amount") or 0)
    display_amount = -amount  # Our convention: positive = income

    # personal_finance_category is a sub-object
    pfc = get("personal_finance_category")
    if pfc is not None:
        plaid_primary    = _safe_str(pfc.get("primary")          if isinstance(pfc, dict) else getattr(pfc, "primary", None))
        plaid_detailed   = _safe_str(pfc.get("detailed")         if isinstance(pfc, dict) else getattr(pfc, "detailed", None))
        plaid_confidence = _safe_str(pfc.get("confidence_level") if isinstance(pfc, dict) else getattr(pfc, "confidence_level", None))
    else:
        plaid_primary = plaid_detailed = plaid_confidence = None

    # Date handling
    txn_date = get("date")
    if isinstance(txn_date, datetime.date):
        txn_date = txn_date.isoformat()

    auth_date = get("authorized_date")
    if isinstance(auth_date, datetime.date):
        auth_date = auth_date.isoformat()

    # Location
    loc = get("location")
    if loc is not None:
        loc_address      = _safe_str(loc.get("address")       if isinstance(loc, dict) else getattr(loc, "address", None))
        loc_city         = _safe_str(loc.get("city")          if isinstance(loc, dict) else getattr(loc, "city", None))
        loc_region       = _safe_str(loc.get("region")        if isinstance(loc, dict) else getattr(loc, "region", None))
        loc_postal       = _safe_str(loc.get("postal_code")   if isinstance(loc, dict) else getattr(loc, "postal_code", None))
        loc_country      = _safe_str(loc.get("country")       if isinstance(loc, dict) else getattr(loc, "country", None))
        loc_lat          = loc.get("lat")  if isinstance(loc, dict) else getattr(loc, "lat", None)
        loc_lon          = loc.get("lon")  if isinstance(loc, dict) else getattr(loc, "lon", None)
        loc_store_number = _safe_str(loc.get("store_number")  if isinstance(loc, dict) else getattr(loc, "store_number", None))
    else:
        loc_address = loc_city = loc_region = loc_postal = loc_country = None
        loc_lat = loc_lon = loc_store_number = None

    return {
        "household_id":            household_id,
        "account_id":              db_account_id,
        "plaid_transaction_id":    _safe_str(get("transaction_id")),
        "pending_transaction_id":  _safe_str(get("pending_transaction_id")),
        "date":                    txn_date,
        "authorized_date":         auth_date,
        "amount":                  amount,
        "display_amount":          display_amount,
        "iso_currency_code":       _safe_str(get("iso_currency_code")) or "USD",
        "unofficial_currency_code":_safe_str(get("unofficial_currency_code")),
        "name":                    _safe_str(get("name")) or "",
        "merchant_name":           _safe_str(get("merchant_name")),
        "merchant_entity_id":      _safe_str(get("merchant_entity_id")),
        "logo_url":                _safe_str(get("logo_url")),
        "check_number":            _safe_str(get("check_number")),
        "pending":                 bool(get("pending") or False),
        "payment_channel":         _safe_str(get("payment_channel")),
        "transaction_type":        _safe_str(get("transaction_type")),
        "account_owner":           _safe_str(get("account_owner")),
        "plaid_category_primary":  plaid_primary,
        "plaid_category_detailed": plaid_detailed,
        "plaid_category_confidence": plaid_confidence,
        "location_address":        loc_address,
        "location_city":           loc_city,
        "location_region":         loc_region,
        "location_postal_code":    loc_postal,
        "location_country":        loc_country,
        "location_lat":            float(loc_lat) if loc_lat is not None else None,
        "location_lon":            float(loc_lon) if loc_lon is not None else None,
        "location_store_number":   loc_store_number,
        "import_source":           "plaid_api",
    }


def sync_transactions(household_id: str) -> dict:
    """
    Full transaction sync for a household:
      1. Fetch all plaid_items (with access tokens) for the household
      2. Build plaid_account_id → db account_id map
      3. Fetch all available transaction history from Plaid (start date 2000-01-01,
         Plaid caps at whatever the institution supports — typically 24 months)
      4. Run cross-account matching across all transactions
      5. Categorize each transaction
      6. Upsert into transactions table
      7. Update last_synced_at on plaid_items
      8. Return summary stats

    Returns dict with keys: items_synced, transactions_fetched, inserted, skipped, errors
    """
    supabase   = get_supabase_admin_client()
    plaid      = _get_plaid_client()

    end_date   = datetime.date.today()
    start_date = datetime.date(2000, 1, 1)  # as far back as Plaid allows per institution

    # -- 1. Get all plaid_items for this household --
    items_result = (
        supabase.table("plaid_items")
        .select("item_id, plaid_item_id, access_token, institution_name")
        .eq("household_id", household_id)
        .eq("status", "active")
        .execute()
    )
    if not items_result.data:
        return {"error": "No active Plaid items found for this household."}

    # -- 1b. Refresh account balances for each item --
    fetch_errors = []
    items_requiring_reauth = []
    seen_reauth_ids: set = set()
    for item in items_result.data:
        try:
            accounts_response = plaid.accounts_get(
                AccountsGetRequest(access_token=item["access_token"])
            )
            raw_accounts = (
                accounts_response.get("accounts")
                if isinstance(accounts_response, dict)
                else getattr(accounts_response, "accounts", [])
            ) or []

            for acct in raw_accounts:
                def _get(obj, key):
                    if isinstance(obj, dict):
                        return obj.get(key)
                    return getattr(obj, key, None)

                plaid_acct_id = _safe_str(_get(acct, "account_id"))
                if not plaid_acct_id:
                    continue

                balances      = _get(acct, "balances") or {}
                bal_current   = _get(balances, "current")
                bal_available = _get(balances, "available")
                bal_limit     = _get(balances, "limit")

                updates = {}
                if bal_current   is not None: updates["current_balance"]   = float(bal_current)
                if bal_available is not None: updates["available_balance"] = float(bal_available)
                if bal_limit     is not None: updates["credit_limit"]      = float(bal_limit)

                if updates:
                    supabase.table("plaid_accounts").update(updates).eq(
                        "plaid_account_id", plaid_acct_id
                    ).eq("household_id", household_id).execute()

        except ApiException as e:
            try:
                error_code = json.loads(e.body).get("error_code", "") if e.body else ""
            except Exception:
                error_code = ""
            if error_code == "ITEM_LOGIN_REQUIRED" and item["plaid_item_id"] not in seen_reauth_ids:
                seen_reauth_ids.add(item["plaid_item_id"])
                items_requiring_reauth.append({
                    "institution_name": item.get("institution_name", "Unknown"),
                    "plaid_item_id":    item["plaid_item_id"],
                })
            else:
                fetch_errors.append(
                    f"Balance refresh error ({item.get('institution_name', 'unknown')}): {str(e)}"
                )
        except Exception as e:
            fetch_errors.append(
                f"Balance refresh error ({item.get('institution_name', 'unknown')}): {str(e)}"
            )

    # -- 2. Build plaid_account_id → db account_id map --
    accounts_result = (
        supabase.table("plaid_accounts")
        .select("account_id, plaid_account_id")
        .eq("household_id", household_id)
        .execute()
    )
    account_map = {a["plaid_account_id"]: a["account_id"] for a in accounts_result.data}

    # -- 3. Fetch all transactions from Plaid --
    all_raw_txns = []
    items_synced = 0

    for item in items_result.data:
        institution = item.get("institution_name", item["plaid_item_id"])
        # Skip items already flagged for re-auth — token is dead, fetch will fail too.
        if item["plaid_item_id"] in seen_reauth_ids:
            continue
        try:
            raw = _fetch_all_transactions(plaid, item["access_token"], start_date, end_date)
            all_raw_txns.extend(raw)
            items_synced += 1
        except ApiException as e:
            try:
                error_code = json.loads(e.body).get("error_code", "") if e.body else ""
            except Exception:
                error_code = ""
            if error_code == "ITEM_LOGIN_REQUIRED" and item["plaid_item_id"] not in seen_reauth_ids:
                seen_reauth_ids.add(item["plaid_item_id"])
                items_requiring_reauth.append({
                    "institution_name": item.get("institution_name", "Unknown"),
                    "plaid_item_id":    item["plaid_item_id"],
                })
            else:
                fetch_errors.append(f"{institution}: {str(e)}")
        except Exception as e:
            fetch_errors.append(f"{institution}: {str(e)}")

    if not all_raw_txns:
        return {
            "items_synced":           items_synced,
            "transactions_fetched":   0,
            "inserted":               0,
            "skipped":                0,
            "errors":                 fetch_errors,
            "items_requiring_reauth": items_requiring_reauth,
        }

    # -- 4. Convert raw Plaid objects to dicts for matching and categorization --
    txn_dicts = []
    for raw in all_raw_txns:
        d = _plaid_txn_to_dict(raw, account_map, household_id)
        if d:
            txn_dicts.append(d)

    # -- 5. Cross-account matching --
    cross_matches = find_cross_account_matches(txn_dicts)

    # -- 6. Categorize each transaction --
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows_to_upsert = []
    for txn in txn_dicts:
        cat_result = categorize_transaction(txn, cross_matches)
        row = {**txn, **cat_result, "processing_batch_id": now_iso[:10] + "-sync"}
        rows_to_upsert.append(row)

    # -- 7a. Protect user_override transactions from re-categorization on sync --
    # Match on EITHER categorization_source OR manually_overridden so that transactions
    # whose categorization_source was accidentally nulled out are still protected.
    override_result = (
        supabase.table("transactions")
        .select("plaid_transaction_id")
        .eq("household_id", household_id)
        .or_("categorization_source.eq.user_override,manually_overridden.eq.true")
        .execute()
    )
    override_ids = {r["plaid_transaction_id"] for r in (override_result.data or [])}

    if override_ids:
        # Skip user_override transactions entirely — do not upsert them at all.
        # Partial-field stripping caused PostgREST to NULL out the category columns
        # on UPDATE. Skipping preserves all manually-set fields in the DB unchanged.
        rows_to_upsert = [
            row for row in rows_to_upsert
            if row.get("plaid_transaction_id") not in override_ids
        ]

    # -- 7. Upsert into transactions table in batches of 100 --
    inserted = 0
    skipped  = 0
    batch_size = 100

    for i in range(0, len(rows_to_upsert), batch_size):
        batch = rows_to_upsert[i : i + batch_size]
        try:
            result = (
                supabase.table("transactions")
                .upsert(batch, on_conflict="household_id,plaid_transaction_id")
                .execute()
            )
            inserted += len(result.data) if result.data else 0
        except Exception as e:
            skipped += len(batch)
            fetch_errors.append(f"Batch upsert error: {str(e)}")

    # -- 8. Update last_synced_at on all items --
    for item in items_result.data:
        try:
            supabase.table("plaid_items").update(
                {"last_synced_at": now_iso}
            ).eq("item_id", item["item_id"]).execute()
        except Exception:
            pass

    # -- 9. Re-tag payroll deposits for all active payroll connections --
    # New transactions won't have is_payroll_deposit set; run after every sync.
    # Lazy import to avoid circular dependency (payroll_service imports plaid_service).
    try:
        from app.services.payroll_service import _mark_payroll_deposits  # noqa: PLC0415
        payroll_conns = (
            supabase.table("payroll_connections")
            .select("id, member_id, employer_name")
            .eq("household_id", household_id)
            .execute()
        )
        for conn in (payroll_conns.data or []):
            _mark_payroll_deposits(
                connection_id=conn["id"],
                household_id=household_id,
                member_id=conn.get("member_id"),
                employer_name=conn.get("employer_name"),
            )
    except Exception as e:
        fetch_errors.append(f"Payroll deposit tagging error: {str(e)}")

    return {
        "items_synced":           items_synced,
        "transactions_fetched":   len(all_raw_txns),
        "transactions_processed": len(rows_to_upsert),
        "inserted":               inserted,
        "skipped":                skipped,
        "errors":                 fetch_errors,
        "items_requiring_reauth": items_requiring_reauth,
        "period": {
            "start_date": start_date.isoformat(),
            "end_date":   end_date.isoformat(),
        },
    }
