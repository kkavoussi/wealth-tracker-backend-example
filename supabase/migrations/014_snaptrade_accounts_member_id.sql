-- Phase 4.13 — SnapTrade per-member account ownership
-- Adds member_id to snaptrade_accounts so investment accounts can be filtered
-- by individual household member (same pattern as plaid_accounts).
-- NULL = unassigned (legacy rows pre-migration; fixed by data fix script).

ALTER TABLE snaptrade_accounts
ADD COLUMN IF NOT EXISTS member_id UUID REFERENCES household_members(member_id);
