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
    dial_timeout: int = 15  # seconds — must beat carrier voicemail

    public_domain: str = "sparkal.ai"

    tts_provider: str = "Google"
    tts_voice: str = "en-US-Journey-F"

    silence_prompt_seconds: float = 8.0  # quiet before "Are you still there?"
    silence_hangup_seconds: float = 8.0  # more quiet after that, then goodbye
    # Twilio doesn't tell us when TTS finishes, and it's undocumented whether
    # `end` cuts off audio still playing — so hold the socket open this long
    # after the goodbye. Raise it if callers hear the goodbye clipped.
    end_grace_seconds: float = 8.0

    anthropic_api_key: str = ""
    llm_primary: str = "claude-haiku-4-5"
    llm_max_tokens: int = 200  # replies are spoken, so keep them short

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""  # your Twilio number, E.164 — must be SMS-capable
    notify_sms_to: str = ""


settings = Settings()
