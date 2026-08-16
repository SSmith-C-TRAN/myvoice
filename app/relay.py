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

Protocol reference:
  Twilio -> us:  setup | prompt | interrupt | dtmf | error
  us -> Twilio:  text | play | sendDigits | language | end
Twilio ignores message types it doesn't recognize, so a typo here reads as
"the bot never hangs up" rather than as an error.
"""

import asyncio
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from app import llm, messages, notify
from app.config import settings
from app.prompts import system_prompt

logger = logging.getLogger("relay")

END_MARKER = "[[END]]"
# Withhold a little more than the marker itself, so a marker followed by a
# stray newline or space is still caught whole instead of half-spoken.
TAIL = len(END_MARKER) + 4

# Spoken when the caller goes quiet. Fixed lines, not LLM-generated: on a
# timeout the caller is often already gone, and waiting on a model round-trip
# to talk to dead air just delays the hangup.
STILL_THERE = "Are you still there?"
SILENT_GOODBYE = "Alright, I'll let you go. Thanks for calling. Goodbye!"

# Rough speaking rate, used only to keep the welcome greeting from counting
# against the caller as silence while it's still playing.
SPEECH_CHARS_PER_SEC = 14.0


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
        self.finalized = False
        self.last_activity = time.monotonic()
        self.nudged = False  # already asked "are you still there?"

    def cancel_turn(self) -> None:
        if self.turn and not self.turn.done():
            self.turn.cancel()

    def speaking(self) -> bool:
        return self.turn is not None and not self.turn.done()

    def touch(self, offset: float = 0.0) -> None:
        """Restart the silence clock. `offset` pushes it into the future, to
        cover audio we know is still playing."""
        self.last_activity = time.monotonic() + offset

    def quiet_remaining(self) -> float:
        """Seconds until the next silence action is due."""
        allowance = (
            settings.silence_hangup_seconds
            if self.nudged
            else settings.silence_prompt_seconds
        )
        elapsed = time.monotonic() - self.last_activity
        return max(0.0, allowance - elapsed)


async def say(ws: WebSocket, session: Session, text: str) -> None:
    """Speak a fixed line and record it in the transcript."""
    await ws.send_json({"type": "text", "token": text, "last": True})
    session.history.append({"role": "assistant", "content": text})


async def end_call(ws: WebSocket, session: Session, reason: str) -> None:
    """Let the last words play, then tell Twilio to hang up."""
    logger.info("ending call: call=%s reason=%s", session.call_sid, reason)
    await asyncio.sleep(settings.end_grace_seconds)
    # handoffData must be a JSON-encoded *string*, not an object.
    await ws.send_json(
        {"type": "end", "handoffData": json.dumps({"reasonCode": reason})}
    )


async def on_silence(ws: WebSocket, session: Session) -> bool:
    """The caller has been quiet. Returns False when the call should end."""
    # The bot talking is not the caller being silent.
    if session.speaking():
        session.touch()
        return True

    if not session.nudged:
        logger.info("silence nudge: call=%s", session.call_sid)
        await say(ws, session, STILL_THERE)
        session.nudged = True
        session.touch(offset=speech_seconds(STILL_THERE))
        return True

    logger.info("silence hangup: call=%s", session.call_sid)
    await say(ws, session, SILENT_GOODBYE)
    await end_call(ws, session, "caller-silent")
    return False


async def run_turn(ws: WebSocket, session: Session, heard: str) -> None:
    """Stream one LLM reply, strip a trailing end marker, record it in history."""
    session.history.append({"role": "user", "content": heard})
    spoken: list[str] = []
    pending = ""  # withhold a marker-length tail so it's never spoken
    try:
        async for token in llm.stream_reply(session.system, session.history):
            pending += token
            if len(pending) > TAIL:
                flush, pending = pending[:-TAIL], pending[-TAIL:]
                spoken.append(flush)
                await ws.send_json({"type": "text", "token": flush, "last": False})

        ending = pending.rstrip().endswith(END_MARKER)
        if ending:
            pending = pending.rstrip()[: -len(END_MARKER)]
        if pending:
            spoken.append(pending)
            await ws.send_json({"type": "text", "token": pending, "last": False})
        await ws.send_json({"type": "text", "token": "", "last": True})
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
            session.history, session.call_sid, session.from_number
        )
        if msg:
            await notify.send_sms(notify.build_summary(msg))
        else:
            who = session.from_number or "unknown"
            await notify.send_sms(f"Missed call from {who} — no message left.")
    except Exception:
        logger.exception("finalize failed for call=%s", session.call_sid)


async def handle_relay(ws: WebSocket, greeting: str = "") -> None:
    await ws.accept()
    session = Session()
    # The receive is kept as a task across timeouts rather than cancelled and
    # reissued, so waking up to check the clock can't drop a caller's message.
    receive: asyncio.Task | None = None
    try:
        while True:
            if receive is None:
                receive = asyncio.create_task(ws.receive_json())
            done, _ = await asyncio.wait({receive}, timeout=session.quiet_remaining())

            if receive not in done:
                if not await on_silence(ws, session):
                    break
                continue

            msg = receive.result()
            receive = None
            session.touch()
            msg_type = msg.get("type")

            if msg_type == "setup":
                session.call_sid = msg.get("callSid")
                session.from_number = msg.get("customParameters", {}).get("from")
                session.system = system_prompt(session.from_number)
                # The welcome greeting is still playing — don't count it
                # against the caller as silence.
                session.touch(offset=speech_seconds(greeting))
                logger.info(
                    "relay setup: call=%s from=%s",
                    session.call_sid,
                    session.from_number,
                )

            elif msg_type == "prompt":
                heard = msg.get("voicePrompt", "")
                logger.info("caller said: %s", heard)
                session.nudged = False
                session.cancel_turn()
                session.turn = asyncio.create_task(run_turn(ws, session, heard))

            elif msg_type == "interrupt":
                logger.info("caller interrupted")
                session.cancel_turn()

            elif msg_type == "error":
                logger.warning("relay error: %s", msg)

    except WebSocketDisconnect:
        logger.info("relay disconnected: call=%s", session.call_sid)
    finally:
        if receive is not None:
            receive.cancel()
        session.cancel_turn()
        await finalize(session)
