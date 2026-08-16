"""TwiML response builders. Kept as plain string templates — no extra deps."""


def dial_then_bot(number: str, timeout: int) -> str:
    """Ring the cell first; on no-answer the action callback routes to the bot."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Dial timeout="{timeout}" action="/voice/after-dial" method="POST">'
        f"<Number>{number}</Number>"
        "</Dial>"
        "</Response>"
    )


def hangup() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def connect_relay(
    public_domain: str,
    greeting: str,
    tts_provider: str,
    tts_voice: str,
    events: str = "speaker-events",
) -> str:
    """Hand the caller to the ConversationRelay bot over a WebSocket.

    The bot speaks `greeting` first (no dead air) and the caller's number is
    passed through as a <Parameter>, arriving in the setup message's
    customParameters. `tts_provider`/`tts_voice` pick the spoken voice —
    ElevenLabs additionally needs an API key configured in the Twilio Console.

    `events` is a space-separated subscription list. "speaker-events" is what
    makes Twilio tell us when the agent and the caller start and stop speaking
    — without it we'd be back to guessing when TTS finishes from the length of
    the text. See docs/conversationrelay-events.md.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        '<Connect action="/voice/handoff">'
        f'<ConversationRelay url="wss://{public_domain}/ws" '
        f'welcomeGreeting="{greeting}" '
        f'ttsProvider="{tts_provider}" voice="{tts_voice}" '
        f'events="{events}">'
        '<Parameter name="from" value="{{From}}"/>'
        "</ConversationRelay>"
        "</Connect>"
        "</Response>"
    )
