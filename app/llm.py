"""Thin LLM interface so swapping providers never touches call logic.

Primary: Claude Haiku 4.5 — fast first-token, strong instruction following.
Reasoning stays off: Haiku 4.5 does no thinking unless explicitly enabled, so
we simply don't pass a `thinking` param. That keeps time-to-first-token low,
which is what a real-time voice turn needs.
"""

from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic

from app.config import settings

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)


async def stream_reply(
    system: str, messages: list[dict[str, str]]
) -> AsyncIterator[str]:
    """Stream the assistant's reply token by token."""
    async with _client.messages.stream(
        model=settings.llm_primary,
        max_tokens=settings.llm_max_tokens,
        system=system,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text
