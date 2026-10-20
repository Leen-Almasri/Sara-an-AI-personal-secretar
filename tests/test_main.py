"""API-level error mapping. The agent is faked; no Gemini or Google calls are made."""

import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from google.genai import errors

from app import main


class FailingAgent:
    def __init__(self, error: Exception):
        self.error = error

    def send_message(self, text: str) -> str:
        raise self.error


@pytest.fixture
def client():
    main._sessions.clear()
    yield TestClient(main.app)
    main._sessions.clear()


def post(client: TestClient, error: Exception):
    with patch.object(main, "SaraAgent", lambda: FailingAgent(error)):
        return client.post("/api/chat", json={"session_id": "s1", "message": "hi"})


def test_gemini_quota_error_gets_a_specific_retry_message(client):
    response = post(client, errors.ClientError(429, {"error": {"message": "quota exceeded"}}))
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert "بعد دقيقة" in detail
    assert not re.search("[A-Za-z]", detail)  # Arabic only, no raw provider text


def test_other_gemini_errors_map_to_a_generic_502_without_details(client):
    response = post(client, errors.ServerError(503, {"error": {"message": "internal detail"}}))
    assert response.status_code == 502
    assert "internal detail" not in response.json()["detail"]
    assert not re.search("[A-Za-z]", response.json()["detail"])


def test_unexpected_errors_map_to_a_generic_502(client):
    response = post(client, RuntimeError("secret internals"))
    assert response.status_code == 502
    assert "secret" not in response.json()["detail"]
