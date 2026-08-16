import logging
import sys

from fastapi import FastAPI, Form, WebSocket
from fastapi.responses import PlainTextResponse, Response

from app.config import settings
from app.prompts import GREETING
from app.relay import handle_relay
from app.twiml import connect_relay, dial_then_bot, hangup

# Uvicorn configures its own loggers and leaves the root logger alone, so
# without this every logger.info() in the app goes nowhere — including the
# per-call summary that says whether Twilio's speaker events arrived. Silent
# by default is the wrong default for something you can only debug from the
# outside, one phone call at a time.
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

app = FastAPI(title="firstsignal-voice")


def twiml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def bot_answers(caller_id: str = "") -> Response:
    """Hand the caller straight to the message bot."""
    return twiml(
        connect_relay(
            settings.public_domain,
            GREETING,
            settings.tts_provider,
            settings.tts_voice,
            caller_id,
            settings.relay_events,
        )
    )


@app.get("/healthz")
def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.post("/voice")
def voice(From: str = Form("")) -> Response:
    """The number's voice webhook.

    In "bot" mode the assistant picks up right away — the caller already rang
    the cell and the carrier forwarded them here, so dialing out again would
    just bounce back. "dial-first" mode is for pointing a Twilio number at the
    cell directly: ring it with a short timeout, then fall back to the bot.
    """
    if settings.answer_mode == "dial-first":
        return twiml(dial_then_bot(settings.forward_to_number, settings.dial_timeout))
    return bot_answers(From)


@app.post("/voice/after-dial")
def after_dial(DialCallStatus: str = Form(...), From: str = Form("")) -> Response:
    """Dial leg ended ("dial-first" mode only). 'completed' means the cell took
    the call — otherwise the caller reached no one, so hand off to the bot."""
    if DialCallStatus == "completed":
        return twiml(hangup())
    return bot_answers(From)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """ConversationRelay turn loop. The greeting isn't passed through — Twilio
    speaks it and reports its start and stop like any other utterance, so the
    relay doesn't need to know its text to keep it out of the silence clock."""
    await handle_relay(websocket)


@app.post("/voice/handoff")
def handoff() -> Response:
    """Fires when the relay session ends. Just close the call for now."""
    return twiml(hangup())
