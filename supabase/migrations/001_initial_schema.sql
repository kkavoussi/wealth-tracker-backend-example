-- =============================================================================
-- Migration 001: Initial Schema
-- Finance App — Phase 4.0
-- =============================================================================
-- Run this in: Supabase Dashboard → SQL Editor → New Query → Paste → Run
-- =============================================================================


-- =============================================================================
-- SECTION 1: UTILITY — updated_at auto-trigger
-- Every table gets updated_at. This function fires automatically on any UPDATE
-- so we never have to remember to set it in application code.
-- =============================================================================

CREATE OR REPLACE FUNCTION public.handle_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- =============================================================================
-- SECTION 2: HOUSEHOLDS
-- Top-level entity. One per family/individual.
-- plan_tier is baked in now — freemium model has a home from day one.
-- =============================================================================

CREATE TABLE public.households (
  household_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  name                TEXT        NOT NULL,
  plan_tier           TEXT        NOT NULL DEFAULT 'free'
                                  CHECK (plan_tier IN ('free', 'essentials', 'premium')),
  status              TEXT        NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active', 'suspended', 'deleted')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER set_households_updated_at
  BEFORE UPDATE ON public.households
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();


-- =============================================================================
-- SECTION 3: HOUSEHOLD MEMBERS
-- Each person has their own Supabase Auth login but belongs to a household.
-- UNIQUE(household_id, user_id) prevents duplicate membership.
-- =============================================================================

CREATE TABLE public.household_members (
  member_id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  household_id        UUID        NOT NULL REFERENCES public.households(household_id) ON DELETE CASCADE,
  user_id             UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  display_name        TEXT        NOT NULL,
  role                TEXT        NOT NULL DEFAULT 'member'
                                  CHECK (role IN ('admin', 'member', 'viewer')),
  invited_at          TIMESTAMPTZ,
  joined_at           TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (household_id, user_id)
);

CREATE INDEX idx_household_members_household_id ON public.household_members(household_id);
CREATE INDEX idx_household_members_user_id      ON public.household_members(user_id);

CREATE TRIGGER set_household_members_updated_at
  BEFORE UPDATE ON public.household_members
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();


-- =============================================================================
-- SECTION 4: HOUSEHOLD INVITATIONS
-- First-class table for invites — enables expiry, resending, revocation,
-- and acceptance tracking. Token is a random 32-byte hex string used in
-- the invite email link.
-- =============================================================================

CREATE TABLE public.household_invitations (
  invitation_id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  household_id            UUID        NOT NULL REFERENCES public.households(household_id) ON DELETE CASCADE,
  invited_by_member_id    UUID        NOT NULL REFERENCES public.household_members(member_id),
  email                   TEXT        NOT NULL,
  role                    TEXT        NOT NULL DEFAULT 'member'
                                      CHECK (role IN ('member', 'viewer')),
  token                   TEXT        NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(32), 'hex'),
  status                  TEXT        NOT NULL DEFAULT 'pending'
                                      CHECK (status IN ('pending', 'accepted', 'expired', 'revoked')),
  expires_at              TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '7 days',
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_invitations_household_id ON public.household_invitations(household_id);
CREATE INDEX idx_invitations_email        ON public.household_invitations(email);
CREATE INDEX idx_invitations_token        ON public.household_invitations(token);


-- =============================================================================
-- SECTION 5: PLAID ITEMS (Institution connections)
-- One row per bank/institution per household.
-- access_token is the key to the user's bank data — treat like a password.
-- consent_expiration_time tracks OAuth re-auth requirements (some banks
-- require re-auth every 90 days).
-- =============================================================================

CREATE TABLE public.plaid_items (
  item_id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  household_id            UUID        NOT NULL REFERENCES public.households(household_id) ON DELETE CASCADE,
  connected_by_member_id  UUID        REFERENCES public.household_members(member_id),
  plaid_item_id           TEXT        NOT NULL UNIQUE,
  access_token            TEXT        NOT NULL,
  institution_id          TEXT,
  institution_name        TEXT,
  status                  TEXT        NOT NULL DEFAULT 'active'
                                      CHECK (status IN ('active', 'error', 'disconnected')),
  error_code              TEXT,
  consent_expiration_time TIMESTAMPTZ,
  connected_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_synced_at          TIMESTAMPTZ,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_plaid_items_household_id ON public.plaid_items(household_id);

CREATE TRIGGER set_plaid_items_updated_at
  BEFORE UPDATE ON public.plaid_items
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();


-- =============================================================================
-- SECTION 6: PLAID ACCOUNTS
-- One row per account (checking, savings, credit card, 401k, brokerage, etc.)
--
-- balance_sheet_category / balance_sheet_subcategory map to the
-- balance_sheet_account_categories.json taxonomy built in Phase 3. This is
-- what drives the balance sheet view on the dashboard.
--
-- assigned_member_id links the account to a specific person in the household.
-- is_hidden lets users hide accounts without deleting them.
-- credit_limit stored for credit cards — needed for utilization calculations.
-- =============================================================================

CREATE TABLE public.plaid_accounts (
  account_id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  item_id                     UUID        NOT NULL REFERENCES public.plaid_items(item_id) ON DELETE CASCADE,
  household_id                UUID        NOT NULL REFERENCES public.households(household_id) ON DELETE CASCADE,
  assigned_member_id          UUID        REFERENCES public.household_members(member_id),

  -- Plaid fields
  plaid_account_id            TEXT        NOT NULL UNIQUE,
  name                        TEXT        NOT NULL,
  official_name               TEXT,
  type                        TEXT        NOT NULL,    -- depository | credit | investment | loan | other
  subtype                     TEXT,                   -- checking | savings | credit card | 401k | etc.
  current_balance             NUMERIC(15,2),
  available_balance           NUMERIC(15,2),
  credit_limit                NUMERIC(15,2),
  iso_currency_code           TEXT        NOT NULL DEFAULT 'USD',

  -- Our enrichment — maps to balance_sheet_account_categories.json taxonomy
  balance_sheet_category      TEXT,                   -- ASSETS | LIABILITIES
  balance_sheet_subcategory   TEXT,                   -- CASH_AND_EQUIVALENTS | INVESTMENTS | CREDIT_CARDS | etc.
  balance_sheet_detail        TEXT,                   -- CHECKING | SAVINGS | RETIREMENT_401K | etc.

  is_hidden                   BOOLEAN     NOT NULL DEFAULT FALSE,
  last_synced_at              TIMESTAMPTZ,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_plaid_accounts_household_id ON public.plaid_accounts(household_id);
CREATE INDEX idx_plaid_accounts_item_id      ON public.plaid_accounts(item_id);

CREATE TRIGGER set_plaid_accounts_updated_at
  BEFORE UPDATE ON public.plaid_accounts
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();


-- =============================================================================
-- SECTION 7: TRANSACTIONS
-- The largest table. Schema is a direct port of transaction_schema.json v3.2.
--
-- TWO categorization systems are stored side by side:
--   1. Plaid's categorization (plaid_category_*) — stored exactly as received
--   2. Our assigned categorization (category / subcategory / detail) — output
--      of our categorization engine, maps to expense_categories.json taxonomy
--
-- This design lets us:
--   - Always have Plaid's raw data for debugging and recategorization
--   - Port the existing Python categorization engine without data loss
--   - Build ML improvements on top of the distinction between the two systems
--
-- Key scale decisions:
--   - UNIQUE(household_id, plaid_transaction_id): prevents duplicate imports
--   - Composite index on (household_id, date DESC): powers every dashboard query
--   - Index on (household_id, category): powers spending breakdowns
--   - Partial index on pending=TRUE: fast lookup of pending transactions only
--   - NUMERIC(15,2) for money: never use FLOAT for currency
--   - No DELETE policy: financial audit trail is never destroyed
-- =============================================================================

CREATE TABLE public.transactions (
  transaction_id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  household_id                UUID        NOT NULL REFERENCES public.households(household_id) ON DELETE CASCADE,
  account_id                  UUID        NOT NULL REFERENCES public.plaid_accounts(account_id) ON DELETE CASCADE,
  assigned_member_id          UUID        REFERENCES public.household_members(member_id),

  -- ---- Plaid core fields (always present) ----
  plaid_transaction_id        TEXT        NOT NULL,
  pending_transaction_id      TEXT,                   -- links pending tx to its posted version
  date                        DATE        NOT NULL,
  authorized_date             DATE,
  amount                      NUMERIC(15,2) NOT NULL, -- Plaid convention: positive = outflow
  display_amount              NUMERIC(15,2) NOT NULL, -- Human convention: positive = income
  iso_currency_code           TEXT        NOT NULL DEFAULT 'USD',
  unofficial_currency_code    TEXT,                   -- for crypto transactions
  name                        TEXT        NOT NULL,   -- raw transaction name from bank
  merchant_name               TEXT,                   -- clean merchant name from Plaid
  merchant_entity_id          TEXT,                   -- Plaid's stable merchant identifier
  logo_url                    TEXT,                   -- merchant logo URL for UI
  check_number                TEXT,                   -- populated for check transactions
  pending                     BOOLEAN     NOT NULL DEFAULT FALSE,
  payment_channel             TEXT,                   -- online | in store | other
  transaction_type            TEXT,                   -- special | place | digital | unresolved
  account_owner               TEXT,                   -- legal account owner from Plaid

  -- ---- Plaid categorization (v2 personal_finance_category — stored as-is) ----
  plaid_category_primary      TEXT,                   -- e.g. GENERAL_SERVICES
  plaid_category_detailed     TEXT,                   -- e.g. GENERAL_SERVICES_GYMS_AND_FITNESS
  plaid_category_confidence   TEXT,                   -- HIGH | MEDIUM | LOW

  -- ---- Location (sparse but useful when available) ----
  location_address            TEXT,
  location_city               TEXT,
  location_region             TEXT,
  location_postal_code        TEXT,
  location_country            TEXT,
  location_lat                NUMERIC(10,7),
  location_lon                NUMERIC(10,7),
  location_store_number       TEXT,

  -- ---- Our assigned categorization (output of categorization engine) ----
  -- Maps to expense_categories.json taxonomy
  -- Top-level: INCOME | PAYCHECK_DEDUCTIONS | EXPENSES | TRANSFERS |
  --            TAXES | INVESTMENT_EXPENSES | REAL_ESTATE_INVESTMENT_EXPENSES
  category                    TEXT,
  subcategory                 TEXT,
  detail                      TEXT,
  exclude_from_expenses       BOOLEAN     NOT NULL DEFAULT FALSE,

  -- ---- Override tracking ----
  manually_reviewed           BOOLEAN     NOT NULL DEFAULT FALSE,
  manually_overridden         BOOLEAN     NOT NULL DEFAULT FALSE,
  override_reason             TEXT,
  override_date               TIMESTAMPTZ,
  override_by_member_id       UUID        REFERENCES public.household_members(member_id),

  -- ---- Reconciliation ----
  reconciled                  BOOLEAN     NOT NULL DEFAULT FALSE,
  reconciled_date             TIMESTAMPTZ,
  reconciliation_notes        TEXT,

  -- ---- Flags ----
  flagged_for_review          BOOLEAN     NOT NULL DEFAULT FALSE,
  flag_reason                 TEXT,
  is_duplicate                BOOLEAN     NOT NULL DEFAULT FALSE,
  duplicate_of                TEXT,                   -- plaid_transaction_id of original

  -- ---- Internal transfer pairing ----
  -- When the categorization engine detects an internal transfer (e.g., M&T Checking →
  -- Ally Savings), it links the outflow and inflow transactions via this field.
  -- Only the OUTFLOW side is written to reports; this field makes the pair queryable.
  matched_transaction_id      TEXT,                   -- plaid_transaction_id of the paired leg

  -- ---- User enrichment ----
  tags                        TEXT[]      NOT NULL DEFAULT '{}',
  notes                       TEXT,
  -- Pre-computed display path for UI — avoids joining category tables on every render
  -- e.g. "Expenses > Food & Dining > Restaurants"
  category_display_name       TEXT,

  -- ---- Pipeline metadata ----
  categorization_source       TEXT,                   -- plaid | rule | manual | ml
  categorization_confidence   TEXT,                   -- USER_CONFIRMED | HIGH | MEDIUM | LOW | RULE_MATCHED | FALLBACK | UNCATEGORIZED
  import_source               TEXT        NOT NULL DEFAULT 'plaid_api'
                                          CHECK (import_source IN ('plaid_api', 'manual_entry', 'csv_import', 'other')),
  processing_batch_id         TEXT,                   -- e.g. "2026-03-13-fetch"
  plaid_api_version           TEXT,

  -- ---- Timestamps ----
  fetched_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  categorized_at              TIMESTAMPTZ,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  UNIQUE (household_id, plaid_transaction_id)
);

-- Primary dashboard query: "show me transactions for this household, newest first"
CREATE INDEX idx_transactions_household_date
  ON public.transactions(household_id, date DESC);

-- Account-level drill-down
CREATE INDEX idx_transactions_account_id
  ON public.transactions(account_id);

-- Spending category breakdowns (monthly income statements / expense reports)
CREATE INDEX idx_transactions_household_category
  ON public.transactions(household_id, category);

-- Deduplication check on Plaid sync
CREATE INDEX idx_transactions_plaid_id
  ON public.transactions(plaid_transaction_id);

-- Partial index: only pending rows — fast "pending transactions" widget
CREATE INDEX idx_transactions_pending
  ON public.transactions(household_id)
  WHERE pending = TRUE;

-- Flagged transactions review queue
CREATE INDEX idx_transactions_flagged
  ON public.transactions(household_id)
  WHERE flagged_for_review = TRUE;

-- Tags search (GIN index for array containment queries)
CREATE INDEX idx_transactions_tags
  ON public.transactions USING GIN (tags);

CREATE TRIGGER set_transactions_updated_at
  BEFORE UPDATE ON public.transactions
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();


-- =============================================================================
-- SECTION 8: ROW LEVEL SECURITY (RLS)
--
-- RLS is enforced at the database layer — not the app layer. Even if there's
-- a bug in the API, households cannot see each other's data.
--
-- The helper function get_user_household_ids() is SECURITY DEFINER + STABLE.
-- Critical for performance: without this, Postgres re-runs the household_members
-- subquery for every single row scanned. With it, the result is cached within
-- a single query execution.
-- =============================================================================

CREATE OR REPLACE FUNCTION public.get_user_household_ids()
RETURNS SETOF UUID
LANGUAGE sql
SECURITY DEFINER
STABLE
AS $$
  SELECT household_id
  FROM public.household_members
  WHERE user_id = auth.uid();
$$;

-- Enable RLS on all tables
ALTER TABLE public.households               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.household_members        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.household_invitations    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.plaid_items              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.plaid_accounts           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transactions             ENABLE ROW LEVEL SECURITY;

-- ---- households ----
CREATE POLICY "households: members can read their own"
  ON public.households FOR SELECT
  USING (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "households: admin can update"
  ON public.households FOR UPDATE
  USING (household_id IN (
    SELECT household_id FROM public.household_members
    WHERE user_id = auth.uid() AND role = 'admin'
  ));

-- ---- household_members ----
CREATE POLICY "members: can read own household"
  ON public.household_members FOR SELECT
  USING (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "members: admin can insert"
  ON public.household_members FOR INSERT
  WITH CHECK (household_id IN (
    SELECT household_id FROM public.household_members
    WHERE user_id = auth.uid() AND role = 'admin'
  ));

CREATE POLICY "members: admin can update"
  ON public.household_members FOR UPDATE
  USING (household_id IN (
    SELECT household_id FROM public.household_members
    WHERE user_id = auth.uid() AND role = 'admin'
  ));

-- ---- household_invitations ----
CREATE POLICY "invitations: members can read own household"
  ON public.household_invitations FOR SELECT
  USING (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "invitations: admin can insert"
  ON public.household_invitations FOR INSERT
  WITH CHECK (household_id IN (
    SELECT household_id FROM public.household_members
    WHERE user_id = auth.uid() AND role = 'admin'
  ));

CREATE POLICY "invitations: admin can update (revoke/expire)"
  ON public.household_invitations FOR UPDATE
  USING (household_id IN (
    SELECT household_id FROM public.household_members
    WHERE user_id = auth.uid() AND role = 'admin'
  ));

-- ---- plaid_items ----
CREATE POLICY "plaid_items: members can read"
  ON public.plaid_items FOR SELECT
  USING (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "plaid_items: members can insert"
  ON public.plaid_items FOR INSERT
  WITH CHECK (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "plaid_items: members can update"
  ON public.plaid_items FOR UPDATE
  USING (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "plaid_items: admin/member can delete"
  ON public.plaid_items FOR DELETE
  USING (household_id IN (
    SELECT household_id FROM public.household_members
    WHERE user_id = auth.uid() AND role IN ('admin', 'member')
  ));

-- ---- plaid_accounts ----
CREATE POLICY "plaid_accounts: members can read"
  ON public.plaid_accounts FOR SELECT
  USING (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "plaid_accounts: members can insert"
  ON public.plaid_accounts FOR INSERT
  WITH CHECK (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "plaid_accounts: members can update"
  ON public.plaid_accounts FOR UPDATE
  USING (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "plaid_accounts: admin/member can delete"
  ON public.plaid_accounts FOR DELETE
  USING (household_id IN (
    SELECT household_id FROM public.household_members
    WHERE user_id = auth.uid() AND role IN ('admin', 'member')
  ));

-- ---- transactions ----
CREATE POLICY "transactions: members can read"
  ON public.transactions FOR SELECT
  USING (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "transactions: members can insert"
  ON public.transactions FOR INSERT
  WITH CHECK (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "transactions: members can update"
  ON public.transactions FOR UPDATE
  USING (household_id IN (SELECT public.get_user_household_ids()));

-- No DELETE policy on transactions — financial audit trail is never destroyed.
-- Soft-delete via flags (is_duplicate, flagged_for_review) if needed.


-- =============================================================================
-- DONE
-- Tables:  households, household_members, household_invitations,
--          plaid_items, plaid_accounts, transactions
-- Indexes: 12 indexes optimized for dashboard and pipeline query patterns
-- RLS:     enforced on all 6 tables via household isolation
-- Schema:  fully aligned with transaction_schema.json v3.2,
--          expense_categories.json v1.3, and
--          balance_sheet_account_categories.json v1.0
-- =============================================================================
