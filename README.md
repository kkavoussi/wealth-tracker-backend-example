# Wealth Tracker — Backend

A personal finance web application built for a multi-member household. It pulls real banking, credit, investment, and payroll data via Plaid and SnapTrade, processes and categorizes it through a FastAPI backend, stores everything in Supabase, and surfaces it through a Next.js frontend (not in this repo).

**This repo contains the backend only.** The full system is live in production. Code is shared here as a reference — the production repo is private because it connects to live financial accounts.

---

## What makes it different

Most personal finance tools track from net deposits. This app tracks from **gross income** — it ingests full payroll data (gross pay, every pre-tax deduction, every post-tax deduction, employer contributions, taxes withheld, YTD figures) and uses that as the foundation for the income statement. The result is a household financial picture that matches what actually happened, not what hit the bank account.

---

## Stack

| Layer | Technology |
|---|---|
| Backend framework | FastAPI (Python 3.13) |
| Database / Auth | Supabase (PostgreSQL + Supabase Auth) |
| Banking & credit data | Plaid Transactions API (Production) |
| Payroll data | Plaid Income API (Production) |
| Investment data | SnapTrade (Production) |
| AI paystub parsing | Anthropic Claude API |
| Hosting | Railway |
| Frontend (separate repo) | Next.js, TypeScript, Tailwind CSS v4, shadcn/ui — Vercel |

---

## Architecture

```
Plaid Transactions API ──┐
                         ├──► FastAPI backend ──► Supabase (PostgreSQL)
Plaid Income API ────────┤         │
                         │    categorization,        │
SnapTrade API ───────────┘    payroll injection,     ▼
                              IS / CF / BS       Next.js frontend
Manual paystub upload         reports
(Claude AI parsing) ─────────────────────────────────────────────
```

The backend has no rendering layer — it's a pure JSON API. All financial logic (categorization, payroll deposit matching, report assembly) lives here.

---

## Plaid integration

### Products used

| Product | Purpose |
|---|---|
| Transactions (`/transactions/sync`) | Bank and credit card transaction history — sync-based, cursor-driven |
| Auth | Account and routing numbers |
| Balance | Real-time account balances |
| Income Verification (`/credit/payroll_income/get`) | Employer-verified payroll data: gross pay, all deductions, pay frequency, YTD figures |
| Link (update mode) | Re-authentication flow for expired OAuth connections |

### Institution routing

Users never see "Plaid" or "SnapTrade." The app silently routes based on account type:
- Banks and credit cards → Plaid
- Investment brokerages → SnapTrade
- Payroll → Plaid Income

### Key endpoints

```
POST /plaid/create-link-token       ← new bank/credit connections
POST /plaid/exchange-token          ← new connections only
POST /plaid/sync                    ← pulls transactions; detects ITEM_LOGIN_REQUIRED
POST /plaid/create-update-link-token ← re-auth: opens Link in update mode
POST /plaid/complete-reauth         ← re-auth: upserts existing item (not exchange-token)
POST /payroll/create-link-token     ← Plaid Income link
POST /payroll/sync                  ← fetches paystub snapshot, injects into IS
GET  /payroll/needs-refresh         ← detects elapsed pay periods; triggers re-link
POST /payroll/parse-paystub         ← multipart upload → Claude API → extracted fields
```

---

## Engineering challenges worth noting

These are the problems I ran into that aren't obvious from reading the Plaid docs.

### 1. `ITEM_LOGIN_REQUIRED` was silently invisible to users

When a Plaid item's OAuth token expires (e.g. Chase's consent policy), `accounts_get()` and `transactions_sync()` both throw `ITEM_LOGIN_REQUIRED`. My original error handling caught these exceptions and appended them to a `fetch_errors` list that the frontend ignored on a 200 response. Users never found out their institution had disconnected.

**Fix:** Parse the `error_code` from the exception body and route `ITEM_LOGIN_REQUIRED` specifically to an `items_requiring_reauth` list returned in the sync response. Frontend checks this on every sync and opens Plaid Link in update mode automatically if non-empty.

**Critical distinction learned:** `POST /plaid/exchange-token` creates a new `plaid_items` row. Re-auth must use a separate `POST /plaid/complete-reauth` endpoint that upserts the existing row on `plaid_item_id`. Using exchange-token for re-auth creates a duplicate row, orphans the existing accounts, and breaks member ownership tracking. These two code paths cannot be swapped.

### 2. Plaid re-categorizes historical transactions after a Chase re-auth

After a Chase OAuth re-auth, Plaid re-evaluated and re-assigned categories on some historical transactions. Two "AUTOMATIC PAYMENT - THANK YOU" entries — credit card account-side autopayment credits — were re-assigned `INCOME_SALARY` at LOW confidence. All 30+ prior instances of the same transaction had `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT` at VERY_HIGH confidence. This was a Plaid-side re-categorization artifact.

**Fix:** The categorization pipeline runs a name-pattern match step (`_CC_PATTERNS`) *before* consulting Plaid's category assignment. Any transaction whose name matches a known CC payment pattern is routed to `TRANSFERS > CREDIT_CARD_PAYMENTS` regardless of what Plaid assigned. Because this step fires first, it's immune to post-re-auth re-categorization.

### 3. Plaid Income returns a frozen snapshot — polling does nothing

`credit_payroll_income_get(user_token)` always returns the snapshot from the most recent Plaid Link session. I initially tried polling this endpoint after calling `/credit/payroll_income/refresh`, expecting fresh paystubs. The refresh fires a webhook but delivers no new data — it is effectively a no-op for data retrieval.

**Fix:** The only way to get new paystubs is to re-run Plaid Link. The `GET /payroll/needs-refresh` endpoint detects elapsed pay periods by comparing `last_synced_at` to expected pay dates (using stored `pay_frequency`), and surfaces a re-link prompt when stale. A 36-hour cooling period prevents the endpoint from triggering a re-link loop while Plaid's async processing completes.

### 4. Payroll deposit double-counting: `is_payroll_deposit` vs `exclude_from_expenses`

The income statement excludes raw bank deposits for pay periods where a paystub exists, replacing them with gross income injected from the paystub. The IS income query filters on `is_payroll_deposit = TRUE` to remove those deposits.

Early on I tried to suppress double-counted deposits by setting `exclude_from_expenses = TRUE`. That flag does nothing to IS income display — it only affects expense totals. Only `is_payroll_deposit = TRUE` removes a transaction from IS income. This is not obvious from the data model.

**Two-pass matching logic in `_mark_payroll_deposits()`:**
- Pass 1 (precise): matches INCOME transactions to stored paystubs by date (±3 days) and amount (net pay ±$1.00). Handles split deposits where multiple ACH transactions sum to net pay.
- Pass 2 (historical): keyword-matches `employer_name` against all INCOME transactions using `ILIKE`. Catches historical deposits that predate stored paystubs, and handles employer rebrands where the ACH sender name changed mid-year.

### 5. supabase-py does not raise on write failures

`supabase-py` swallows PostgREST errors silently — a failed `INSERT` or `UPDATE` returns an empty `result.data` with no exception. For most writes this is acceptable. For writes where the lost data creates permanent unrecoverable state (e.g. storing a Plaid `user_token` that can only be obtained once), silent failure is catastrophic.

**Production incident:** `plaid.user_create()` succeeded and registered the user with Plaid, but the subsequent `INSERT` into `payroll_connections` failed silently. On every future attempt, Plaid returned "user already exists" with no recovery path — the user was permanently locked out of payroll connection.

**Fix:** Explicitly check `result.data` after every write that creates unrecoverable state, and raise immediately if empty. For the lost-token recovery case: catch the "user already exists" exception from `user_create()`, detect the missing DB row, and fall back to creating a fresh Plaid user with a different `client_user_id`.

### 6. Transaction sync idempotency

Re-syncing the same data must be safe. The `transactions` table has a `UNIQUE (household_id, plaid_transaction_id)` constraint and the pipeline upserts on that key. Pending transactions that later settle get a new `plaid_transaction_id` from Plaid — both rows are kept, with the pending one marked `is_duplicate = TRUE` and `exclude_from_expenses = TRUE`. Deleting the pending row would orphan any user notes on it and break the audit trail.

---

## Database schema

15 migrations in `supabase/migrations/`. Key tables:

| Table | Purpose |
|---|---|
| `households` | Household name and metadata |
| `household_members` | Members with roles and display names |
| `plaid_items` | One row per institution connection |
| `plaid_accounts` | Individual accounts — balance sheet classification, member ownership |
| `transactions` | All transactions — categorization source, payroll flags, override tracking |
| `snaptrade_accounts` | Brokerage accounts and balances |
| `payroll_connections` | Plaid user token per member (NULL = manual-only connection) |
| `payroll_paystubs` | Full paystub data — gross, net, all deduction fields, YTD, raw JSONB |
| `payroll_w2s` | Annual W2 data per member per tax year |
| `manual_assets` | Manually-entered assets and liabilities |

Schema changes are numbered SQL files applied via psycopg2 directly against Supabase Postgres. No ORM, no Supabase CLI.

---

## Reports generated

```
GET /reports/balance-sheet       ← assets + liabilities across all sources
GET /reports/income-statement    ← gross income → deductions → expenses → net surplus
GET /reports/cash-flow           ← operating / investing / financing
```

The income statement deduction block breaks out three buckets from payroll data: investment contributions (401k, Roth, HSA, ESPP), taxes withheld (federal, state, local, FICA), and benefit premiums (health, dental, vision). This is what tracking from gross income enables — a full picture of where every dollar goes before it reaches a bank account.

---

## AI paystub parsing

For employers not connected via Plaid Income, paystubs can be uploaded as PDF, PNG, JPEG, or WEBP. The file is base64-encoded and sent to Claude (`claude-sonnet-4-6`) with a structured extraction prompt. Seven explicit rules in the prompt handle common ambiguities: NY SDI mapping, FSA vs. Dependent Care FSA separation, Roth 401k vs. traditional 401k, employer match vs. employee contribution, and post-tax benefit premium classification.

---

## What's next

- **Phase 4.20:** AI Chat Agent — `POST /chat` endpoint with Claude tool use, giving the agent access to payroll summaries, income statement data, and transaction history. The payroll-level data (gross, every deduction, YTD figures) is what makes this more capable than a generic finance chatbot.
- **Phase 5.0:** Budget creation via natural language — agent reviews spending history with the user, proposes a budget by category, and saves it to the database.
- **Staging environment:** currently the only environment is production.

---

## Running locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your Plaid, Supabase, SnapTrade, and Anthropic credentials
uvicorn app.main:app --reload
```

Requires a Plaid account (Production or Sandbox), a Supabase project, and a SnapTrade account. All environment variables are documented in `.env.example`.

---

*Built by Kavon Kavoussi at Kairos Digital LLC. Developed with Claude Code.*
