-- Phase 4.8.6 — SnapTrade integration
-- Stores synced brokerage accounts and their balances per household.

CREATE TABLE snaptrade_accounts (
    id                          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    household_id                UUID REFERENCES households(household_id) NOT NULL,
    snaptrade_account_id        TEXT NOT NULL UNIQUE,
    brokerage_authorization_id  TEXT,
    institution_name            TEXT,
    name                        TEXT,
    account_number              TEXT,
    account_category            TEXT,   -- INVESTMENT, DEPOSIT, or LOC (from SnapTrade)
    raw_type                    TEXT,   -- brokerage-specific type string
    currency                    TEXT DEFAULT 'USD',
    current_balance             NUMERIC,
    balance_sheet_category      TEXT,
    balance_sheet_subcategory   TEXT,
    balance_sheet_detail        TEXT,
    is_hidden                   BOOLEAN DEFAULT FALSE,
    created_at                  TIMESTAMPTZ DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE snaptrade_accounts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "household members can read snaptrade_accounts"
  ON snaptrade_accounts FOR SELECT
  USING (household_id IN (
    SELECT household_id FROM household_members WHERE user_id = auth.uid()
  ));
