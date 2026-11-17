-- Migration 009: Per-member account ownership
-- Adds member_id to the three account tables so we can filter by household member.
-- Uses IF NOT EXISTS so re-running is safe (manual_assets.member_id may already exist).

ALTER TABLE plaid_accounts     ADD COLUMN IF NOT EXISTS member_id UUID REFERENCES household_members(member_id);
ALTER TABLE snaptrade_accounts ADD COLUMN IF NOT EXISTS member_id UUID REFERENCES household_members(member_id);
ALTER TABLE manual_assets      ADD COLUMN IF NOT EXISTS member_id UUID REFERENCES household_members(member_id);

-- Backfill: assign all existing accounts to the household admin.
UPDATE plaid_accounts pa
SET member_id = hm.member_id
FROM household_members hm
WHERE hm.household_id = pa.household_id
  AND hm.role = 'admin'
  AND pa.member_id IS NULL;

UPDATE snaptrade_accounts sa
SET member_id = hm.member_id
FROM household_members hm
WHERE hm.household_id = sa.household_id
  AND hm.role = 'admin'
  AND sa.member_id IS NULL;

UPDATE manual_assets ma
SET member_id = hm.member_id
FROM household_members hm
WHERE hm.household_id = ma.household_id
  AND hm.role = 'admin'
  AND ma.member_id IS NULL;
