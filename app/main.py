from fastapi import FastAPI, Form, WebSocket
from fastapi.responses import PlainTextResponse, Response

from app.config import settings
from app.relay import handle_relay
from app.twiml import connect_relay, dial_then_bot, hangup

app = FastAPI(title="firstsignal-voice")

GREETING = (
    "Hi, you've reached Steve's line. He's not available right now, "
    "but I can take a message. Who's calling?"
)


def twiml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


@app.get("/healthz")
def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.post("/voice")
def voice() -> Response:
    """Number's voice webhook: ring the cell first with a short timeout."""
    return twiml(dial_then_bot(settings.forward_to_number, settings.dial_timeout))


@app.post("/voice/after-dial")
def after_dial(DialCallStatus: str = Form(...)) -> Response:
    """Dial leg ended. 'completed' means the cell took the call — otherwise the
    caller reached no one, so hand off to the message bot (placeholder for now)."""
    if DialCallStatus == "completed":
        return twiml(hangup())
    return twiml(
        connect_relay(
            settings.public_domain,
            GREETING,
            settings.tts_provider,
            settings.tts_voice,
        )
    )


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """ConversationRelay turn loop (echo bot for now)."""
    await handle_relay(websocket)


@app.post("/voice/handoff")
def handoff() -> Response:
    """Fires when the relay session ends. Just close the call for now."""
    return twiml(hangup())
