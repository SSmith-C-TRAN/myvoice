"""Structured message model and end-of-call extraction.

The bot talks freely during the call; at the end we distill the transcript into
a small record worth texting you. The four caller-derived fields come from a
one-shot LLM extraction; the rest we already know and fill in ourselves.
"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from app import llm
from app.prompts import EXTRACT_SYSTEM


class Extracted(BaseModel):
    """The fields the LLM pulls from the transcript."""

    caller_name: str | None
    callback_number: str | None
    reason: str
    urgency: Literal["low", "normal", "high"]


class Message(BaseModel):
    """The full record we text to you."""

    caller_name: str | None
    callback_number: str | None
    reason: str
    urgency: Literal["low", "normal", "high"]
    from_number: str | None  # caller ID from the setup params
    transcript: str  # verbatim conversation
    call_sid: str
    received_at: datetime


def _render_transcript(history: list[dict[str, str]]) -> str:
    speaker = {"user": "Caller", "assistant": "Bot"}
    return "\n".join(
        f"{speaker.get(turn['role'], turn['role'])}: {turn['content']}"
        for turn in history
    )


async def capture(
    history: list[dict[str, str]],
    call_sid: str,
    from_number: str | None,
    matched_name: str | None = None,
) -> Message | None:
    """Extract a Message from the conversation, or None if nothing was said.

    A `matched_name` from the contacts lookup (reliable, from caller ID) wins
    over the name the LLM guesses from the transcript.
    """
    if not any(turn["role"] == "user" for turn in history):
        return None

    transcript = _render_transcript(history)
    extracted = await llm.extract(EXTRACT_SYSTEM, transcript, Extracted)
    return Message(
        caller_name=matched_name or extracted.caller_name,
        callback_number=extracted.callback_number,
        reason=extracted.reason,
        urgency=extracted.urgency,
        from_number=from_number,
        transcript=transcript,
        call_sid=call_sid,
        received_at=datetime.now(timezone.utc),
    )
