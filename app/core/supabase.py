"""
Supabase client — two clients:
  - client:        uses anon key  (safe for user-scoped operations with RLS)
  - admin_client:  uses service_role key (bypasses RLS — backend use only, never expose to frontend)
"""
from supabase import create_client, Client
from app.core.config import settings


def get_supabase_client() -> Client:
    """Anon client — respects Row Level Security."""
    return create_client(settings.supabase_url, settings.supabase_anon_key)


def get_supabase_admin_client() -> Client:
    """Service role client — bypasses RLS. Backend only."""
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
