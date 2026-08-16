from fastapi import FastAPI, Form, WebSocket
from fastapi.responses import PlainTextResponse, Response

from app.config import settings
from app.relay import handle_relay
from app.twiml import connect_relay, dial_then_bot, hangup

app = FastAPI(title="firstsignal-voice")

GREETING = (
    "Hi, this is Jace, Steve's assistant. He's not available right now. "
    "But I can take a message. Who's calling?"
)


def twiml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def bot_answers() -> Response:
    """Hand the caller straight to the message bot."""
    return twiml(
        connect_relay(
            settings.public_domain,
            GREETING,
            settings.tts_provider,
            settings.tts_voice,
        )
    )


@app.get("/healthz")
def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.post("/voice")
def voice() -> Response:
    """The number's voice webhook.

    In "bot" mode the assistant picks up right away — the caller already rang
    the cell and the carrier forwarded them here, so dialing out again would
    just bounce back. "dial-first" mode is for pointing a Twilio number at the
    cell directly: ring it with a short timeout, then fall back to the bot.
    """
    if settings.answer_mode == "dial-first":
        return twiml(dial_then_bot(settings.forward_to_number, settings.dial_timeout))
    return bot_answers()


@app.post("/voice/after-dial")
def after_dial(DialCallStatus: str = Form(...)) -> Response:
    """Dial leg ended ("dial-first" mode only). 'completed' means the cell took
    the call — otherwise the caller reached no one, so hand off to the bot."""
    if DialCallStatus == "completed":
        return twiml(hangup())
    return bot_answers()


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """ConversationRelay turn loop. The greeting is passed through so the relay
    knows how long Twilio will be speaking it, and doesn't mistake it for the
    caller sitting silent."""
    await handle_relay(websocket, GREETING)


@app.post("/voice/handoff")
def handoff() -> Response:
    """Fires when the relay session ends. Just close the call for now."""
    return twiml(hangup())
