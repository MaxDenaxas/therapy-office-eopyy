"""Environment-based configuration. Every variable is prefixed THERAPY_."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("PROGRAMDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "TherapyOffice"
    return Path.home() / ".local" / "share" / "therapy-office"


def _inside_git_repository(path: Path) -> bool:
    return any((p / ".git").exists() for p in [path, *path.parents])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="THERAPY_", env_file=".env", extra="ignore")

    environment: Literal["development", "production"] = "development"
    data_dir: Path = Field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    frontend_dist: Path | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # LibreOffice for DOCX -> PDF. None = look in PATH and the standard install folders.
    soffice_path: Path | None = None
    pdf_timeout_seconds: int = Field(default=120, ge=5, le=900)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "therapy.db"

    @field_validator("data_dir")
    @classmethod
    def data_dir_outside_repository(cls, value: Path) -> Path:
        value = value.expanduser().resolve()
        if _inside_git_repository(value):
            raise ValueError(
                f"data_dir {value} is inside a Git repository; patient data and templates "
                "must be stored outside it (set THERAPY_DATA_DIR)."
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
