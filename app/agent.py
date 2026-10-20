"""Sara's agent: wraps a Gemini chat session with calendar tool-calling.

Each SaraAgent instance represents one conversation with one friend. It
keeps the friend's chat history (handled internally by the Gemini SDK) and
a small piece of server-side state - the last availability check - used as
a safety gate so an event can never be created for a slot that was not
just verified as available.

The LLM never sees Google credentials. It can only call the two tool
methods below; this module is the only place that talks to app.calendar.

Logging (INFO) makes tool calling diagnosable without leaking anything
private. Per tool: `tool_call` (arguments) then `tool_result` (outcome).
Per message: `turn_end tools=...` lists every tool that ran, or `none` if
Gemini answered without calling any tool - so "never called" and "called
and failed" look different. Friend messages, event titles, API keys and
calendar contents are never logged.

Do NOT add `from __future__ import annotations` here. The google-genai SDK runs
tool calls by checking arguments against the methods' annotations with
isinstance(); with postponed (string) annotations that check raises, the SDK
swallows it and returns {"error": "isinstance() arg 2 must be a type..."} to
Gemini as the tool result - and the tool body never runs.
"""

import datetime as dt
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

from app import calendar as calendar_service
from app.config import settings
from app.prompts import build_system_prompt

logger = logging.getLogger(__name__)

# Retry transient Gemini failures (overload/5xx) so one blip does not drop a whole turn.
_client = genai.Client(
    api_key=settings.gemini_api_key,
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=3, initial_delay=1.0, max_delay=8.0, http_status_codes=[500, 502, 503, 504])
    ),
)

_INPUT_ERROR_CODES = {"invalid_date", "invalid_time", "invalid_duration"}

# Last line of defence: past-tense claims that something was booked/added. If Gemini
# says this but Google did not confirm a booking in the same turn, the reply is replaced.
_BOOKING_CLAIM = re.compile(
    r"(\bتم\s+(?:ال)?حجز|\bتم\s+تأكيد\s+(?:ال)?حجز|\bتم(?:ت)?\s+(?:ال)?(?:إضافة|اضافة|جدولة|تثبيت)|"
    r"\bتمت\s+(?:ال)?(?:إضافة|اضافة|جدولة)|(?<!ما )(?<!لم )\bحجزت(?:ه|ها|لك|لكم|ك)?\b|"
    r"(?<!ما )(?<!لم )\b(?:ثبّ?تت?|سجلت|أضفت|اضفت|ضفت)(?:ه|ها|لك|لكم|ك)?\b|(?<!ما )(?<!لم )\b(?:انحجز|انسجل|انضاف)|"
    r"\bi(?:'ve| have)\s+(?:now\s+)?(?:booked|scheduled|added|created)\b|"
    r"(?<!not )(?<!nothing )(?<!never )\bhas been (?:booked|scheduled|added)\b|"
    r"\b(?:successfully|now) booked\b|"
    r"(?<!no )\bbooking (?:is |has been )?(?:confirmed|created)\b|\badded (?:it )?to (?:the |her |his |their |my |\w+'s )?(?:google )?calendar\b|"
    r"(?<!no )\bevent (?:has been |was )?created\b)",
    re.IGNORECASE,
)

# Gemini sometimes greets, says "let me check" and ends its turn without calling the tool.
_ANNOUNCES_CHECK = re.compile(
    r"(?:[أا]شيك|بشيك|نشيك|[أا]شوف|بشوف|[أا]تأكد|بتأكد|نتأكد|[أا]تحقق|بتحقق|[أا]راجع|براجع|[أا]فحص|بفحص)"
)
# A reply that ends by asking the friend something is waiting for them, not stalled
# (a joke or rhetorical question earlier in the reply does not count).
_ENDS_WITH_QUESTION = re.compile(r"[؟?][\W_]*$")
_AVAILABILITY_CLAIM = re.compile(r"(?:فاضي|فاضية|متاح|متاحة|مشغول|مشغولة)")
_TOOL_REMINDER = (
    "SYSTEM REMINDER (not from the friend): you have not called any tool in this turn. If the friend's "
    "date and clock time are clear, call check_calendar_availability now and then answer based on its "
    "result. If something is missing or ambiguous, ask your one short clarifying question instead. "
    "Reply in Saudi Arabic and do not mention this reminder."
)

# Sara always answers in Saudi Arabic, including when these safety messages replace a reply.
_MESSAGES = {
    "not_booked": "للأسف ما انحجز أي موعد في الجدول للحين. عطني التاريخ والوقت والمدة اللي تبيها وأتأكد منها أول 😊",
    "tool_failed": "ما قدرت أتأكد من الجدول الحين، وما انحجز أي شي. جرب مرة ثانية بعد شوي ",
    "no_reply": "سامحني، ما قدرت أرد عليك الحين، وما انحجز أي شي. ممكن تعيد كلامك بصيغة ثانية؟",
    "booked": "تم حجز الموعد في الجدول ✅ {date} الساعة {start_time} لمدة {minutes} دقيقة.",
}


def _now_in_local_timezone() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(settings.timezone))


def _short(value: Any) -> str:
    return repr(value)[:40]


class SaraAgent:
    def __init__(self) -> None:
        self._last_checked: dict | None = None
        # Per-message bookkeeping, reset in send_message().
        self._turn_tools: list[str] = []
        self._turn_booking: tuple[str, str, int] | None = None
        self._chat = _client.chats.create(model=settings.gemini_model, config=self._chat_config())

    def _chat_config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=self._build_system_prompt(),
            tools=[self.check_calendar_availability, self.create_calendar_event],
        )

    @staticmethod
    def _build_system_prompt() -> str:
        now = _now_in_local_timezone()
        tomorrow = now + dt.timedelta(days=1)
        current = f"{now:%A, %Y-%m-%d %H:%M} (tomorrow is {tomorrow:%A, %Y-%m-%d})"
        return build_system_prompt(
            current_datetime=current,
            timezone=settings.timezone,
            default_duration=settings.default_event_duration_minutes,
        )

    # ------------------------------------------------------------------ tools

    def check_calendar_availability(self, date: str, start_time: str, duration_minutes: int) -> dict:
        """Check the owner's real Google Calendar for a specific date, start time, and duration.

        This is the ONLY way to know whether the owner is free; never state availability without it.
        Returns "verified": true with "available" (true/false) and, when unavailable, up to
        3 "alternatives" (each with its own date) that are free. Never reveals private
        calendar details. If "verified" is false the check failed: availability is
        UNKNOWN (not busy, not free) - tell the friend it could not be verified.

        Args:
            date: The date to check, in YYYY-MM-DD format.
            start_time: The requested start time, 24-hour HH:MM format.
            duration_minutes: How long the event would last, in minutes.
        """
        logger.info(
            "tool_call check_calendar_availability date=%s start_time=%s duration_minutes=%s",
            _short(date),
            _short(start_time),
            _short(duration_minutes),
        )
        self._last_checked = None
        try:
            date, start_time, duration_minutes = calendar_service.normalize_slot(date, start_time, duration_minutes)
            result = calendar_service.check_availability(date, start_time, duration_minutes)
        except calendar_service.CalendarError as exc:
            return self._check_failed(exc.code, exc.message)
        except Exception:
            logger.exception("tool_error check_calendar_availability unexpected exception")
            return self._check_failed("calendar_check_failed", "The calendar could not be checked.")

        self._last_checked = {
            "date": date,
            "start_time": start_time,
            "duration_minutes": duration_minutes,
            "available": result["available"],
        }
        self._turn_tools.append("check_calendar_availability:ok")
        logger.info(
            "tool_result check_calendar_availability status=ok available=%s alternatives=%d error=%s",
            result["available"],
            len(result.get("alternatives", [])),
            result.get("error"),
        )
        return {"verified": True, **result}

    def _check_failed(self, code: str, message: str) -> dict:
        self._turn_tools.append(f"check_calendar_availability:failed({code})")
        logger.warning("tool_result check_calendar_availability status=failed error=%s", code)
        if code in _INPUT_ERROR_CODES:
            guidance = f"The request details were invalid: {message} Ask the friend to correct them."
        else:
            guidance = f"Availability could NOT be verified ({message}) and nothing is booked. Do not guess."
        return {"verified": False, "error": code, "message": guidance}

    def create_calendar_event(self, date: str, start_time: str, duration_minutes: int, title: str) -> dict:
        """Create an event on the owner's Google Calendar.

        Only call this AFTER the friend has explicitly confirmed a specific date,
        start time, and duration that was just checked with check_calendar_availability
        and found available. Never call this speculatively. Returns "success": true only
        if Google Calendar confirmed the event; otherwise "success": false and the event
        was NOT created - never tell the friend it is booked in that case.

        Args:
            date: The event date, in YYYY-MM-DD format.
            start_time: The event start time, 24-hour HH:MM format.
            duration_minutes: The event duration in minutes.
            title: A short, descriptive event title, e.g. "لقاء مع أحمد".
        """
        logger.info(
            "tool_call create_calendar_event date=%s start_time=%s duration_minutes=%s",
            _short(date),
            _short(start_time),
            _short(duration_minutes),
        )
        try:
            date, start_time, duration_minutes = calendar_service.normalize_slot(date, start_time, duration_minutes)
        except calendar_service.CalendarError as exc:
            return self._create_failed(exc.code, exc.message)

        matches_last_check = (
            self._last_checked is not None
            and self._last_checked["date"] == date
            and self._last_checked["start_time"] == start_time
            and self._last_checked["duration_minutes"] == duration_minutes
            and self._last_checked["available"] is True
        )
        if not matches_last_check:
            return self._create_failed(
                "not_confirmed_available",
                "This exact slot was not just checked and confirmed available. "
                "Call check_calendar_availability for it first.",
            )

        # Whatever happens next, a fresh availability check is required before any further booking.
        self._last_checked = None
        try:
            result = calendar_service.create_event(date, start_time, duration_minutes, title)
        except calendar_service.CalendarError as exc:
            return self._create_failed(exc.code, exc.message)
        except Exception:
            logger.exception("tool_error create_calendar_event unexpected exception")
            return self._create_failed("creation_failed", "The calendar could not create the event.")

        if result.get("success") is not True:
            return self._create_failed("creation_failed", "The calendar did not confirm the event.")

        self._turn_tools.append("create_calendar_event:ok")
        self._turn_booking = (date, start_time, duration_minutes)
        logger.info("tool_result create_calendar_event status=ok")
        return result

    def _create_failed(self, code: str, message: str) -> dict:
        self._turn_tools.append(f"create_calendar_event:failed({code})")
        logger.warning("tool_result create_calendar_event status=failed error=%s", code)
        return {"success": False, "error": code, "message": f"The event was NOT created. {message}"}

    # ------------------------------------------------------------------ chat

    def send_message(self, text: str) -> str:
        """Send a friend's message to Sara and return her reply text."""
        self._turn_tools = []
        self._turn_booking = None
        logger.info("turn_start message_chars=%d", len(text))

        requested: list[tuple[str, str | None]] = []
        try:
            response = self._send(text, requested)
            if self._needs_tool_reminder(response.text or ""):
                logger.warning("turn_stalled: no tool was called but the reply announces a check or states availability")
                response = self._send(_TOOL_REMINDER, requested)
        except Exception:
            if self._turn_booking is None:
                raise
            # Google already confirmed the booking; only Gemini's final reply failed.
            logger.exception("Gemini failed after the event was booked; replying deterministically")
            return self._finalize_reply("")

        reply = response.text or ""
        if self._needs_tool_reminder(reply):
            logger.error("turn_stalled_after_reminder: replacing a reply that was never backed by a calendar check")
            reply = _MESSAGES["tool_failed"]
            self._rewrite_last_model_reply(reply)
        pending_calls = response.function_calls or []
        logger.info(
            "turn_end tools_requested=%d tools_ran=%s finish_reason=%s reply_chars=%d unexecuted_function_calls=%d",
            len(requested),
            ",".join(self._turn_tools) or "none",
            self._finish_reason(response),
            len(reply),
            len(pending_calls),
        )
        if len(requested) != len(self._turn_tools):
            logger.error(
                "tool_dispatch_failed: Gemini requested %d tool call(s) %s but %d ran inside Sara. "
                "The SDK rejected the call before it reached our code; Gemini was told: %s",
                len(requested),
                [name for name, _ in requested],
                len(self._turn_tools),
                [error for _, error in requested if error] or "n/a",
            )
        if pending_calls:
            logger.warning("Gemini returned function calls that were not executed: %s", [c.name for c in pending_calls])

        return self._finalize_reply(reply)

    def _send(self, text: str, requested: list[tuple[str, str | None]]) -> types.GenerateContentResponse:
        # The config is rebuilt on every call so "today"/"tomorrow" in the prompt never go stale.
        history_before = len(self._chat.get_history(curated=True))
        response = self._chat.send_message(text, config=self._chat_config())
        requested.extend(self._requested_tool_calls(response, skip=history_before))
        return response

    def _needs_tool_reminder(self, reply: str) -> bool:
        """True if Gemini skipped the calendar tool but talks as if it had checked.

        Either it announces a check and stops ("خليني أشيك") instead of calling the tool, or it
        states the owner's availability although no verified check exists in this conversation.
        """
        if self._turn_tools:
            return False
        if _ANNOUNCES_CHECK.search(reply) and not _ENDS_WITH_QUESTION.search(reply):
            return True
        return bool(_AVAILABILITY_CLAIM.search(reply)) and self._last_checked is None

    @staticmethod
    def _requested_tool_calls(response: types.GenerateContentResponse, skip: int) -> list[tuple[str, str | None]]:
        """Tool calls Gemini made this turn, with any SDK-level error returned to it for that call.

        The SDK's AFC history starts with the whole prior chat, so the first `skip` entries are ignored.
        """
        history = (response.automatic_function_calling_history or [])[skip:]
        parts = [p for c in history for p in (c.parts or [])]
        calls = [p.function_call.name or "?" for p in parts if p.function_call]
        results = [p.function_response.response or {} for p in parts if p.function_response]
        # Our tools always include "message" when they fail; an "error" without it came from the SDK.
        return [
            (name, result.get("error") if "error" in result and "message" not in result else None)
            for name, result in zip(calls, results, strict=False)
        ]

    @staticmethod
    def _finish_reason(response: types.GenerateContentResponse) -> str:
        if not response.candidates:
            block_reason = getattr(response.prompt_feedback, "block_reason", None)
            return f"no_candidates(block_reason={getattr(block_reason, 'name', block_reason)})"
        reason = response.candidates[0].finish_reason
        return getattr(reason, "name", str(reason))

    def _finalize_reply(self, reply: str) -> str:
        """Make sure the reply never contradicts what actually happened with the calendar."""
        if not reply.strip():
            logger.warning("Gemini returned no text; using a deterministic reply")
            if self._turn_booking:
                date, start_time, minutes = self._turn_booking
                return _MESSAGES["booked"].format(date=date, start_time=start_time, minutes=minutes)
            failed = any(":failed" in outcome for outcome in self._turn_tools)
            return _MESSAGES["tool_failed" if failed else "no_reply"]

        if self._turn_booking is None and _BOOKING_CLAIM.search(reply):
            logger.error("Blocked a reply claiming a booking although Google Calendar confirmed none this turn")
            failed = any(":failed" in outcome for outcome in self._turn_tools)
            safe_reply = _MESSAGES["tool_failed" if failed else "not_booked"]
            self._rewrite_last_model_reply(safe_reply)
            return safe_reply

        return reply

    def _rewrite_last_model_reply(self, new_text: str) -> None:
        """Keep Gemini's memory consistent with what the friend was actually told."""
        for curated in (True, False):
            history = self._chat.get_history(curated=curated)
            if history and history[-1].role == "model":
                history[-1] = types.Content(role="model", parts=[types.Part(text=new_text)])
