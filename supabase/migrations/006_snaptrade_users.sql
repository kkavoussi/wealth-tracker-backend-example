-- Phase 4.8.6 — SnapTrade integration
-- Stores SnapTrade user credentials (userId + userSecret) per household.
-- No user-facing RLS: userSecret is sensitive and only accessed server-side
-- via the service role key.

CREATE TABLE snaptrade_users (
    id                      UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    household_id            UUID REFERENCES households(household_id) NOT NULL UNIQUE,
    snaptrade_user_id       TEXT NOT NULL,
    snaptrade_user_secret   TEXT NOT NULL,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    last_synced_at          TIMESTAMPTZ
);

ALTER TABLE snaptrade_users ENABLE ROW LEVEL SECURITY;
