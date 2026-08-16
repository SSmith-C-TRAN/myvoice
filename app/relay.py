"""ConversationRelay WebSocket handler.

Twilio owns the audio, STT, and TTS; we own the brain. On each finalized caller
utterance we stream Claude's reply back token by token so TTS starts before the
full answer is ready. A barge-in (`interrupt`) cancels the in-flight generation.

Ending a call (step 5): the bot appends a silent `[[END]]` marker to its final
goodbye when it has what it needs. We strip the marker (never speak it), send
`end-session`, and Twilio closes the socket. Whether the bot ends the call or
the caller simply hangs up, both paths land in the disconnect handler, where
`finalize` turns the transcript into a message and texts it to you.

Protocol reference:
  Twilio -> us:  setup | prompt | interrupt | dtmf | error
  us -> Twilio:  {"type":"text","token":...,"last":bool} | {"type":"end-session",...}
"""

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app import llm, messages, notify
from app.prompts import system_prompt

logger = logging.getLogger("relay")

END_MARKER = "[[END]]"
# Withhold a little more than the marker itself, so a marker followed by a
# stray newline or space is still caught whole instead of half-spoken.
TAIL = len(END_MARKER) + 4


class Session:
    """In-memory state for one call."""

    def __init__(self) -> None:
        self.system = system_prompt(None)
        self.history: list[dict[str, str]] = []
        self.turn: asyncio.Task | None = None
        self.call_sid: str | None = None
        self.from_number: str | None = None
        self.finalized = False

    def cancel_turn(self) -> None:
        if self.turn and not self.turn.done():
            self.turn.cancel()


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
            logger.info("bot ending call: %s", session.call_sid)
            await ws.send_json({"type": "end-session", "handoffData": "captured"})
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


async def handle_relay(ws: WebSocket) -> None:
    await ws.accept()
    session = Session()
    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")

            if msg_type == "setup":
                session.call_sid = msg.get("callSid")
                session.from_number = msg.get("customParameters", {}).get("from")
                session.system = system_prompt(session.from_number)
                logger.info(
                    "relay setup: call=%s from=%s",
                    session.call_sid,
                    session.from_number,
                )

            elif msg_type == "prompt":
                heard = msg.get("voicePrompt", "")
                logger.info("caller said: %s", heard)
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
        session.cancel_turn()
        await finalize(session)
