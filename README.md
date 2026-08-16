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

## Ending the call

Three ways a call ends. The bot decides it's done and appends a silent
`[[END]]` marker to its goodbye; the caller hangs up; or the caller goes quiet.

Silence needs its own handling because a caller who says nothing sends no
WebSocket messages at all — with no message there's no turn, and the LLM never
gets a chance to hang up. The relay loop therefore waits on the clock as well
as the socket. For a caller who never speaks, the timeline runs:

| At    | What happens                                          |
|-------|-------------------------------------------------------|
| 0s    | Greeting plays. The clock is stopped while it does     |
| +10s  | "Are you still there?" — 10s after the greeting *finishes* |
| +10s  | "Alright, I'll let you go. Thanks for calling. Goodbye!" |
| —     | Hangup, the moment the goodbye finishes playing         |

The clock only runs when the call is genuinely idle: no TTS playing, no caller
talking, no reply being generated. That's not estimated from how long the text
is — Twilio reports when the agent and the caller each start and stop, and the
relay subscribes to those events with `events="speaker-events"` on the
`<ConversationRelay>` element. It matters because generating a 200-token reply
takes about 2 seconds while *speaking* it takes up to 40, so a clock started
when generation ended would fire "Are you still there?" over the tail of the
bot's own answer. The hangup keys off the same events, which is why it lands
right as the goodbye ends instead of after a fixed pause.

Any caller speech resets the clock and re-arms the nudge. Tune with
`SILENCE_PROMPT_SECONDS` / `SILENCE_HANGUP_SECONDS`. The first is thinking
time — how long a caller gets to answer a question — so it's the more patient
of the two.

If the speaker events don't arrive, the relay falls back to estimating
playback from the length of what it sent, which is a guess but a much better
one than assuming silence. The first real event switches the guess off for
good. Every call logs which mode it ran in:

```
call summary: call=CAxxx speaker_events=live nudges=0 turns=4
```

`speaker_events=NONE` there means the subscription isn't reaching us — check
the `events` attribute in the TwiML. Twilio doesn't publish the JSON shape of
these messages; see [docs/conversationrelay-events.md](docs/conversationrelay-events.md)
for what they look like and how confident we are.

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
| `LOG_LEVEL`        | `INFO`         | `INFO` logs a line per turn and a summary per call; `DEBUG` adds unrecognized WebSocket messages. |
| `TTS_PROVIDER`     | `Google`       | Twilio TTS provider: `Google`, `Amazon`, or `ElevenLabs` (key required in Console). |
| `TTS_VOICE`        | `en-US-Journey-F` | Voice ID for the provider. Baked into TwiML — restart on change. |
| `RELAY_EVENTS`     | `speaker-events` | Twilio event subscriptions, space-separated. `speaker-events` is load-bearing — see Ending the call. Add `tokens-played` to debug playback. |
| `SILENCE_PROMPT_SECONDS` | `10`     | Real seconds of dead air before "Are you still there?" |
| `SILENCE_HANGUP_SECONDS` | `10`     | More dead air after that, then goodbye and hang up. |
| `END_GRACE_SECONDS`| `8`            | Fallback only: longest we'll wait for the goodbye's stop event before hanging up regardless. Not a pause callers normally hear. |
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
