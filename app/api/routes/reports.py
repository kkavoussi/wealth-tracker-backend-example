"""
Report endpoints — balance sheet, income statement, cash flow.

Period filter (query param ?period=):
  ytd          — Jan 1 of current year → today (default)
  2026-01      — specific month
  2026-Q1      — specific quarter (Q1/Q2/Q3/Q4)
  2025         — full calendar year
"""
import datetime
from collections import defaultdict
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from app.core.security import get_current_user
from app.core.supabase import get_supabase_admin_client
from app.services.plaid_service import get_household_id_for_user
from app.services import payroll_service
from app.models.reports import BalanceSheetResponse, IncomeStatementResponse, CashFlowResponse

router = APIRouter()


# =============================================================================
# HELPERS
# =============================================================================

SUBCATEGORY_LABELS = {
    # Assets
    "CASH_AND_EQUIVALENTS": "Cash & Equivalents",
    "INVESTMENTS":          "Investments",
    "REAL_ESTATE":          "Real Estate",
    "VEHICLES":             "Vehicles",
    "OTHER_ASSETS":         "Other Assets",
    # Liabilities
    "CREDIT_CARDS":         "Credit Cards",
    "MORTGAGES":            "Mortgages",
    "LOANS":                "Loans",
    "OTHER_LIABILITIES":    "Other Liabilities",
    # Income
    "SALARY_AND_WAGES":     "Salary & Wages",
    "INVESTMENT_INCOME":    "Investment Income",
    "REAL_ESTATE_INCOME":   "Real Estate Income",
    "OTHER_INCOME":         "Other Income",
    # Payroll-withheld expenses (injected from paystub data)
    "PAYROLL_TAXES":           "Payroll Taxes",
    "PAYROLL_BENEFITS":        "Payroll Benefit Premiums",
    # Payroll-withheld investment vehicles (injected into CF investing)
    "401K_CONTRIBUTION":       "401(k) Contributions",
    "ROTH_401K_CONTRIBUTION":  "Roth 401(k) Contributions",
    "HSA_CONTRIBUTION":        "HSA Contributions",
    "ESPP_CONTRIBUTION":       "ESPP Contributions",
    # Expenses
    "HOUSING":                  "Housing",
    "RENT":                     "Rent",
    "MORTGAGE":                 "Mortgage",
    "CAR_LOAN_PAYMENT":         "Car Loan Payment",
    "FOOD_AND_DINING":          "Food & Dining",
    "TRANSPORTATION":           "Transportation",
    "HEALTHCARE":               "Healthcare",
    "PERSONAL_AND_FITNESS":     "Personal & Fitness",
    "ENTERTAINMENT_AND_TRAVEL": "Entertainment & Travel",
    "SUBSCRIPTION_SERVICES":    "Subscription Services",
    "CHILDREN":                 "Children",
    "EDUCATION":                "Education",
    "PETS":                     "Pets",
    "FINANCIAL":                "Financial",
    "MISCELLANEOUS":                "Miscellaneous",
    "CHECKING_RELATED_EXPENSES":    "Checking Related Expenses",
    "PAYMENT_METHODS":              "Payment Methods",
    # Transfers
    "INTERNAL_TRANSFERS":         "Account Transfers",
    "CREDIT_CARD_PAYMENTS":       "Credit Card Payments",
    "INVESTMENT_CONTRIBUTIONS":   "Investment Contributions",
    "VEHICLE_PURCHASE":           "Vehicle Purchase",
    "REAL_ESTATE_PURCHASE":       "Real Estate Purchase",
}


def _parse_period(period: str) -> tuple[datetime.date, datetime.date]:
    """Parse the period string and return (start_date, end_date)."""
    today = datetime.date.today()

    if period == "ytd":
        return datetime.date(today.year, 1, 1), today

    # Full year: "2025"
    if len(period) == 4 and period.isdigit():
        year = int(period)
        return datetime.date(year, 1, 1), datetime.date(year, 12, 31)

    # Quarter: "2026-Q1"
    if len(period) == 7 and "-Q" in period:
        parts = period.split("-Q")
        year, q = int(parts[0]), int(parts[1])
        if q not in (1, 2, 3, 4):
            raise ValueError(f"Invalid quarter: {period}")
        start_month = (q - 1) * 3 + 1
        end_month   = start_month + 2
        end_day     = (datetime.date(year, end_month, 1) + datetime.timedelta(days=31)).replace(day=1) - datetime.timedelta(days=1)
        return datetime.date(year, start_month, 1), end_day

    # Month: "2026-01"
    if len(period) == 7 and period[4] == "-":
        year, month = int(period[:4]), int(period[5:])
        start = datetime.date(year, month, 1)
        end   = (start + datetime.timedelta(days=31)).replace(day=1) - datetime.timedelta(days=1)
        return start, end

    raise ValueError(f"Unrecognized period format: '{period}'. Use ytd, 2026-01, 2026-Q1, or 2025.")


def _require_household(user) -> str:
    hid = get_household_id_for_user(str(user.id))
    if not hid:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No household found for this user.")
    return hid


# =============================================================================
# INCOME STATEMENT CATEGORY HIERARCHY
# Matches Excel ROW_MAP order from Scripts/generate_income_statement.py
# =============================================================================

INCOME_SKELETON = [
    ("SALARY_AND_WAGES", "Salary & Wages", [
        ("BASE_SALARY",            "Base Salary / Hourly Pay"),
        ("BONUS",                  "Bonus"),
        ("COMMISSIONS",            "Commissions"),
        ("REWARDS_INCENTIVES",     "Rewards / Incentives"),
        ("EXPENSE_REIMBURSEMENTS", "Expense Reimbursements"),
    ]),
    ("INVESTMENT_INCOME", "Investment Income", [
        ("DIVIDENDS",    "Dividends"),
        ("INTEREST",     "Interest"),
        ("CAPITAL_GAINS","Capital Gains"),
        ("RSU_VESTING",  "RSU / Stock Vesting"),
        ("ESPP_DISCOUNT","ESPP Discount"),
    ]),
    ("REAL_ESTATE_INCOME", "Real Estate Income", [
        ("RENTAL_INCOME", "Rental Income"),
        ("PROPERTY_SALE", "Property Sale Proceeds"),
    ]),
    ("OTHER_INCOME", "Other Income", [
        ("CASH_TRANSFERS_RECEIVED", "Cash Transfers Received"),
        ("TAX_REFUNDS",             "Tax Refunds"),
        ("MISCELLANEOUS",           "Miscellaneous Income"),
    ]),
]

EXPENSE_SKELETON = [
    ("HOUSING", "Housing", [
        ("RENT",             "Rent"),
        ("MORTGAGE",         "Mortgage"),
        ("UTILITIES",        "Utilities"),
        ("HOME_MAINTENANCE", "Home Maintenance"),
        ("PROPERTY_TAX",     "Property Tax"),
        ("HOME_INSURANCE",   "Home Insurance"),
    ]),
    ("CHILDREN", "Children", [
        ("CHILDCARE_OUT_OF_POCKET", "Childcare (Out-of-Pocket)"),
        ("SCHOOL_EXPENSES",         "School Expenses"),
        ("ACTIVITIES_SPORTS",       "Activities & Sports"),
    ]),
    ("FOOD_AND_DINING", "Food & Dining", [
        ("GROCERIES",    "Groceries"),
        ("RESTAURANTS",  "Restaurants"),
        ("COFFEE_SHOPS", "Coffee Shops"),
        ("ALCOHOL_BARS", "Alcohol & Bars"),
    ]),
    ("TRANSPORTATION", "Transportation", [
        ("CAR_LOAN_PAYMENT",          "Car Loan Payment"),
        ("GAS_FUEL",                  "Gas & Fuel"),
        ("AUTO_INSURANCE",            "Auto Insurance"),
        ("PARKING_OUT_OF_POCKET",     "Parking"),
        ("PUBLIC_TRANSIT_OUT_OF_POCKET", "Public Transit"),
        ("RIDE_SHARE",                "Ride Share"),
        ("AUTO_MAINTENANCE",          "Auto Maintenance"),
    ]),
    ("HEALTHCARE", "Healthcare", [
        ("HEALTH_INSURANCE_SELF_PAID", "Health Insurance (Self-Paid)"),
        ("MEDICAL",           "Medical"),
        ("DENTAL",            "Dental"),
        ("VISION",            "Vision"),
        ("PRESCRIPTIONS",     "Prescriptions"),
        ("THERAPY_MENTAL_HEALTH", "Therapy / Mental Health"),
        ("MEDICAL_SUPPLIES",  "Medical Supplies"),
    ]),
    ("INSURANCE_NON_HEALTH", "Insurance (Non-Health)", [
        ("LIFE_INSURANCE_SELF_PAID",        "Life Insurance"),
        ("DISABILITY_INSURANCE_SELF_PAID",  "Disability Insurance"),
        ("UMBRELLA_INSURANCE",              "Umbrella Insurance"),
        ("PET_INSURANCE",                   "Pet Insurance"),
    ]),
    ("PERSONAL_AND_FITNESS", "Personal & Fitness", [
        ("GYM_FITNESS",     "Gym & Fitness"),
        ("PERSONAL_CARE",   "Personal Care"),
        ("CLOTHING",        "Clothing"),
        ("HEALTH_WELLNESS", "Health & Wellness"),
    ]),
    ("ENTERTAINMENT_AND_TRAVEL", "Entertainment & Travel", [
        ("VACATION",        "Vacation"),
        ("TRAVEL",          "Travel"),
        ("EVENTS_CONCERTS", "Events & Concerts"),
        ("HOBBIES",         "Hobbies"),
    ]),
    ("SUBSCRIPTION_SERVICES", "Subscription Services", [
        ("STREAMING",                "Streaming"),
        ("SOFTWARE_TOOLS",           "Software & Tools"),
        ("NEWS_MEDIA",               "News & Media"),
        ("PROFESSIONAL_MEMBERSHIPS", "Professional Memberships"),
        ("OTHER_SUBSCRIPTIONS",      "Other Subscriptions"),
    ]),
    ("FINANCIAL", "Financial", [
        ("POST_TAX_INVESTMENTS",       "Post-Tax Investments"),
        ("LOAN_PAYMENTS",              "Loan Payments"),
        ("CREDIT_CARD_INTEREST_FEES",  "Credit Card Interest & Fees"),
        ("BANK_FEES",                  "Bank Fees"),
        ("TAX_PAYMENTS",               "Tax Payments"),
        ("FINANCIAL_ADVISOR",          "Financial Advisor"),
        ("CASH_TRANSFERS_SENT",        "Cash Transfers Sent"),
    ]),
    ("EDUCATION", "Education", [
        ("TUITION",                "Tuition"),
        ("BOOKS_SUPPLIES",         "Books & Supplies"),
        ("STUDENT_LOAN_PAYMENTS",  "Student Loan Payments"),
    ]),
    ("GIFTS_AND_DONATIONS", "Gifts & Donations", [
        ("CHARITABLE_DONATIONS",    "Charitable Donations"),
        ("GIFTS",                   "Gifts"),
        ("RELIGIOUS_CONTRIBUTIONS", "Religious Contributions"),
    ]),
    ("PETS", "Pets", [
        ("VETERINARY",       "Veterinary"),
        ("PET_FOOD_SUPPLIES","Pet Food & Supplies"),
    ]),
    ("BUSINESS_EXPENSES", "Business Expenses", [
        ("OFFICE_SUPPLIES",         "Office Supplies"),
        ("BUSINESS_TRAVEL",         "Business Travel"),
        ("PROFESSIONAL_DEVELOPMENT","Professional Development"),
        ("OTHER_BUSINESS",          "Other Business"),
    ]),
    ("CHECKING_RELATED_EXPENSES", "Checking Related Expenses", [
        ("CASH_WITHDRAWALS", "Cash Withdrawals"),
        ("CHECK_PAYMENTS",   "Check Payments"),
    ]),
    ("MISCELLANEOUS", "Miscellaneous", [
        ("GENERAL_MERCHANDISE", "General Merchandise"),
        ("UNCATEGORIZED",       "Uncategorized"),
    ]),
]


def _fetch_paginated(query_builder, page_size: int = 1000) -> list:
    """
    Fetch all matching rows, bypassing Supabase/PostgREST's server-side max_rows cap.
    Paginates in `page_size` batches until a partial page signals end of results.
    """
    rows: list = []
    offset = 0
    while True:
        batch = query_builder.range(offset, offset + page_size - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def _build_category_skeleton(skeleton: list) -> dict:
    """Return an ordered dict with all categories pre-populated at $0."""
    result = {}
    for subcat_key, subcat_label, details in skeleton:
        result[subcat_key] = {
            "label": subcat_label,
            "total": 0.0,
            "details": {
                dk: {"label": dl, "total": 0.0, "transactions": []}
                for dk, dl in details
            },
        }
    return result


def _add_to_bucket(bucket: dict, subcat: str, detail: str, amount: float, entry: dict):
    """Accumulate a transaction into the matching bucket, creating keys if needed."""
    if subcat not in bucket:
        bucket[subcat] = {
            "label": SUBCATEGORY_LABELS.get(subcat, subcat.replace("_", " ").title()),
            "total": 0.0,
            "details": {},
        }
    group = bucket[subcat]
    group["total"] = round(group["total"] + amount, 2)

    if detail not in group["details"]:
        group["details"][detail] = {
            "label": detail.replace("_", " ").title(),
            "total": 0.0,
            "transactions": [],
        }
    group["details"][detail]["total"] = round(
        group["details"][detail]["total"] + amount, 2
    )
    group["details"][detail]["transactions"].append(entry)


# =============================================================================
# BALANCE SHEET
# =============================================================================

@router.get("/balance-sheet", response_model=BalanceSheetResponse)
def balance_sheet(
    member_id: Optional[str] = Query(default=None, description="Filter by member ID; omit for combined household view"),
    user=Depends(get_current_user),
):
    """
    Current balance sheet — assets and liabilities from the latest Plaid sync,
    grouped by balance_sheet_subcategory.
    """
    household_id = _require_household(user)
    supabase     = get_supabase_admin_client()

    plaid_q = (
        supabase.table("plaid_accounts")
        .select("account_id, name, official_name, type, subtype, current_balance, "
                "available_balance, credit_limit, balance_sheet_category, "
                "balance_sheet_subcategory, balance_sheet_detail, is_hidden, member_id")
        .eq("household_id", household_id)
        .eq("is_hidden", False)
    )
    if member_id:
        plaid_q = plaid_q.eq("member_id", member_id)
    accounts = plaid_q.execute().data

    assets      = defaultdict(lambda: {"label": "", "total": 0.0, "accounts": []})
    liabilities = defaultdict(lambda: {"label": "", "total": 0.0, "accounts": []})

    for acct in accounts:
        cat    = acct.get("balance_sheet_category")
        subcat = acct.get("balance_sheet_subcategory") or "OTHER"
        bal    = float(acct.get("current_balance") or 0)
        entry  = {
            "id":                       str(acct["account_id"]),
            "source":                   "plaid",
            "name":                     acct["name"],
            "official_name":            acct.get("official_name"),
            "type":                     acct.get("type"),
            "subtype":                  acct.get("subtype"),
            "balance":                  bal,
            "detail":                   acct.get("balance_sheet_detail"),
            "member_id":                acct.get("member_id"),
            "balance_sheet_category":    acct.get("balance_sheet_category"),
            "balance_sheet_subcategory": acct.get("balance_sheet_subcategory"),
        }

        if cat == "ASSETS":
            assets[subcat]["label"]   = SUBCATEGORY_LABELS.get(subcat, subcat)
            assets[subcat]["total"]  += bal
            assets[subcat]["accounts"].append(entry)
        elif cat == "LIABILITIES":
            liabilities[subcat]["label"]   = SUBCATEGORY_LABELS.get(subcat, subcat)
            liabilities[subcat]["total"]  += bal
            liabilities[subcat]["accounts"].append(entry)

    # Include manually-entered assets and liabilities
    manual_q = (
        supabase.table("manual_assets")
        .select("asset_id, name, asset_type, current_value, balance_sheet_category, "
                "balance_sheet_subcategory, balance_sheet_detail, description, member_id")
        .eq("household_id", household_id)
        .eq("is_hidden", False)
    )
    if member_id:
        manual_q = manual_q.eq("member_id", member_id)
    manual = manual_q.execute().data

    for asset in manual:
        cat    = asset.get("balance_sheet_category") or "ASSETS"
        subcat = asset["balance_sheet_subcategory"]
        bal    = float(asset.get("current_value") or 0)
        entry  = {
            "id":            str(asset["asset_id"]),
            "source":        "manual",
            "name":          asset["name"],
            "official_name": None,
            "type":          "manual",
            "subtype":       asset["asset_type"],
            "balance":       bal,
            "detail":        asset.get("balance_sheet_detail"),
            "description":   asset.get("description"),
            "member_id":     asset.get("member_id"),
        }
        if cat == "LIABILITIES":
            liabilities[subcat]["label"]   = SUBCATEGORY_LABELS.get(subcat, subcat)
            liabilities[subcat]["total"]  += bal
            liabilities[subcat]["accounts"].append(entry)
        else:
            assets[subcat]["label"]   = SUBCATEGORY_LABELS.get(subcat, subcat)
            assets[subcat]["total"]  += bal
            assets[subcat]["accounts"].append(entry)

    total_assets      = sum(g["total"] for g in assets.values())
    total_liabilities = sum(g["total"] for g in liabilities.values())

    return BalanceSheetResponse(
        as_of=datetime.date.today().isoformat(),
        net_worth=round(total_assets - total_liabilities, 2),
        total_assets=round(total_assets, 2),
        total_liabilities=round(total_liabilities, 2),
        assets={k: {**v, "total": round(v["total"], 2)} for k, v in assets.items()},
        liabilities={k: {**v, "total": round(v["total"], 2)} for k, v in liabilities.items()},
    )


# =============================================================================
# INCOME STATEMENT
# =============================================================================

@router.get("/income-statement", response_model=IncomeStatementResponse)
def income_statement(
    period: str = Query(default="ytd", description="ytd | 2026-01 | 2026-Q1 | 2025"),
    member_id: Optional[str] = Query(default=None, description="Filter by member ID; omit for combined household view"),
    user=Depends(get_current_user),
):
    """
    Income statement — all income and expense transactions for the period,
    grouped by subcategory then detail. All categories shown even at $0.
    """
    try:
        start_date, end_date = _parse_period(period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    household_id = _require_household(user)
    supabase     = get_supabase_admin_client()

    txn_q = (
        supabase.table("transactions")
        .select("transaction_id, date, name, merchant_name, display_amount, category, subcategory, "
                "detail, exclude_from_expenses, pending, flagged_for_review, flag_reason, "
                "payment_channel, plaid_category_detailed, plaid_category_confidence")
        .eq("household_id", household_id)
        .eq("pending", False)
        .eq("is_duplicate", False)
        .eq("is_payroll_deposit", False)
        .gte("date", start_date.isoformat())
        .lte("date", end_date.isoformat())
        .order("date", desc=True)
    )

    if member_id:
        # Restrict to account_ids belonging to this member
        acct_result = (
            supabase.table("plaid_accounts")
            .select("account_id")
            .eq("household_id", household_id)
            .eq("member_id", member_id)
            .execute()
        )
        account_ids = [r["account_id"] for r in (acct_result.data or [])]
        if account_ids:
            txn_q = txn_q.in_("account_id", account_ids)
        else:
            # Member has no Plaid accounts — return empty result
            txns = []

    if not member_id or (member_id and account_ids):
        txns = _fetch_paginated(txn_q)

    income    = _build_category_skeleton(INCOME_SKELETON)
    expenses  = _build_category_skeleton(EXPENSE_SKELETON)
    transfers: dict = {}

    # Tracks expenses that route to CF Financing/Investing (excluded from IS net_surplus)
    is_rerouted_expense_total = 0.0

    for txn in txns:
        cat    = txn.get("category")
        subcat = txn.get("subcategory") or "MISCELLANEOUS"
        detail = txn.get("detail") or "UNCATEGORIZED"
        amt    = float(txn.get("display_amount") or 0)
        excl   = txn.get("exclude_from_expenses", False)

        entry = {
            "id":              str(txn["transaction_id"]),
            "date":            txn["date"],
            "name":            txn.get("merchant_name") or txn.get("name"),
            "amount":          amt,
            "flagged":         txn.get("flagged_for_review", False),
            "flag_reason":     txn.get("flag_reason"),
            "raw_name":        txn.get("name"),
            "payment_channel": txn.get("payment_channel"),
            "plaid_category":  txn.get("plaid_category_detailed"),
            "plaid_confidence":txn.get("plaid_category_confidence"),
        }

        if cat == "INCOME":
            _add_to_bucket(income, subcat, detail, amt, entry)
        elif cat == "EXPENSES" and not excl:
            _add_to_bucket(expenses, subcat, detail, -amt, entry)  # display as positive spend
            # Track items that route to CF Financing/Investing so they're excluded from net_surplus
            if detail in CF_REROUTED_EXPENSE_DETAILS:
                is_rerouted_expense_total = round(is_rerouted_expense_total + abs(amt), 2)
        elif cat == "TRANSFERS":
            # Only count outflows to avoid double-counting internal transfers
            if amt < 0:
                subcat_key = subcat or "INTERNAL_TRANSFERS"
                if subcat_key not in transfers:
                    transfers[subcat_key] = {
                        "label": SUBCATEGORY_LABELS.get(subcat_key, subcat_key.replace("_", " ").title()),
                        "total": 0.0,
                        "transactions": [],
                    }
                transfers[subcat_key]["total"] = round(transfers[subcat_key]["total"] + abs(amt), 2)
                transfers[subcat_key]["transactions"].append(entry)

    # ── Payroll gross injection ────────────────────────────────────────────────
    # In filtered view inject for the specified member only.
    # In combined view inject for every household member so gross pay from all
    # connected payrolls is included (fixes the bug where only the auth user's
    # payroll appeared in the combined household view).
    if member_id:
        payroll_member_ids = [member_id]
    else:
        mr = (
            supabase.table("household_members")
            .select("member_id")
            .eq("household_id", household_id)
            .execute()
        )
        payroll_member_ids = [m["member_id"] for m in (mr.data or [])]

    payroll_data = None         # kept for the response field; last member with payroll data
    total_deductions_acc = 0.0  # accumulates taxes + benefit premiums only (not 401k/HSA/ESPP)
    # Aggregators so combined view shows all-member deductions, not just the last member's
    _pd_pre_tax:  dict = {}
    _pd_post_tax: dict = {}
    _pd_taxes:    dict = {}
    _pd_gross    = 0.0
    _pd_net      = 0.0
    _pd_er_match = 0.0
    for pmid in payroll_member_ids:
        pdata = payroll_service.get_payroll_summary(household_id, pmid, start_date, end_date)
        if not (pdata and pdata["gross_income"] > 0):
            continue
        payroll_data = pdata
        for _k, _v in (pdata.get("pre_tax") or {}).items():
            _pd_pre_tax[_k] = round(_pd_pre_tax.get(_k, 0.0) + float(_v or 0), 2)
        for _k, _v in (pdata.get("post_tax") or {}).items():
            _pd_post_tax[_k] = round(_pd_post_tax.get(_k, 0.0) + float(_v or 0), 2)
        for _k, _v in (pdata.get("taxes") or {}).items():
            _pd_taxes[_k] = round(_pd_taxes.get(_k, 0.0) + float(_v or 0), 2)
        _pd_gross    += float(pdata.get("gross_income") or 0)
        _pd_net      += float(pdata.get("net_pay") or 0)
        _pd_er_match += float(pdata.get("employer_401k_match") or 0)
        paystubs = pdata.get("paystubs") or []
        if paystubs:
            for stub in paystubs:
                entry = {
                    "id":              f"payroll-{pmid[:8]}-{stub['pay_date']}",
                    "date":            stub["pay_date"],
                    "name":            stub.get("employer_name") or pdata["employer_name"] or "Employer",
                    "amount":          stub["gross_pay"],
                    "flagged":         False,
                    "flag_reason":     None,
                    "raw_name":        "Payroll (gross)",
                    "payment_channel": "payroll",
                    "plaid_category":  None,
                    "plaid_confidence": None,
                    "paystub_id":      stub.get("paystub_id"),
                    "paystub_field":   "gross_pay",
                }
                bonus_pay   = float(stub.get("bonus_pay") or 0)
                regular_pay = float(stub.get("regular_pay") or 0)
                overtime_pay = float(stub.get("overtime_pay") or 0)
                other_earnings = float(stub.get("other_earnings") or 0)
                base_pay    = regular_pay + overtime_pay + other_earnings

                if bonus_pay > 0 or regular_pay > 0:
                    # We have a breakdown — route each bucket separately
                    if base_pay > 0:
                        base_entry = dict(entry)
                        base_entry["amount"] = base_pay
                        base_entry["paystub_field"] = "regular_pay"
                        _add_to_bucket(income, "SALARY_AND_WAGES", "BASE_SALARY", base_pay, base_entry)
                    if bonus_pay > 0:
                        bonus_entry = dict(entry)
                        bonus_entry["amount"] = bonus_pay
                        bonus_entry["id"] = entry["id"] + "-bonus"
                        bonus_entry["paystub_field"] = "bonus_pay"
                        _add_to_bucket(income, "SALARY_AND_WAGES", "BONUS", bonus_pay, bonus_entry)
                else:
                    # No breakdown available — lump everything into BASE_SALARY
                    _add_to_bucket(income, "SALARY_AND_WAGES", "BASE_SALARY", stub["gross_pay"], entry)

            prior_balance = pdata.get("prior_balance")
            if prior_balance and prior_balance.get("gross_pay", 0) > 0:
                pb_label = prior_balance.get("label") or "prior period"
                pb_entry = {
                    "id":              f"payroll-prior-{pmid[:8]}",
                    "date":            prior_balance["period_end"],
                    "name":            f"Prior Pay ({pb_label})",
                    "amount":          prior_balance["gross_pay"],
                    "flagged":         False,
                    "flag_reason":     None,
                    "raw_name":        "Payroll prior balance (YTD minus stored paystubs)",
                    "payment_channel": "payroll",
                    "plaid_category":  None,
                    "plaid_confidence": None,
                    "paystub_id":      prior_balance.get("paystub_id"),
                    "paystub_field":   "prior_pay_amount",
                }
                _add_to_bucket(income, "SALARY_AND_WAGES", "BASE_SALARY", prior_balance["gross_pay"], pb_entry)
        else:
            gross_entry = {
                "id":              f"payroll-gross-{pmid[:8]}",
                "date":            end_date.isoformat(),
                "name":            pdata["employer_name"] or "Employer",
                "amount":          pdata["gross_income"],
                "flagged":         False,
                "flag_reason":     None,
                "raw_name":        "Payroll (gross)",
                "payment_channel": "payroll",
                "plaid_category":  None,
                "plaid_confidence": None,
            }
            _add_to_bucket(income, "SALARY_AND_WAGES", "BASE_SALARY", pdata["gross_income"], gross_entry)

        # ── Accumulate paycheck deductions for IS formula ─────────────────────
        # IS deductions = taxes + benefit premiums ONLY.
        # 401k / Roth / HSA / ESPP are excluded here; they flow to CF Investing instead.
        ptax = pdata.get("pre_tax",  {}) or {}
        ttax = pdata.get("taxes",    {}) or {}
        poxt = pdata.get("post_tax", {}) or {}
        total_deductions_acc += round(
            ttax.get("total", 0.0) +
            ptax.get("health_insurance", 0.0) + ptax.get("dental_insurance", 0.0) +
            ptax.get("vision_insurance", 0.0) + ptax.get("fsa", 0.0) +
            ptax.get("dep_care_fsa", 0.0) + ptax.get("other", 0.0) +
            poxt.get("other", 0.0), 2
        )

    # Merge aggregated values so combined view shows all-member deductions (not just last member's)
    if payroll_data and _pd_pre_tax:
        payroll_data = {
            **payroll_data,
            "pre_tax":             _pd_pre_tax,
            "post_tax":            _pd_post_tax,
            "taxes":               _pd_taxes,
            "gross_income":        round(_pd_gross, 2),
            "net_pay":             round(_pd_net, 2),
            "employer_401k_match": round(_pd_er_match, 2),
        }

    total_income      = sum(g["total"] for g in income.values())
    total_expenses    = sum(g["total"] for g in expenses.values())
    total_deductions  = round(total_deductions_acc, 2)
    # net_surplus excludes financing/investing items (they appear in CF Financing/Investing, not Operating)
    operating_expenses = round(total_expenses - is_rerouted_expense_total, 2)

    return IncomeStatementResponse(
        period=period,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        summary={
            "total_income":      round(total_income, 2),
            "total_deductions":  total_deductions,
            "total_expenses":    round(total_expenses, 2),
            "net_surplus":       round(total_income - total_deductions - operating_expenses, 2),
        },
        income=income,
        expenses=expenses,
        transfers={k: {**v, "total": round(v["total"], 2)} for k, v in transfers.items()},
        payroll=payroll_data,
    )


# =============================================================================
# CASH FLOW STATEMENT
# =============================================================================

# Cash flow classification:
# OPERATING  — income received + operating expenses paid
# INVESTING  — investment contributions (401k, brokerage deposits, etc.) + asset purchases
# FINANCING  — credit card payments, loan payments

INVESTING_SUBCATEGORIES = {
    "401K_CONTRIBUTION", "IRA_CONTRIBUTION", "HSA_CONTRIBUTION",
    "INVESTMENT_CONTRIBUTION", "CAPITAL_GAINS",
    "INVESTMENT_CONTRIBUTIONS",  # Fidelity 529 / brokerage / retirement fund transfers
    "VEHICLE_PURCHASE",          # Vehicle down payment (TRANSFERS category)
    "REAL_ESTATE_PURCHASE",      # Real estate down payment (TRANSFERS category)
}

FINANCING_SUBCATEGORIES = {
    "AUTO_LOAN_PAYMENT",
    "STUDENT_LOAN_PAYMENT", "PERSONAL_LOAN_PAYMENT", "MORTGAGE_PAYMENT",
}

# EXPENSES-category transactions whose detail key routes them OUT of CF Operating.
# These are also excluded from IS net_surplus so IS and CF reconcile.
CF_FINANCING_EXPENSE_DETAILS = {
    "MORTGAGE",              # Housing > Mortgage → CF Financing
    "CAR_LOAN_PAYMENT",      # Transportation > Car Loan Payment → CF Financing
    "STUDENT_LOAN_PAYMENTS", # Education > Student Loan Payments → CF Financing
    "LOAN_PAYMENTS",         # Financial > Loan Payments → CF Financing
}
CF_INVESTING_EXPENSE_DETAILS: set = set()
# (Vehicle and real estate purchases now use TRANSFERS subcategories VEHICLE_PURCHASE /
# REAL_ESTATE_PURCHASE and route to CF Investing via INVESTING_SUBCATEGORIES.)
CF_REROUTED_EXPENSE_DETAILS = CF_FINANCING_EXPENSE_DETAILS | CF_INVESTING_EXPENSE_DETAILS

CF_FINANCING_EXPENSE_LABELS = {
    "MORTGAGE":              "Mortgage Payments",
    "CAR_LOAN_PAYMENT":      "Auto Loan Payments",
    "STUDENT_LOAN_PAYMENTS": "Student Loan Payments",
    "LOAN_PAYMENTS":         "Other Loan Payments",
}
CF_INVESTING_EXPENSE_LABELS: dict = {}


@router.get("/cash-flow", response_model=CashFlowResponse)
def cash_flow(
    period: str = Query(default="ytd", description="ytd | 2026-01 | 2026-Q1 | 2025"),
    member_id: Optional[str] = Query(default=None, description="Filter by member ID; omit for combined household view"),
    user=Depends(get_current_user),
):
    """
    Cash flow statement — operating, investing, and financing activities
    for the period.
    """
    try:
        start_date, end_date = _parse_period(period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    household_id = _require_household(user)
    supabase     = get_supabase_admin_client()

    # Build account-type map upfront for CC vs. direct expense split
    acct_type_res = (
        supabase.table("plaid_accounts")
        .select("account_id, type")
        .eq("household_id", household_id)
        .execute()
    )
    account_type_map = {
        str(r["account_id"]): r["type"]
        for r in (acct_type_res.data or [])
    }

    txn_q = (
        supabase.table("transactions")
        .select("transaction_id, date, name, merchant_name, display_amount, amount, category, "
                "account_id, subcategory, detail, category_display_name, exclude_from_expenses, "
                "pending, flagged_for_review, flag_reason, payment_channel")
        .eq("household_id", household_id)
        .eq("pending", False)
        .eq("is_duplicate", False)
        .eq("is_payroll_deposit", False)
        .gte("date", start_date.isoformat())
        .lte("date", end_date.isoformat())
        .order("date", desc=True)
    )

    if member_id:
        acct_result = (
            supabase.table("plaid_accounts")
            .select("account_id")
            .eq("household_id", household_id)
            .eq("member_id", member_id)
            .execute()
        )
        account_ids = [r["account_id"] for r in (acct_result.data or [])]
        if account_ids:
            txn_q = txn_q.in_("account_id", account_ids)
        else:
            txns = []

    if not member_id or (member_id and account_ids):
        txns = _fetch_paginated(txn_q)

    operating  = defaultdict(lambda: {"label": "", "total": 0.0, "transactions": []})
    investing  = defaultdict(lambda: {"label": "", "total": 0.0, "transactions": []})
    financing  = defaultdict(lambda: {"label": "", "total": 0.0, "transactions": []})

    # Breakdown accumulators — mirror IS formula for Net Income sub-items
    cf_total_income      = 0.0
    cf_total_expenses    = 0.0
    cf_cc_expenses       = 0.0   # expenses charged to credit card accounts
    cf_non_cc_expenses   = 0.0   # expenses paid directly from depository accounts

    for txn in txns:
        cat    = txn.get("category")
        subcat = txn.get("subcategory") or ""
        # display_amount: positive = income (inflow), negative = expense (outflow)
        disp_amt = float(txn.get("display_amount") or 0)
        excl     = txn.get("exclude_from_expenses", False)

        entry = {
            "id":              str(txn["transaction_id"]),
            "date":            txn["date"],
            "name":            txn.get("merchant_name") or txn.get("name"),
            "amount":          disp_amt,
            "category":        txn.get("category_display_name"),
            "subcategory":     txn.get("subcategory"),
            "detail":          txn.get("detail"),
            "flagged":         txn.get("flagged_for_review", False),
            "flag_reason":     txn.get("flag_reason"),
            "raw_name":        txn.get("name"),
            "payment_channel": txn.get("payment_channel"),
        }

        if cat == "TRANSFERS" and subcat in FINANCING_SUBCATEGORIES:
            # Financing: loan payments (auto, student, mortgage, personal) are cash outflows
            financing[subcat]["label"]   = SUBCATEGORY_LABELS.get(subcat, subcat)
            financing[subcat]["total"]  += disp_amt
            financing[subcat]["transactions"].append(entry)

        elif cat in ("PAYCHECK_DEDUCTIONS",) or subcat in INVESTING_SUBCATEGORIES:
            # Investing: contributions to investment vehicles are outflows
            investing[subcat]["label"]   = SUBCATEGORY_LABELS.get(subcat, subcat)
            investing[subcat]["total"]  += disp_amt
            investing[subcat]["transactions"].append(entry)

        elif cat == "INCOME":
            # Operating inflow
            operating[subcat]["label"]   = SUBCATEGORY_LABELS.get(subcat, subcat)
            operating[subcat]["total"]  += disp_amt
            operating[subcat]["transactions"].append(entry)
            cf_total_income = round(cf_total_income + disp_amt, 2)

        elif cat == "EXPENSES" and not excl:
            detail_val = txn.get("detail") or ""
            if detail_val in CF_INVESTING_EXPENSE_DETAILS:
                # Asset purchase (vehicle, real estate down payment) → CF Investing
                label = CF_INVESTING_EXPENSE_LABELS.get(detail_val, detail_val.replace("_", " ").title())
                investing[detail_val]["label"] = label
                investing[detail_val]["total"] = round(investing[detail_val]["total"] + disp_amt, 2)
                investing[detail_val]["transactions"].append(entry)
            elif detail_val in CF_FINANCING_EXPENSE_DETAILS:
                # Debt service (mortgage, car loan, student loan) → CF Financing
                label = CF_FINANCING_EXPENSE_LABELS.get(detail_val, detail_val.replace("_", " ").title())
                financing[detail_val]["label"] = label
                financing[detail_val]["total"] = round(financing[detail_val]["total"] + disp_amt, 2)
                financing[detail_val]["transactions"].append(entry)
            else:
                # Operating outflow (stored as negative for CF statement)
                operating[subcat]["label"]   = SUBCATEGORY_LABELS.get(subcat, subcat)
                operating[subcat]["total"]  += disp_amt  # already negative for expenses
                operating[subcat]["transactions"].append(entry)
                expense_amt = abs(disp_amt)
                cf_total_expenses = round(cf_total_expenses + expense_amt, 2)
                acct_id = str(txn.get("account_id") or "")
                if account_type_map.get(acct_id) == "credit":
                    cf_cc_expenses = round(cf_cc_expenses + expense_amt, 2)
                else:
                    cf_non_cc_expenses = round(cf_non_cc_expenses + expense_amt, 2)

    # ── Payroll gross injection ────────────────────────────────────────────────
    # In filtered view inject for the specified member only.
    # In combined view inject for every household member (same fix as income
    # statement) so gross pay from all connected payrolls is included.
    if member_id:
        payroll_member_ids = [member_id]
    else:
        mr = (
            supabase.table("household_members")
            .select("member_id")
            .eq("household_id", household_id)
            .execute()
        )
        payroll_member_ids = [m["member_id"] for m in (mr.data or [])]

    sw_key = "SALARY_AND_WAGES"
    cf_total_deductions = 0.0   # taxes + benefit premiums from paystubs (mirrors IS)
    for pmid in payroll_member_ids:
        pdata = payroll_service.get_payroll_summary(household_id, pmid, start_date, end_date)
        if not (pdata and pdata["gross_income"] > 0):
            continue
        operating[sw_key]["label"] = SUBCATEGORY_LABELS.get(sw_key, "Salary & Wages")
        paystubs = pdata.get("paystubs") or []
        if paystubs:
            for stub in paystubs:
                operating[sw_key]["total"] = round(
                    operating[sw_key]["total"] + stub["gross_pay"], 2
                )
                operating[sw_key]["transactions"].append({
                    "id":              f"payroll-cf-{pmid[:8]}-{stub['pay_date']}",
                    "date":            stub["pay_date"],
                    "name":            stub.get("employer_name") or pdata["employer_name"] or "Employer",
                    "amount":          stub["gross_pay"],
                    "category":        "Salary & Wages",
                    "subcategory":     None,
                    "detail":          None,
                    "flagged":         False,
                    "flag_reason":     None,
                    "raw_name":        "Payroll (gross)",
                    "payment_channel": "payroll",
                    "paystub_id":      stub.get("paystub_id"),
                    "paystub_field":   "gross_pay",
                })
            prior_balance = pdata.get("prior_balance")
            if prior_balance and prior_balance.get("gross_pay", 0) > 0:
                operating[sw_key]["total"] = round(
                    operating[sw_key]["total"] + prior_balance["gross_pay"], 2
                )
                operating[sw_key]["transactions"].append({
                    "id":              f"payroll-cf-prior-{pmid[:8]}",
                    "date":            prior_balance["period_end"],
                    "name":            f"Prior Pay ({prior_balance.get('label') or 'prior period'})",
                    "amount":          prior_balance["gross_pay"],
                    "category":        "Salary & Wages",
                    "subcategory":     None,
                    "detail":          None,
                    "flagged":         False,
                    "flag_reason":     None,
                    "raw_name":        "Payroll prior balance (YTD minus stored paystubs)",
                    "payment_channel": "payroll",
                    "paystub_id":      prior_balance.get("paystub_id"),
                    "paystub_field":   "prior_pay_amount",
                })
        else:
            operating[sw_key]["total"] = round(
                operating[sw_key]["total"] + pdata["gross_income"], 2
            )
            operating[sw_key]["transactions"].append({
                "id":              f"payroll-cf-gross-{pmid[:8]}",
                "date":            end_date.isoformat(),
                "name":            pdata["employer_name"] or "Employer",
                "amount":          pdata["gross_income"],
                "category":        "Salary & Wages",
                "subcategory":     None,
                "detail":          None,
                "flagged":         False,
                "flag_reason":     None,
                "raw_name":        "Payroll (gross)",
                "payment_channel": "payroll",
            })

        # Add gross to income breakdown total
        cf_total_income = round(cf_total_income + pdata["gross_income"], 2)

        # Accumulate taxes + benefit premiums (mirrors IS total_deductions formula)
        ptax = pdata.get("pre_tax",  {}) or {}
        ttax = pdata.get("taxes",    {}) or {}
        poxt = pdata.get("post_tax", {}) or {}
        cf_total_deductions = round(cf_total_deductions + (
            ttax.get("total", 0.0) +
            ptax.get("health_insurance", 0.0) + ptax.get("dental_insurance", 0.0) +
            ptax.get("vision_insurance", 0.0) + ptax.get("fsa", 0.0) +
            ptax.get("dep_care_fsa", 0.0) + ptax.get("other", 0.0) +
            poxt.get("other", 0.0)
        ), 2)

        # ── Inject payroll-withheld investment contributions into CF investing ─
        # Emit one transaction per paystub (with paystub_id + paystub_field so
        # the modal can edit them), plus a gap entry for any prior-period amount
        # not covered by stored stubs — mirrors how IS handles salary entries.
        pretax_d  = pdata.get("pre_tax",  {}) or {}
        posttax_d = pdata.get("post_tax", {}) or {}
        employer  = pdata.get("employer_name") or "Employer"
        prior_bal = pdata.get("prior_balance")
        gap_date  = prior_bal["period_end"] if prior_bal else end_date.isoformat()
        most_recent_stub_id = paystubs[-1]["paystub_id"] if paystubs else None

        # (cf_key, ytd_total, stub_section, stub_key, db_col, label)
        for inv_key, ytd_total, stub_section, stub_key, db_col, label in [
            ("401K_CONTRIBUTION",         pretax_d.get("retirement_401k", 0.0), "pre_tax",  "retirement_401k", "retirement_401k",   "401(k) Contributions"),
            ("ROTH_401K_CONTRIBUTION",    posttax_d.get("roth_401k",      0.0), "post_tax", "roth_401k",       "roth_401k",         "Roth 401(k) Contributions"),
            ("HSA_CONTRIBUTION",          pretax_d.get("hsa",             0.0), "pre_tax",  "hsa",             "hsa_contribution",  "HSA Contributions"),
            ("ESPP_CONTRIBUTION",         posttax_d.get("espp",           0.0), "post_tax", "espp",            "espp_contribution", "ESPP Contributions"),
            ("DEP_CARE_FSA_CONTRIBUTION", pretax_d.get("dep_care_fsa",    0.0), "pre_tax",  "dep_care_fsa",    "dep_care_fsa",      "Dependent Care FSA"),
        ]:
            if ytd_total <= 0:
                continue
            investing[inv_key]["label"] = SUBCATEGORY_LABELS.get(inv_key, label)
            investing[inv_key]["total"] = round(investing[inv_key]["total"] - ytd_total, 2)

            stub_sum = 0.0
            for stub in paystubs:
                stub_amount = float((stub.get(stub_section) or {}).get(stub_key) or 0)
                if stub_amount <= 0:
                    continue
                investing[inv_key]["transactions"].append({
                    "id":              f"payroll-cf-inv-{inv_key}-{stub['paystub_id'][:8]}",
                    "date":            stub["pay_date"],
                    "name":            f"{label} — {stub.get('employer_name') or employer}",
                    "amount":          -stub_amount,
                    "category":        "Investing",
                    "subcategory":     None,
                    "detail":          None,
                    "flagged":         False,
                    "flag_reason":     None,
                    "raw_name":        label,
                    "payment_channel": "payroll",
                    "paystub_id":      stub["paystub_id"],
                    "paystub_field":   db_col,
                })
                stub_sum += stub_amount

            # Gap entry for prior-period amounts not covered by stored stubs
            gap = round(ytd_total - stub_sum, 2)
            if gap > 0.01 and most_recent_stub_id:
                investing[inv_key]["transactions"].append({
                    "id":              f"payroll-cf-inv-prior-{inv_key}-{pmid[:8]}",
                    "date":            gap_date,
                    "name":            f"Prior Period {label} — {employer}",
                    "amount":          -gap,
                    "category":        "Investing",
                    "subcategory":     None,
                    "detail":          None,
                    "flagged":         False,
                    "flag_reason":     None,
                    "raw_name":        f"Prior Period {label}",
                    "payment_channel": "payroll",
                    "paystub_id":      most_recent_stub_id,
                    "paystub_field":   f"{db_col}_prior",
                })

    net_operating  = sum(g["total"] for g in operating.values())
    net_investing  = sum(g["total"] for g in investing.values())
    net_financing  = sum(g["total"] for g in financing.values())

    return CashFlowResponse(
        period=period,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        summary={
            "net_operating":  round(net_operating, 2),
            "net_investing":  round(net_investing, 2),
            "net_financing":  round(net_financing, 2),
            "net_cash_change":round(net_operating + net_investing + net_financing, 2),
        },
        net_income_breakdown={
            "gross_income":       round(cf_total_income, 2),
            "total_deductions":   round(cf_total_deductions, 2),
            "total_expenses":     round(cf_total_expenses, 2),
            "cc_expenses":        round(cf_cc_expenses, 2),
            "non_cc_expenses":    round(cf_non_cc_expenses, 2),
        },
        operating={k: {**v, "total": round(v["total"], 2)} for k, v in operating.items()},
        investing={k: {**v, "total": round(v["total"], 2)} for k, v in investing.items()},
        financing={k: {**v, "total": round(v["total"], 2)} for k, v in financing.items()},
    )
