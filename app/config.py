from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Call routing
    forward_to_number: str = "+18084649192"  # your cell, E.164
    dial_timeout: int = 15  # seconds — must beat carrier voicemail

    # Public host App Platform serves on, e.g. firstsignal-voice-xxxxx.ondigitalocean.app.
    # Baked into the relay's wss:// URL, so a change needs a restart.
    public_domain: str = "sparkal.ai"

    # LLM (step 4). Reasoning stays off — Haiku 4.5 does no thinking unless asked,
    # which is exactly what real-time voice needs.
    anthropic_api_key: str = ""
    llm_primary: str = "claude-haiku-4-5"
    llm_max_tokens: int = 200  # replies are spoken, so keep them short


settings = Settings()
