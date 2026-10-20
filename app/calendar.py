"""Google Calendar integration.

This module talks to a single personal Google Calendar (the owner's) using an
OAuth2 refresh token. It never exposes raw credentials to the agent/LLM -
the LLM only ever calls the high-level tool functions defined in agent.py,
which in turn call the functions below.

Failures (bad input, Google API errors, missing data) are raised as
CalendarError. Its `code` and `message` are safe to hand to the agent: they
never contain credentials, tokens, or private event details.

To obtain GOOGLE_REFRESH_TOKEN, run scripts/get_refresh_token.py once
locally (see README.md).
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable
from zoneinfo import ZoneInfo

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

MIN_DURATION_MINUTES = 5
MAX_DURATION_MINUTES = 480
MAX_TITLE_LENGTH = 100
DEFAULT_EVENT_TITLE = "Booked via Sara"

MAX_ALTERNATIVES = 3
ALTERNATIVE_STEP_MINUTES = 30
ALTERNATIVE_LOOKAHEAD_DAYS = 7
# Suggested slots must start at least this far in the future so they are still
# valid by the time the friend confirms.
ALTERNATIVE_MIN_NOTICE_MINUTES = 15

# Read-only calls are safe to retry. Event creation is not (a retry after a
# lost response could create a duplicate), so insert() is never retried.
READ_RETRIES = 2

_UTC = dt.timezone.utc


class CalendarError(Exception):
    """A failure with a stable machine-readable `code` and a safe `message`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Slot:
    start: dt.datetime  # timezone-aware, in the configured timezone
    end: dt.datetime
    duration_minutes: int


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------


def _zone() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def _now() -> dt.datetime:
    return dt.datetime.now(_zone())


def _add_minutes(moment: dt.datetime, minutes: int) -> dt.datetime:
    """Add real elapsed minutes (correct across DST changes) and keep the local zone."""
    return (moment.astimezone(_UTC) + dt.timedelta(minutes=minutes)).astimezone(_zone())


def _local_datetime(day: dt.date, hour: int = 0, minute: int = 0) -> dt.datetime:
    """Local wall-clock time on `day`; hour 24 means midnight at the end of the day."""
    if hour == 24:
        day, hour = day + dt.timedelta(days=1), 0
    naive = dt.datetime.combine(day, dt.time(hour, minute))
    # Normalising through UTC moves a non-existent DST time to a real instant.
    return naive.replace(tzinfo=_zone()).astimezone(_UTC).astimezone(_zone())


def _coerce_duration(value: Any) -> int:
    if isinstance(value, bool):
        raise CalendarError("invalid_duration", "Duration must be a whole number of minutes.")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise CalendarError("invalid_duration", "Duration must be a whole number of minutes.")
    if not MIN_DURATION_MINUTES <= value <= MAX_DURATION_MINUTES:
        raise CalendarError(
            "invalid_duration",
            f"Duration must be between {MIN_DURATION_MINUTES} and {MAX_DURATION_MINUTES} minutes.",
        )
    return value


def _parse_slot(date: Any, start_time: Any, duration_minutes: Any) -> Slot:
    """Validate raw tool arguments and return an aware slot in the configured timezone."""
    if not isinstance(date, str) or not isinstance(start_time, str):
        raise CalendarError("invalid_date", "Date and start time must be text (YYYY-MM-DD and HH:MM).")
    try:
        day = dt.datetime.strptime(date.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise CalendarError("invalid_date", "Date must be a real calendar date in YYYY-MM-DD format.") from None
    try:
        clock = dt.datetime.strptime(start_time.strip(), "%H:%M").time()
    except ValueError:
        raise CalendarError("invalid_time", "Start time must be a valid 24-hour time in HH:MM format.") from None
    minutes = _coerce_duration(duration_minutes)

    naive = dt.datetime.combine(day, clock)
    try:
        start = naive.replace(tzinfo=_zone()).astimezone(_UTC).astimezone(_zone())
        end = _add_minutes(start, minutes)
    except (OverflowError, ValueError):
        raise CalendarError("invalid_date", "Date is out of the supported range.") from None
    if start.replace(tzinfo=None) != naive:
        raise CalendarError("invalid_time", "That local time does not exist because of a daylight-saving change.")
    return Slot(start=start, end=end, duration_minutes=minutes)


def normalize_slot(date: Any, start_time: Any, duration_minutes: Any) -> tuple[str, str, int]:
    """Validate a slot and return it in canonical form ("YYYY-MM-DD", "HH:MM", minutes)."""
    slot = _parse_slot(date, start_time, duration_minutes)
    return slot.start.strftime("%Y-%m-%d"), slot.start.strftime("%H:%M"), slot.duration_minutes


def _clean_title(title: Any) -> str:
    cleaned = " ".join(str(title or "").split())[:MAX_TITLE_LENGTH].strip()
    return cleaned or DEFAULT_EVENT_TITLE


def _conflicts(start: dt.datetime, end: dt.datetime, busy: list[tuple[dt.datetime, dt.datetime]]) -> bool:
    return any(start < busy_end and busy_start < end for busy_start, busy_end in busy)


# --------------------------------------------------------------------------
# Google API access
# --------------------------------------------------------------------------

_credentials: Credentials | None = None
_credentials_lock = threading.Lock()
# Serialises "re-check then insert" so two chats in this process cannot book the same slot.
_booking_lock = threading.Lock()


def _get_credentials() -> Credentials:
    """Return refresh-token credentials, refreshing the access token only when needed."""
    global _credentials
    with _credentials_lock:
        if _credentials is None:
            _credentials = Credentials(
                token=None,
                refresh_token=settings.google_refresh_token,
                token_uri=TOKEN_URI,
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
                scopes=SCOPES,
            )
        if not _credentials.valid:
            _credentials.refresh(Request())
        return _credentials


def _get_service():
    # A new service object per call: the underlying HTTP client is not thread-safe.
    return build("calendar", "v3", credentials=_get_credentials(), cache_discovery=False)


def _http_error_to_calendar_error(exc: HttpError) -> CalendarError:
    status = getattr(exc.resp, "status", None)
    reason = str(getattr(exc, "reason", "") or "").lower()
    logger.error("Google Calendar API error (HTTP %s)", status)  # never log the body or URL
    if status in (401,):
        return CalendarError("calendar_auth_failed", "The calendar connection needs to be re-authorized by the calendar owner.")
    if status == 429 or (status == 403 and ("rate" in reason or "quota" in reason)):
        return CalendarError("calendar_rate_limited", "The calendar service is busy. Please try again in a moment.")
    if status == 403:
        return CalendarError("calendar_permission_denied", "Sara does not have permission to use the owner's calendar.")
    if status == 404:
        return CalendarError("calendar_not_found", "The calendar could not be found. The owner needs to check the setup.")
    if isinstance(status, int) and status >= 500:
        return CalendarError("calendar_unavailable", "The calendar service is temporarily unavailable.")
    return CalendarError("calendar_request_failed", "The calendar request could not be completed.")


def _execute(build_request: Callable[[Any], Any], *, retries: int = 0) -> dict:
    """Run one Google API request, translating every failure into CalendarError."""
    try:
        service = _get_service()
        return build_request(service).execute(num_retries=retries)
    except CalendarError:
        raise
    except HttpError as exc:
        raise _http_error_to_calendar_error(exc) from None
    except RefreshError as exc:
        # Log only Google's short error code (e.g. "invalid_grant"), never the response body.
        detail = exc.args[1].get("error") if len(exc.args) > 1 and isinstance(exc.args[1], dict) else None
        logger.error("Google token refresh failed (%s)", detail or type(exc).__name__)
        raise CalendarError(
            "calendar_auth_failed", "The calendar connection needs to be re-authorized by the calendar owner."
        ) from None
    except Exception as exc:
        logger.error("Unexpected error talking to Google Calendar (%s)", type(exc).__name__)
        raise CalendarError("calendar_unavailable", "The calendar service could not be reached.") from None


def _parse_google_datetime(value: Any) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive datetime")
    return parsed.astimezone(_zone())


def _fetch_busy(time_min: dt.datetime, time_max: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    """Return busy (start, end) periods in [time_min, time_max). Fails closed on any bad data."""
    calendar_id = settings.google_calendar_id
    body = {
        "timeMin": time_min.astimezone(_UTC).isoformat(),
        "timeMax": time_max.astimezone(_UTC).isoformat(),
        "timeZone": settings.timezone,
        "items": [{"id": calendar_id}],
    }
    response = _execute(lambda service: service.freebusy().query(body=body), retries=READ_RETRIES)

    calendars = response.get("calendars") if isinstance(response, dict) else None
    info = calendars.get(calendar_id) if isinstance(calendars, dict) else None
    if info is None and isinstance(calendars, dict) and len(calendars) == 1:
        # "primary" may be echoed back under the calendar's real id.
        info = next(iter(calendars.values()))
    if not isinstance(info, dict):
        logger.error("Google freebusy response had no data for the configured calendar")
        raise CalendarError("bad_calendar_response", "The calendar returned incomplete data.")

    errors = info.get("errors")
    if errors:
        reasons = [str(error.get("reason")) for error in errors if isinstance(error, dict)]
        logger.error("Google freebusy returned calendar errors: %s", reasons)
        if "notFound" in reasons:
            raise CalendarError("calendar_not_found", "The calendar could not be found. The owner needs to check the setup.")
        raise CalendarError("calendar_unavailable", "The calendar service is temporarily unavailable.")

    busy: list[tuple[dt.datetime, dt.datetime]] = []
    try:
        for period in info.get("busy", []):
            busy.append((_parse_google_datetime(period["start"]), _parse_google_datetime(period["end"])))
    except (KeyError, TypeError, ValueError):
        logger.error("Google freebusy response contained an unreadable busy period")
        raise CalendarError("bad_calendar_response", "The calendar returned incomplete data.") from None
    return busy


# --------------------------------------------------------------------------
# Public API used by agent.py
# --------------------------------------------------------------------------


def _suggest_alternatives(
    first_day: dt.date,
    duration_minutes: int,
    busy: list[tuple[dt.datetime, dt.datetime]],
    now: dt.datetime,
) -> list[dict]:
    """Next free slots inside working hours, in the future, never overlapping a busy period."""
    earliest_start = _add_minutes(now, ALTERNATIVE_MIN_NOTICE_MINUTES)
    suggestions: list[dict] = []
    for offset in range(ALTERNATIVE_LOOKAHEAD_DAYS):
        day = first_day + dt.timedelta(days=offset)
        opens = _local_datetime(day, settings.work_start_hour)
        closes = _local_datetime(day, settings.work_end_hour)
        slot_start = opens
        while len(suggestions) < MAX_ALTERNATIVES:
            slot_end = _add_minutes(slot_start, duration_minutes)
            if slot_end > closes:
                break
            if slot_start >= earliest_start and not _conflicts(slot_start, slot_end, busy):
                suggestions.append(
                    {
                        "date": slot_start.strftime("%Y-%m-%d"),
                        "start_time": slot_start.strftime("%H:%M"),
                        "duration_minutes": duration_minutes,
                    }
                )
            slot_start = _add_minutes(slot_start, ALTERNATIVE_STEP_MINUTES)
        if len(suggestions) >= MAX_ALTERNATIVES:
            break
    return suggestions


def check_availability(date: str, start_time: str, duration_minutes: int) -> dict:
    """Check whether the requested slot is free, and suggest alternatives if not.

    Returns only non-private information:
      {"available": bool,
       "alternatives": [{"date": "YYYY-MM-DD", "start_time": "HH:MM", "duration_minutes": int}, ...]}
    A request in the past is reported as unavailable with an extra "error": "time_in_past".
    Raises CalendarError for invalid input or calendar/API failures.
    """
    slot = _parse_slot(date, start_time, duration_minutes)
    now = _now()

    first_day = max(slot.start.date(), now.date())
    window_start = _local_datetime(first_day)
    window_end = _local_datetime(first_day + dt.timedelta(days=ALTERNATIVE_LOOKAHEAD_DAYS))
    busy = _fetch_busy(window_start, window_end)

    result: dict = {"available": False}
    if slot.start <= now:
        result["error"] = "time_in_past"
        result["message"] = "That time has already passed."
    elif not _conflicts(slot.start, slot.end, busy):
        return {"available": True, "alternatives": []}

    result["alternatives"] = _suggest_alternatives(first_day, slot.duration_minutes, busy, now)
    return result


def create_event(date: str, start_time: str, duration_minutes: int, title: str) -> dict:
    """Create an event on the owner's calendar. Returns {"success": True, "event_id": str | None}.

    The slot is re-verified against the live calendar immediately before inserting. This
    narrows, but cannot fully close, the window for a conflicting booking: Google Calendar
    has no atomic "insert if free", so an event added by another client between the check
    and the insert can still overlap. Raises CalendarError on any failure.
    """
    slot = _parse_slot(date, start_time, duration_minutes)
    if slot.start <= _now():
        raise CalendarError("time_in_past", "That time has already passed, so it cannot be booked.")

    event_body = {
        "summary": _clean_title(title),
        "start": {"dateTime": slot.start.isoformat(), "timeZone": settings.timezone},
        "end": {"dateTime": slot.end.isoformat(), "timeZone": settings.timezone},
    }

    with _booking_lock:
        if _conflicts(slot.start, slot.end, _fetch_busy(slot.start, slot.end)):
            raise CalendarError("slot_unavailable", "That time is no longer available.")
        created = _execute(
            lambda service: service.events().insert(calendarId=settings.google_calendar_id, body=event_body),
            retries=0,
        )

    return {"success": True, "event_id": created.get("id") if isinstance(created, dict) else None}
