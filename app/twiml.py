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


def connect_relay(public_domain: str, greeting: str) -> str:
    """Hand the caller to the ConversationRelay bot over a WebSocket.

    The bot speaks `greeting` first (no dead air) and the caller's number is
    passed through as a <Parameter>, arriving in the setup message's
    customParameters. Uses Twilio's default STT/TTS providers — no extra
    console setup needed for the echo test.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        '<Connect action="/voice/handoff">'
        f'<ConversationRelay url="wss://{public_domain}/ws" '
        f'welcomeGreeting="{greeting}">'
        '<Parameter name="from" value="{{From}}"/>'
        "</ConversationRelay>"
        "</Connect>"
        "</Response>"
    )
