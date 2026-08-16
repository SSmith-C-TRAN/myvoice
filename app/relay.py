"""ConversationRelay WebSocket handler.

Twilio owns the audio, STT, and TTS; we own the brain. On each finalized caller
utterance we stream Claude's reply back token by token so TTS starts before the
full answer is ready. A barge-in (`interrupt`) cancels the in-flight generation.

Ending a call (step 5): the bot appends a silent `[[END]]` marker to its final
goodbye when it has what it needs. We strip the marker (never speak it), send
`end`, and Twilio closes the socket. Whether the bot ends the call or the
caller simply hangs up, both paths land in the disconnect handler, where
`finalize` turns the transcript into a message and texts it to you.

Silence: a caller who says nothing sends us nothing — no message means no turn
means the LLM never gets a chance to hang up. So the receive loop watches the
clock as well as the socket: a stretch of quiet earns one "are you still
there?", and a second stretch ends the call with a goodbye.

What makes that clock trustworthy is `events="speaker-events"` on the TwiML.
Twilio then tells us when TTS and the caller actually start and stop, and we
count silence only while both are quiet. Without it we'd be back to inferring
playback from how long the text is — which is how the bot used to talk over
the tail of its own reply, since generating 200 tokens takes ~2s but speaking
them takes ~40s.

Protocol reference:
  Twilio -> us:  setup | prompt | interrupt | dtmf | error | info
  us -> Twilio:  text | play | sendDigits | language | end
Twilio ignores message types it doesn't recognize, so a typo here reads as
"the bot never hangs up" rather than as an error.
"""

import asyncio
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from app import contacts, llm, messages, notify
from app.config import settings
from app.prompts import END_MARKER, greeting, system_prompt

logger = logging.getLogger("relay")

# Withhold a little more than the marker itself, so a marker followed by a
# stray newline or space is still caught whole instead of half-spoken.
TAIL = len(END_MARKER) + 4

# Spoken when the caller goes quiet. Fixed lines, not LLM-generated: on a
# timeout the caller is often already gone, and waiting on a model round-trip
# to talk to dead air just delays the hangup.
STILL_THERE = "Are you still there?"
SILENT_GOODBYE = "Alright, I'll let you go. Thanks for calling. Goodbye!"

# Speaker events arrive on the shared "info" channel, discriminated by name:
#   {"type": "info", "name": "agentSpeaking", "value": "on"|"off", "ts": ...}
# `value` is a string, not a boolean. Twilio doesn't publish these shapes; see
# docs/conversationrelay-events.md for where they come from and how sure we
# are. The extra accepted words cost nothing and cover a wording change.
AGENT_SPEAKING = "agentSpeaking"
CLIENT_SPEAKING = "clientSpeaking"
SPEAKING_ON = {"on", "start", "started", "true"}
SPEAKING_OFF = {"off", "stop", "stopped", "false"}

# While someone is speaking there's no countdown to wait out, so the loop has
# no deadline of its own. It wakes on this interval instead, to re-check the
# flags and run the staleness guards below.
SPEAKING_POLL_SECONDS = 2.0

# A dropped "off" event would leave a speaking flag stuck true, and a stuck
# flag holds the call open forever. Past these bounds we assume it was lost.
# Both are far longer than any real utterance — they're a backstop, not a
# timeout anyone should hit.
MAX_AGENT_SPEECH_SECONDS = 180.0
MAX_CALLER_SPEECH_SECONDS = 90.0

# Rough speaking rate, deliberately on the slow side so the estimate errs
# toward patience. This is a *fallback only*, used on calls where the speaker
# events never arrive — the first agent event switches it off permanently.
#
# Estimating playback was the original bug, so it's worth being precise about
# what changed: the old code applied an estimate to the welcome greeting alone
# and gave replies no playback allowance at all, so the clock started the
# instant generation ended and the nudge landed over the bot's own answer.
# Covering every utterance is what that code should have done. It's still only
# a guess, which is why real events beat it whenever they show up.
SPEECH_CHARS_PER_SEC = 12.0


def speech_seconds(text: str) -> float:
    return len(text) / SPEECH_CHARS_PER_SEC


class Session:
    """In-memory state for one call."""

    def __init__(self) -> None:
        self.system = system_prompt(None)
        self.history: list[dict[str, str]] = []
        self.turn: asyncio.Task | None = None
        self.call_sid: str | None = None
        self.from_number: str | None = None
        self.caller_name: str | None = None  # resolved from contacts at setup
        self.finalized = False
        self.last_activity = time.monotonic()
        self.nudged = False  # already asked "are you still there?"

        # Who's talking, per Twilio's speaker events. These are the whole
        # point: the silence clock only runs when both are False.
        self.agent_speaking = False
        self.caller_speaking = False
        self._agent_since = 0.0
        self._caller_since = 0.0
        self._was_idle = True
        # Set when the agent finishes an utterance, cleared whenever we hand
        # Twilio new tokens. end_call waits on it so a goodbye plays in full.
        self._agent_stopped = asyncio.Event()

        # Fallback for calls where the speaker events never show up: when we
        # think the tokens we've sent will have finished being spoken. Dead
        # weight on a call that reports its events, and switched off for good
        # by the first one that does.
        self.saw_speaker_events = False
        self._speech_estimate_until = 0.0
        self.nudges = 0  # for the end-of-call diagnostic

    def cancel_turn(self) -> None:
        if self.turn and not self.turn.done():
            self.turn.cancel()

    def generating(self) -> bool:
        """A reply is still being streamed out of the LLM."""
        return self.turn is not None and not self.turn.done()

    def maybe_playing(self) -> bool:
        """True while we *estimate* TTS is still going. Only consulted on calls
        that never sent a speaker event; otherwise the events know better."""
        if self.saw_speaker_events:
            return False
        return time.monotonic() < self._speech_estimate_until

    def idle(self) -> bool:
        """Nobody is speaking and nothing is being generated — the only state
        in which quiet actually means the caller has gone quiet."""
        return not (
            self.agent_speaking
            or self.caller_speaking
            or self.generating()
            or self.maybe_playing()
        )

    def touch(self) -> None:
        """Restart the silence clock from now."""
        self.last_activity = time.monotonic()

    def speech_sent(self, text: str = "") -> None:
        """We just handed Twilio tokens to speak. Any earlier agent-stop is
        stale now, and the clock restarts — TTS is about to begin, and there's
        a beat before agentSpeaking=on arrives to cover it for us.

        `text` extends the fallback playback estimate. Each chunk pushes the
        estimated finish further out, from now if the last estimate has already
        run out, so a reply streamed in twenty pieces still adds up to one
        continuous stretch of speech.
        """
        self._agent_stopped.clear()
        self.touch()
        if text:
            now = time.monotonic()
            start = max(now, self._speech_estimate_until)
            self._speech_estimate_until = start + speech_seconds(text)

    def set_agent_speaking(self, speaking: bool) -> None:
        self.agent_speaking = speaking
        self._agent_since = time.monotonic()
        if not speaking:
            self._agent_stopped.set()
        self.settle()

    def set_caller_speaking(self, speaking: bool) -> None:
        self.caller_speaking = speaking
        self._caller_since = time.monotonic()
        self.settle()

    def settle(self) -> None:
        """Re-anchor the silence clock on the edge into idle.

        The caller's turn to respond starts when the bot stops talking, not
        when it stopped generating — so that transition, and only it, is what
        the countdown measures from.
        """
        idle = self.idle()
        if idle and not self._was_idle:
            self.touch()
        self._was_idle = idle

    def _expire_stale_flags(self) -> None:
        now = time.monotonic()
        if self.agent_speaking and now - self._agent_since > MAX_AGENT_SPEECH_SECONDS:
            logger.warning(
                "no agentSpeaking=off after %.0fs; assuming it was dropped: call=%s",
                MAX_AGENT_SPEECH_SECONDS,
                self.call_sid,
            )
            self.set_agent_speaking(False)
        stale_caller = now - self._caller_since > MAX_CALLER_SPEECH_SECONDS
        if self.caller_speaking and stale_caller:
            logger.warning(
                "no clientSpeaking=off after %.0fs; assuming it was dropped: call=%s",
                MAX_CALLER_SPEECH_SECONDS,
                self.call_sid,
            )
            self.set_caller_speaking(False)

    def quiet_remaining(self) -> float:
        """Idle seconds still owed before the next silence action is due."""
        allowance = (
            settings.silence_hangup_seconds
            if self.nudged
            else settings.silence_prompt_seconds
        )
        return max(0.0, allowance - (time.monotonic() - self.last_activity))

    def next_wakeup(self) -> float:
        """How long the loop may sleep before it needs to look at the clock."""
        self._expire_stale_flags()
        self.settle()
        if not self.idle():
            return SPEAKING_POLL_SECONDS
        return self.quiet_remaining()

    def silence_due(self) -> bool:
        """Has the call been idle for the full allowance?

        Settles first, and that ordering is the whole point. A call can fall
        idle because a message said so, or just because the clock ran out on
        the bot's speech — and only the first of those goes through settle()
        on its own. Checking idle() without settling would measure the
        allowance from before the bot started talking, so the nudge would land
        the instant its audio ended and the caller would get no room to answer
        at all.
        """
        self.settle()
        return self.idle() and self.quiet_remaining() <= 0.0

    async def wait_for_agent_silence(self, cap: float) -> bool:
        """Block until the agent's current utterance finishes playing. False if
        the event never came and we fell back to something else.

        On a call with no speaker events, waiting out the full cap would put a
        silent pause on the end of every goodbye. The estimate is a better
        guess than the cap, so it wins when there are no events to trust.
        """
        if not self.saw_speaker_events:
            remaining = self._speech_estimate_until - time.monotonic()
            await asyncio.sleep(min(max(remaining, 0.0), cap))
            return False
        try:
            await asyncio.wait_for(self._agent_stopped.wait(), timeout=cap)
            return True
        except asyncio.TimeoutError:
            return False


def handle_info(session: Session, msg: dict) -> None:
    """Track who's talking, from Twilio's speaker events.

    Anything else arriving on the `info` channel — tokensPlayed, or an event
    name Twilio adds later — is logged and ignored. So is a speaker event whose
    value we can't read: guessing wrong here means either muting the bot's
    silence detection or hanging the call open, and doing nothing instead just
    falls back to the timer.
    """
    name, value = msg.get("name"), msg.get("value")
    if name not in (AGENT_SPEAKING, CLIENT_SPEAKING):
        logger.debug("relay info: %s", msg)
        return

    if value in SPEAKING_ON:
        speaking = True
    elif value in SPEAKING_OFF:
        speaking = False
    else:
        logger.warning("unreadable speaker event, ignoring: %s", msg)
        return

    logger.info("speaker event: %s=%s call=%s", name, value, session.call_sid)
    if name == AGENT_SPEAKING:
        # Twilio is reporting real playback, so stop guessing at it.
        if not session.saw_speaker_events:
            logger.info("speaker events are live; dropping the playback estimate")
            session.saw_speaker_events = True
        session.set_agent_speaking(speaking)
    else:
        session.set_caller_speaking(speaking)
        if speaking:
            # They're there after all — earn back a full nudge.
            session.nudged = False


async def say(ws: WebSocket, session: Session, text: str) -> None:
    """Speak a fixed line and record it in the transcript."""
    session.speech_sent(text)
    await ws.send_json({"type": "text", "token": text, "last": True})
    session.history.append({"role": "assistant", "content": text})


async def end_call(ws: WebSocket, session: Session, reason: str) -> None:
    """Let the last words finish playing, then tell Twilio to hang up.

    "Finish playing" is the agent-stop event for the goodbye, not a fixed
    sleep — so the hangup lands right as the audio ends rather than after a
    guessed pause. `end_grace_seconds` only bounds the wait for a lost event.
    """
    logger.info("ending call: call=%s reason=%s", session.call_sid, reason)
    if not await session.wait_for_agent_silence(settings.end_grace_seconds):
        logger.info(
            "no agent-stop within %.1fs, ending anyway: call=%s",
            settings.end_grace_seconds,
            session.call_sid,
        )
    # handoffData must be a JSON-encoded *string*, not an object.
    await ws.send_json(
        {"type": "end", "handoffData": json.dumps({"reasonCode": reason})}
    )


async def on_silence(ws: WebSocket, session: Session) -> str | None:
    """The call has been idle long enough. Returns a hangup reason, or None to
    carry on.

    Only ever called from the idle state, so there's no "is the bot talking?"
    check here — that's what idle() means. It doesn't hang up itself because
    ending waits on an event that only arrives if someone keeps reading the
    socket; the caller owns that loop, so the caller owns the hangup.
    """
    if not session.nudged:
        logger.info(
            "silence nudge: call=%s after %.1fs idle",
            session.call_sid,
            settings.silence_prompt_seconds,
        )
        await say(ws, session, STILL_THERE)
        session.nudged = True
        session.nudges += 1
        return None

    logger.info("silence hangup: call=%s", session.call_sid)
    await say(ws, session, SILENT_GOODBYE)
    return "caller-silent"


async def run_turn(ws: WebSocket, session: Session, heard: str) -> None:
    """Stream one LLM reply, strip a trailing end marker, record it in history."""
    session.history.append({"role": "user", "content": heard})
    spoken: list[str] = []
    pending = ""  # withhold a marker-length tail so it's never spoken

    async def send(token: str, last: bool) -> None:
        session.speech_sent(token)
        await ws.send_json({"type": "text", "token": token, "last": last})

    try:
        async for token in llm.stream_reply(session.system, session.history):
            pending += token
            if len(pending) > TAIL:
                flush, pending = pending[:-TAIL], pending[-TAIL:]
                spoken.append(flush)
                await send(flush, last=False)

        ending = pending.rstrip().endswith(END_MARKER)
        if ending:
            pending = pending.rstrip()[: -len(END_MARKER)]
        if pending:
            spoken.append(pending)
            await send(pending, last=False)
        await send("", last=True)
        session.history.append({"role": "assistant", "content": "".join(spoken)})

        if ending:
            await end_call(ws, session, "message-captured")
    except asyncio.CancelledError:
        # Caller barged in — drop the rest of this reply.
        logger.info("turn cancelled mid-reply")
        raise


async def finalize(session: Session) -> None:
    """Capture the message and text it to you. Runs once, at call end."""
    if session.finalized or not session.call_sid:
        return
    session.finalized = True
    try:
        msg = await messages.capture(
            session.history,
            session.call_sid,
            session.from_number,
            session.caller_name,
        )
        if msg:
            await notify.send_sms(notify.build_summary(msg))
        else:
            who = session.from_number or "unknown"
            if session.caller_name:
                who = f"{who} ({session.caller_name})"
            await notify.send_sms(f"Missed call from {who} — no message left.")
    except Exception:
        logger.exception("finalize failed for call=%s", session.call_sid)


async def handle_relay(ws: WebSocket) -> None:
    await ws.accept()
    session = Session()
    # The receive is kept as a task across timeouts rather than cancelled and
    # reissued, so waking up to check the clock can't drop a caller's message.
    receive: asyncio.Task | None = None
    # The silence hangup runs alongside the loop rather than inside it: it
    # waits for the goodbye's agent-stop event, and that event only arrives if
    # this loop keeps reading the socket in the meantime.
    hangup: asyncio.Task | None = None
    try:
        while True:
            if receive is None:
                receive = asyncio.create_task(ws.receive_json())
            pending = {receive} | ({hangup} if hangup else set())
            done, _ = await asyncio.wait(
                pending,
                timeout=session.next_wakeup(),
                return_when=asyncio.FIRST_COMPLETED,
            )

            if hangup is not None and hangup in done:
                hangup.result()  # surface a failed hangup rather than swallow it
                break

            if receive not in done:
                # Woke on the clock. While anyone is speaking that's just the
                # watchdog tick; only a call idle for the full allowance has
                # gone quiet, and one already saying goodbye is past nudging.
                if hangup is None and session.silence_due():
                    reason = await on_silence(ws, session)
                    if reason:
                        hangup = asyncio.create_task(end_call(ws, session, reason))
                continue

            msg = receive.result()
            receive = None
            msg_type = msg.get("type")

            # Speaker events drive their own state and must not count as
            # caller activity — agentSpeaking is the bot, after all.
            if msg_type == "info":
                handle_info(session, msg)
                continue

            session.touch()

            if msg_type == "setup":
                session.call_sid = msg.get("callSid")
                session.from_number = msg.get("customParameters", {}).get("from")
                session.caller_name = contacts.lookup(session.from_number)
                session.system = system_prompt(
                    session.from_number, session.caller_name
                )
                # Twilio speaks the welcome greeting from the TwiML attribute,
                # so its tokens never pass through here. Seed the fallback
                # estimate with it by hand, or a call with no speaker events
                # counts the greeting as the caller sitting silent. Use the same
                # (possibly personalized) greeting the caller actually heard.
                session.speech_sent(greeting(session.caller_name))
                logger.info(
                    "relay setup: call=%s from=%s name=%s",
                    session.call_sid,
                    session.from_number,
                    session.caller_name,
                )

            elif msg_type == "prompt":
                heard = msg.get("voicePrompt", "")
                logger.info("caller said: %s", heard)
                session.nudged = False
                session.cancel_turn()
                session.turn = asyncio.create_task(run_turn(ws, session, heard))

            elif msg_type == "interrupt":
                logger.info("caller interrupted")
                # A barge-in stops playback, so agent speech is over whether or
                # not the agent-stop event beat this message to us.
                session.set_agent_speaking(False)
                session.cancel_turn()

            elif msg_type == "error":
                logger.warning("relay error: %s", msg)

    except WebSocketDisconnect:
        logger.info("relay disconnected: call=%s", session.call_sid)
    finally:
        # One line per call saying which mechanism was actually driving the
        # silence clock. If a mistimed "are you still there?" gets reported,
        # this is what says whether the events were there to prevent it.
        logger.info(
            "call summary: call=%s speaker_events=%s nudges=%d turns=%d",
            session.call_sid,
            "live" if session.saw_speaker_events else "NONE (estimated playback)",
            session.nudges,
            sum(1 for m in session.history if m["role"] == "user"),
        )
        if receive is not None:
            receive.cancel()
        if hangup is not None:
            hangup.cancel()
        session.cancel_turn()
        await finalize(session)
