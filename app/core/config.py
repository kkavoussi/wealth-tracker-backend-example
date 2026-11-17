"""
Application configuration — reads all settings from .env
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Supabase
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    supabase_db_password: str = ""

    # Plaid
    plaid_client_id: str
    plaid_secret: str
    plaid_sandbox_secret: str = ""
    plaid_env: str = "production"

    # SnapTrade
    snaptrade_client_id:    str = ""
    snaptrade_consumer_key: str = ""

    # Anthropic
    anthropic_api_key: str = ""

    # App
    app_env: str = "development"
    secret_key: str = "change-me"
    frontend_url: str = "http://localhost:3000"
    backend_url: str = "http://localhost:8000"

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
