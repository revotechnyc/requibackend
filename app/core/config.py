"""
Application settings — loaded once from requi-backend/.env

Hosting: change database in ONE place only (see .env "DATABASE" section):
  • Set DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD — recommended, or
  • Set DATABASE_URL only (full connection string; overrides DB_* if set)

config.py reads .env; the rest of the app uses `settings.database_url` only.
"""

from typing import Optional
from urllib.parse import quote_plus

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = "Requi Health API"
    app_env: str = "development"
    debug: bool = False
    secret_key: str = Field(..., description="Secret key for JWT signing")
    api_v1_prefix: str = "/api/v1"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database — full URL or components (components used when DATABASE_URL is unset)
    database_url: Optional[str] = Field(
        default=None,
        description="PostgreSQL URL; overrides DB_* when set",
    )
    # IPv4 Session Pooler URL (Supabase Connect → Session). Required on AWS EC2 / IPv4-only hosts.
    database_pooler_url: Optional[str] = Field(
        default=None,
        description="When set, used instead of DATABASE_URL for app connections",
    )
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "requi_health"
    db_user: str = "postgres"
    db_password: str = ""
    database_pool_size: int = 20
    database_max_overflow: int = 30
    # Per-process pool budget when using Supabase Session pooler (api | worker | beat).
    database_pool_role: str = "api"

    @model_validator(mode="after")
    def assemble_database_url(self) -> "Settings":
        pooler = (self.database_pooler_url or "").strip()
        url = (self.database_url or "").strip()
        if pooler:
            # EC2 / Docker bridge: Supabase Session pooler (IPv4). Copy from dashboard Connect → Session.
            self.database_url = pooler
        elif not url:
            user = quote_plus(self.db_user)
            password = quote_plus(self.db_password)
            url = (
                f"postgresql://{user}:{password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
            self.database_url = url
        else:
            self.database_url = url
        return self
    
    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_pool_size: int = 50
    
    # Celery
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    celery_worker_concurrency: int = 4
    # When True, document uploads queue `ingest_document_task` (requires a Celery worker).
    # Default False = ingest in the API request (avoids stuck "Indexing…" if no worker consumes the queue).
    document_ingest_use_async_worker: bool = False
    
    # Authentication
    jwt_secret_key: str = Field(..., description="Secret key for JWT tokens")
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24 hours
    refresh_token_expire_days: int = 30

    # SaaS admin portal (separate JWT + seed user)
    platform_admin_jwt_secret_key: Optional[str] = Field(
        default=None,
        description="JWT secret for platform admin tokens; defaults to jwt_secret_key if unset",
    )
    platform_admin_access_token_expire_minutes: int = 480
    platform_admin_seed_email: str = "nuvostudios99@gmail.com"
    platform_admin_seed_password: Optional[str] = Field(
        default=None,
        description="Password for seed platform admin (required to create/update owner)",
    )
    platform_admin_seed_first_name: str = "Angelo"
    platform_admin_seed_last_name: str = "Admin"
    admin_portal_url: str = Field(
        default="http://localhost:5174",
        description="Public URL of the SaaS admin portal (invite emails, CTAs)",
        validation_alias=AliasChoices(
            "admin_portal_url",
            "ADMIN_PORTAL_URL",
            "PLATFORM_ADMIN_PORTAL_URL",
        ),
    )

    @property
    def platform_admin_jwt_secret_effective(self) -> str:
        key = (self.platform_admin_jwt_secret_key or "").strip()
        return key or self.jwt_secret_key

    @property
    def platform_admin_portal_url(self) -> str:
        """Backward-compatible alias for admin_portal_url."""
        return self.admin_portal_url_normalized

    @property
    def admin_portal_url_normalized(self) -> str:
        return (self.admin_portal_url or "").strip().rstrip("/")

    # OpenAI GPT-5.5 (Primary AI Model)
    openai_api_key: str = Field(..., description="OpenAI API key")
    openai_model: str = "gpt-5.5"  # GPT-5.5 series for agents
    compliance_extraction_model: str = "gpt-4o-mini"
    compliance_ai_extraction_enabled: bool = True
    openai_max_tokens: int = 4096
    openai_temperature: float = 0.1  # Low temperature for deterministic outputs
    
    # OpenAI Responses API (prompt-driven Intelligence chat)
    openai_responses_model: str = "gpt-5"
    openai_prompt_id: Optional[str] = None
    openai_prompt_version: Optional[str] = "40"
    openai_vector_store_id: Optional[str] = None

    # OpenAI Realtime API (live voice — Requi Sonia Assistant + Marin)
    openai_voice_prompt_id: Optional[str] = None
    openai_voice_prompt_version: Optional[str] = "7"
    openai_realtime_model: str = "gpt-realtime-1.5"
    openai_realtime_voice: str = "marin"

    # Intelligence chat testing (simulated SSE stream; no OpenAI calls when true)
    mock_chat_stream: bool = False
    mock_chat_stream_delay_ms: int = 50

    # OpenAI Embeddings
    embedding_model: str = "text-embedding-3-small"
    
    # Stripe
    stripe_secret_key: str = Field(..., description="Stripe secret key")
    stripe_publishable_key: str = Field(..., description="Stripe publishable key")
    stripe_webhook_secret: str = Field(..., description="Stripe webhook secret")
    
    # Stripe Price IDs
    stripe_price_standard: str = Field(..., description="Stripe price ID for STANDARD plan")
    stripe_price_pro: str = Field(..., description="Stripe price ID for PRO plan")
    stripe_price_enterprise: str = Field(..., description="Stripe price ID for ENTERPRISE plan")
    stripe_price_enterprise_additional: str = Field(
        default="",
        description="Stripe price ID for Enterprise additional team seats ($1,500/mo). Falls back to PRO price.",
    )

    # Frontend base URL for Stripe Checkout redirects (no trailing slash)
    frontend_url: str = "http://localhost:5173"
    
    # Plan Configuration (in cents) — v2.1 Pricing
    standard_plan_price: int = 50000  # $500/month
    standard_plan_min_seats: int = 1
    standard_plan_max_seats: int = 1
    
    pro_plan_price: int = 150000  # $1,500/month
    pro_plan_min_seats: int = 1
    pro_plan_max_seats: int = 50
    
    enterprise_plan_price: int = 350000  # $3,500/month
    enterprise_additional_seat_price: int = 150000  # $1,500/month per additional Enterprise seat
    enterprise_plan_min_seats: int = 1
    enterprise_plan_max_seats: int = 1000
    
    # Knowledge Pipeline
    chunk_size: int = 1000
    chunk_overlap: int = 200
    # Max characters injected per explicitly selected Intelligence document (library or attachment).
    intelligence_document_context_max_chars: int = 200_000
    # Max characters sent to compliance gap extraction (document upload + Intelligence analysis).
    compliance_analysis_max_chars: int = 200_000
    max_sources_per_query: int = 5
    min_confidence_threshold: float = 0.7
    gap_detection_threshold: float = 0.5
    
    # Daily Update Job
    daily_update_hour: int = 2
    daily_update_minute: int = 0
    knowledge_stale_days: int = 30
    
    # Document Storage — local | s3 | gcs
    document_storage_type: str = "local"
    document_upload_dir: str = "data/document_uploads"
    # AWS S3 (legacy; keep configured for reading migrated objects if needed)
    s3_bucket_name: Optional[str] = None
    s3_region: str = "us-east-1"
    s3_endpoint_url: Optional[str] = None
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    # Google Cloud Storage
    gcs_bucket_name: Optional[str] = None
    gcs_credentials_path: Optional[str] = Field(
        default=None,
        description="Path to GCP service account JSON key file",
    )
    gcs_project_id: Optional[str] = Field(
        default=None,
        description="GCP project ID; inferred from credentials file when unset",
    )
    gcs_public_base_url: Optional[str] = Field(
        default=None,
        description="Optional public base URL for file_url (e.g. https://storage.googleapis.com/bucket)",
    )

    @model_validator(mode="after")
    def align_gcs_settings(self) -> "Settings":
        """Keep public URL in sync with GCS_BUCKET_NAME to avoid stale bucket names in .env."""
        if (self.document_storage_type or "").strip().lower() != "gcs":
            return self
        bucket = (self.gcs_bucket_name or "").strip()
        if not bucket:
            return self
        expected = f"https://storage.googleapis.com/{bucket}"
        base = (self.gcs_public_base_url or "").strip().rstrip("/")
        if not base or not base.endswith(bucket):
            self.gcs_public_base_url = expected
        return self
    
    # Logging
    log_level: str = "INFO"
    log_format: str = "json"
    
    # Rate Limiting
    rate_limit_requests_per_minute: int = 60
    
    # Trial Configuration
    trial_days: int = 7  # Free trial duration
    trial_prompt_limit: int = 3  # AI prompts allowed on trial

    # Trial reminder email (Celery Beat — override via .env: TRIAL_REMINDER_CRON_*)
    trial_reminder_email_enabled: bool = True
    trial_reminder_days_before_end: int = 2
    trial_reminder_cron_hour: int = 9
    trial_reminder_cron_minute: int = 0
    trial_reminder_timezone: str = "America/Los_Angeles"

    # Task due-date reminders (Celery Beat + optional startup run)
    task_reminder_enabled: bool = True
    task_reminder_email_enabled: bool = True
    task_reminder_days_before: int = 7
    task_reminder_cron_hour: int = 8
    task_reminder_cron_minute: int = 0

    # SMTP (transactional email — welcome, reminders, etc.)
    smtp_server: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from_email: Optional[str] = None
    smtp_sender_name: str = "Requi Health"

    # CORS — comma-separated origins, or set CORS_ORIGINS=* / CORS_ALLOW_ALL=true for any frontend
    cors_origins: str = ""
    cors_allow_all: bool = False

    @property
    def cors_origins_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if not raw or raw == "*":
            return []
        return [
            origin.strip().rstrip("/")
            for origin in raw.split(",")
            if origin.strip() and origin.strip() != "*"
        ]

    @property
    def cors_allow_all_enabled(self) -> bool:
        """True when any browser origin may call the API (server .env: CORS_ALLOW_ALL=true)."""
        if self.cors_allow_all:
            return True
        return self.cors_origins.strip() == "*"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"
    
    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def smtp_enabled(self) -> bool:
        return bool(
            (self.smtp_server or "").strip()
            and (self.smtp_user or "").strip()
            and (self.smtp_password or "").strip()
        )

    @model_validator(mode="after")
    def _smtp_from_defaults(self) -> "Settings":
        if not (self.smtp_from_email or "").strip() and (self.smtp_user or "").strip():
            self.smtp_from_email = self.smtp_user.strip()
        return self

    @model_validator(mode="after")
    def _legacy_document_ingest_sync_env(self) -> "Settings":
        """DOCUMENT_INGEST_SYNC=true (inline) / false (Celery) — deprecated; prefer DOCUMENT_INGEST_USE_ASYNC_WORKER."""
        import os

        raw = os.getenv("DOCUMENT_INGEST_SYNC")
        if raw is None or not str(raw).strip():
            return self
        sync_inline = str(raw).strip().lower() in ("1", "true", "yes", "on")
        self.document_ingest_use_async_worker = not sync_inline
        return self
    
    def get_plan_price(self, plan_type: str) -> int:
        """Get price in cents for a plan type"""
        prices = {
            "standard": self.standard_plan_price,
            "pro": self.pro_plan_price,
            "enterprise": self.enterprise_plan_price,
        }
        return prices.get(plan_type.lower(), 0)

    def get_enterprise_additional_price_id(self) -> str:
        """Stripe price for Enterprise additional team seats ($1,500/mo per user)."""
        if self.stripe_price_enterprise_additional.strip():
            return self.stripe_price_enterprise_additional.strip()
        return self.stripe_price_pro
    
    def get_plan_limits(self, plan_type: str) -> dict:
        """Get seat limits for a plan type"""
        limits = {
            "standard": {"min": self.standard_plan_min_seats, "max": self.standard_plan_max_seats},
            "pro": {"min": self.pro_plan_min_seats, "max": self.pro_plan_max_seats},
            "enterprise": {"min": self.enterprise_plan_min_seats, "max": self.enterprise_plan_max_seats},
        }
        return limits.get(plan_type.lower(), {"min": 1, "max": 1})


# Global settings instance
settings = Settings()
