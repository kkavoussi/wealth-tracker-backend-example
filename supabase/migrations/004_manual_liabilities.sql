-- =============================================================================
-- Migration 004: Manual Liabilities Support
-- Finance App — Phase 4.8.4
-- =============================================================================
-- Adds balance_sheet_category column to manual_assets so entries can be
-- classified as either ASSETS or LIABILITIES. Also expands the asset_type
-- CHECK constraint to include liability types.
--
-- Run in: Supabase Dashboard → SQL Editor → New Query → Paste → Run
-- =============================================================================


-- 1. Add balance_sheet_category column (existing rows default to 'ASSETS')
ALTER TABLE public.manual_assets
  ADD COLUMN IF NOT EXISTS balance_sheet_category TEXT NOT NULL DEFAULT 'ASSETS';


-- 2. Drop the existing asset_type constraint
ALTER TABLE public.manual_assets
  DROP CONSTRAINT IF EXISTS manual_assets_asset_type_check;


-- 3. Add expanded constraint with all valid types (assets + liabilities)
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
    -- Liabilities
    'personal_loan', 'auto_loan', 'student_loan', 'mortgage_manual', 'heloc',
    'credit_card_manual', 'other_liability'
  ));


-- =============================================================================
-- DONE
-- Column added:   balance_sheet_category (default 'ASSETS')
-- Constraint:     manual_assets_asset_type_check — expanded to include liabilities
-- Existing rows:  balance_sheet_category = 'ASSETS' (correct for all existing data)
-- =============================================================================
