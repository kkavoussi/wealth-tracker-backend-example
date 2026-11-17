"""
Payroll routes — Plaid income verification link token, sync, and connection status.
"""
import base64
import json
import os

import anthropic
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel
from typing import Optional

from app.core.security import get_current_user
from app.core.supabase import get_supabase_admin_client
from app.services.plaid_service import get_household_id_for_user
from app.services.household_service import get_member_id_for_user
from app.services import payroll_service

router = APIRouter()

ALLOWED_PAYSTUB_FIELDS = {
    "gross_pay", "net_pay",
    "regular_pay", "bonus_pay", "overtime_pay", "other_earnings",
    "federal_income_tax", "state_income_tax", "local_income_tax",
    "social_security_tax", "medicare_tax",
    "retirement_401k", "employer_401k_match", "roth_401k",
    "hsa_contribution", "fsa_contribution",
    "dep_care_fsa", "health_insurance", "dental_insurance", "vision_insurance",
    "other_pretax", "espp_contribution", "other_posttax",
    # Virtual field: sets gross_pay_ytd = user_value + sum_of_stored_stubs_gross
    "prior_pay_amount",
}

# Base field names allowed for the _prior virtual suffix.
# "{base}_prior" sets "{base}_ytd" = user_value + sum_of_stored_stubs_{base}.
_PRIOR_BASE_FIELDS = {
    "federal_income_tax", "state_income_tax", "local_income_tax",
    "social_security_tax", "medicare_tax",
    "retirement_401k", "roth_401k", "hsa_contribution", "fsa_contribution",
    "dep_care_fsa", "health_insurance", "dental_insurance", "vision_insurance",
    "other_pretax", "espp_contribution", "other_posttax",
}


class UpdatePaystubRequest(BaseModel):
    field: str
    value: float
    employer_name: Optional[str] = None


@router.patch("/paystubs/{paystub_id}")
def update_paystub(
    paystub_id: str,
    body: UpdatePaystubRequest,
    user=Depends(get_current_user),
):
    """Edit a single numeric field on a paystub. Only whitelisted fields are allowed."""
    is_prior = body.field.endswith("_prior")
    base_col = body.field[:-6] if is_prior else None  # strip "_prior"

    if is_prior:
        if base_col not in _PRIOR_BASE_FIELDS:
            raise HTTPException(status_code=400, detail=f"Field '{body.field}' cannot be edited.")
    elif body.field not in ALLOWED_PAYSTUB_FIELDS:
        raise HTTPException(status_code=400, detail=f"Field '{body.field}' cannot be edited.")

    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=404, detail="No household found for this user.")

    supabase = get_supabase_admin_client()

    check = (
        supabase.table("payroll_paystubs")
        .select("id, member_id")
        .eq("id", paystub_id)
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if not check.data:
        raise HTTPException(status_code=404, detail="Paystub not found.")

    # Virtual field: user enters the desired Prior Pay amount; we back-compute
    # gross_pay_ytd = desired_prior_pay + sum_of_all_stored_stubs_gross.
    if body.field == "prior_pay_amount":
        member_id = check.data[0]["member_id"]
        all_stubs = (
            supabase.table("payroll_paystubs")
            .select("gross_pay")
            .eq("member_id", member_id)
            .execute()
        )
        sum_gross = round(sum(float(r.get("gross_pay") or 0) for r in (all_stubs.data or [])), 2)
        update: dict = {"gross_pay_ytd": round(body.value + sum_gross, 2)}
        if body.employer_name is not None:
            update["employer_name"] = body.employer_name
        supabase.table("payroll_paystubs").update(update).eq("id", paystub_id).execute()
        return {"success": True}

    # Virtual "_prior" fields: user enters desired prior-period amount;
    # we back-compute {base_col}_ytd = desired_prior + sum_of_stored_stubs.
    if is_prior and base_col:
        member_id = check.data[0]["member_id"]
        all_stubs = (
            supabase.table("payroll_paystubs")
            .select(base_col)
            .eq("member_id", member_id)
            .execute()
        )
        sum_col = round(sum(float(r.get(base_col) or 0) for r in (all_stubs.data or [])), 2)
        update: dict = {f"{base_col}_ytd": round(body.value + sum_col, 2)}
        if body.employer_name is not None:
            update["employer_name"] = body.employer_name
        supabase.table("payroll_paystubs").update(update).eq("id", paystub_id).execute()
        return {"success": True}

    update = {body.field: body.value}
    if body.employer_name is not None:
        update["employer_name"] = body.employer_name

    supabase.table("payroll_paystubs").update(update).eq("id", paystub_id).execute()

    return {"success": True}


@router.post("/create-link-token")
def payroll_create_link_token(user=Depends(get_current_user)):
    """Create a Plaid Link token scoped to income verification / payroll."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=404, detail="No household found for this user.")
    member_id = get_member_id_for_user(str(user.id))

    try:
        link_token = payroll_service.create_payroll_link_token(
            str(user.id), household_id, member_id
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create payroll link token: {str(e)}")

    return {"link_token": link_token}


@router.post("/sync")
def payroll_sync(user=Depends(get_current_user)):
    """
    Fetch paystubs using the stored user_token for this member and mark
    matching payroll deposit transactions.
    """
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=404, detail="No household found for this user.")
    member_id = get_member_id_for_user(str(user.id))

    try:
        result = payroll_service.sync_paystubs(
            household_id=household_id,
            member_id=member_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Payroll sync failed: {str(e)}")

    return result


@router.post("/webhook")
async def payroll_webhook(request: Request):
    """
    Plaid income verification webhook. Called by Plaid when a
    /credit/payroll_income/refresh completes.

    Plaid does not sign these with a user JWT in all cases, so we accept all
    POST bodies and log them. When we see a PROCESSING_COMPLETE status we
    look up the connection by user_token and re-run sync to store fresh data.
    """
    body = await request.json()
    print(f"[payroll_webhook] received: {body}", flush=True)

    webhook_type        = (body.get("webhook_type") or "").upper()
    verification_status = (body.get("verification_status") or "").upper()

    # Plaid fires webhook_type=INCOME / verification_status=VERIFICATION_STATUS_PROCESSING_COMPLETE
    # when a /credit/payroll_income/refresh completes. The body contains user_id and item_id
    # but NOT user_token, so we sync all active payroll connections.
    # trigger_refresh=False prevents re-triggering a refresh (and an infinite loop).
    if webhook_type == "INCOME" and "PROCESSING_COMPLETE" in verification_status:
        from app.core.supabase import get_supabase_admin_client
        supabase = get_supabase_admin_client()
        connections = (
            supabase.table("payroll_connections")
            .select("household_id, member_id")
            .not_.is_("plaid_user_token", "null")
            .execute()
        )
        for conn in (connections.data or []):
            try:
                result = payroll_service.sync_paystubs(
                    household_id=conn["household_id"],
                    member_id=conn["member_id"],
                    trigger_refresh=False,
                )
                print(f"[payroll_webhook] sync complete: {result}", flush=True)
            except Exception as exc:
                print(f"[payroll_webhook] sync failed: {exc}", flush=True)
    else:
        print(
            f"[payroll_webhook] unhandled — type={webhook_type} status={verification_status}",
            flush=True,
        )

    # Always return 200 so Plaid doesn't retry.
    return {"received": True}


@router.get("/needs-refresh")
def payroll_needs_refresh(user=Depends(get_current_user)):
    """
    Returns whether the user's payroll data is stale — i.e., a full pay period
    has elapsed since the most recent stored paystub. When true, the frontend
    should trigger a Plaid re-link instead of a silent sync.

    Cooling-off period: if the Plaid connection was synced within the last 36
    hours, always return needs_refresh=False regardless of paystub dates. Plaid
    processes payroll refreshes asynchronously — the new paystub may not be
    visible in /credit/payroll_income/get immediately after re-link. Without
    this guard the user gets re-prompted on every sync attempt until Plaid posts
    the new paystub (which can take hours), creating an infinite re-auth loop.
    """
    import datetime
    from app.core.supabase import get_supabase_admin_client

    member_id = get_member_id_for_user(str(user.id))
    if not member_id:
        return {"needs_refresh": False}

    supabase = get_supabase_admin_client()

    stub = (
        supabase.table("payroll_paystubs")
        .select("pay_date")
        .eq("member_id", member_id)
        .order("pay_date", desc=True)
        .limit(1)
        .execute()
    )
    if not stub.data:
        return {"needs_refresh": False}

    last_pay_date = datetime.date.fromisoformat(stub.data[0]["pay_date"])

    # Only query Plaid connections (non-null token) for pay_frequency and
    # last_synced_at. Manual connections have no sync timestamp and no
    # pay_frequency; an unfiltered query could pick the manual row instead.
    conn = (
        supabase.table("payroll_connections")
        .select("pay_frequency, last_synced_at")
        .eq("member_id", member_id)
        .not_.is_("plaid_user_token", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not conn.data:
        return {"needs_refresh": False}

    pay_frequency = conn.data[0].get("pay_frequency") or ""
    last_synced_at = conn.data[0].get("last_synced_at")

    freq = pay_frequency.upper()
    if "SEMI" in freq or "BIMONTHLY" in freq:
        period_days = 15
    elif "BIWEEKLY" in freq or "BI_WEEKLY" in freq:
        period_days = 14
    elif "WEEKLY" in freq:
        period_days = 7
    elif "MONTHLY" in freq:
        period_days = 30
    else:
        period_days = 14  # conservative default

    today = datetime.date.today()
    next_expected = last_pay_date + datetime.timedelta(days=period_days)

    # Cooling-off: if we synced within the last 36 hours, suppress re-auth
    # regardless of whether the expected paystub has appeared yet.
    if last_synced_at:
        try:
            synced_dt = datetime.datetime.fromisoformat(last_synced_at.replace("Z", "+00:00"))
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            if (now_utc - synced_dt).total_seconds() < 36 * 3600:
                return {
                    "needs_refresh": False,
                    "last_pay_date": last_pay_date.isoformat(),
                    "next_expected_pay_date": next_expected.isoformat(),
                    "pay_frequency": pay_frequency,
                }
        except Exception:
            pass

    return {
        "needs_refresh": today >= next_expected,
        "last_pay_date": last_pay_date.isoformat(),
        "next_expected_pay_date": next_expected.isoformat(),
        "pay_frequency": pay_frequency,
    }


_PAYSTUB_EXTRACTION_PROMPT = """Extract payroll fields from this paystub as JSON. Return ONLY valid JSON, no other text.

CRITICAL RULES:
1. Use CURRENT PAY PERIOD amounts only. If the paystub has both a "Current" and "YTD" (Year-to-Date) column, use ONLY the Current/Period column values. If a field is $0 or blank in the current period, return null — do NOT substitute the YTD amount.
2. State-specific payroll taxes such as NYSPL-E, NYSDI-E, NY SDI, NY PFL, CA SDI, NJ SUI, and similar codes belong in state_income_tax, not other_posttax.
3. fsa_contribution is for Medical/Health FSA only. dep_care_fsa is for Dependent Care FSA only. Keep these separate.
4. employer_401k_match is the employer's matching contribution (not the employee's own 401k deduction).
5. other_pretax is for pre-tax benefits NOT already captured by the named fields above. The most common items here are transit/commuter pass and parking (Section 132 fringe benefits). Do NOT put disability insurance or life insurance premiums here.
6. other_posttax is for post-tax deductions NOT already captured by the named fields above. This includes: short-term disability (STD), long-term disability (LTD), group life insurance, voluntary/supplemental life insurance, and spouse or child life coverage. Employee-paid disability and life insurance premiums are always post-tax — never pre-tax.
7. roth_401k is a Roth 401(k) employee contribution, paid with after-tax dollars. It is entirely separate from retirement_401k (traditional pre-tax 401k). If the paystub shows both, extract them into their respective fields.

{
  "pay_date": "YYYY-MM-DD",
  "pay_period_start": "YYYY-MM-DD or null",
  "pay_period_end": "YYYY-MM-DD or null",
  "employer_name": "string or null",
  "gross_pay": number or null,
  "net_pay": number or null,
  "regular_pay": number or null,
  "bonus_pay": number or null,
  "overtime_pay": number or null,
  "other_earnings": number or null,
  "federal_income_tax": number or null,
  "state_income_tax": number or null,
  "social_security_tax": number or null,
  "medicare_tax": number or null,
  "retirement_401k": number or null,
  "employer_401k_match": number or null,
  "roth_401k": number or null,
  "hsa_contribution": number or null,
  "fsa_contribution": number or null,
  "dep_care_fsa": number or null,
  "health_insurance": number or null,
  "dental_insurance": number or null,
  "vision_insurance": number or null,
  "other_pretax": number or null,
  "espp_contribution": number or null,
  "other_posttax": number or null
}"""

_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
}


@router.post("/parse-paystub")
async def parse_paystub(
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    """Upload a paystub PDF or image; Claude extracts all fields and returns them as JSON."""
    content_type = (file.content_type or "").lower()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{content_type}'. Upload a PDF, PNG, JPEG, or WebP.",
        )

    file_bytes = await file.read()
    b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    if content_type == "application/pdf":
        file_block: dict = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
        }
    else:
        file_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": content_type, "data": b64},
        }

    try:
        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        file_block,
                        {"type": "text", "text": _PAYSTUB_EXTRACTION_PROMPT},
                    ],
                }
            ],
        )
        raw_text = message.content[0].text.strip()
        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        extracted = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Claude returned non-JSON: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Paystub parsing failed: {exc}")

    # Ensure all expected keys are present (null if Claude omitted them)
    defaults = {
        "pay_date": None, "pay_period_start": None, "pay_period_end": None,
        "employer_name": None, "gross_pay": None, "net_pay": None,
        "regular_pay": None, "bonus_pay": None, "overtime_pay": None,
        "other_earnings": None, "federal_income_tax": None, "state_income_tax": None,
        "social_security_tax": None, "medicare_tax": None,
        "retirement_401k": None, "employer_401k_match": None, "roth_401k": None,
        "hsa_contribution": None, "fsa_contribution": None, "dep_care_fsa": None,
        "health_insurance": None, "dental_insurance": None, "vision_insurance": None,
        "other_pretax": None, "espp_contribution": None, "other_posttax": None,
    }
    defaults.update(extracted)
    return defaults


class ManualPaystubRequest(BaseModel):
    member_id: str
    pay_date: str
    employer_name: str
    gross_pay: float
    net_pay: float
    pay_period_start: Optional[str] = None
    pay_period_end: Optional[str] = None
    regular_pay: Optional[float] = None
    bonus_pay: Optional[float] = None
    overtime_pay: Optional[float] = None
    other_earnings: Optional[float] = None
    federal_income_tax: Optional[float] = None
    state_income_tax: Optional[float] = None
    social_security_tax: Optional[float] = None
    medicare_tax: Optional[float] = None
    retirement_401k: Optional[float] = None
    employer_401k_match: Optional[float] = None
    roth_401k: Optional[float] = None
    hsa_contribution: Optional[float] = None
    fsa_contribution: Optional[float] = None
    dep_care_fsa: Optional[float] = None
    health_insurance: Optional[float] = None
    dental_insurance: Optional[float] = None
    vision_insurance: Optional[float] = None
    other_pretax: Optional[float] = None
    espp_contribution: Optional[float] = None
    other_posttax: Optional[float] = None


@router.post("/manual-paystub")
def create_manual_paystub(
    body: ManualPaystubRequest,
    user=Depends(get_current_user),
):
    """Save a manually entered (AI-parsed) paystub to the database."""
    household_id = get_household_id_for_user(str(user.id))
    if not household_id:
        raise HTTPException(status_code=404, detail="No household found for this user.")

    supabase = get_supabase_admin_client()

    # Validate that member_id belongs to the caller's household
    member_check = (
        supabase.table("household_members")
        .select("member_id")
        .eq("member_id", body.member_id)
        .eq("household_id", household_id)
        .limit(1)
        .execute()
    )
    if not member_check.data:
        raise HTTPException(status_code=403, detail="member_id does not belong to your household.")

    # Find or create a manual payroll connection for this member + employer
    conn_result = (
        supabase.table("payroll_connections")
        .select("id")
        .eq("member_id", body.member_id)
        .is_("plaid_user_token", "null")
        .eq("employer_name", body.employer_name)
        .limit(1)
        .execute()
    )

    if conn_result.data:
        connection_id = conn_result.data[0]["id"]
    else:
        insert_result = (
            supabase.table("payroll_connections")
            .insert({
                "household_id": household_id,
                "member_id": body.member_id,
                "employer_name": body.employer_name,
                "plaid_user_token": None,
            })
            .execute()
        )
        connection_id = insert_result.data[0]["id"]

    # Build the paystub row from all non-None fields
    _OPTIONAL_FIELDS = [
        "pay_period_start", "pay_period_end",
        "regular_pay", "bonus_pay", "overtime_pay", "other_earnings",
        "federal_income_tax", "state_income_tax", "social_security_tax", "medicare_tax",
        "retirement_401k", "employer_401k_match", "roth_401k",
        "hsa_contribution", "fsa_contribution", "dep_care_fsa",
        "health_insurance", "dental_insurance", "vision_insurance", "other_pretax",
        "espp_contribution", "other_posttax",
    ]
    row: dict = {
        "connection_id": connection_id,
        "household_id": household_id,
        "member_id": body.member_id,
        "pay_date": body.pay_date,
        "employer_name": body.employer_name,
        "gross_pay": body.gross_pay,
        "net_pay": body.net_pay,
        "source": "manual",
    }
    for field in _OPTIONAL_FIELDS:
        val = getattr(body, field)
        if val is not None:
            row[field] = val

    result = (
        supabase.table("payroll_paystubs")
        .upsert(row, on_conflict="connection_id,pay_date")
        .execute()
    )
    paystub_id = result.data[0]["id"] if result.data else None

    # Tag matching bank deposit transactions as payroll deposits immediately.
    # Without this, deposits only get tagged during the next Plaid sync, leaving
    # a window where the IS double-counts both the paystub gross and the deposit.
    try:
        from app.services.payroll_service import _mark_payroll_deposits
        _mark_payroll_deposits(
            connection_id=connection_id,
            household_id=household_id,
            member_id=body.member_id,
            employer_name=body.employer_name,
        )
    except Exception as exc:
        print(f"[manual_paystub] deposit tagging error (non-fatal): {exc}", flush=True)

    return {"success": True, "paystub_id": paystub_id}


@router.get("/connected")
def payroll_connected(user=Depends(get_current_user)):
    """
    Check whether this user has an active payroll connection.
    Returns connection details or null if none exists.
    """
    from app.core.supabase import get_supabase_admin_client
    member_id = get_member_id_for_user(str(user.id))
    if not member_id:
        return None

    supabase = get_supabase_admin_client()
    conn = (
        supabase.table("payroll_connections")
        .select("id, employer_name, last_synced_at")
        .eq("member_id", member_id)
        .limit(1)
        .execute()
    )
    return conn.data[0] if conn.data else None
