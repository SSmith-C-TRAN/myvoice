# ConversationRelay speaker events — wire shapes

Phase 0 note for the "fix speaking & silence detection" workplan.

## Why this note exists

Twilio's WebSocket message reference documents only the inbound `setup`,
`prompt`, `dtmf`, `interrupt`, and `error` messages. The `speaker-events` and
`tokens-played` subscriptions are named in the `<ConversationRelay>` attribute
table, but the JSON they produce is not published anywhere in the docs. We
need those shapes before we can anchor silence detection to them, so this note
records what they are.

## Status of this note — read before trusting it

**These shapes were not captured from a call on this number.** The workplan
asked for an empirical spike; that step needs someone to dial in, and it has
not been run. What follows is instead cross-referenced from four independent
implementations, two of which are Twilio-maintained. They agree exactly, which
is strong evidence but is not the same as having watched our own traffic.

| Source | What it shows |
|---|---|
| [`twilio-demos/twilio-aws-blueprint`](https://github.com/twilio-demos/twilio-aws-blueprint) `src/websocket/conversationrelayhandler.py` | Twilio-maintained. Branches on `message.name == "clientSpeaking" and message.value == "on"`, and `message.name == "agentSpeaking" and message.value == "off"` — and uses exactly those two to drive idle detection. |
| [`danbartlett-twilio/ConversationRelay-WebRTC-Demo`](https://github.com/danbartlett-twilio/ConversationRelay-WebRTC-Demo) `client/src/ui-components/Transcription/Transcript.js` | Twilio employee. Reads `data.type === "info"`, `data.name`, `data.value === "on"/"off"`, and `data.ts` (epoch ms) to compute agent-response latency. |
| [`pBread/twilio-signal-2025-keynote-demo`](https://github.com/pBread/twilio-signal-2025-keynote-demo) | Twilio employee. Types `TokensPlayedMessage` as `{type: "info", name: "tokensPlayed", value: string}` and filters with `ev.type === "info" && ev.name === "tokensPlayed"`. |
| [`JacksonFalgoust/AI-Receptionist`](https://github.com/JacksonFalgoust/AI-Receptionist) `app/speaker_events.py` | Community. Comments record the live wire shape observed on real calls (2026-07-09) as `{"type": "info", "name": "agentSpeaking", "value": "on"\|"off"}`. |

The code in `app/relay.py` is written to degrade safely if any of this is
wrong on our account — see "If the shapes are wrong" below.

## The shapes

All four event kinds arrive on one envelope, `type: "info"`, discriminated by
`name`:

```json
{"type": "info", "name": "agentSpeaking",  "value": "on",  "ts": 1752091234567}
{"type": "info", "name": "agentSpeaking",  "value": "off", "ts": 1752091238901}
{"type": "info", "name": "clientSpeaking", "value": "on",  "ts": 1752091240110}
{"type": "info", "name": "clientSpeaking", "value": "off", "ts": 1752091243502}
{"type": "info", "name": "tokensPlayed",   "value": "Hi, this is Jace,"}
```

- `value` is the string `"on"` or `"off"` — not a boolean, and not a
  `start`/`stop` word. One `name` per speaker with a direction flag, rather
  than separate start and stop message types.
- `ts` is epoch milliseconds. Present on the speaker events; we don't use it
  (we timestamp on arrival), but it's there if playback-latency measurement is
  ever wanted.
- `tokensPlayed` carries the text just played, not a speaker state. We
  subscribe to `speaker-events` only — `agentSpeaking: off` already tells us
  what we need, and `tokensPlayed` fires per chunk, which is a lot of traffic
  on a long reply. Set `RELAY_EVENTS="speaker-events tokens-played"` to turn
  it on for a spike; unrecognized `info` names are logged at DEBUG and ignored.

## The Phase 0 questions

- **Agent start vs. stop, caller start vs. caller stop.** One `name` per
  speaker, `value` carrying the direction. Answered.
- **Does `clientSpeaking` fire while the agent is speaking?** Not established.
  It stopped mattering — see the decision below.
- **Barge-in: `agentSpeaking: off`, `interrupt`, or both?** Not established.
  `relay.py` treats `interrupt` as ending agent speech on its own, so either
  ordering lands in the same state.
- **Does the "Are you still there?" nudge produce its own start/stop pair?**
  Not established. It's ordinary TTS, so it should. If it doesn't, the
  `touch()` inside `say()` still holds the clock for one full allowance, so
  the worst case is a nudge and goodbye arriving closer together than intended
  — not a nudge over live audio.

## The `reportInputDuringAgentSpeech` decision

**Left at the default `none`.** The workplan flagged setting it to `"speech"`
if `clientSpeaking` turns out to be suppressed during agent speech, to catch
overlap. That turns out not to be needed: `agent_speaking` already suppresses
the countdown by itself, so a caller talking over the bot is inside a window
where the timer is stopped regardless of whether we can see them. Setting it
to `"speech"` would buy nothing for silence detection and would start
delivering `prompt` messages that did *not* interrupt playback — backchannel
like "mhm" would spawn a turn while TTS is still going, which is the
talking-over-itself bug from the other direction. Twilio's own blueprint drives
idle detection off these events and does not set the attribute either.

## If the shapes are wrong

`app/relay.py` never assumes an event will arrive:

- An `info` message whose `value` is neither on-ish nor off-ish is logged as a
  warning and **ignored**, rather than being coerced into a state change.
- If the events never arrive at all, `agent_speaking` stays False, the
  countdown runs as it does today, and `end_call` falls back to waiting out
  `END_GRACE_SECONDS` — i.e. current behavior, not worse.
- If an `off` event is dropped, a stuck flag would hold the call open forever,
  so `MAX_AGENT_SPEECH_SECONDS` / `MAX_CALLER_SPEECH_SECONDS` clear it and log
  a warning.

The log line to look for on a real call is `speaker event:` at INFO. If a call
produces none of those, the subscription isn't reaching us and the fallbacks
above are what's running.
