"""Centralized config loaded from environment variables (or .env in dev)."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
# --- LLM ------------------------------------------------------
    # --- LLM ------------------------------------------------------
    anthropic_api_key: str = ""
    extraction_model: str = "claude-haiku-4-5-20251001"
    synthesis_model: str = "claude-opus-4-7"
    # --- Search ---------------------------------------------------
    brave_search_api_key: str = ""

    # --- Database -------------------------------------------------
    database_url: str = "sqlite:///./data/sector_intel.db"

    # --- Storage --------------------------------------------------
    raw_docs_dir: Path = Path("./data/raw")
    parsed_docs_dir: Path = Path("./data/parsed")

    def ensure_dirs(self) -> None:
        """Create data directories if missing. Safe to call multiple times."""
        self.raw_docs_dir.mkdir(parents=True, exist_ok=True)
        self.parsed_docs_dir.mkdir(parents=True, exist_ok=True)
        # Also ensure SQLite's parent dir exists for file: URLs
        if self.database_url.startswith("sqlite:///") and not self.database_url.startswith("sqlite:////"):
            db_path = Path(self.database_url.replace("sqlite:///", "", 1))
            db_path.parent.mkdir(parents=True, exist_ok=True)

    sec_user_agent: str = ""
        # ... other fields ...

        # This is the V2 way:
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()