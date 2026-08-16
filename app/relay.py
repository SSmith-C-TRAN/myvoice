"""ConversationRelay WebSocket handler.

Step 4: the LLM turn loop. Twilio owns the audio, STT, and TTS; we own the
brain. On each finalized caller utterance we stream Claude's reply back token by
token so TTS starts before the full answer is ready. A barge-in (`interrupt`)
cancels the in-flight generation for that turn.

Protocol reference:
  Twilio -> us:  setup | prompt | interrupt | dtmf | error
  us -> Twilio:  {"type":"text","token":...,"last":bool} | {"type":"end-session",...}
"""

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app import llm
from app.prompts import system_prompt

logger = logging.getLogger("relay")


class Session:
    """In-memory state for one call, keyed by callSid."""

    def __init__(self) -> None:
        self.system = system_prompt(None)
        self.history: list[dict[str, str]] = []
        self.turn: asyncio.Task | None = None

    def cancel_turn(self) -> None:
        if self.turn and not self.turn.done():
            self.turn.cancel()


async def run_turn(ws: WebSocket, session: Session, heard: str) -> None:
    """Stream one LLM reply to Twilio, then record it in history."""
    session.history.append({"role": "user", "content": heard})
    reply_parts: list[str] = []
    try:
        async for token in llm.stream_reply(session.system, session.history):
            reply_parts.append(token)
            await ws.send_json({"type": "text", "token": token, "last": False})
        # Signal end of this turn so TTS knows the reply is complete.
        await ws.send_json({"type": "text", "token": "", "last": True})
        session.history.append(
            {"role": "assistant", "content": "".join(reply_parts)}
        )
    except asyncio.CancelledError:
        # Caller barged in — drop the rest of this reply.
        logger.info("turn cancelled mid-reply")
        raise


async def handle_relay(ws: WebSocket) -> None:
    await ws.accept()
    session = Session()
    call_sid = None
    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")

            if msg_type == "setup":
                call_sid = msg.get("callSid")
                caller = msg.get("customParameters", {}).get("from")
                session.system = system_prompt(caller)
                logger.info("relay setup: call=%s from=%s", call_sid, caller)

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
        logger.info("relay disconnected: call=%s", call_sid)
    finally:
        session.cancel_turn()
