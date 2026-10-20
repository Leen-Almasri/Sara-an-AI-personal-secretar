"""Application configuration loaded from environment variables.

All secrets (API keys, OAuth credentials, tokens) MUST be supplied via
environment variables or a local .env file. Nothing sensitive is
hard-coded here or anywhere else in the codebase.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            "Copy .env.example to .env and fill in the values, "
            "or set it in your deployment's secret store."
        )
    return value


@dataclass(frozen=True)
class Settings:
    # Gemini
    gemini_api_key: str
    gemini_model: str

    # Google Calendar OAuth
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    google_calendar_id: str

    # Scheduling behavior
    timezone: str
    work_start_hour: int
    work_end_hour: int
    default_event_duration_minutes: int


def _validate_scheduling(loaded: Settings) -> None:
    try:
        ZoneInfo(loaded.timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise RuntimeError(f"TIMEZONE '{loaded.timezone}' is not a valid IANA timezone name.") from None
    if not 0 <= loaded.work_start_hour < loaded.work_end_hour <= 24:
        raise RuntimeError("WORK_START_HOUR and WORK_END_HOUR must satisfy 0 <= start < end <= 24.")
    if loaded.default_event_duration_minutes <= 0:
        raise RuntimeError("DEFAULT_EVENT_DURATION_MINUTES must be positive.")


def load_settings() -> Settings:
    loaded = Settings(
        gemini_api_key=_require("GEMINI_API_KEY"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        google_client_id=_require("GOOGLE_CLIENT_ID"),
        google_client_secret=_require("GOOGLE_CLIENT_SECRET"),
        google_refresh_token=_require("GOOGLE_REFRESH_TOKEN"),
        google_calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
        timezone=os.getenv("TIMEZONE", "Asia/Riyadh"),
        work_start_hour=int(os.getenv("WORK_START_HOUR", "9")),
        work_end_hour=int(os.getenv("WORK_END_HOUR", "21")),
        default_event_duration_minutes=int(os.getenv("DEFAULT_EVENT_DURATION_MINUTES", "30")),
    )
    _validate_scheduling(loaded)
    return loaded


settings = load_settings()
