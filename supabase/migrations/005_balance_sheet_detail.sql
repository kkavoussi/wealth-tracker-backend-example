-- =============================================================================
-- Migration 005: balance_sheet_detail on manual_assets + expand liability types
-- Finance App — Phase 4.8.4
-- =============================================================================
-- Adds balance_sheet_detail column so manual entries (especially credit cards)
-- can store a 3rd-level detail (e.g. MASTERCARD, VISA, PERSONAL_LOAN).
-- Also expands asset_type CHECK to include all liability sub-types aligned
-- with balance_sheet_account_categories.json.
--
-- Run in: Supabase Dashboard → SQL Editor → New Query → Paste → Run
-- (Already applied to production via psycopg2 on 2026-04-20)
-- =============================================================================


-- 1. Add balance_sheet_detail column (nullable)
ALTER TABLE public.manual_assets
  ADD COLUMN IF NOT EXISTS balance_sheet_detail TEXT;


-- 2. Drop existing asset_type constraint
ALTER TABLE public.manual_assets
  DROP CONSTRAINT IF EXISTS manual_assets_asset_type_check;


-- 3. Expanded constraint — all asset + liability sub-types
ALTER TABLE public.manual_assets
  ADD CONSTRAINT manual_assets_asset_type_check
  CHECK (asset_type IN (
    -- Cash & Equivalents
    'checking', 'savings', 'money_market', 'cash_management', 'cd', 'cash_other',
    -- Investments
    'taxable_brokerage', 'retirement_401k', 'retirement_ira', 'retirement_roth',
    'education_529', 'hsa', 'cryptocurrency', 'stock_options', 'espp',
    -- Real Estate
    'primary_residence', 'investment_property', 'vacation_property', 'land', 'real_estate',
    -- Other Assets
    'vehicle', 'personal_property', 'business_assets', 'other_asset',
    -- Liabilities: Loans
    'personal_loan', 'auto_loan', 'student_loan', 'margin_loan', '401k_loan',
    -- Liabilities: Mortgages
    'mortgage_manual', 'heloc', 'investment_mortgage', 'home_equity_loan',
    -- Liabilities: Credit Cards
    'credit_card_manual',
    -- Liabilities: Other
    'other_liability', 'medical_debt', 'tax_debt', 'family_loan'
  ));


-- =============================================================================
-- DONE
-- Column added:   balance_sheet_detail (nullable TEXT)
-- Constraint:     manual_assets_asset_type_check — fully expanded
-- =============================================================================
