-- Migration 003: Expand manual_assets asset_type CHECK constraint
-- Adds ~20 new valid asset types and migrates vehicle rows to OTHER_ASSETS subcategory.
-- Run manually in: Supabase Dashboard → SQL Editor

-- 1. Drop the existing constraint
ALTER TABLE public.manual_assets
  DROP CONSTRAINT IF EXISTS manual_assets_asset_type_check;

-- 2. Add expanded constraint with all valid types
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
    -- Other Assets (vehicle routes to OTHER_ASSETS, not VEHICLES)
    'vehicle', 'personal_property', 'business_assets', 'other_asset'
  ));

-- 3. Migrate vehicle rows: VEHICLES subcategory → OTHER_ASSETS
--    (currently only the Toyota Prius C row is affected)
UPDATE public.manual_assets
  SET balance_sheet_subcategory = 'OTHER_ASSETS'
  WHERE balance_sheet_subcategory = 'VEHICLES';
