-- Migration 012: Expand payroll_paystubs with YTD amounts, earnings breakdown, employer match
--
-- Design rules:
--   - Every period column gets a matching _ytd column.
--   - YTD columns default to 0 (not null) so get_payroll_summary() can compare with > 0.
--   - gross_pay_ytd / net_pay_ytd move from raw_data JSONB to proper columns.
--   - earnings breakdown captures REGULAR_PAY / BONUS / OVERTIME / OTHER per Plaid's
--     canonical_description (and whatever PDF parsing produces).
--   - employer_401k_match is income, not a deduction — stored at period + YTD.
--   - source column distinguishes 'plaid' | 'manual_upload' | 'manual_entry'.

-- ── YTD totals (gross + net) ─────────────────────────────────────────────────
ALTER TABLE payroll_paystubs
  ADD COLUMN IF NOT EXISTS gross_pay_ytd        NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS net_pay_ytd          NUMERIC(12,2) NOT NULL DEFAULT 0;

-- ── Tax YTDs ─────────────────────────────────────────────────────────────────
ALTER TABLE payroll_paystubs
  ADD COLUMN IF NOT EXISTS federal_income_tax_ytd   NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS state_income_tax_ytd     NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS local_income_tax_ytd     NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS social_security_tax_ytd  NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS medicare_tax_ytd         NUMERIC(12,2) NOT NULL DEFAULT 0;

-- ── Pre-tax deduction YTDs ────────────────────────────────────────────────────
ALTER TABLE payroll_paystubs
  ADD COLUMN IF NOT EXISTS retirement_401k_ytd  NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS roth_401k_ytd        NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS hsa_contribution_ytd NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS fsa_contribution_ytd NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS dep_care_fsa_ytd     NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS health_insurance_ytd NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS dental_insurance_ytd NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS vision_insurance_ytd NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS other_pretax_ytd     NUMERIC(12,2) NOT NULL DEFAULT 0;

-- ── Post-tax deduction YTDs ───────────────────────────────────────────────────
ALTER TABLE payroll_paystubs
  ADD COLUMN IF NOT EXISTS espp_contribution_ytd NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS other_posttax_ytd     NUMERIC(12,2) NOT NULL DEFAULT 0;

-- ── Earnings breakdown by type (period + YTD) ────────────────────────────────
-- Maps to Plaid canonical_description: REGULAR_PAY, BONUS, OVERTIME,
-- SHIFT_DIFFERENTIAL / other earnings (remote stipend, commissions, etc.)
ALTER TABLE payroll_paystubs
  ADD COLUMN IF NOT EXISTS regular_pay        NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS regular_pay_ytd    NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS bonus_pay          NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS bonus_pay_ytd      NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS overtime_pay       NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS overtime_pay_ytd   NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS other_earnings     NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS other_earnings_ytd NUMERIC(12,2) NOT NULL DEFAULT 0;

-- ── Employer contributions ────────────────────────────────────────────────────
-- Some payroll providers return employer 401k match in earnings.breakdown.
-- PDF parsing will also populate this when the stub shows it.
ALTER TABLE payroll_paystubs
  ADD COLUMN IF NOT EXISTS employer_401k_match     NUMERIC(12,2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS employer_401k_match_ytd NUMERIC(12,2) NOT NULL DEFAULT 0;

-- ── Source tracking ───────────────────────────────────────────────────────────
-- Distinguishes how the row was created so the UI can show provenance.
ALTER TABLE payroll_paystubs
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'plaid';
-- Values: 'plaid' | 'manual_upload' (PDF parsed by Claude) | 'manual_entry' (form)
