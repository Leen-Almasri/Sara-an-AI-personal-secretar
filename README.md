# Sara AI Personal Secretary

Sara is an AI personal secretary that sits between your friends and your Google Calendar.
You share a link; a friend chats with Sara in a web page; Sara checks your **real** calendar,
proposes free times, and creates the event only after the friend explicitly confirms. She
never reveals what else is on your calendar.

It is a reusable open-source project: bring your own **Gemini API key** and your own **Google
Calendar OAuth credentials** (see [Get your credentials](#get-your-credentials)), and Sara manages
*your* calendar. As shipped, Sara replies in conversational Saudi Arabic; see
[Make it yours](#make-it-yours) to change the language, dialect or tone.

## Contents

- [What Sara does](#what-sara-does)
- [Quick start (Windows PowerShell)](#quick-start-windows-powershell)
- [Get your credentials](#get-your-credentials)
- [Environment variables](#environment-variables)
- [Run it](#run-it)
- [Troubleshooting](#troubleshooting)
- [Make it yours](#make-it-yours)
- [How it works](#how-it-works)
- [Example conversation](#example-conversation)
- [Deployment](#deployment)
- [Privacy and security](#privacy-and-security)
- [Future improvements](#future-improvements)

## What Sara does

- Chats in a simple web page, understands Arabic and English, and **always replies in
  conversational Saudi Arabic**.
- Introduces herself, asks the friend's name once, and remembers it for the conversation.
- Resolves dates such as "tomorrow", "بكرة", "next Thursday", "الخميس الجاي" and times such as
  "8 صباحًا" or "9 مساءً" in your timezone, and asks when something is ambiguous.
- Checks real availability in Google Calendar **before** saying a time is free or busy.
- Asks the friend to confirm a specific time, then creates the calendar event.
- If the time is busy, offers up to 3 free alternatives, without revealing event titles,
  attendees, descriptions or locations.
- If the calendar cannot be reached, says so honestly and books nothing.

## Quick start (Windows PowerShell)

### Prerequisites

- **Git** and **Python 3.12** (the version this project is tested with; its Docker image
  uses 3.11). Check them:

  ```powershell
  git --version
  python --version
  ```

- A **Gemini API key** and a **Google account** with a calendar (see
  [Get your credentials](#get-your-credentials)).

### 1. Clone and enter the project

```powershell
git clone https://github.com/<owner>/<repo>.git
cd <repo>
```

Replace `<owner>/<repo>` with the address of this repository.

### 2. Create a virtual environment and install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell refuses to run `Activate.ps1` ("running scripts is disabled"), allow scripts
for this window only and activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Your prompt shows `(.venv)` when the environment is active. (On macOS/Linux use
`python3 -m venv .venv` and `source .venv/bin/activate`; everything else is the same.)

### 3. Create your `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

Fill in the placeholders using the next section. `.env` is listed in `.gitignore` so it is
never committed. **Never share it or paste it anywhere public.**

### 4. Run

```powershell
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. See [Run it](#run-it) for more.

## Get your credentials

You need four values: `GEMINI_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and
`GOOGLE_REFRESH_TOKEN`. Everything else has a default.

### Gemini API key

1. Go to <https://aistudio.google.com/apikey> and sign in.
2. Create an API key and copy it.
3. Put it in `.env` as `GEMINI_API_KEY`.

`GEMINI_MODEL` is optional; if unset the app uses `gemini-3.5-flash-lite`. Free-tier keys have
low request-per-minute limits (see [Troubleshooting](#troubleshooting)).

### Google Calendar (OAuth)

The app talks to **one** Google Calendar (the account that authorizes it). It uses an OAuth
"refresh token" so it can reach the calendar without anyone signing in again. You create the
token once, locally.

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create a project
   (or pick one).
2. **Enable the API:** *APIs & Services → Library → "Google Calendar API" → Enable*.
3. **Configure the consent screen:** *APIs & Services → OAuth consent screen* (called
   *Google Auth Platform* in newer consoles). Choose **External**, fill in the required app
   name and email fields, and while the app is in **Testing**, add your own Google account
   under **Test users**.
4. **Create the credentials:** *APIs & Services → Credentials → Create credentials →
   OAuth client ID → Application type: **Desktop app***. Copy the **Client ID** and **Client
   secret** into `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
5. **Authorize once.** The helper script reads the client ID and secret from *environment
   variables* (it does not read `.env`), so set them for this PowerShell window and run it:

   ```powershell
   $env:GOOGLE_CLIENT_ID = "your_google_client_id_here"
   $env:GOOGLE_CLIENT_SECRET = "your_google_client_secret_here"
   python scripts/get_refresh_token.py
   ```

   A browser opens. Sign in with the Google account whose calendar Sara should manage and
   allow calendar access (Google may warn that the app is unverified; this is your own app,
   so continue). The terminal then prints `GOOGLE_REFRESH_TOKEN=...`. Copy the value into
   `.env`.

6. Clear the secrets from your PowerShell session:

   ```powershell
   Remove-Item Env:GOOGLE_CLIENT_ID, Env:GOOGLE_CLIENT_SECRET
   ```

7. `GOOGLE_CALENDAR_ID` defaults to `primary` (the authorized account's main calendar). To use
   another calendar, copy its ID from *Google Calendar → Settings → your calendar →
   Integrate calendar*.

> **Refresh tokens can expire.** While the consent screen is in **Testing**, Google expires
> the refresh token after 7 days and Sara will report that the calendar needs re-authorizing.
> For a long-running setup, set the consent screen's publishing status to **In production**
> and run the script again.

## Environment variables

Defined in `app/config.py`. The first four are required; the app refuses to start without them.

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | yes | – | Gemini API key |
| `GOOGLE_CLIENT_ID` | yes | – | OAuth client ID (Desktop app) |
| `GOOGLE_CLIENT_SECRET` | yes | – | OAuth client secret |
| `GOOGLE_REFRESH_TOKEN` | yes | – | Printed by `scripts/get_refresh_token.py` |
| `GEMINI_MODEL` | no | `gemini-3.5-flash-lite` | Gemini model name |
| `GOOGLE_CALENDAR_ID` | no | `primary` | Calendar to check and book |
| `TIMEZONE` | no | `Asia/Riyadh` | IANA timezone for all dates and times |
| `WORK_START_HOUR` | no | `9` | Earliest hour used when suggesting alternatives |
| `WORK_END_HOUR` | no | `21` | Latest hour (suggestions end by then); `WORK_START_HOUR < WORK_END_HOUR <= 24` |
| `DEFAULT_EVENT_DURATION_MINUTES` | no | `30` | Duration Sara assumes when the friend gives none |
| `LOG_LEVEL` | no | `INFO` | Python logging level |

Keep these private. `.env.example` contains only placeholders and is safe to commit.

## Run it

```powershell
uvicorn app.main:app --reload
```

| URL | What it is |
|---|---|
| <http://127.0.0.1:8000/> | The chat page |
| <http://127.0.0.1:8000/api/health> | Health check, returns `{"status":"ok"}` |
| `POST /api/chat` | The chat API used by the page (`{"session_id": "...", "message": "..."}`) |

Run the tests (no real Gemini or Google calls are made, and no real events can be created):

```powershell
python -m pytest
```

To let friends reach it you must [deploy it](#deployment); `127.0.0.1` is only reachable
from your own computer.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `RuntimeError: Missing required environment variable '...'` | `.env` is missing, not in the project root, or the variable is empty. Re-do step 3 and run from the project root. |
| `TIMEZONE '...' is not a valid IANA timezone name` | Use a name like `Asia/Riyadh` or `Europe/London`. |
| `ModuleNotFoundError: No module named 'app'` | Run from the project root (the folder containing `app/`) with the virtual environment active. For tests use `python -m pytest`; plain `pytest` cannot find the `app` package. |
| `Activate.ps1 ... running scripts is disabled` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again. |
| `Address already in use` | Another program uses port 8000. Run `uvicorn app.main:app --reload --port 8001`. |
| The page loads but Sara says she is busy ("عندي ضغط طلبات…") or returns HTTP 429 | Gemini quota. Free keys allow few requests per minute and one chat message needs at least two. Wait a minute or use a key with higher limits. |
| Sara says she "could not verify" the calendar | Check the server log for `tool_result ... error=<code>`. `calendar_auth_failed` / `invalid_grant`: the refresh token expired or was revoked, run `scripts/get_refresh_token.py` again. `calendar_not_found`: check `GOOGLE_CALENDAR_ID`. `calendar_permission_denied`: the authorized account cannot write to that calendar. |
| `Google did not return a refresh token` | Remove this app's access at <https://myaccount.google.com/permissions> and run the script again. |
| Google shows "Access blocked" while authorizing | Add your Google account under *Test users* on the OAuth consent screen. |
| Sara answers but never checks the calendar | Look for `tools_requested=0` in the log. See "Reading the logs" below. |

### Reading the logs

The server logs every tool call and every message turn, without friend messages, event titles,
keys, tokens or calendar contents:

```text
tool_call check_calendar_availability date='2026-10-13' start_time='21:00' duration_minutes=30
tool_result check_calendar_availability status=ok available=True alternatives=0 error=None
turn_end tools_requested=1 tools_ran=check_calendar_availability:ok finish_reason=STOP ...
```

- `tools_requested=0 tools_ran=none`: Gemini answered without calling a tool.
- `tools_ran=...:failed(<code>)`: the tool ran and failed; the code says why.
- `tool_dispatch_failed`: the SDK rejected the tool call before it reached Sara's code. Do not
  add `from __future__ import annotations` to `app/agent.py`; it causes exactly this.
- `turn_stalled`: Gemini said "let me check" (or stated availability) without calling the
  calendar tool. Sara sends one internal reminder; if that fails the reply is replaced with an
  honest message.
- `Blocked a reply claiming a booking`: Gemini claimed a booking Google Calendar did not
  confirm; the reply was replaced.

## Make it yours

Sara's wording is plain text in a few places. To change the language, dialect, greeting or tone,
edit only these:

| File | What to change |
|---|---|
| `app/prompts.py` | Sara's personality and rules: dialect, greeting, humor |
| `static/script.js` | The opening greeting bubble and the error texts shown in the page |
| `static/index.html` | The page title and header subtitle |
| `app/agent.py` | `_MESSAGES`: the fixed safety replies (used when Sara's own reply must be replaced) |
| `app/main.py` | Error messages returned by the API |

If you change the language, keep the scheduling rules in `app/prompts.py` (sections "Hard rules"
and "Workflow"); the tests in `tests/test_prompts.py` check that they are still present. The
safety checks in `app/agent.py` that detect booking claims and "let me check" stalls match
Arabic wording, so extend `_BOOKING_CLAIM`, `_ANNOUNCES_CHECK` and `_AVAILABILITY_CLAIM`
if Sara will answer in another language.

## How it works

```text
Friend's browser
      │  POST /api/chat { session_id, message }
      ▼
FastAPI backend (app/main.py)
      ▼
SaraAgent (app/agent.py): one Gemini chat per browser session, system prompt from app/prompts.py
      │  function calling
      ▼
Gemini ──► check_calendar_availability / create_calendar_event
      ▼
app/calendar.py ── Google Calendar API (OAuth2 refresh token)
      ▼
Your Google Calendar
```

- Gemini never receives Google credentials. It only sees what the two tools return.
- `create_calendar_event` is gated on the server: it runs only if the exact date, time and
  duration were just verified as available, and it re-checks the live calendar right before
  inserting. Google Calendar has no atomic "insert if free", so an event added by another
  client in that instant could still overlap, and bookings are only serialized within one
  server process (run a single worker).
- Sara only tells a friend an event is booked when Google Calendar confirmed it in that same
  turn; a reply claiming otherwise is replaced.
- Chat sessions are kept in memory (no database), so a restart makes Sara forget conversations
  in progress. Events already created live in Google Calendar.
- Alternatives are limited to `WORK_START_HOUR`–`WORK_END_HOUR`; a friend's requested time is
  judged by your calendar alone.

Project layout:

```text
app/
  main.py         FastAPI app: chat page, /api/chat, /api/health
  agent.py        SaraAgent: Gemini chat, the two tools, safety checks, logging
  calendar.py     Google Calendar access, input validation, alternatives
  prompts.py      Sara's system prompt
  config.py       Reads environment variables
static/           index.html, style.css, script.js (the chat page)
scripts/
  get_refresh_token.py   One-time Google authorization helper
tests/            pytest suite (Gemini scripted, Google Calendar mocked)
.env.example      Placeholder configuration (safe to commit)
Dockerfile        Container image (listens on port 7860)
render.yaml       Render Blueprint
requirements.txt  Python dependencies
```

## Example conversation

Sara answers in Arabic as shipped. The friend may write in Arabic or English:

```text
Sara:   هلا والله! أنا سارة، السكرتيرة الشخصية الذكية. وش اسمك؟ 😊
Friend: أنا أحمد
Sara:   يا هلا أحمد! وش تبي أنسق لك؟
Friend: Can I book a call tomorrow at 9 PM for 30 minutes?
Sara:   للأسف الوقت هذا مو متاح في الجدول. عندي لك 9:30 أو 10، وش يناسبك؟
Friend: 9:30
Sara:   الجدول فاضي بكرة 9:30 بالليل لمدة نص ساعة. أأكد لك الحجز؟
Friend: أيوه
Sara:   أبشر، تم الحجز ✅
```

If the calendar cannot be reached, Sara says so and books nothing:

```text
Sara:   ما قدرت أتأكد من الجدول الحين، وما انحجز أي شي. جرب مرة ثانية بعد شوي 
```

## Deployment

You need the same variables as locally, set as **secrets in the host's dashboard**, never in
the repository.

- **Render (free plan):** `render.yaml` defines a Python web service
  (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`, health check `/api/health`). Push the
  repository to GitHub, then *New → Blueprint* in the Render dashboard, connect the repo and
  enter the secret values when prompted (`GEMINI_API_KEY`, `GEMINI_MODEL`, `GOOGLE_CLIENT_ID`,
  `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GOOGLE_CALENDAR_ID`). Free services sleep
  after 15 minutes without traffic and take about a minute to wake, and in-memory chats are lost
  when they sleep.
- **Hugging Face Spaces (Docker):** create a Space with the Docker SDK and push this repository.
  The `Dockerfile` listens on port 7860. Add the variables above under *Settings → Variables
  and secrets*.

## Privacy and security

- No credentials are stored in the code. Secrets come from environment variables
  (`.env` locally, the host's secret store when deployed). `.gitignore` excludes `.env`,
  credential and token files, keys and the virtual environment.
- Never commit or share `.env`, the client secret, the refresh token or your API key. If any
  of them leaks: delete the key in Google AI Studio, revoke the app at
  <https://myaccount.google.com/permissions>, reset the OAuth client secret in Google Cloud
  Console, then create new values.
- Friends only learn "available", "unavailable" and suggested times, never what is on your
  calendar. The logs contain no friend messages, event titles or calendar contents.
- Your Google account name and email may appear in Git commit history. Check
  `git log --format="%an <%ae>"` before publishing a repository.

## Future improvements

- Persist sessions (for example in Redis) so conversations survive restarts.
- Support several calendars or users.
- Rate limiting and abuse protection on the public endpoint.
- Make the reply language and dialect configurable by environment variable instead of editing the prompt.
