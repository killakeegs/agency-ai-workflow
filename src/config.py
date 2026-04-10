from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Anthropic ──────────────────────────────────────────────────────────────
    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-4-6"

    # ── Notion ─────────────────────────────────────────────────────────────────
    notion_api_key: str
    notion_workspace_root_page_id: str

    # ── ClickUp ────────────────────────────────────────────────────────────────
    clickup_api_key: str
    clickup_workspace_id: str
    clickup_default_space_id: str | None = None

    # ── Replicate (image generation) ──────────────────────────────────────────
    replicate_api_key: str | None = None

    # ── Pexels (stock photography for client image libraries) ────────────────
    pexels_api_key: str | None = None

    # ── Unsplash (stock photos for lifestyle/people categories) ───────────────
    unsplash_access_key: str | None = None

    # ── Google ────────────────────────────────────────────────────────────────
    google_api_key: str | None = None
    google_service_account_json_path: str | None = None

    # ── Notifications ─────────────────────────────────────────────────────────
    slack_bot_token: str | None = None
    slack_channel_internal: str = "#agency-pipeline"
    gmail_sender_address: str | None = None

    # ── Application ───────────────────────────────────────────────────────────
    app_env: str = "development"
    log_level: str = "INFO"

    @field_validator("anthropic_api_key")
    @classmethod
    def validate_anthropic_key(cls, v: str) -> str:
        if not v.startswith("sk-ant-"):
            raise ValueError("ANTHROPIC_API_KEY must start with 'sk-ant-'")
        return v

    @field_validator("notion_api_key")
    @classmethod
    def validate_notion_key(cls, v: str) -> str:
        if not (v.startswith("secret_") or v.startswith("ntn_")):
            raise ValueError("NOTION_API_KEY must start with 'secret_' or 'ntn_'")
        return v

    @field_validator("clickup_api_key")
    @classmethod
    def validate_clickup_key(cls, v: str) -> str:
        if not v.startswith("pk_"):
            raise ValueError("CLICKUP_API_KEY must start with 'pk_'")
        return v


# Singleton — import this everywhere
settings = Settings()
