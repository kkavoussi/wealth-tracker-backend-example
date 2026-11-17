"""
Payroll Service
Handles Plaid income verification: link token creation, paystub syncing,
transaction matching, and period summary aggregation.

Flow:
1. /payroll/create-link-token
   - Calls /user/create to get a user_token (stored in payroll_connections).
   - Creates a Plaid Link token with user_token + income_verification config.
2. Frontend opens Plaid Link. On success, calls /payroll/sync (no body needed).
3. /payroll/sync — backend retrieves user_token from DB and calls
   income_verification_paystubs_get(user_token=...) to fetch paystubs.
"""

import datetime
import requests
from typing import Optional

from plaid.model.credit_payroll_income_get_request import CreditPayrollIncomeGetRequest
from plaid.model.user_create_request import UserCreateRequest

from app.core.config import settings
from app.core.supabase import get_supabase_admin_client
from app.services.plaid_service import _get_plaid_client


def _plaid_base_url() -> str:
    return "https://sandbox.plaid.com" if settings.plaid_env == "sandbox" else "https://production.plaid.com"


def _plaid_secret() -> str:
    return settings.plaid_sandbox_secret if settings.plaid_env == "sandbox" else settings.plaid_secret


# =============================================================================
# DEDUCTION DESCRIPTION → COLUMN MAPPING
# =============================================================================

# All US state + DC abbreviations. Used to detect state-specific payroll tax
# codes from any payroll processor without hardcoding individual codes.
_US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}


def _is_state_payroll_code(desc: str) -> bool:
    """
    Return True if desc looks like a state income or payroll tax code.

    Handles:
      - 2-letter state abbreviations: "ny", "ca", "nj"
      - State-prefixed payroll tax codes: "nypsl-e", "nysdi-e", "ca sdi",
        "nj sui" — common across ADP, Paylocity, Paychex, etc.

    Must be called AFTER SS and Medicare checks so "ss" is not caught here.
    """
    if len(desc) == 2 and desc.isalpha():
        return True  # exact state abbreviation
    if len(desc) >= 3 and desc[:2] in _US_STATE_CODES:
        rest = desc[2:]
        # Common state payroll tax suffixes used by payroll processors
        if any(s in rest for s in ("sdi", "sui", "psl", "pdi", "sit", "swt", "wh", "tax")):
            return True
    return False


def _map_deduction(description: str) -> str:
    """
    Map a free-text deduction description from Plaid to a payroll_paystubs column.
    Returns the column name.

    Handles both full descriptions (e.g. "Federal Income Tax") and the
    abbreviated codes Paylocity sends (e.g. "FITW", "SS", "MED", "NY").

    SS and Medicare are checked before state so "SS" (2 letters, all alpha)
    is not mis-routed to state_income_tax by the state abbreviation rule.
    """
    desc = description.lower().strip()

    # Federal
    if (
        ("federal" in desc and ("income" in desc or "withhold" in desc or "tax" in desc))
        or desc in ("fit", "fitw", "fed tax", "federal wh")
    ):
        return "federal_income_tax"

    # Social Security — must come before state to prevent "ss" hitting the
    # 2-letter state abbreviation catch-all inside _is_state_payroll_code.
    if (
        "social security" in desc or "oasdi" in desc
        or desc in ("ss", "social sec")
        or ("fica" in desc and "medicare" not in desc)
    ):
        return "social_security_tax"

    # Medicare — also before state for consistency.
    if "medicare" in desc or desc in ("med", "fica med"):
        return "medicare_tax"

    # State income / payroll taxes (NY, CA, NYPSL-E, NYSDI-E, NJ SUI, etc.)
    if _is_state_payroll_code(desc):
        return "state_income_tax"

    if "local" in desc and ("income" in desc or "withhold" in desc or "tax" in desc):
        return "local_income_tax"

    # 401k / retirement (check Roth first)
    if ("roth" in desc and "401" in desc) or ("roth" in desc and "retirement" in desc):
        return "roth_401k"
    if "401" in desc or "403b" in desc or "403(b)" in desc:
        return "retirement_401k"

    # HSA / FSA
    if "hsa" in desc or "health savings" in desc:
        return "hsa_contribution"
    if ("dep" in desc and "care" in desc) or (
        "dependent" in desc and ("care" in desc or "fsa" in desc)
    ):
        return "dep_care_fsa"
    if "fsa" in desc or "flexible spending" in desc or "flex spending" in desc:
        return "fsa_contribution"

    # Insurance
    if "dental" in desc:
        return "dental_insurance"
    if "vision" in desc:
        return "vision_insurance"
    if "health" in desc or "medical" in desc or "insurance" in desc:
        return "health_insurance"

    # ESPP / Roth (post-tax)
    if "espp" in desc or "stock purchase" in desc:
        return "espp_contribution"

    # Default: other_pretax (conservative — most deductions are pre-tax)
    return "other_pretax"


# =============================================================================
# PAYROLL INCOME REFRESH
# =============================================================================

def _trigger_payroll_refresh(plaid_user_token: str) -> None:
    """
    Call /credit/payroll_income/refresh to ask Plaid to pull fresh data from
    the payroll provider. This is fire-and-forget: Plaid processes the refresh
    asynchronously and POSTs to our webhook when done.

    Never raises — a failed refresh degrades gracefully to cached data.
    """
    webhook_url = f"{settings.backend_url.rstrip('/')}/payroll/webhook"
    payload = {
        "client_id": settings.plaid_client_id,
        "secret": _plaid_secret(),
        "user_token": plaid_user_token,
        "options": {"webhook": webhook_url},
    }
    try:
        resp = requests.post(
            f"{_plaid_base_url()}/credit/payroll_income/refresh",
            json=payload,
            headers={"Plaid-Version": "2020-09-14"},
            timeout=15,
        )
        print(
            f"[payroll_refresh] status={resp.status_code} body={resp.json()}",
            flush=True,
        )
    except Exception as exc:
        print(f"[payroll_refresh] error (non-fatal): {exc}", flush=True)


# =============================================================================
# LINK TOKEN
# =============================================================================

def create_payroll_link_token(user_id: str, household_id: str, member_id: Optional[str]) -> str:
    """
    Create a Plaid Link token scoped to income_verification (payroll source).

    The income_verification product requires a user_token (obtained via
    /user/create) passed to link_token_create — NOT the regular client_user_id
    flow. The user_token is stored in payroll_connections so sync can reuse it.

    Returns the link_token string.
    """
    plaid = _get_plaid_client()
    supabase = get_supabase_admin_client()

    # Reuse an existing user_token if we already have one for this member.
    # Prefer a Plaid connection (non-null token) over a manual connection (null token).
    # A manual paystub upload creates a NULL-token row for the same member_id, and
    # an unordered .limit(1) could return that row instead of the Plaid row, causing
    # user_create to be called and fail with "user already exists".
    existing = (
        supabase.table("payroll_connections")
        .select("id, plaid_user_token")
        .eq("member_id", member_id)
        .not_.is_("plaid_user_token", "null")
        .limit(1)
        .execute()
    )
    if not existing.data:
        existing = (
            supabase.table("payroll_connections")
            .select("id, plaid_user_token")
            .eq("member_id", member_id)
            .limit(1)
            .execute()
        )

    # /user/create returns user_token (format: "user-<env>-<id>") once
    # user_token access has been enabled on the Plaid account.
    plaid_user_token = None
    if existing.data:
        stored = existing.data[0].get("plaid_user_token") or ""
        if stored.startswith("user-"):
            plaid_user_token = stored

    if not plaid_user_token:
        # Create a Plaid user — required for income_verification product.
        # If user_id was already registered with Plaid but the token was never
        # persisted to our DB (silent insert failure on a previous attempt),
        # fall back to member_id as client_user_id to create a fresh Plaid user
        # and recover the connection.
        try:
            user_response = plaid.user_create(UserCreateRequest(client_user_id=user_id))
        except Exception as e:
            if "user already exists" in str(e).lower() and member_id:
                print(
                    f"[payroll] user_create conflict for user_id={user_id} — "
                    f"token not in DB, retrying with member_id={member_id}",
                    flush=True,
                )
                user_response = plaid.user_create(UserCreateRequest(client_user_id=member_id))
            else:
                raise
        plaid_user_token = user_response["user_token"]  # "user-<env>-<id>"

        # Persist so sync can retrieve it later.
        # Check result.data explicitly — a silent failure here permanently locks
        # the user out (Plaid has the user, we've lost the token).
        if existing.data:
            result = supabase.table("payroll_connections").update(
                {"plaid_user_token": plaid_user_token}
            ).eq("id", existing.data[0]["id"]).execute()
            if not result.data:
                raise RuntimeError("Failed to persist plaid_user_token to existing payroll_connections row.")
        else:
            result = supabase.table("payroll_connections").insert({
                "household_id": household_id,
                "member_id": member_id,
                "plaid_user_token": plaid_user_token,
            }).execute()
            if not result.data:
                raise RuntimeError("Failed to insert payroll_connections row with plaid_user_token.")

    # plaid-python v38 SDK's LinkTokenCreateRequest has user_token support.
    # Bypass the SDK and POST directly to keep this explicit and avoid any
    # version-specific SDK quirks.
    payload = {
        "client_id": settings.plaid_client_id,
        "secret": _plaid_secret(),
        "client_name": "Wealth Tracker",
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": user_id},
        "user_token": plaid_user_token,
        "products": ["income_verification"],
        "income_verification": {
            "income_source_types": ["payroll"],
        },
    }
    resp = requests.post(
        f"{_plaid_base_url()}/link/token/create",
        json=payload,
        headers={"Plaid-Version": "2020-09-14"},
        timeout=30,
    )
    body = resp.json()
    if not resp.ok:
        raise RuntimeError(
            f"Plaid link/token/create failed {resp.status_code}: "
            f"{body.get('error_message') or body}"
        )
    return body["link_token"]


# =============================================================================
# PAYSTUB SYNC
# =============================================================================

def sync_paystubs(household_id: str, member_id: Optional[str], trigger_refresh: bool = True) -> dict:
    """
    Retrieve paystubs using the stored user_token for this member, parse
    deductions, store them, and mark matching payroll deposit transactions.

    The user_token was stored by create_payroll_link_token before the Link
    session. Plaid's income_verification_paystubs_get accepts user_token
    directly — no income_verification_id needed.

    Returns { paystubs_synced: int, employer_name: str }
    """
    supabase = get_supabase_admin_client()
    plaid = _get_plaid_client()

    conn_result = (
        supabase.table("payroll_connections")
        .select("id, plaid_user_token")
        .eq("member_id", member_id)
        .not_.is_("plaid_user_token", "null")
        .limit(1)
        .execute()
    )
    if not conn_result.data:
        raise ValueError("No payroll connection found for this member.")

    connection_id = conn_result.data[0]["id"]
    plaid_user_token = conn_result.data[0]["plaid_user_token"]  # stored as "user-<env>-<id>"

    # Ask Plaid to pull fresh data from the payroll provider. Async — Plaid will
    # POST to /payroll/webhook when the refresh completes, triggering another sync.
    # Skipped when called from the webhook handler to prevent an infinite loop.
    if trigger_refresh:
        _trigger_payroll_refresh(plaid_user_token)

    response = plaid.credit_payroll_income_get(
        CreditPayrollIncomeGetRequest(user_token=plaid_user_token)
    )

    # Convert the SDK typed response object to a plain dict.
    # Confirmed response shape: { "items": [...], "request_id": "..." }
    try:
        resp_dict = response.to_dict()
    except Exception:
        resp_dict = {}

    # Response shape: items[].payroll_income[].pay_stubs[]
    # Each top-level item is one institution (e.g. Paylocity).
    # Within each item, payroll_income is a list of income groups (usually one).
    # Paystubs live inside each income group.
    all_paystubs = []
    for item in (resp_dict.get("items") or []):
        institution_name = item.get("institution_name")
        for income_group in (item.get("payroll_income") or []):
            for stub in (income_group.get("pay_stubs") or []):
                stub["_institution_name"] = institution_name
                all_paystubs.append(stub)

    # Log what Plaid returned so we can debug missing paystubs / W2s in Railway.
    item_summary = [
        {
            "institution": item.get("institution_name"),
            "paystub_count": sum(
                len(ig.get("pay_stubs") or [])
                for ig in (item.get("payroll_income") or [])
            ),
            "w2_count": len(item.get("w2s") or []),
            "w2_years": [str(w.get("tax_year")) for w in (item.get("w2s") or [])],
        }
        for item in (resp_dict.get("items") or [])
    ]
    print(f"[payroll_sync] items: {item_summary}", flush=True)

    if not all_paystubs:
        return {"paystubs_synced": 0, "employer_name": None}

    employer_name = None
    paystubs_synced = 0

    for stub in all_paystubs:
        # ── Employer ──────────────────────────────────────────────────────────
        # employer.name inside the stub is the actual company name (e.g.
        # "Merlyn Mind Inc"). institution_name is the payroll processor
        # (e.g. "Paylocity") — ACH deposits use the company name, not the
        # processor, so we must not use the processor for Pass 2 matching.
        true_employer_name = None
        try:
            true_employer_name = stub["employer"]["name"] or None
        except (KeyError, TypeError):
            pass

        # Row-level employer_name: use real employer name when available,
        # else fall back to payroll processor name for the stored record.
        stub_employer = true_employer_name or stub.get("_institution_name")

        # Connection-level employer_name (used for transaction matching):
        # update whenever we find the real employer name so the true company
        # name wins over any previously set processor name.
        if true_employer_name:
            employer_name = true_employer_name
        elif not employer_name:
            employer_name = stub_employer

        # ── Pay Period ────────────────────────────────────────────────────────
        # Plaid uses pay_period_details (not pay_period).
        # Gross pay: earnings.total.current_amount
        # Net pay:   net_pay.current_amount
        try:
            pp = stub["pay_period_details"]
        except (KeyError, TypeError):
            continue

        # Prefer pay_date; fall back to end_date
        pay_date = pp.get("pay_date") or pp.get("end_date")
        if not pay_date:
            continue
        pay_date_str = pay_date.isoformat() if isinstance(pay_date, datetime.date) else str(pay_date)

        def _to_date_str(val) -> Optional[str]:
            if val is None:
                return None
            return val.isoformat() if isinstance(val, datetime.date) else str(val)

        pay_period_start = _to_date_str(pp.get("start_date"))
        pay_period_end   = _to_date_str(pp.get("end_date"))

        pay_frequency = None
        try:
            pf = pp.get("pay_frequency")
            pay_frequency = pf["value"] if isinstance(pf, dict) else str(pf) if pf else None
        except (KeyError, TypeError):
            pass

        # ── Gross Pay + YTD ───────────────────────────────────────────────────
        gross_pay     = 0.0
        gross_pay_ytd = 0.0
        try:
            earnings_total = stub["earnings"]["total"]
            gross_pay     = float(earnings_total.get("current_amount") or 0)
            gross_pay_ytd = float(earnings_total.get("ytd_amount") or 0)
        except (KeyError, TypeError):
            pass

        # ── Earnings breakdown by type (REGULAR_PAY, BONUS, OVERTIME, etc.) ──
        earnings_breakdown: dict[str, float] = {}
        earnings_breakdown_ytd: dict[str, float] = {}
        try:
            for eb in (stub["earnings"].get("breakdown") or []):
                canonical = (eb.get("canonical_description") or "").upper()
                current   = float(eb.get("current_amount") or 0)
                ytd       = float(eb.get("ytd_amount") or 0)
                col = {
                    "REGULAR_PAY":        "regular_pay",
                    "BONUS":              "bonus_pay",
                    "OVERTIME":           "overtime_pay",
                }.get(canonical, "other_earnings")
                earnings_breakdown[col]     = earnings_breakdown.get(col, 0.0)     + current
                earnings_breakdown_ytd[col] = earnings_breakdown_ytd.get(col, 0.0) + ytd
        except (KeyError, TypeError):
            pass

        # ── Net Pay + YTD ─────────────────────────────────────────────────────
        net_pay     = 0.0
        net_pay_ytd = 0.0
        try:
            net_pay     = float(stub["net_pay"].get("current_amount") or 0)
            net_pay_ytd = float(stub["net_pay"].get("ytd_amount") or 0)
        except (KeyError, TypeError):
            pass

        # ── Deductions (period + YTD when provided) ───────────────────────────
        deduction_cols:     dict[str, float] = {}
        deduction_ytd_cols: dict[str, float] = {}
        try:
            breakdown = stub["deductions"]["breakdown"] or []
            for item_d in breakdown:
                try:
                    desc    = item_d.get("description") or item_d.get("type") or ""
                    current = float(item_d.get("current_amount") or item_d.get("amount") or 0)
                    ytd     = float(item_d.get("ytd_amount") or 0)
                    if current == 0 and ytd == 0:
                        continue
                    col = _map_deduction(str(desc))
                    deduction_cols[col]     = deduction_cols.get(col, 0.0)     + abs(current)
                    deduction_ytd_cols[col] = deduction_ytd_cols.get(col, 0.0) + abs(ytd)

                    # Employer 401k match lives in deductions on some providers
                    if "employer" in desc.lower() and "match" in desc.lower():
                        deduction_cols["employer_401k_match"]     = deduction_cols.get("employer_401k_match", 0.0)     + abs(current)
                        deduction_ytd_cols["employer_401k_match"] = deduction_ytd_cols.get("employer_401k_match", 0.0) + abs(ytd)
                except (KeyError, TypeError):
                    continue
        except (KeyError, TypeError):
            pass

        # ── Upsert row ────────────────────────────────────────────────────────
        row = {
            "connection_id":      connection_id,
            "household_id":       household_id,
            "member_id":          member_id,
            "pay_date":           pay_date_str,
            "pay_period_start":   pay_period_start,
            "pay_period_end":     pay_period_end,
            "employer_name":      stub_employer,
            "source":             "plaid",
            # Gross + net (period + YTD)
            "gross_pay":          gross_pay,
            "gross_pay_ytd":      gross_pay_ytd,
            "net_pay":            net_pay,
            "net_pay_ytd":        net_pay_ytd,
            # Earnings breakdown
            "regular_pay":        earnings_breakdown.get("regular_pay", 0.0),
            "regular_pay_ytd":    earnings_breakdown_ytd.get("regular_pay", 0.0),
            "bonus_pay":          earnings_breakdown.get("bonus_pay", 0.0),
            "bonus_pay_ytd":      earnings_breakdown_ytd.get("bonus_pay", 0.0),
            "overtime_pay":       earnings_breakdown.get("overtime_pay", 0.0),
            "overtime_pay_ytd":   earnings_breakdown_ytd.get("overtime_pay", 0.0),
            "other_earnings":     earnings_breakdown.get("other_earnings", 0.0),
            "other_earnings_ytd": earnings_breakdown_ytd.get("other_earnings", 0.0),
            # Taxes (period)
            "federal_income_tax":  deduction_cols.get("federal_income_tax", 0.0),
            "state_income_tax":    deduction_cols.get("state_income_tax", 0.0),
            "local_income_tax":    deduction_cols.get("local_income_tax", 0.0),
            "social_security_tax": deduction_cols.get("social_security_tax", 0.0),
            "medicare_tax":        deduction_cols.get("medicare_tax", 0.0),
            # Taxes (YTD)
            "federal_income_tax_ytd":  deduction_ytd_cols.get("federal_income_tax", 0.0),
            "state_income_tax_ytd":    deduction_ytd_cols.get("state_income_tax", 0.0),
            "local_income_tax_ytd":    deduction_ytd_cols.get("local_income_tax", 0.0),
            "social_security_tax_ytd": deduction_ytd_cols.get("social_security_tax", 0.0),
            "medicare_tax_ytd":        deduction_ytd_cols.get("medicare_tax", 0.0),
            # Pre-tax deductions (period)
            "retirement_401k":     deduction_cols.get("retirement_401k", 0.0),
            "roth_401k":           deduction_cols.get("roth_401k", 0.0),
            "hsa_contribution":    deduction_cols.get("hsa_contribution", 0.0),
            "fsa_contribution":    deduction_cols.get("fsa_contribution", 0.0),
            "dep_care_fsa":        deduction_cols.get("dep_care_fsa", 0.0),
            "health_insurance":    deduction_cols.get("health_insurance", 0.0),
            "dental_insurance":    deduction_cols.get("dental_insurance", 0.0),
            "vision_insurance":    deduction_cols.get("vision_insurance", 0.0),
            "other_pretax":        deduction_cols.get("other_pretax", 0.0),
            # Pre-tax deductions (YTD)
            "retirement_401k_ytd":  deduction_ytd_cols.get("retirement_401k", 0.0),
            "roth_401k_ytd":        deduction_ytd_cols.get("roth_401k", 0.0),
            "hsa_contribution_ytd": deduction_ytd_cols.get("hsa_contribution", 0.0),
            "fsa_contribution_ytd": deduction_ytd_cols.get("fsa_contribution", 0.0),
            "dep_care_fsa_ytd":     deduction_ytd_cols.get("dep_care_fsa", 0.0),
            "health_insurance_ytd": deduction_ytd_cols.get("health_insurance", 0.0),
            "dental_insurance_ytd": deduction_ytd_cols.get("dental_insurance", 0.0),
            "vision_insurance_ytd": deduction_ytd_cols.get("vision_insurance", 0.0),
            "other_pretax_ytd":     deduction_ytd_cols.get("other_pretax", 0.0),
            # Post-tax (period + YTD)
            "espp_contribution":    deduction_cols.get("espp_contribution", 0.0),
            "espp_contribution_ytd":deduction_ytd_cols.get("espp_contribution", 0.0),
            "other_posttax":        deduction_cols.get("other_posttax", 0.0),
            "other_posttax_ytd":    deduction_ytd_cols.get("other_posttax", 0.0),
            # Employer contributions
            "employer_401k_match":     deduction_cols.get("employer_401k_match", 0.0),
            "employer_401k_match_ytd": deduction_ytd_cols.get("employer_401k_match", 0.0),
            "raw_data": {
                "employer": stub_employer,
                "pay_frequency": pay_frequency,
            },
        }

        try:
            supabase.table("payroll_paystubs").upsert(
                row, on_conflict="connection_id,pay_date"
            ).execute()
            paystubs_synced += 1
            print(f"[payroll_sync] paystub upserted pay_date={pay_date_str} gross={gross_pay:.2f}", flush=True)
        except Exception as exc:
            print(f"[payroll_sync] paystub upsert FAILED pay_date={pay_date_str}: {exc}", flush=True)

    # ── Update connection metadata ─────────────────────────────────────────────
    connection_update: dict = {
        "last_synced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if employer_name:
        connection_update["employer_name"] = employer_name

    # Store pay frequency from first paystub if available
    if all_paystubs:
        try:
            pf_obj = all_paystubs[0]["pay_period_details"]["pay_frequency"]
            connection_update["pay_frequency"] = pf_obj["value"] if isinstance(pf_obj, dict) else str(pf_obj)
        except (KeyError, TypeError, IndexError):
            pass

    supabase.table("payroll_connections").update(connection_update).eq("id", connection_id).execute()

    # ── Upsert W2s from Plaid response ────────────────────────────────────────
    # Plaid returns W2 documents alongside paystubs. We store them in payroll_w2s
    # keyed by (connection_id, tax_year). Duplicate items return the same W2 — the
    # upsert deduplicates by that unique key.
    def _dec(val) -> Optional[float]:
        try:
            return float(val) if val not in (None, "") else None
        except (TypeError, ValueError):
            return None

    w2s_stored = 0
    for item in (resp_dict.get("items") or []):
        raw_w2s = item.get("w2s") or []
        print(f"[payroll_sync] W2 loop institution={item.get('institution_name')} w2_count={len(raw_w2s)} raw_keys={list(raw_w2s[0].keys()) if raw_w2s else []}", flush=True)
        for w2 in raw_w2s:
            try:
                tax_year = str(w2.get("tax_year") or "")
                if not tax_year:
                    print(f"[payroll_sync] W2 skipped — no tax_year. keys={list(w2.keys())}", flush=True)
                    continue
                w2_employer = (w2.get("employer") or {}).get("name") or employer_name

                w2_row = {
                    "connection_id":                connection_id,
                    "household_id":                 household_id,
                    "member_id":                    member_id,
                    "tax_year":                     tax_year,
                    "employer_name":                w2_employer,
                    "employer_ein":                 w2.get("employer_id_number"),
                    "wages_tips_other_comp":        _dec(w2.get("wages_tips_other_comp")),
                    "federal_income_tax_withheld":  _dec(w2.get("federal_income_tax_withheld")),
                    "social_security_wages":        _dec(w2.get("social_security_wages")),
                    "social_security_tax_withheld": _dec(w2.get("social_security_tax_withheld")),
                    "medicare_wages_and_tips":      _dec(w2.get("medicare_wages_and_tips")),
                    "medicare_tax_withheld":        _dec(w2.get("medicare_tax_withheld")),
                    "dependent_care_benefits":      _dec(w2.get("dependent_care_benefits")),
                    "box_12":                       w2.get("box_12"),
                    "state_and_local_wages":        w2.get("state_and_local_wages"),
                    "document_id":                  w2.get("document_id"),
                    "source":                       "plaid",
                }
                supabase.table("payroll_w2s").upsert(
                    w2_row, on_conflict="connection_id,tax_year"
                ).execute()
                w2s_stored += 1
                print(f"[payroll_sync] W2 stored tax_year={tax_year} employer={w2_employer}", flush=True)
            except Exception as exc:
                print(f"[payroll_sync] W2 upsert FAILED: {exc} | w2 keys={list(w2.keys())}", flush=True)

    print(f"[payroll_sync] W2 sync complete — stored {w2s_stored} W2(s)", flush=True)

    # ── Mark payroll deposit transactions ─────────────────────────────────────
    _mark_payroll_deposits(connection_id, household_id, member_id, employer_name)

    return {"paystubs_synced": paystubs_synced, "employer_name": employer_name}


# =============================================================================
# TRANSACTION MATCHING
# =============================================================================

def _mark_payroll_deposits(
    connection_id: str,
    household_id: str,
    member_id: Optional[str],
    employer_name: Optional[str] = None,
) -> None:
    """
    Mark paycheck ACH deposits as is_payroll_deposit = TRUE so the income
    statement can exclude them and use gross pay from paystubs instead.

    Two passes:
    1. Precise match: stored paystubs → date ±3 days + net_pay ±$1.00.
       Handles current paystubs; correctly skips FSA reimbursements.
    2. Name-pattern match: any INCOME transaction from the member's accounts
       whose name contains the employer keyword (e.g. "MERLYN" from
       "Merlyn Mind Inc"). Catches historical deposits that predate stored
       paystubs.
    """
    supabase = get_supabase_admin_client()

    # Get account_ids for this member
    if member_id:
        acct_result = (
            supabase.table("plaid_accounts")
            .select("account_id")
            .eq("household_id", household_id)
            .eq("member_id", member_id)
            .execute()
        )
        account_ids = [r["account_id"] for r in (acct_result.data or [])]
    else:
        account_ids = []

    # ── Pass 1: paystub date + individual amount matching ──────────────────
    stubs_result = (
        supabase.table("payroll_paystubs")
        .select("pay_date, net_pay")
        .eq("connection_id", connection_id)
        .execute()
    )
    for stub in (stubs_result.data or []):
        pay_date = datetime.date.fromisoformat(stub["pay_date"])
        net_pay  = float(stub["net_pay"])
        start    = (pay_date - datetime.timedelta(days=3)).isoformat()
        end      = (pay_date + datetime.timedelta(days=3)).isoformat()

        txn_q = (
            supabase.table("transactions")
            .select("transaction_id, date, display_amount")
            .eq("household_id", household_id)
            .eq("category", "INCOME")
            .eq("is_payroll_deposit", False)
            .gte("date", start)
            .lte("date", end)
        )
        if account_ids:
            txn_q = txn_q.in_("account_id", account_ids)

        window_txns = txn_q.execute().data or []

        # Individual match: single transaction equals net_pay (standard direct deposit)
        for txn in window_txns:
            if abs(float(txn.get("display_amount") or 0) - net_pay) <= 1.00:
                supabase.table("transactions").update(
                    {"is_payroll_deposit": True}
                ).eq("transaction_id", txn["transaction_id"]).execute()

        # Sum match: some payroll processors split a single net deposit into
        # multiple ACH transactions (e.g. main + expense + benefit allocations).
        # Group same-date transactions and tag the group if their sum ≈ net_pay.
        from collections import defaultdict
        by_date: dict = defaultdict(list)
        for txn in window_txns:
            by_date[txn["date"]].append(txn)

        for date_txns in by_date.values():
            if len(date_txns) < 2:
                continue
            group_sum = sum(float(t.get("display_amount") or 0) for t in date_txns)
            if abs(group_sum - net_pay) <= 1.00:
                for txn in date_txns:
                    supabase.table("transactions").update(
                        {"is_payroll_deposit": True}
                    ).eq("transaction_id", txn["transaction_id"]).execute()

    # ── Pass 2: employer name keyword matching (catches pre-paystub history) ──
    # Run a separate ilike query for each significant word (>= 4 chars, not a
    # generic stop word) in the employer name. This handles cases where the ACH
    # sender name differs from the stored employer name — e.g. "CBS INTERACTIVE
    # INC." sends deposits as both "CBS INTERACTIVE" and "PARAMOUNT GLOBAL
    # PAYROLL" because CBS is a Paramount subsidiary.
    _STOP_WORDS = {"INC", "LLC", "LTD", "CORP", "CO", "THE", "AND"}
    keywords: list[str] = []
    if employer_name:
        for word in employer_name.upper().split():
            clean = word.strip(".,;")
            if len(clean) >= 4 and clean not in _STOP_WORDS:
                keywords.append(clean)

    for keyword in keywords:
        txn_q = (
            supabase.table("transactions")
            .select("transaction_id")
            .eq("household_id", household_id)
            .eq("category", "INCOME")
            .eq("is_payroll_deposit", False)
            .ilike("name", f"%{keyword}%")
        )
        if account_ids:
            txn_q = txn_q.in_("account_id", account_ids)

        for txn in (txn_q.execute().data or []):
            supabase.table("transactions").update(
                {"is_payroll_deposit": True}
            ).eq("transaction_id", txn["transaction_id"]).execute()


# =============================================================================
# SUMMARY
# =============================================================================

def get_payroll_summary(
    household_id: str,
    member_id: Optional[str],
    start_date: datetime.date,
    end_date: datetime.date,
) -> Optional[dict]:
    """
    Aggregate payroll_paystubs for the period.

    YTD-first logic: for every deduction/tax field, if the most recent paystub
    (highest pay_date) has a non-zero YTD value stored, that value is used
    directly. Otherwise the field is estimated by scaling the sum of period
    amounts proportionally from ytd_gross / sum_gross.

    This means:
    - Providers that return per-deduction YTD (e.g. via PDF parsing) get
      exact figures with no estimation.
    - Paylocity via Plaid (all deduction ytd_amount = null) falls back to
      proportional scaling as before.
    - Manual uploads in any order are handled correctly because we always
      read YTD from the stub with the latest pay_date.

    Returns None if no payroll connection or no paystubs exist.
    """
    supabase = get_supabase_admin_client()

    q = (
        supabase.table("payroll_paystubs")
        .select(
            "id, pay_date, pay_period_start, pay_period_end, employer_name, source, "
            "gross_pay, gross_pay_ytd, net_pay, net_pay_ytd, "
            "regular_pay, bonus_pay, overtime_pay, other_earnings, "
            "federal_income_tax, federal_income_tax_ytd, "
            "state_income_tax, state_income_tax_ytd, "
            "local_income_tax, local_income_tax_ytd, "
            "social_security_tax, social_security_tax_ytd, "
            "medicare_tax, medicare_tax_ytd, "
            "retirement_401k, retirement_401k_ytd, "
            "roth_401k, roth_401k_ytd, "
            "hsa_contribution, hsa_contribution_ytd, "
            "fsa_contribution, fsa_contribution_ytd, "
            "dep_care_fsa, dep_care_fsa_ytd, "
            "health_insurance, health_insurance_ytd, "
            "dental_insurance, dental_insurance_ytd, "
            "vision_insurance, vision_insurance_ytd, "
            "other_pretax, other_pretax_ytd, "
            "espp_contribution, espp_contribution_ytd, "
            "other_posttax, other_posttax_ytd, "
            "employer_401k_match, employer_401k_match_ytd"
        )
        .eq("household_id", household_id)
        .gte("pay_date", start_date.isoformat())
        .lte("pay_date", end_date.isoformat())
    )
    if member_id:
        q = q.eq("member_id", member_id)

    rows = q.execute().data or []
    if not rows:
        return None

    # Sort ascending by pay_date; most_recent = rows[-1].
    rows = sorted(rows, key=lambda r: r["pay_date"])
    most_recent = rows[-1]

    def _raw_sum(col: str) -> float:
        return sum(float(r.get(col) or 0) for r in rows)

    sum_gross = _raw_sum("gross_pay")
    sum_net   = _raw_sum("net_pay")

    # YTD gross/net: now proper columns (moved from raw_data).
    ytd_gross = float(most_recent.get("gross_pay_ytd") or 0)
    ytd_net   = float(most_recent.get("net_pay_ytd")   or 0)

    # YTD-first scaling: only valid for Jan 1-anchored periods (YTD / full-year).
    # Paystub ytd fields always accumulate from Jan 1 of the calendar year, so
    # substituting ytd_gross for a March-only or Q1 call would inflate that
    # period's income with earnings from prior months.
    is_year_start = (start_date.month == 1 and start_date.day == 1)

    if is_year_start and ytd_gross > sum_gross and sum_gross > 0:
        gross_income = round(ytd_gross, 2)
        net_pay_out  = round(ytd_net, 2)
        scale        = ytd_gross / sum_gross
    else:
        gross_income = round(sum_gross, 2)
        net_pay_out  = round(sum_net, 2)
        scale        = 1.0

    def _ytd_or_scaled(col: str) -> float:
        """
        Use the YTD column value from the most recent stub when non-zero
        (provider returned it), otherwise scale the sum of period amounts.
        This is the YTD-first rule: exact when available, estimated as fallback.
        """
        ytd_val = float(most_recent.get(f"{col}_ytd") or 0)
        if ytd_val > 0:
            return round(ytd_val, 2)
        return round(_raw_sum(col) * scale, 2)

    employer_name = None
    for r in rows:
        if r.get("employer_name"):
            employer_name = r["employer_name"]
            break

    pre_tax_total = round(
        _ytd_or_scaled("retirement_401k") + _ytd_or_scaled("hsa_contribution") +
        _ytd_or_scaled("fsa_contribution") + _ytd_or_scaled("dep_care_fsa") +
        _ytd_or_scaled("health_insurance") + _ytd_or_scaled("dental_insurance") +
        _ytd_or_scaled("vision_insurance") + _ytd_or_scaled("other_pretax"),
        2,
    )
    taxes_total = round(
        _ytd_or_scaled("federal_income_tax") + _ytd_or_scaled("state_income_tax") +
        _ytd_or_scaled("local_income_tax")   + _ytd_or_scaled("social_security_tax") +
        _ytd_or_scaled("medicare_tax"),
        2,
    )
    post_tax_total = round(
        _ytd_or_scaled("roth_401k") + _ytd_or_scaled("espp_contribution") +
        _ytd_or_scaled("other_posttax"),
        2,
    )

    # Per-paystub list at actual pay-period amounts.
    paystubs = [
        {
            "paystub_id":       str(r["id"]),
            "pay_date":         r["pay_date"],
            "pay_period_start": r.get("pay_period_start"),
            "pay_period_end":   r.get("pay_period_end"),
            "employer_name":    r.get("employer_name") or employer_name,
            "gross_pay":        round(float(r.get("gross_pay") or 0), 2),
            "net_pay":          round(float(r.get("net_pay") or 0), 2),
            "regular_pay":      round(float(r.get("regular_pay") or 0), 2),
            "bonus_pay":        round(float(r.get("bonus_pay") or 0), 2),
            "overtime_pay":     round(float(r.get("overtime_pay") or 0), 2),
            "other_earnings":   round(float(r.get("other_earnings") or 0), 2),
            "source":           r.get("source", "plaid"),
            "pre_tax": {
                "retirement_401k":  round(float(r.get("retirement_401k") or 0), 2),
                "hsa":              round(float(r.get("hsa_contribution") or 0), 2),
                "fsa":              round(float(r.get("fsa_contribution") or 0), 2),
                "dep_care_fsa":     round(float(r.get("dep_care_fsa") or 0), 2),
                "health_insurance": round(float(r.get("health_insurance") or 0), 2),
                "dental_insurance": round(float(r.get("dental_insurance") or 0), 2),
                "vision_insurance": round(float(r.get("vision_insurance") or 0), 2),
                "other":            round(float(r.get("other_pretax") or 0), 2),
            },
            "taxes": {
                "federal":         round(float(r.get("federal_income_tax") or 0), 2),
                "state":           round(float(r.get("state_income_tax") or 0), 2),
                "local":           round(float(r.get("local_income_tax") or 0), 2),
                "social_security": round(float(r.get("social_security_tax") or 0), 2),
                "medicare":        round(float(r.get("medicare_tax") or 0), 2),
            },
            "post_tax": {
                "roth_401k": round(float(r.get("roth_401k") or 0), 2),
                "espp":      round(float(r.get("espp_contribution") or 0), 2),
                "other":     round(float(r.get("other_posttax") or 0), 2),
            },
        }
        for r in rows
    ]

    # ── Prior Balance ─────────────────────────────────────────────────────────
    # Synthetic entry covering the gap between Jan 1 and the earliest stored
    # paystub. Only generated for Jan 1-anchored periods: for monthly/quarterly
    # calls the ytd fields already exceed sum_gross for a different reason
    # (they reflect the full calendar YTD, not just the queried window), so
    # injecting a prior_balance there would add earnings from outside the period.
    prior_balance = None
    if is_year_start and ytd_gross > sum_gross and sum_gross > 0:
        balance_gross = round(ytd_gross - sum_gross, 2)
        balance_net   = round(ytd_net   - sum_net,   2)

        def _fmt(d: datetime.date) -> str:
            return f"{d.strftime('%b')} {d.day}"

        try:
            first_stub_date = datetime.date.fromisoformat(rows[0]["pay_date"])
            prior_end_date  = first_stub_date - datetime.timedelta(days=1)
            prior_end       = prior_end_date.isoformat()
            label = f"{_fmt(start_date)} \u2013 {_fmt(prior_end_date)}, {prior_end_date.year}"
        except Exception:
            prior_end = rows[0]["pay_date"]
            label     = "prior period"

        prior_balance = {
            "period_start":           start_date.isoformat(),
            "period_end":             prior_end,
            "gross_pay":              balance_gross,
            "net_pay":                balance_net,
            "label":                  label,
            "deductions_estimated":   True,   # flag for frontend warning
            "paystub_id":             str(most_recent["id"]),  # most recent stub owns gross_pay_ytd
        }

    return {
        "employer_name":       employer_name,
        "paystub_count":       len(rows),
        "gross_income":        gross_income,
        "net_pay":             net_pay_out,
        "paystubs":            paystubs,
        "prior_balance":       prior_balance,
        "deductions_scaled":   scale > 1.0,  # true when any deductions are estimated
        "employer_401k_match": _ytd_or_scaled("employer_401k_match"),
        "pre_tax": {
            "retirement_401k":  _ytd_or_scaled("retirement_401k"),
            "hsa":              _ytd_or_scaled("hsa_contribution"),
            "fsa":              _ytd_or_scaled("fsa_contribution"),
            "dep_care_fsa":     _ytd_or_scaled("dep_care_fsa"),
            "health_insurance": _ytd_or_scaled("health_insurance"),
            "dental_insurance": _ytd_or_scaled("dental_insurance"),
            "vision_insurance": _ytd_or_scaled("vision_insurance"),
            "other":            _ytd_or_scaled("other_pretax"),
            "total":            pre_tax_total,
        },
        "taxes": {
            "federal":         _ytd_or_scaled("federal_income_tax"),
            "state":           _ytd_or_scaled("state_income_tax"),
            "local":           _ytd_or_scaled("local_income_tax"),
            "social_security": _ytd_or_scaled("social_security_tax"),
            "medicare":        _ytd_or_scaled("medicare_tax"),
            "total":           taxes_total,
        },
        "post_tax": {
            "roth_401k": _ytd_or_scaled("roth_401k"),
            "espp":      _ytd_or_scaled("espp_contribution"),
            "other":     _ytd_or_scaled("other_posttax"),
            "total":     post_tax_total,
        },
    }
