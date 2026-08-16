"""Text the message summary to your cell via the Twilio Messages API."""

import asyncio
import logging

from twilio.rest import Client

from app.config import settings
from app.messages import Message

logger = logging.getLogger("notify")


def build_summary(msg: Message) -> str:
    """The SMS body: a scannable summary plus the full transcript."""
    who = msg.from_number or "unknown"
    if msg.caller_name:
        who = f"{who} ({msg.caller_name})"
    return (
        "Missed call → message taken\n"
        f"From: {who}\n"
        f"Reason: {msg.reason}\n"
        f"Callback: {msg.callback_number or 'not given'}\n"
        f"Urgency: {msg.urgency}\n"
        "— transcript —\n"
        f"{msg.transcript}"
    )


def _send(body: str) -> None:
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    client.messages.create(
        from_=settings.twilio_from_number,
        to=settings.notify_sms_to,
        body=body,
    )


async def send_sms(body: str) -> None:
    """Send the text, off the event loop. Missing creds log and skip."""
    if not (settings.twilio_account_sid and settings.twilio_from_number):
        logger.warning("Twilio not configured — skipping SMS")
        return
    await asyncio.to_thread(_send, body)
