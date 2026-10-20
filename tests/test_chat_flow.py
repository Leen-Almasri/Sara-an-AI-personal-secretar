"""End-to-end chat flow tests.

The real google-genai chat object and its automatic-function-calling loop run
unmodified, executing Sara's real tool methods. Only the HTTP call to Gemini
(`Models._generate_content`) is scripted and Google Calendar (`app.calendar`) is
mocked, so nothing here touches the network or a real calendar.
"""

import copy
import datetime as dt
import logging
import re
from contextlib import contextmanager
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from google.genai import types
from google.genai.models import Models

from app.agent import _BOOKING_CLAIM, _MESSAGES, SaraAgent
from app.calendar import CalendarError

DATE, TIME, MINUTES = "2026-10-13", "21:00", 30
AVAILABLE = {"available": True, "alternatives": []}
CHECK_ARGS = {"date": DATE, "start_time": TIME, "duration_minutes": MINUTES}


# ---------------------------------------------------------------- scripted Gemini


def _response(*parts: types.Part) -> types.GenerateContentResponse:
    content = types.Content(role="model", parts=list(parts))
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=content, finish_reason=types.FinishReason.STOP)]
    )


def calls(name: str, **args) -> types.GenerateContentResponse:
    return _response(types.Part.from_function_call(name=name, args=args))


def says(text: str) -> types.GenerateContentResponse:
    return _response(types.Part(text=text))


class ScriptedGemini:
    """Returns pre-written responses in order and records every request it receives."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, *, model, contents, config=None):
        self.requests.append({"contents": copy.deepcopy(contents), "config": config})
        if not self.responses:
            raise AssertionError("Gemini was called more times than the test scripted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def tool_result_seen_by_gemini(self, request_index: int = -1) -> dict:
        """The function_response payload the SDK sent back to Gemini in a request."""
        last = self.requests[request_index]["contents"][-1]
        return last.parts[0].function_response.response


@contextmanager
def gemini(*responses):
    script = ScriptedGemini(*responses)

    def fake_generate_content(models_self, *, model, contents, config=None):
        return script(model=model, contents=contents, config=config)

    with patch.object(Models, "_generate_content", new=fake_generate_content):
        yield script
    assert not script.responses, "scripted Gemini responses were left unused"


@pytest.fixture
def agent() -> SaraAgent:
    return SaraAgent()


def check_ok():
    return patch("app.agent.calendar_service.check_availability", return_value=AVAILABLE)


def create_ok():
    return patch("app.agent.calendar_service.create_event", return_value={"success": True, "event_id": "evt-1"})


def available_then_confirm(agent: SaraAgent, create_mock, replies=("Booked.",)):
    """Turn 1: availability check. Turn 2: the friend confirms and Gemini books."""
    with check_ok(), gemini(calls("check_calendar_availability", **CHECK_ARGS), says("Free. Confirm?")):
        agent.send_message("Can I book a call tomorrow at 9 PM for 30 minutes?")
    return gemini(
        calls("create_calendar_event", **CHECK_ARGS, title="Call with a friend"),
        says(replies[0]),
    )


# ---------------------------------------------------------------- availability


def test_availability_request_reaches_the_tool_and_its_result_reaches_gemini(agent):
    with check_ok() as mock_check, gemini(
        calls("check_calendar_availability", **CHECK_ARGS), says("That time is free. Shall I book it?")
    ) as script:
        reply = agent.send_message("Can I book a call tomorrow at 9 PM for 30 minutes?")

    mock_check.assert_called_once_with(DATE, TIME, MINUTES)
    assert reply == "That time is free. Shall I book it?"
    seen = script.tool_result_seen_by_gemini()
    # Regression: the SDK used to reject the call before our code ran and send back
    # {"error": "isinstance() arg 2 must be a type..."} (caused by postponed annotations).
    assert "error" not in seen
    assert seen == {"result": {"verified": True, "available": True, "alternatives": []}}


def test_unavailable_slot_result_carries_alternatives_but_nothing_private(agent):
    busy = {"available": False, "alternatives": [{"date": DATE, "start_time": "22:00", "duration_minutes": 30}]}
    with patch("app.agent.calendar_service.check_availability", return_value=busy), gemini(
        calls("check_calendar_availability", **CHECK_ARGS), says("Not then, but 22:00 works.")
    ) as script:
        agent.send_message("9 PM tomorrow for 30 minutes?")

    result = script.tool_result_seen_by_gemini()["result"]
    assert result["verified"] is True and result["available"] is False
    assert result["alternatives"] == busy["alternatives"]


def test_sdk_rejected_tool_call_is_logged_as_dispatch_failure_not_as_never_called(agent, caplog):
    with caplog.at_level(logging.INFO, logger="app.agent"), gemini(
        calls("check_calendar_availability", date=DATE, start_time=TIME),  # duration missing
        says("Sorry, something went wrong."),
    ):
        agent.send_message("9 PM tomorrow?")

    assert "tool_dispatch_failed" in caplog.text
    assert "tools_requested=1 tools_ran=none" in caplog.text


def test_turn_without_any_tool_call_is_logged_as_none(agent, caplog):
    with caplog.at_level(logging.INFO, logger="app.agent"), gemini(says("Which day did you have in mind?")):
        agent.send_message("Can I book a call?")

    assert "tools_requested=0 tools_ran=none" in caplog.text
    assert "tool_dispatch_failed" not in caplog.text


# ---------------------------------------------------------------- tool failures


def test_calendar_failure_tells_gemini_availability_is_unknown_not_busy(agent, caplog):
    error = CalendarError("calendar_unavailable", "The calendar service is temporarily unavailable.")
    with caplog.at_level(logging.INFO, logger="app.agent"), patch(
        "app.agent.calendar_service.check_availability", side_effect=error
    ), gemini(
        calls("check_calendar_availability", **CHECK_ARGS),
        says("I couldn't verify the calendar right now. Nothing is booked."),
    ) as script:
        reply = agent.send_message("Can I book a call tomorrow at 9 PM for 30 minutes?")

    seen = script.tool_result_seen_by_gemini()["result"]
    assert seen["verified"] is False
    assert "available" not in seen and "alternatives" not in seen
    assert "could NOT be verified" in seen["message"]
    assert reply.startswith("I couldn't verify")
    assert "tools_ran=check_calendar_availability:failed(calendar_unavailable)" in caplog.text
    assert "status=failed error=calendar_unavailable" in caplog.text


def test_failed_check_leaves_the_booking_gate_closed(agent):
    with patch("app.agent.calendar_service.check_availability", side_effect=CalendarError("calendar_unavailable", "x")):
        with gemini(calls("check_calendar_availability", **CHECK_ARGS), says("Could not verify.")):
            agent.send_message("9 PM tomorrow, 30 minutes?")

    with patch("app.agent.calendar_service.create_event") as mock_create, gemini(
        calls("create_calendar_event", **CHECK_ARGS, title="Call"), says("Could not book.")
    ) as script:
        agent.send_message("yes book it")

    mock_create.assert_not_called()
    assert script.tool_result_seen_by_gemini()["result"]["error"] == "not_confirmed_available"


# ---------------------------------------------------------------- booking flow


def test_available_then_explicit_confirmation_books_exactly_the_checked_slot(agent, caplog):
    with caplog.at_level(logging.INFO, logger="app.agent"), create_ok() as mock_create:
        with available_then_confirm(agent, mock_create, replies=("Done! I've booked it for tomorrow at 9 PM.",)) as script:
            reply = agent.send_message("Yes, please book it")

    mock_create.assert_called_once_with(DATE, TIME, MINUTES, "Call with a friend")
    assert script.tool_result_seen_by_gemini()["result"] == {"success": True, "event_id": "evt-1"}
    assert reply == "Done! I've booked it for tomorrow at 9 PM."  # a real booking may be reported
    assert "tools_ran=create_calendar_event:ok" in caplog.text
    # Turn 2 request carried turn 1 (the check) as history.
    assert len(script.requests[0]["contents"]) > 3


def test_booking_without_prior_check_is_refused_and_creates_nothing(agent):
    with patch("app.agent.calendar_service.create_event") as mock_create, gemini(
        calls("create_calendar_event", **CHECK_ARGS, title="Call"), says("Let me check the calendar first.")
    ) as script:
        reply = agent.send_message("book me 9 PM tomorrow")

    mock_create.assert_not_called()
    result = script.tool_result_seen_by_gemini()["result"]
    assert result["success"] is False and result["error"] == "not_confirmed_available"
    assert result["message"].startswith("The event was NOT created.")
    assert reply == "Let me check the calendar first."


def test_booking_a_different_slot_than_the_one_checked_is_refused(agent):
    with check_ok(), gemini(calls("check_calendar_availability", **CHECK_ARGS), says("Free. Confirm?")):
        agent.send_message("9 PM tomorrow for 30 minutes?")

    other = {**CHECK_ARGS, "start_time": "22:00"}
    with patch("app.agent.calendar_service.create_event") as mock_create, gemini(
        calls("create_calendar_event", **other, title="Call"), says("I need to check 22:00 first.")
    ) as script:
        agent.send_message("Actually 10 PM, book it")

    mock_create.assert_not_called()
    assert script.tool_result_seen_by_gemini()["result"]["error"] == "not_confirmed_available"


def test_confirmation_cannot_book_a_slot_that_was_found_unavailable(agent):
    busy = {"available": False, "alternatives": []}
    with patch("app.agent.calendar_service.check_availability", return_value=busy), gemini(
        calls("check_calendar_availability", **CHECK_ARGS), says("That time is unavailable.")
    ):
        agent.send_message("9 PM tomorrow for 30 minutes?")

    with patch("app.agent.calendar_service.create_event") as mock_create, gemini(
        calls("create_calendar_event", **CHECK_ARGS, title="Call"), says("Sorry.")
    ):
        agent.send_message("book it anyway")

    mock_create.assert_not_called()


# ---------------------------------------------------------------- creation failure & false claims


def test_failed_creation_is_reported_to_gemini_as_not_created(agent, caplog):
    error = CalendarError("calendar_unavailable", "The calendar service is temporarily unavailable.")
    with caplog.at_level(logging.INFO, logger="app.agent"), patch(
        "app.agent.calendar_service.create_event", side_effect=error
    ) as mock_create:
        with available_then_confirm(agent, mock_create, replies=("The booking did not go through; nothing was added.",)) as script:
            reply = agent.send_message("Yes, book it")

    result = script.tool_result_seen_by_gemini()["result"]
    assert result["success"] is False and result["message"].startswith("The event was NOT created.")
    assert reply == "The booking did not go through; nothing was added."  # honest replies pass through
    assert "tools_ran=create_calendar_event:failed(calendar_unavailable)" in caplog.text


@pytest.mark.parametrize(
    "false_claim, friend_text",
    [
        ("Done! I've booked it for tomorrow at 9 PM.", "Yes, book it"),
        ("Your booking is confirmed.", "Yes, book it"),
        ("تم حجز الموعد ✅ بكرة 9 مساءً", "أيوه احجز"),
        ("حجزتلك الموعد، ننتظرك!", "أيوه احجز"),
        ("أبشر، انحجز الموعد بكرة 9 بالليل", "ايوا ثبته"),
        ("ثبتت لك الموعد يا أحمد", "ايوا ثبته"),
        ("سجلته لك في الجدول", "ايوا ثبته"),
    ],
)
def test_sara_never_claims_success_when_creation_failed(agent, false_claim, friend_text):
    error = CalendarError("calendar_unavailable", "The calendar service is temporarily unavailable.")
    with patch("app.agent.calendar_service.create_event", side_effect=error) as mock_create:
        with available_then_confirm(agent, mock_create, replies=(false_claim,)):
            reply = agent.send_message(friend_text)

    assert reply == _MESSAGES["tool_failed"]  # honest, Saudi Arabic, even when the friend wrote English
    assert not _BOOKING_CLAIM.search(reply)
    # Gemini's own memory is corrected too, so it does not "remember" a booking that never happened.
    last = agent._chat.get_history(curated=True)[-1]
    assert last.role == "model" and last.parts[0].text == reply


def test_sara_never_claims_success_when_the_booking_tool_was_never_called(agent):
    with gemini(says("All set - I've booked it!")):
        reply = agent.send_message("Yes please")
    assert reply == _MESSAGES["not_booked"]


@pytest.mark.parametrize(
    "text, is_claim",
    [
        ("I've booked it for you", True),
        ("I have booked the call", True),
        ("Your booking is confirmed", True),
        ("The event has been created", True),
        ("added it to the calendar", True),
        ("تم حجز الموعد", True),
        ("تم الحجز بنجاح", True),
        ("حجزتلك الموعد", True),
        ("تمت إضافة الموعد", True),
        ("أبشر، انحجز الموعد", True),
        ("ثبتت لك الموعد", True),
        ("سجلته لك في الجدول", True),
        ("ضفته لك", True),
        ("ما انحجز شي للحين", False),
        ("ما ثبتّ شي، لازم تأكد أول", False),
        ("هل تبي أثبته لك؟", False),
        ("الجدول مشغول بهالوقت", False),
        ("The calendar is booked at that time, sorry", False),
        ("Shall I book it for you?", False),
        ("I haven't booked anything yet", False),
        ("هل أحجزه لك؟", False),
        ("لم يتم حجز الموعد", False),
        ("ما حجزت شي بعد", False),
        ("Nothing has been booked yet", False),
        ("No event has been created", False),
        ("That slot is free at 9 PM. Confirm?", False),
    ],
)
def test_booking_claim_detector(text, is_claim):
    assert bool(_BOOKING_CLAIM.search(text)) is is_claim


# ---------------------------------------------------------------- empty replies & Gemini failures


def test_empty_gemini_reply_falls_back_to_an_honest_arabic_message_whatever_the_input_language(agent):
    with gemini(says("")):
        english_input = agent.send_message("Hello?")
    with gemini(says("")):
        arabic_input = agent.send_message("مرحبا؟")

    assert english_input == arabic_input == _MESSAGES["no_reply"]
    assert "ما انحجز" in english_input


def test_empty_reply_after_a_confirmed_booking_still_reports_the_booking(agent):
    with create_ok() as mock_create, available_then_confirm(agent, mock_create, replies=("",)):
        reply = agent.send_message("Yes, book it")

    assert DATE in reply and TIME in reply


def test_gemini_outage_after_a_confirmed_booking_still_reports_the_booking(agent):
    with create_ok():
        with check_ok(), gemini(calls("check_calendar_availability", **CHECK_ARGS), says("Free. Confirm?")):
            agent.send_message("9 PM tomorrow for 30 minutes?")
        with gemini(
            calls("create_calendar_event", **CHECK_ARGS, title="Call"), RuntimeError("503 overloaded")
        ):
            reply = agent.send_message("Yes, book it")

    assert DATE in reply and TIME in reply


def test_gemini_outage_without_a_booking_propagates(agent):
    with gemini(RuntimeError("503 overloaded")):
        with pytest.raises(RuntimeError):
            agent.send_message("Hello")


# ---------------------------------------------------------------- prompt / dates


def test_system_prompt_is_rebuilt_every_turn_with_the_current_date(agent):
    clock = {"now": dt.datetime(2026, 10, 12, 10, 0, tzinfo=ZoneInfo("Asia/Riyadh"))}
    with patch("app.agent._now_in_local_timezone", side_effect=lambda: clock["now"]):
        with gemini(says("One."), says("Two.")) as script:
            agent.send_message("hi")
            clock["now"] = dt.datetime(2026, 10, 15, 9, 0, tzinfo=ZoneInfo("Asia/Riyadh"))
            agent.send_message("hi again")

    first, second = (r["config"].system_instruction for r in script.requests)
    assert "Monday, 2026-10-12 10:00 (tomorrow is Tuesday, 2026-10-13)" in first
    assert "Thursday, 2026-10-15 09:00 (tomorrow is Friday, 2026-10-16)" in second


def test_system_prompt_forbids_inferring_availability_and_guards_failures(agent):
    prompt = agent._build_system_prompt()

    assert "normal available hours" not in prompt and "working hours" in prompt  # only as a "don't infer" rule
    assert "8 صباحًا" in prompt and "08:00" in prompt and "21:00" in prompt
    assert "could not verify" in prompt
    assert "Asia/Riyadh" in prompt or "Asia/Amman" in prompt  # configured timezone is stated


def test_sarahs_own_fallback_messages_are_arabic_only_and_never_mistaken_for_booking_claims():
    for key, text in _MESSAGES.items():
        assert not re.search("[A-Za-z]", text.replace("{date}", "").replace("{start_time}", "").replace("{minutes}", ""))
        if key != "booked":  # the confirmation is only ever sent after Google confirmed the booking
            assert not _BOOKING_CLAIM.search(text)


# ---------------------------------------------------------------- stalled turns (greets, promises to check, no tool)


def reminder_was_sent(script: ScriptedGemini, request_index: int) -> bool:
    last_user_parts = script.requests[request_index]["contents"][-1].parts
    return any("SYSTEM REMINDER" in (part.text or "") for part in last_user_parts)


def test_announcing_a_check_without_calling_the_tool_triggers_one_reminder_and_recovers(agent):
    with check_ok() as mock_check, gemini(
        says("هلا والله يا سلمى! ثواني أشيك الجدول."),  # stalls: no tool call
        calls("check_calendar_availability", **CHECK_ARGS),
        says("الجدول فاضي بكرة 9 بالليل لمدة نص ساعة. أأكد لك الحجز؟"),
    ) as script:
        reply = agent.send_message("أنا سلمى، ابي أحجز مكالمة بكرة 9 مساءً نص ساعة")

    mock_check.assert_called_once_with(DATE, TIME, MINUTES)
    assert reply == "الجدول فاضي بكرة 9 بالليل لمدة نص ساعة. أأكد لك الحجز؟"  # the stalled text never reaches the friend
    assert reminder_was_sent(script, 1)
    assert len(script.requests) == 3


@pytest.mark.parametrize(
    "stalled_reply",
    [
        "هلا والله يا ريم! بشيك على الجدول بكرة.",
        "ثواني أشوف لك الجدول.",
        "خليني أتأكد من جدولها.",
        "لحظة، براجع لك الجدول.",
        "تبي أقول لك سر؟ 😄 بس ثواني أشوف الجدول بكرة...",  # a "؟" in the middle is not a real question
    ],
)
def test_saudi_forms_of_announcing_a_check_are_recognised(agent, stalled_reply):
    with check_ok(), gemini(
        says(stalled_reply),
        calls("check_calendar_availability", **CHECK_ARGS),
        says("الجدول فاضي بكرة 9 بالليل. أأكد؟"),
    ) as script:
        reply = agent.send_message("أنا ريم، بكرة 9 مساءً نص ساعة")

    assert reminder_was_sent(script, 1)
    assert reply == "الجدول فاضي بكرة 9 بالليل. أأكد؟"


def test_a_stall_that_survives_the_reminder_is_replaced_by_an_honest_message(agent, caplog):
    with caplog.at_level(logging.WARNING, logger="app.agent"), gemini(
        says("دقايق أتأكد لك من الجدول."),
        says("ثواني أشوف الجدول."),
    ) as script:
        reply = agent.send_message("أنا سلمى، ابي أحجز مكالمة بكرة 9 مساءً")

    assert reply == _MESSAGES["tool_failed"]
    assert len(script.requests) == 2  # exactly one reminder, never a loop
    assert "turn_stalled_after_reminder" in caplog.text
    assert agent._chat.get_history(curated=True)[-1].parts[0].text == reply


def test_stating_availability_without_any_calendar_check_is_corrected(agent):
    with check_ok() as mock_check, gemini(
        says("أبشرك، الجدول فاضي بكرة 9 بالليل!"),  # fabricated: no tool was called
        calls("check_calendar_availability", **CHECK_ARGS),
        says("تأكدت: الجدول فاضي بكرة 9 بالليل. أأكد لك الحجز؟"),
    ) as script:
        reply = agent.send_message("هل الجدول فاضي بكرة 9 مساءً نص ساعة؟")

    mock_check.assert_called_once()
    assert reminder_was_sent(script, 1)
    assert "تأكدت" in reply


def test_fabricated_availability_that_persists_is_never_shown(agent):
    with gemini(says("الجدول فاضي بكرة."), says("أكيد الجدول فاضي.")):
        reply = agent.send_message("بكرة 9 مساءً؟")
    assert reply == _MESSAGES["tool_failed"]


@pytest.mark.parametrize(
    "harmless_reply",
    [
        "هلا والله! أنا سارة، السكرتيرة الشخصية الذكية. وش اسمك؟ 😊",
        "9 الصبح ولا 9 بالليل؟ 😊",  # a clarifying question that ends the reply is not a stall
        "يا هلا أحمد! وش ودّك تنسق؟",
        "العفو، نشوفك على خير! 😊",  # "see you" must not be mistaken for "I'll look"
        "ثواني... تبي أشيك لك الجدول؟ 😊",  # ends by asking the friend something
    ],
)
def test_normal_replies_without_a_tool_call_are_left_alone(agent, harmless_reply):
    with gemini(says(harmless_reply)) as script:
        reply = agent.send_message("هلا")
    assert reply == harmless_reply
    assert len(script.requests) == 1


def test_restating_a_verified_availability_later_does_not_trigger_a_reminder(agent):
    with check_ok(), gemini(calls("check_calendar_availability", **CHECK_ARGS), says("فاضية. أأكد؟")):
        agent.send_message("بكرة 9 مساءً نص ساعة؟")

    with gemini(says("الجدول فاضي بكرة الساعة 9 بالليل زي ما قلت لك، أأكد الحجز؟")) as script:
        reply = agent.send_message("هاه؟")

    assert len(script.requests) == 1
    assert reply.startswith("الجدول فاضي")


def test_a_reminder_never_lets_a_booking_through_without_confirmation(agent):
    with patch("app.agent.calendar_service.create_event") as mock_create, gemini(
        says("ثواني أشيك."),
        calls("create_calendar_event", **CHECK_ARGS, title="لقاء"),
        says("ما قدرت أحجز."),
    ) as script:
        agent.send_message("احجز لي بكرة 9 مساءً")

    mock_create.assert_not_called()  # the server-side gate still demands a prior verified check
    assert script.tool_result_seen_by_gemini()["result"]["error"] == "not_confirmed_available"
