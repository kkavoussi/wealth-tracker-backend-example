-- =============================================================================
-- Migration 002: Manual Assets
-- Finance App — Phase 4.1
-- =============================================================================
-- Tracks manually-entered assets (real estate, vehicles, personal property,
-- cash & other) that do not connect to Plaid.
--
-- Run in: Supabase Dashboard → SQL Editor → New Query → Paste → Run
-- =============================================================================


CREATE TABLE public.manual_assets (
  asset_id                  UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
  household_id              UUID          NOT NULL REFERENCES public.households(household_id) ON DELETE CASCADE,
  member_id                 UUID          REFERENCES public.household_members(member_id),
  name                      TEXT          NOT NULL,
  asset_type                TEXT          NOT NULL
                                          CHECK (asset_type IN ('real_estate','vehicle','personal_property','cash_other')),
  current_value             NUMERIC(15,2) NOT NULL DEFAULT 0,
  description               TEXT,
  balance_sheet_subcategory TEXT          NOT NULL,
  is_hidden                 BOOLEAN       NOT NULL DEFAULT FALSE,
  created_at                TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
  updated_at                TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_manual_assets_household_id ON public.manual_assets(household_id);

CREATE TRIGGER set_manual_assets_updated_at
  BEFORE UPDATE ON public.manual_assets
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();


-- =============================================================================
-- ROW LEVEL SECURITY
-- Same household-isolation pattern as all other tables.
-- =============================================================================

ALTER TABLE public.manual_assets ENABLE ROW LEVEL SECURITY;

CREATE POLICY "manual_assets: members can read"
  ON public.manual_assets FOR SELECT
  USING (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "manual_assets: members can insert"
  ON public.manual_assets FOR INSERT
  WITH CHECK (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "manual_assets: members can update"
  ON public.manual_assets FOR UPDATE
  USING (household_id IN (SELECT public.get_user_household_ids()));

CREATE POLICY "manual_assets: admin/member can delete"
  ON public.manual_assets FOR DELETE
  USING (household_id IN (
    SELECT household_id FROM public.household_members
    WHERE user_id = auth.uid() AND role IN ('admin', 'member')
  ));


-- =============================================================================
-- DONE
-- Table:   manual_assets
-- Indexes: idx_manual_assets_household_id
-- RLS:     SELECT / INSERT / UPDATE / DELETE — household-scoped
-- =============================================================================
