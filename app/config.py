from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Call routing
    forward_to_number: str = "+18084649192"  # your cell, E.164
    dial_timeout: int = 15  # seconds — must beat carrier voicemail

    public_domain: str = "sparkal.ai"

    tts_provider: str = "Google"
    tts_voice: str = "en-US-Journey-F"

    anthropic_api_key: str = ""
    llm_primary: str = "claude-haiku-4-5"
    llm_max_tokens: int = 200  # replies are spoken, so keep them short

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""  # your Twilio number, E.164 — must be SMS-capable
    notify_sms_to: str = ""


settings = Settings()
