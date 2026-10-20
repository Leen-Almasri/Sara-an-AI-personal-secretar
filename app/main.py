"""FastAPI application: serves the web UI and the chat API."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.genai import errors as genai_errors
from pydantic import BaseModel

from app.agent import SaraAgent

logger = logging.getLogger(__name__)

app = FastAPI(title="Sara - AI Personal Secretary")

# In-memory session store: session_id -> SaraAgent.
# This is intentionally simple for the MVP (single process, no persistence).
# See README.md "Future improvements" for scaling this beyond a demo.
_sessions: dict[str, SaraAgent] = {}


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="اكتب رسالتك أول 😊")
    if len(message) > 2000:
        raise HTTPException(status_code=400, detail="رسالتك طويلة مرة، اختصرها شوي.")

    session_id = request.session_id or str(uuid.uuid4())
    agent = _sessions.get(session_id)
    if agent is None:
        agent = SaraAgent()
        _sessions[session_id] = agent

    try:
        reply = agent.send_message(message)
    except genai_errors.APIError as exc:
        logger.error("Gemini API error (HTTP %s) for session %s", exc.code, session_id)
        if exc.code == 429:
            raise HTTPException(
                status_code=429,
                detail="عندي ضغط طلبات الحين. جرب مرة ثانية بعد دقيقة تقريبًا 🙏",
            ) from None
        raise HTTPException(
            status_code=502,
            detail="صار عندي خلل بسيط الحين. جرب مرة ثانية بعد شوي 🙏",
        ) from None
    except Exception:
        logger.exception("Agent failed to handle message for session %s", session_id)
        raise HTTPException(
            status_code=502,
            detail="صار عندي خلل بسيط الحين. جرب مرة ثانية بعد شوي 🙏",
        ) from None

    return ChatResponse(reply=reply)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse("static/index.html")
