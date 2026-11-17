-- Migration 011: Add is_payroll_deposit flag to transactions
-- Used to exclude net paycheck ACH deposits from income totals when
-- gross payroll data is available from Plaid income verification.

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS is_payroll_deposit BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_transactions_payroll
  ON transactions (household_id, is_payroll_deposit)
  WHERE is_payroll_deposit = TRUE;
