-- Phase 4.18: Allow manual payroll connections (no Plaid token)
ALTER TABLE payroll_connections ALTER COLUMN plaid_user_token DROP NOT NULL;
