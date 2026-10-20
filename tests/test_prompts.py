"""Static checks on Sara's system prompt and the page's opening greeting.

These cannot prove how the model behaves (that was checked with live Gemini runs); they make
sure the rules that drive that behavior are present and consistent, and that none of the
scheduling-safety rules were dropped.
"""

import pathlib
import re

import pytest

from app.agent import SaraAgent
from app.prompts import SARA_SYSTEM_PROMPT_TEMPLATE, build_system_prompt

GREETING = "هلا والله! أنا سارة، السكرتيرة الشخصية الذكية. وش اسمك؟ 😊"
LEVANTINE = ["شو", "هلق", "بدي", "كتير", "لسا", "هيك", "تحكيلي", "ليش"]
ARABIC_LETTER = re.compile("[" + chr(0x600) + "-" + chr(0x6FF) + "]")
SCRIPT_JS = pathlib.Path(__file__).resolve().parent.parent / "static" / "script.js"


def flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(scope="module")
def prompt() -> str:
    return flat(build_system_prompt("Monday, 2026-10-12 10:00 (tomorrow is Tuesday, 2026-10-13)", "Asia/Riyadh", 30))


def quoted_arabic_examples(text: str) -> list[str]:
    return [q for q in re.findall(r'"([^"]+)"', text) if ARABIC_LETTER.search(q)]


def test_prompt_renders_without_leftover_placeholders(prompt):
    assert "{" not in prompt and "}" not in prompt
    assert "Asia/Riyadh" in prompt and "tomorrow is Tuesday, 2026-10-13" in prompt
    assert "use 30 minutes" in prompt


def test_template_has_exactly_the_expected_placeholders():
    assert set(re.findall(r"\{(\w+)\}", SARA_SYSTEM_PROMPT_TEMPLATE)) == {
        "current_datetime",
        "timezone",
        "default_duration",
    }


def test_agent_uses_the_prompt_in_its_gemini_config():
    assert "SAUDI Arabic" in SaraAgent._build_system_prompt()


def test_replies_are_always_arabic_even_for_english_input(prompt):
    assert "Reply ONLY in Arabic, no matter what language the friend writes in" in prompt
    assert "If the friend writes English, understand it and answer in Arabic" in prompt
    assert "Never write a full sentence in English" in prompt
    # The old "mirror the friend's language" rule must be gone.
    assert "English message ->" not in prompt and "English reply" not in prompt


def test_prompt_demands_saudi_dialect_and_excludes_levantine(prompt):
    assert "SAUDI Arabic" in prompt and "لهجة سعودية" in prompt
    assert "Levantine/Jordanian" in prompt  # named only in the "do not use" list
    assert "Gulf/Levantine" not in prompt
    # None of the example sentences the model will imitate may be Levantine.
    examples = quoted_arabic_examples(prompt)
    assert len(examples) >= 8
    for example in examples:
        for word in LEVANTINE:
            assert word not in example.split(), f"Levantine word {word!r} in example {example!r}"


def test_opening_greeting_introduces_sara_and_asks_for_the_name(prompt):
    assert GREETING in prompt
    assert "السكرتيرة الشخصية الذكية" in GREETING and "وش اسمك" in GREETING
    assert "ask for their name" in prompt
    assert "BEFORE doing any scheduling" in prompt
    assert "Ask for the name only once" in prompt
    assert "never ask again" in prompt
    assert "Introduce yourself only once" in prompt


def test_the_pages_welcome_bubble_is_the_same_saudi_greeting_and_ui_errors_are_arabic():
    script = SCRIPT_JS.read_text(encoding="utf-8")
    assert GREETING in script
    for old in ("تحكيلي", "Hi! I'm Sara", "Something went wrong", "couldn't respond"):
        assert old not in script


def test_humor_is_generic_sparing_and_never_replaces_the_calendar_check(prompt):
    section = prompt[prompt.index("## Humor and personal details") : prompt.index("## Dates and times")]
    assert "sparingly" in section
    assert "never instead of a needed question or the calendar check" in section
    assert "Never use humor to suggest that the owner is free or busy" in section
    assert "Do not invent personal habits, preferences, places, past events, or inside jokes" in section


@pytest.mark.parametrize(
    "required",
    [
        "`check_calendar_availability`",
        "`create_calendar_event`",
        "You do NOT know whether the owner is free",
        "BEFORE telling them anything about availability",
        '`"verified": false`',
        "could not verify",
        "Do not say the slot is free, do not say it is busy",
        "Never invent alternatives",
        "NEVER call `create_calendar_event` until",
        "explicitly confirmed",
        'Only tell the friend an event is booked if `create_calendar_event` returned `"success": true`',
        "If you did not call the tool, it is not booked",
        "NEVER reveal private calendar details",
        "ask one short clarifying question",
        "9 الصبح ولا 9 بالليل",
        "8 صباحًا",
        "tomorrow",
        "بكرة",
    ],
)
def test_scheduling_safety_rules_are_still_in_force(prompt, required):
    assert required in prompt


def test_prompt_does_not_invite_guessing_from_working_hours(prompt):
    assert "normal available hours" not in prompt
    assert "typical working hours" in prompt  # only inside the "never infer from" rule
