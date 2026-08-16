# firstsignal-voice

A personal phone line that rings your cell first and, when you miss the call,
hands the caller to an AI receptionist that takes a message and texts you a
summary.

Built on Twilio ConversationRelay + FastAPI. See the full plan for the roadmap.

## Status

- [x] **Step 1 — Scaffold + deploy.** FastAPI app, `/healthz`, Dockerfile, App Platform config.
- [x] **Step 2 — Dial-through.** `/voice` rings the cell; `/voice/after-dial` falls through when unanswered.
- [x] **Step 3 — Relay echo bot.** Missed calls hand off to `/ws`, which echoes what the caller says.
- [x] **Step 4 — LLM turn loop.** `/ws` streams Claude Haiku 4.5 replies token by token; barge-in cancels the in-flight turn.
- [x] **Step 5 — Capture + notify.** At call end the transcript is distilled into a structured message and texted to your cell via Twilio.
- [ ] Step 6 — Harden
- [ ] Step 7 — Call log (optional)

## Endpoints

| Method | Path                | Purpose                                                   |
|--------|---------------------|-----------------------------------------------------------|
| GET    | `/healthz`          | Health check for App Platform.                            |
| POST   | `/voice`            | Number's voice webhook. Connects the bot, or dials your cell first — see `ANSWER_MODE`. |
| POST   | `/voice/after-dial` | Dial callback (`dial-first` only). `completed` → hang up; else → connect bot. |
| WS     | `/ws`               | ConversationRelay turn loop. Streams Claude's replies, then captures the message and texts it to you. |
| POST   | `/voice/handoff`    | Relay session ended. Hangs up.                            |

At call end (the bot wraps up and ends the call, or the caller hangs up), the
transcript is distilled into a structured message — name, callback number,
reason, urgency — and texted to `NOTIFY_SMS_TO`. Requires a **SMS-capable**
Twilio number in `TWILIO_FROM_NUMBER`.

## Call flow

`ANSWER_MODE` picks which of the two arrangements you're running.

**`bot` (default)** — people dial your cell, and the carrier forwards to the
Twilio number when you don't pick up. The caller has already sat through the
ringing, so the assistant answers immediately. Set this up with your carrier's
conditional-forwarding codes (no-answer / busy / unreachable), pointing at the
Twilio number. Don't point *unconditional* forwarding at Twilio — the bot would
answer every call, including the ones you would have taken.

**`dial-first`** — Twilio is the number you hand out. `/voice` rings
`FORWARD_TO_NUMBER` for `DIAL_TIMEOUT` seconds, then falls through to the bot.
The **carrier voicemail race** applies here: keep `DIAL_TIMEOUT` at 15s and
disable/delay carrier voicemail on the cell so Twilio's no-answer fallback wins.

The modes don't compose — in `dial-first`, Twilio calls the cell, and if the
cell is also forwarding back to Twilio on no-answer you get a loop.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8080
```

Or with Docker:

```bash
docker compose up --build
```

Run tests:

```bash
pip install pytest httpx
pytest
```

## Configuration

Copy `.env.example` to `.env`. Steps 1–2 only need:

| Var                | Default        | Meaning                                |
|--------------------|----------------|----------------------------------------|
| `ANSWER_MODE`      | `bot`          | `bot` answers immediately; `dial-first` rings your cell first. See Call flow. |
| `FORWARD_TO_NUMBER`| `+18084649192` | Your cell, E.164. `dial-first` only.   |
| `DIAL_TIMEOUT`     | `15`           | Seconds before falling through to bot. `dial-first` only. |
| `PUBLIC_DOMAIN`    | `sparkal.ai`   | Public host; baked into the `wss://` relay URL. Restart on change. |
| `TTS_PROVIDER`     | `Google`       | Twilio TTS provider: `Google`, `Amazon`, or `ElevenLabs` (key required in Console). |
| `TTS_VOICE`        | `en-US-Journey-F` | Voice ID for the provider. Baked into TwiML — restart on change. |
| `ANTHROPIC_API_KEY`| —              | Required from step 4 on for the bot to talk.          |
| `LLM_PRIMARY`      | `claude-haiku-4-5` | Voice model. Reasoning stays off for low latency. |
| `LLM_MAX_TOKENS`   | `200`          | Cap per reply — replies are spoken, so keep them short. |
| `TWILIO_ACCOUNT_SID` | —            | Twilio auth for sending the summary SMS.              |
| `TWILIO_AUTH_TOKEN`  | —            | Twilio auth for sending the summary SMS.              |
| `TWILIO_FROM_NUMBER` | —            | SMS-capable Twilio number the summary is sent from.   |
| `NOTIFY_SMS_TO`    | `+18084649192` | Where the summary text is delivered.                  |

## Deploy — DigitalOcean App Platform

1. Push to GitHub and point an App Platform app at the repo (or use `.do/app.yaml`).
2. It builds from the `Dockerfile` and serves on port 8080.
3. Set env vars; health check is `GET /healthz`.
4. Grab the public domain and set the number's Voice webhook to
   `https://<domain>/voice` (HTTP POST) in the Twilio console.
