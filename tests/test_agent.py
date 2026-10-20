"""Tests for Sara's core agent behavior.

These tests never talk to the real Gemini API or real Google Calendar:
- Building a SaraAgent only constructs a local Gemini chat session (no
  network call happens until send_message() is used, which these tests
  avoid).
- All calendar access goes through app.calendar, which is mocked here.
"""

from unittest.mock import patch

from app.agent import SaraAgent
from app.calendar import CalendarError


def make_agent() -> SaraAgent:
    return SaraAgent()


def test_create_event_rejected_without_prior_check():
    """An event must never be created without a matching availability check first."""
    agent = make_agent()

    with patch("app.agent.calendar_service.create_event") as mock_create:
        result = agent.create_calendar_event(
            date="2026-10-01", start_time="21:00", duration_minutes=30, title="Call with a friend"
        )

    assert result["success"] is False
    assert result["error"] == "not_confirmed_available"
    mock_create.assert_not_called()


def test_create_event_succeeds_after_matching_available_check():
    """Once the exact slot was checked and found available, creation is allowed."""
    agent = make_agent()

    with patch("app.agent.calendar_service.check_availability") as mock_check:
        mock_check.return_value = {"available": True, "alternatives": []}
        agent.check_calendar_availability(date="2026-10-01", start_time="21:00", duration_minutes=30)

    with patch("app.agent.calendar_service.create_event") as mock_create:
        mock_create.return_value = {"success": True, "event_id": "abc123"}
        result = agent.create_calendar_event(
            date="2026-10-01", start_time="21:00", duration_minutes=30, title="Call with a friend"
        )

    assert result["success"] is True
    mock_create.assert_called_once_with("2026-10-01", "21:00", 30, "Call with a friend")


def test_create_event_rejected_if_slot_differs_from_last_check():
    """Confirming one slot must not allow booking a different, unchecked slot."""
    agent = make_agent()

    with patch("app.agent.calendar_service.check_availability") as mock_check:
        mock_check.return_value = {"available": True, "alternatives": []}
        agent.check_calendar_availability(date="2026-10-01", start_time="21:00", duration_minutes=30)

    with patch("app.agent.calendar_service.create_event") as mock_create:
        result = agent.create_calendar_event(
            date="2026-10-01", start_time="22:00", duration_minutes=30, title="Call with a friend"
        )

    assert result["success"] is False
    mock_create.assert_not_called()


def test_unavailable_time_returns_alternatives_without_private_details():
    """Unavailable slots must surface only availability + alternative times, nothing private."""
    agent = make_agent()

    with patch("app.agent.calendar_service.check_availability") as mock_check:
        mock_check.return_value = {
            "available": False,
            "alternatives": [{"start_time": "22:00", "duration_minutes": 30}],
        }
        result = agent.check_calendar_availability(date="2026-10-01", start_time="21:00", duration_minutes=30)

    assert result["available"] is False
    assert result["alternatives"] == [{"start_time": "22:00", "duration_minutes": 30}]
    leaked_keys = {"title", "description", "attendees", "location", "summary"}
    assert leaked_keys.isdisjoint(result.keys())


def test_create_event_requires_fresh_check_after_booking():
    """After a successful booking, the confirmation gate must reset (no double-booking reuse)."""
    agent = make_agent()

    with patch("app.agent.calendar_service.check_availability") as mock_check:
        mock_check.return_value = {"available": True, "alternatives": []}
        agent.check_calendar_availability(date="2026-10-01", start_time="21:00", duration_minutes=30)

    with patch("app.agent.calendar_service.create_event") as mock_create:
        mock_create.return_value = {"success": True, "event_id": "abc123"}
        agent.create_calendar_event(date="2026-10-01", start_time="21:00", duration_minutes=30, title="Call")

    with patch("app.agent.calendar_service.create_event") as mock_create_again:
        result = agent.create_calendar_event(date="2026-10-01", start_time="21:00", duration_minutes=30, title="Call")

    assert result["success"] is False
    mock_create_again.assert_not_called()


def mark_available(agent: SaraAgent, date="2026-10-01", start_time="21:00", duration=30):
    with patch("app.agent.calendar_service.check_availability") as mock_check:
        mock_check.return_value = {"available": True, "alternatives": []}
        agent.check_calendar_availability(date=date, start_time=start_time, duration_minutes=duration)


def test_slot_format_differences_do_not_break_the_confirmation_gate():
    """"9:00" at check time and "09:00" at create time are the same slot."""
    agent = make_agent()
    mark_available(agent, start_time="9:00")

    with patch("app.agent.calendar_service.create_event") as mock_create:
        mock_create.return_value = {"success": True, "event_id": "abc123"}
        result = agent.create_calendar_event(date="2026-10-01", start_time="09:00", duration_minutes=30, title="Call")

    assert result["success"] is True
    mock_create.assert_called_once_with("2026-10-01", "09:00", 30, "Call")


def test_invalid_input_returns_structured_error_and_clears_the_gate():
    agent = make_agent()
    mark_available(agent)

    with patch("app.agent.calendar_service.check_availability") as mock_check:
        result = agent.check_calendar_availability(date="not-a-date", start_time="21:00", duration_minutes=30)
    mock_check.assert_not_called()

    assert result["verified"] is False
    assert "available" not in result  # a failed check must never read as "busy"
    assert result["error"] == "invalid_date"

    with patch("app.agent.calendar_service.create_event") as mock_create:
        blocked = agent.create_calendar_event(date="2026-10-01", start_time="21:00", duration_minutes=30, title="Call")
    assert blocked["success"] is False
    mock_create.assert_not_called()


def test_calendar_error_during_check_is_reported_without_internal_details():
    agent = make_agent()
    error = CalendarError("calendar_unavailable", "The calendar service is temporarily unavailable.")

    with patch("app.agent.calendar_service.check_availability", side_effect=error):
        result = agent.check_calendar_availability(date="2026-10-01", start_time="21:00", duration_minutes=30)

    assert result["verified"] is False
    assert "available" not in result and "alternatives" not in result  # unknown is not busy, and no invented times
    assert result["error"] == "calendar_unavailable"
    assert "could NOT be verified" in result["message"]


def test_unexpected_exception_during_check_does_not_leak_its_message():
    agent = make_agent()

    with patch("app.agent.calendar_service.check_availability", side_effect=RuntimeError("token=SECRET123")):
        result = agent.check_calendar_availability(date="2026-10-01", start_time="21:00", duration_minutes=30)

    assert result["verified"] is False and result["error"] == "calendar_check_failed"
    assert "SECRET123" not in str(result)


def test_failed_booking_reports_error_and_requires_a_fresh_check():
    agent = make_agent()
    mark_available(agent)
    error = CalendarError("slot_unavailable", "That time is no longer available.")

    with patch("app.agent.calendar_service.create_event", side_effect=error) as mock_create:
        failed = agent.create_calendar_event(date="2026-10-01", start_time="21:00", duration_minutes=30, title="Call")
        retried = agent.create_calendar_event(date="2026-10-01", start_time="21:00", duration_minutes=30, title="Call")

    assert failed["success"] is False and failed["error"] == "slot_unavailable"
    assert failed["message"].startswith("The event was NOT created.")
    assert retried["error"] == "not_confirmed_available"
    mock_create.assert_called_once()


def test_unexpected_exception_during_booking_does_not_leak_its_message():
    agent = make_agent()
    mark_available(agent)

    with patch("app.agent.calendar_service.create_event", side_effect=RuntimeError("token=SECRET123")):
        result = agent.create_calendar_event(date="2026-10-01", start_time="21:00", duration_minutes=30, title="Call")

    assert result["success"] is False and result["error"] == "creation_failed"
    assert "SECRET123" not in str(result)
