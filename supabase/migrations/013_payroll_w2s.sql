-- Migration 013: payroll_w2s table
--
-- Stores W2 documents returned by Plaid's income verification endpoint
-- and W2s uploaded manually as PDFs. Plaid already returns 2025 W2 data
-- in credit_payroll_income_get — this captures it properly.
--
-- Box references:
--   Box 1  = wages_tips_other_comp
--   Box 2  = federal_income_tax_withheld
--   Box 3  = social_security_wages
--   Box 4  = social_security_tax_withheld
--   Box 5  = medicare_wages_and_tips
--   Box 6  = medicare_tax_withheld
--   Box 10 = dependent_care_benefits
--   Box 12 = multiple coded entries (D=traditional 401k, AA=Roth 401k,
--             DD=employer health insurance, C=group term life, etc.) — stored as JSONB
--   State  = state_and_local_wages — multiple states possible — stored as JSONB

CREATE TABLE IF NOT EXISTS payroll_w2s (
  id                           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  connection_id                UUID NOT NULL REFERENCES payroll_connections(id) ON DELETE CASCADE,
  household_id                 UUID NOT NULL,
  member_id                    UUID REFERENCES household_members(member_id),

  -- Identity
  tax_year                     TEXT NOT NULL,          -- e.g. '2025'
  employer_name                TEXT,
  employer_ein                 TEXT,                   -- employer identification number

  -- Core wage + federal tax boxes
  wages_tips_other_comp        NUMERIC(12,2),          -- Box 1
  federal_income_tax_withheld  NUMERIC(12,2),          -- Box 2
  social_security_wages        NUMERIC(12,2),          -- Box 3
  social_security_tax_withheld NUMERIC(12,2),          -- Box 4
  medicare_wages_and_tips      NUMERIC(12,2),          -- Box 5
  medicare_tax_withheld        NUMERIC(12,2),          -- Box 6
  dependent_care_benefits      NUMERIC(12,2),          -- Box 10

  -- Coded entries (e.g. [{code: "D", amount: "5500.00"}, {code: "DD", amount: "3273.76"}])
  box_12                       JSONB,

  -- State / local wages (e.g. [{state: "NY", state_income_tax: "3864.61", ...}])
  state_and_local_wages        JSONB,

  -- Provenance
  document_id                  TEXT,                   -- Plaid document_id or uploaded filename
  source                       TEXT NOT NULL DEFAULT 'plaid',  -- 'plaid' | 'manual_upload'

  created_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  UNIQUE (connection_id, tax_year)
);

-- Index for common lookups
CREATE INDEX IF NOT EXISTS idx_payroll_w2s_household
  ON payroll_w2s (household_id);

CREATE INDEX IF NOT EXISTS idx_payroll_w2s_member
  ON payroll_w2s (member_id);
