-- Migration 010: Payroll Income Tables
-- Stores Plaid income verification connections and parsed paystub data.

CREATE TABLE payroll_connections (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  household_id         UUID NOT NULL REFERENCES households(household_id) ON DELETE CASCADE,
  member_id            UUID REFERENCES household_members(member_id),
  plaid_user_token     TEXT NOT NULL,
  employer_name        TEXT,
  pay_frequency        TEXT,          -- 'biweekly', 'semimonthly', 'monthly', etc.
  payroll_ach_pattern  TEXT,          -- e.g. 'MERLYN MI PAYROLL' — used to match + exclude transactions
  last_synced_at       TIMESTAMPTZ,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE payroll_paystubs (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  connection_id         UUID NOT NULL REFERENCES payroll_connections(id) ON DELETE CASCADE,
  household_id          UUID NOT NULL,
  member_id             UUID REFERENCES household_members(member_id),
  pay_date              DATE NOT NULL,
  pay_period_start      DATE,
  pay_period_end        DATE,
  employer_name         TEXT,
  gross_pay             NUMERIC(12,2) NOT NULL DEFAULT 0,
  net_pay               NUMERIC(12,2) NOT NULL DEFAULT 0,
  -- Taxes withheld
  federal_income_tax    NUMERIC(12,2) NOT NULL DEFAULT 0,
  state_income_tax      NUMERIC(12,2) NOT NULL DEFAULT 0,
  local_income_tax      NUMERIC(12,2) NOT NULL DEFAULT 0,
  social_security_tax   NUMERIC(12,2) NOT NULL DEFAULT 0,
  medicare_tax          NUMERIC(12,2) NOT NULL DEFAULT 0,
  -- Pre-tax deductions
  retirement_401k       NUMERIC(12,2) NOT NULL DEFAULT 0,
  roth_401k             NUMERIC(12,2) NOT NULL DEFAULT 0,
  hsa_contribution      NUMERIC(12,2) NOT NULL DEFAULT 0,
  fsa_contribution      NUMERIC(12,2) NOT NULL DEFAULT 0,
  dep_care_fsa          NUMERIC(12,2) NOT NULL DEFAULT 0,
  health_insurance      NUMERIC(12,2) NOT NULL DEFAULT 0,
  dental_insurance      NUMERIC(12,2) NOT NULL DEFAULT 0,
  vision_insurance      NUMERIC(12,2) NOT NULL DEFAULT 0,
  other_pretax          NUMERIC(12,2) NOT NULL DEFAULT 0,
  -- Post-tax deductions
  espp_contribution     NUMERIC(12,2) NOT NULL DEFAULT 0,
  other_posttax         NUMERIC(12,2) NOT NULL DEFAULT 0,
  raw_data              JSONB,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (connection_id, pay_date)
);
