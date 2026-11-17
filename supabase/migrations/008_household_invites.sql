-- Migration 008: Household invite codes
-- One-time-use invite links so a second member can join an existing household.
-- Accessed server-side only via service role (no RLS needed).

CREATE TABLE household_invites (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  household_id UUID NOT NULL REFERENCES households(household_id) ON DELETE CASCADE,
  created_by   UUID NOT NULL REFERENCES auth.users(id),
  code         TEXT NOT NULL UNIQUE,
  expires_at   TIMESTAMPTZ NOT NULL,
  used_at      TIMESTAMPTZ,
  used_by      UUID REFERENCES auth.users(id),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
