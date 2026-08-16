from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Call routing.
    # "bot"        — the assistant answers immediately. Use this when your
    #                carrier forwards to the Twilio number only after you
    #                don't pick up, so the ringing already happened.
    # "dial-first" — Twilio is the number people dial; ring forward_to_number
    #                first and fall back to the bot on no-answer.
    answer_mode: Literal["bot", "dial-first"] = "bot"

    # Only used in "dial-first" mode.
    forward_to_number: str = "+18084649192"  # your cell, E.164
    dial_timeout: int = 5  # seconds — must beat carrier voicemail

    public_domain: str = "sparkal.ai"

    # INFO gives you a line per turn plus a summary per call. DEBUG adds every
    # unrecognized inbound WebSocket message, which is how you'd inspect a
    # Twilio event this code doesn't handle yet.
    log_level: str = "INFO"

    tts_provider: str = "Google"
    tts_voice: str = "en-US-Journey-F"

    # Space-separated <ConversationRelay events="..."> subscription.
    # "speaker-events" is load-bearing: it's how we learn when TTS and the
    # caller actually start and stop, which is what the silence clock and the
    # hangup both key off. Add "tokens-played" to see per-chunk playback
    # confirmations — noisy, useful only for debugging.
    relay_events: str = "speaker-events"

    # Idle time before the bot speaks up. "Idle" now means what it says: no TTS
    # playing, no caller talking, no reply being generated — measured from
    # Twilio's speaker events, not estimated from text length. So these are
    # real seconds of dead air, and can be short.
    # The first one is thinking time: it starts when the bot stops talking, so
    # it's how long a caller gets to answer a question before being prodded.
    # People pause to check a number or gather a thought, so it's the more
    # patient of the two. The second runs after the nudge, when the likeliest
    # explanation is that nobody's there.
    silence_prompt_seconds: float = 15.0  # quiet before "Are you still there?"
    silence_hangup_seconds: float = 12.0  # more quiet after that, then goodbye
    # Fallback bound only. Normally the goodbye's agentSpeaking=off event tells
    # us playback finished and we hang up right then; this caps the wait in
    # case that event never arrives. Not a delay we expect callers to hear.
    end_grace_seconds: float = 10.0

    anthropic_api_key: str = ""
    llm_primary: str = "claude-haiku-4-5-20251001"
    llm_max_tokens: int = 500  # replies are spoken, so keep them short

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""  # your Twilio number, E.164 — must be SMS-capable
    notify_sms_to: str = ""


settings = Settings()
