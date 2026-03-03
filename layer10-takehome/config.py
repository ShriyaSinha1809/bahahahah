"""
Shared application configuration.

Uses pydantic-settings to load from .env files and environment variables.
Every module imports settings from here — single source of truth.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide configuration loaded from env vars / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://layer10:layer10pass@localhost:5432/layer10"
    database_url_sync: str = "postgresql://layer10:layer10pass@localhost:5432/layer10"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ── LLM — Groq (primary) ─────────────────────────────
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # ── LLM — Google (fallback) ───────────────────────────
    google_api_key: str = ""
    google_model: str = "gemini-2.5-flash"

    # ── Extraction ────────────────────────────────────────
    extraction_batch_size: int = 10
    extraction_max_retries: int = 3
    extraction_min_confidence: float = 0.4
    extraction_rate_limit_rpm: int = 80

    # ── Embeddings ────────────────────────────────────────
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimension: int = 384

    # ── Enron Dataset ─────────────────────────────────────
    enron_data_dir: str = "./data/enron_raw/maildir"
    enron_subset_users: str = (
        "allen-p,bass-e,beck-s,campbell-l,dasovich-j,"
        "farmer-d,germany-c,kaminski-v,lay-k,skilling-j"
    )

    # ── API ───────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ── Logging ───────────────────────────────────────────
    log_level: str = "INFO"

    # ── Derived helpers ───────────────────────────────────
    @property
    def enron_user_list(self) -> list[str]:
        return [u.strip() for u in self.enron_subset_users.split(",") if u.strip()]

    @property
    def enron_path(self) -> Path:
        return Path(self.enron_data_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton accessor — cached after first call."""
    return Settings()
