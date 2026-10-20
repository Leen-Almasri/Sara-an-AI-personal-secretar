"""Tests for app.calendar. Every Google API call is mocked - no real calendar is touched."""

import datetime as dt
import json
import logging
from dataclasses import replace
from unittest.mock import MagicMock, PropertyMock, patch
from zoneinfo import ZoneInfo

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from app import calendar as cal
from app.config import settings

ZONE = ZoneInfo("Asia/Amman")  # UTC+3, no DST (matches conftest TIMEZONE)
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 12, 10, 0, tzinfo=ZONE)
TOMORROW = "2026-10-13"


def at(day: str, hm: str, zone: ZoneInfo = ZONE) -> dt.datetime:
    return dt.datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=zone)


def busy_period(start: dt.datetime, end: dt.datetime) -> dict:
    fmt = lambda moment: moment.astimezone(UTC).isoformat().replace("+00:00", "Z")  # noqa: E731
    return {"start": fmt(start), "end": fmt(end)}


def make_service(busy=(), response=None) -> MagicMock:
    service = MagicMock()
    payload = response if response is not None else {"calendars": {"primary": {"busy": [busy_period(*b) for b in busy]}}}
    service.freebusy.return_value.query.return_value.execute.return_value = payload
    service.events.return_value.insert.return_value.execute.return_value = {"id": "evt-1"}
    return service


def http_error(status: int, message: str = "error") -> HttpError:
    content = json.dumps({"error": {"code": status, "message": message}}).encode()
    return HttpError(httplib2.Response({"status": status}), content)


@pytest.fixture(autouse=True)
def fixed_now():
    with patch("app.calendar._now", return_value=NOW):
        yield


def use(service: MagicMock):
    return patch("app.calendar._get_service", return_value=service)


def assert_valid_alternatives(alternatives, busy, duration, now=NOW):
    for alt in alternatives:
        assert set(alt) == {"date", "start_time", "duration_minutes"}
        assert alt["duration_minutes"] == duration
        start = at(alt["date"], alt["start_time"])
        end = start + dt.timedelta(minutes=duration)
        assert start >= now + dt.timedelta(minutes=cal.ALTERNATIVE_MIN_NOTICE_MINUTES)
        assert at(alt["date"], "09:00") <= start
        assert end <= at(alt["date"], "21:00")
        assert not any(start < b_end and b_start < end for b_start, b_end in busy)


# ------------------------------------------------------------------ availability


def test_available_when_calendar_is_empty():
    with use(make_service()):
        assert cal.check_availability(TOMORROW, "14:00", 30) == {"available": True, "alternatives": []}


def test_overlapping_event_is_unavailable_with_valid_alternatives():
    busy = [(at(TOMORROW, "14:00"), at(TOMORROW, "15:00"))]
    with use(make_service(busy)):
        result = cal.check_availability(TOMORROW, "14:30", 30)

    assert result["available"] is False
    assert [(a["date"], a["start_time"]) for a in result["alternatives"]] == [
        (TOMORROW, "09:00"),
        (TOMORROW, "09:30"),
        (TOMORROW, "10:00"),
    ]
    assert_valid_alternatives(result["alternatives"], busy, 30)
    assert set(result) == {"available", "alternatives"}  # nothing private or extra


@pytest.mark.parametrize(
    "busy_start, busy_end, expected_available",
    [
        ("14:00", "14:30", True),  # busy ends exactly when the slot starts
        ("15:00", "16:00", True),  # busy starts exactly when the slot ends
        ("14:45", "15:30", False),  # overlaps the tail of the slot
        ("13:50", "14:40", False),  # overlaps the head of the slot
        ("14:35", "14:50", False),  # fully inside the slot
        ("13:00", "16:00", False),  # fully covers the slot
    ],
)
def test_overlap_boundaries(busy_start, busy_end, expected_available):
    with use(make_service([(at(TOMORROW, busy_start), at(TOMORROW, busy_end))])):
        assert cal.check_availability(TOMORROW, "14:30", 30)["available"] is expected_available


def test_past_time_today_is_unavailable_and_alternatives_are_in_the_future():
    with use(make_service()):
        result = cal.check_availability("2026-10-12", "09:00", 30)

    assert result["available"] is False
    assert result["error"] == "time_in_past"
    assert result["alternatives"][0] == {"date": "2026-10-12", "start_time": "10:30", "duration_minutes": 30}
    assert_valid_alternatives(result["alternatives"], [], 30)


def test_past_date_never_reports_available_and_only_queries_from_today():
    service = make_service()
    with use(service):
        result = cal.check_availability("2020-01-01", "10:00", 30)

    assert result["available"] is False and result["error"] == "time_in_past"
    assert all(a["date"] >= "2026-10-12" for a in result["alternatives"])
    query_body = service.freebusy.return_value.query.call_args.kwargs["body"]
    assert query_body["timeMin"] == "2026-10-11T21:00:00+00:00"  # local midnight today, not 2020


@pytest.mark.parametrize(
    "date, start_time, duration, code",
    [
        ("2026-02-30", "10:00", 30, "invalid_date"),
        ("tomorrow", "10:00", 30, "invalid_date"),
        ("", "10:00", 30, "invalid_date"),
        (None, "10:00", 30, "invalid_date"),
        ("9999-12-31", "23:30", 30, "invalid_date"),  # end of slot overflows the supported range
        (TOMORROW, "25:00", 30, "invalid_time"),
        (TOMORROW, "9pm", 30, "invalid_time"),
        (TOMORROW, "", 30, "invalid_time"),
        (TOMORROW, "10:00", 0, "invalid_duration"),
        (TOMORROW, "10:00", -30, "invalid_duration"),
        (TOMORROW, "10:00", 2, "invalid_duration"),
        (TOMORROW, "10:00", 100000, "invalid_duration"),
        (TOMORROW, "10:00", "thirty", "invalid_duration"),
        (TOMORROW, "10:00", 30.5, "invalid_duration"),
        (TOMORROW, "10:00", True, "invalid_duration"),
        (TOMORROW, "10:00", None, "invalid_duration"),
    ],
)
def test_invalid_input_is_rejected_before_touching_google(date, start_time, duration, code):
    with patch("app.calendar._get_service") as get_service:
        with pytest.raises(cal.CalendarError) as exc_info:
            cal.check_availability(date, start_time, duration)
        with pytest.raises(cal.CalendarError):
            cal.create_event(date, start_time, duration, "Call")

    assert exc_info.value.code == code
    get_service.assert_not_called()


def test_normalize_slot_canonicalises_and_accepts_integral_floats():
    assert cal.normalize_slot(" 2026-10-13 ", "9:05", 30.0) == ("2026-10-13", "09:05", 30)


# ------------------------------------------------------------------ alternatives


def test_alternatives_never_extend_past_closing_time():
    day_busy = [(at(TOMORROW, "09:00"), at(TOMORROW, "20:30"))]

    with use(make_service(day_busy)):
        thirty = cal.check_availability(TOMORROW, "10:00", 30)
    # 20:30-21:00 fits exactly inside working hours.
    assert thirty["alternatives"][0] == {"date": TOMORROW, "start_time": "20:30", "duration_minutes": 30}
    assert_valid_alternatives(thirty["alternatives"], day_busy, 30)

    with use(make_service(day_busy)):
        sixty = cal.check_availability(TOMORROW, "10:00", 60)
    # A 60 minute meeting from 20:30 would end at 21:30, so it must roll over to the next morning.
    assert [(a["date"], a["start_time"]) for a in sixty["alternatives"]] == [
        ("2026-10-14", "09:00"),
        ("2026-10-14", "09:30"),
        ("2026-10-14", "10:00"),
    ]
    assert_valid_alternatives(sixty["alternatives"], day_busy, 60)


def test_alternatives_skip_a_fully_booked_day():
    busy = [(at(TOMORROW, "00:00"), at("2026-10-14", "00:00"))]
    with use(make_service(busy)):
        result = cal.check_availability(TOMORROW, "10:00", 30)

    assert {a["date"] for a in result["alternatives"]} == {"2026-10-14"}
    assert_valid_alternatives(result["alternatives"], busy, 30)


def test_no_alternatives_when_the_whole_lookahead_is_busy():
    busy = [(at("2026-10-12", "00:00"), at("2026-10-20", "00:00"))]
    with use(make_service(busy)):
        assert cal.check_availability(TOMORROW, "10:00", 30) == {"available": False, "alternatives": []}


def test_alternatives_do_not_overlap_any_busy_period():
    busy = [
        (at(TOMORROW, "09:00"), at(TOMORROW, "09:45")),
        (at(TOMORROW, "10:10"), at(TOMORROW, "11:00")),
        (at(TOMORROW, "12:00"), at(TOMORROW, "13:00")),
    ]
    with use(make_service(busy)):
        result = cal.check_availability(TOMORROW, "12:15", 45)

    assert result["available"] is False and len(result["alternatives"]) == 3
    assert_valid_alternatives(result["alternatives"], busy, 45)


# ------------------------------------------------------------------ missing data & API failures


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"calendars": {}},
        {"calendars": {"other": {"busy": []}, "another": {"busy": []}}},
        {"calendars": {"primary": None}},
        {"calendars": {"primary": {"busy": [{"start": "garbage", "end": "garbage"}]}}},
        {"calendars": {"primary": {"busy": [{"start": "2026-10-13T11:00:00Z"}]}}},
        {"calendars": {"primary": {"busy": [{"start": "2026-10-13T11:00:00", "end": "2026-10-13T12:00:00"}]}}},
    ],
)
def test_missing_or_unreadable_calendar_data_fails_closed(response):
    with use(make_service(response=response)):
        with pytest.raises(cal.CalendarError) as exc_info:
            cal.check_availability(TOMORROW, "14:00", 30)
    assert exc_info.value.code == "bad_calendar_response"


def test_calendar_not_found_error_in_freebusy_response():
    response = {"calendars": {"primary": {"errors": [{"domain": "global", "reason": "notFound"}]}}}
    with use(make_service(response=response)):
        with pytest.raises(cal.CalendarError) as exc_info:
            cal.check_availability(TOMORROW, "14:00", 30)
    assert exc_info.value.code == "calendar_not_found"


def test_primary_calendar_echoed_under_its_real_id_is_accepted():
    response = {"calendars": {"owner@example.com": {"busy": []}}}
    with use(make_service(response=response)):
        assert cal.check_availability(TOMORROW, "14:00", 30)["available"] is True


@pytest.mark.parametrize(
    "status, message, code",
    [
        (401, "Invalid Credentials", "calendar_auth_failed"),
        (403, "Rate Limit Exceeded", "calendar_rate_limited"),
        (403, "Forbidden", "calendar_permission_denied"),
        (404, "Not Found", "calendar_not_found"),
        (429, "Too Many Requests", "calendar_rate_limited"),
        (500, "Backend Error", "calendar_unavailable"),
        (400, "Bad Request", "calendar_request_failed"),
    ],
)
def test_google_http_errors_are_mapped_to_safe_errors(status, message, code):
    service = make_service()
    service.freebusy.return_value.query.return_value.execute.side_effect = http_error(status, message)
    with use(service):
        with pytest.raises(cal.CalendarError) as exc_info:
            cal.check_availability(TOMORROW, "14:00", 30)

    assert exc_info.value.code == code
    assert "googleapis" not in exc_info.value.message and "HttpError" not in exc_info.value.message


def test_read_requests_are_retried_by_the_google_client():
    service = make_service()
    with use(service):
        cal.check_availability(TOMORROW, "14:00", 30)
    service.freebusy.return_value.query.return_value.execute.assert_called_once_with(num_retries=cal.READ_RETRIES)


def test_network_failure_is_reported_as_unavailable():
    service = make_service()
    service.freebusy.return_value.query.return_value.execute.side_effect = OSError("connection reset")
    with use(service):
        with pytest.raises(cal.CalendarError) as exc_info:
            cal.check_availability(TOMORROW, "14:00", 30)
    assert exc_info.value.code == "calendar_unavailable"
    assert "connection reset" not in exc_info.value.message


def test_refresh_failure_never_leaks_credentials(caplog):
    refresh_error = RefreshError(
        "invalid_grant: Token has been expired or revoked.",
        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
    )
    with caplog.at_level(logging.DEBUG), patch("app.calendar._get_service", side_effect=refresh_error):
        with pytest.raises(cal.CalendarError) as exc_info:
            cal.check_availability(TOMORROW, "14:00", 30)

    assert exc_info.value.code == "calendar_auth_failed"
    for secret in (settings.google_refresh_token, settings.google_client_secret, settings.google_client_id):
        assert secret not in exc_info.value.message
        assert secret not in caplog.text
    assert "invalid_grant" in caplog.text  # still diagnosable


# ------------------------------------------------------------------ credentials


def test_credentials_are_built_once_and_refreshed_only_when_invalid():
    fake_creds = MagicMock()
    type(fake_creds).valid = PropertyMock(side_effect=[False, True, True])
    with patch("app.calendar._credentials", None), patch("app.calendar.Credentials", return_value=fake_creds) as ctor, patch(
        "app.calendar.Request"
    ):
        assert cal._get_credentials() is fake_creds
        assert cal._get_credentials() is fake_creds

    ctor.assert_called_once()
    assert ctor.call_args.kwargs["refresh_token"] == settings.google_refresh_token
    assert ctor.call_args.kwargs["scopes"] == cal.SCOPES
    fake_creds.refresh.assert_called_once()


# ------------------------------------------------------------------ event creation


def test_create_event_rechecks_availability_then_inserts_in_configured_timezone():
    service = make_service()
    with use(service):
        result = cal.create_event(TOMORROW, "19:00", 45, "Call with Sam")

    assert result == {"success": True, "event_id": "evt-1"}
    insert = service.events.return_value.insert
    insert.assert_called_once_with(
        calendarId="primary",
        body={
            "summary": "Call with Sam",
            "start": {"dateTime": "2026-10-13T19:00:00+03:00", "timeZone": "Asia/Amman"},
            "end": {"dateTime": "2026-10-13T19:45:00+03:00", "timeZone": "Asia/Amman"},
        },
    )
    names = [call[0] for call in service.method_calls]
    assert next(i for i, n in enumerate(names) if n.startswith("freebusy")) < next(
        i for i, n in enumerate(names) if n.startswith("events")
    )
    insert.return_value.execute.assert_called_once_with(num_retries=0)  # never retry a non-idempotent insert


def test_create_event_refuses_when_slot_became_busy_after_the_check():
    service = make_service([(at(TOMORROW, "19:15"), at(TOMORROW, "19:30"))])
    with use(service):
        with pytest.raises(cal.CalendarError) as exc_info:
            cal.create_event(TOMORROW, "19:00", 45, "Call with Sam")

    assert exc_info.value.code == "slot_unavailable"
    service.events.return_value.insert.assert_not_called()


def test_create_event_rejects_past_times_without_calling_google():
    with patch("app.calendar._get_service") as get_service:
        with pytest.raises(cal.CalendarError) as exc_info:
            cal.create_event("2026-10-12", "09:00", 30, "Call")
    assert exc_info.value.code == "time_in_past"
    get_service.assert_not_called()


def test_create_event_insert_failure_is_not_retried_and_is_safe():
    service = make_service()
    service.events.return_value.insert.return_value.execute.side_effect = http_error(500, "Backend Error")
    with use(service):
        with pytest.raises(cal.CalendarError) as exc_info:
            cal.create_event(TOMORROW, "19:00", 30, "Call")

    assert exc_info.value.code == "calendar_unavailable"
    service.events.return_value.insert.return_value.execute.assert_called_once_with(num_retries=0)


@pytest.mark.parametrize(
    "raw_title, expected",
    [
        ("", cal.DEFAULT_EVENT_TITLE),
        (None, cal.DEFAULT_EVENT_TITLE),
        ("   \n ", cal.DEFAULT_EVENT_TITLE),
        ("  Call \n with   Sam ", "Call with Sam"),
        ("x" * 500, "x" * cal.MAX_TITLE_LENGTH),
    ],
)
def test_event_titles_are_sanitised(raw_title, expected):
    service = make_service()
    with use(service):
        cal.create_event(TOMORROW, "19:00", 30, raw_title)
    assert service.events.return_value.insert.call_args.kwargs["body"]["summary"] == expected


# ------------------------------------------------------------------ timezone / DST


@pytest.fixture
def new_york():
    zone = ZoneInfo("America/New_York")
    now = dt.datetime(2027, 3, 1, 10, 0, tzinfo=zone)
    with patch("app.calendar.settings", replace(settings, timezone="America/New_York")), patch(
        "app.calendar._now", return_value=now
    ):
        yield


def test_nonexistent_dst_time_is_rejected(new_york):
    with pytest.raises(cal.CalendarError) as exc_info:
        cal.normalize_slot("2027-03-14", "02:30", 30)  # clocks jump 02:00 -> 03:00
    assert exc_info.value.code == "invalid_time"


def test_event_duration_is_real_elapsed_time_across_a_dst_change(new_york):
    service = make_service()
    with use(service):
        cal.create_event("2027-03-14", "01:30", 60, "Call")

    body = service.events.return_value.insert.call_args.kwargs["body"]
    assert body["start"] == {"dateTime": "2027-03-14T01:30:00-05:00", "timeZone": "America/New_York"}
    assert body["end"] == {"dateTime": "2027-03-14T03:30:00-04:00", "timeZone": "America/New_York"}


def test_ambiguous_dst_time_is_accepted(new_york):
    service = make_service()
    with use(service):
        cal.create_event("2027-11-07", "01:30", 30, "Call")  # 01:30 happens twice; first occurrence is used
    body = service.events.return_value.insert.call_args.kwargs["body"]
    assert body["start"]["dateTime"] == "2027-11-07T01:30:00-04:00"
