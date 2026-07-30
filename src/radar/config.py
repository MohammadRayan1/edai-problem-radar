from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tavily_api_key: str
    anthropic_api_key: str
    perplexity_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    elevenlabs_api_key: str
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"  # George - Warm, Captivating Storyteller
    elevenlabs_model_id: str = "eleven_multilingual_v2"

    pexels_api_key: str | None = None

    font_path: str | None = None

    review_password: str = "changeme"
    review_session_secret: str = "dev-only-insecure-secret-change-in-env"
    review_web_port: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()
