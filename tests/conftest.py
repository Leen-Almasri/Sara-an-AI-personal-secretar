"""Test fixtures.

Dummy (non-functional) values are forced into the environment *before* any app
module is imported, so tests can never pick up real credentials or the
developer's real .env settings (python-dotenv does not override existing
variables). A guard fixture also makes any un-mocked Google API access fail
loudly, so a test can never touch a real calendar.
"""

import os

TEST_ENV = {
    "GEMINI_API_KEY": "test-gemini-key",
    "GOOGLE_CLIENT_ID": "test-client-id",
    "GOOGLE_CLIENT_SECRET": "test-client-secret",
    "GOOGLE_REFRESH_TOKEN": "test-refresh-token",
    "GOOGLE_CALENDAR_ID": "primary",
    "TIMEZONE": "Asia/Amman",
    "WORK_START_HOUR": "9",
    "WORK_END_HOUR": "21",
    "DEFAULT_EVENT_DURATION_MINUTES": "30",
}
os.environ.update(TEST_ENV)

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402


class RealGoogleAccessError(BaseException):
    """BaseException so the app's `except Exception` handlers cannot swallow it."""


@pytest.fixture(autouse=True)
def block_real_google_access():
    with patch("app.calendar._get_service", side_effect=RealGoogleAccessError("Real Google service used in a test")):
        yield
