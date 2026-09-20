"""Settings, loaded once from the environment.

Every external dependency has a mock, and the mock is selected whenever the
credential for the real thing is blank. That is deliberate: the test suite and
the negative-path demo must run with no accounts at all.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    # A key that is present but blank falls back to the default, so "copy .env.example and clear
    # what you don't use" cannot crash the import.
    return (os.getenv(name) or default).strip()


def _int(name: str, default: str) -> int:
    raw = _env(name, default)
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None


def _template_id(name: str) -> str:
    raw = _env(name)
    if raw and not raw.isdigit():
        raise SystemExit(f"{name} must be the numeric template id from the Rocketlane UI, got {raw!r}")
    return raw


@dataclass(frozen=True)
class Settings:
    llm_provider: str = _env("LLM_PROVIDER", "mock")
    gemini_api_key: str = _env("GEMINI_API_KEY")
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY")

    gmail_address: str = _env("GMAIL_ADDRESS")
    gmail_app_password: str = _env("GMAIL_APP_PASSWORD")
    deal_subject_pattern: str = _env("DEAL_SUBJECT_PATTERN", "Closed Won")
    poll_seconds: int = _int("POLL_SECONDS", "30")
    allowed_sender_domains: tuple = field(default_factory=lambda: tuple(d.strip().lower() for d in _env("ALLOWED_SENDER_DOMAINS").split(",") if d.strip()))
    processed_ids_path: str = _env("PROCESSED_IDS_PATH", "processed_message_ids.txt")

    voice_provider: str = _env("VOICE_PROVIDER", "mock")
    bland_api_key: str = _env("BLAND_API_KEY")
    bland_from_number: str = _env("BLAND_FROM_NUMBER")
    # hangup | ignore. "hangup" is correct in production: a voicemail box cannot confirm a tier.
    # Set "ignore" only when the callee runs handset or carrier call screening, whose spoken
    # prompt trips voicemail detection on a call a human is about to pick up.
    bland_voicemail_action: str = _env("BLAND_VOICEMAIL_ACTION", "hangup")
    vapi_api_key: str = _env("VAPI_API_KEY")
    vapi_phone_number_id: str = _env("VAPI_PHONE_NUMBER_ID")
    ae_phone_directory: dict = field(default_factory=lambda: json.loads(_env("AE_PHONE_DIRECTORY", "{}") or "{}"))
    voice_max_attempts: int = _int("VOICE_MAX_ATTEMPTS", "2")
    voice_retry_delay_seconds: int = _int("VOICE_RETRY_DELAY_SECONDS", "20")

    rocketlane_api_key: str = _env("ROCKETLANE_API_KEY")
    rocketlane_owner_email: str = _env("ROCKETLANE_OWNER_EMAIL")
    # Who owns the project per tier. Enterprise gets a dedicated CSM, Growth the pooled queue owner.
    # Both default to the owner email so a one-person trial still works.
    rocketlane_enterprise_csm_email: str = _env("ROCKETLANE_ENTERPRISE_CSM_EMAIL") or _env("ROCKETLANE_OWNER_EMAIL")
    rocketlane_growth_csm_email: str = _env("ROCKETLANE_GROWTH_CSM_EMAIL") or _env("ROCKETLANE_OWNER_EMAIL")
    rocketlane_template_id_enterprise: str = _template_id("ROCKETLANE_TEMPLATE_ID_ENTERPRISE")
    rocketlane_template_id_growth: str = _template_id("ROCKETLANE_TEMPLATE_ID_GROWTH")
    rocketlane_max_retries: int = _int("ROCKETLANE_MAX_RETRIES", "3")

    slack_bot_token: str = _env("SLACK_BOT_TOKEN")
    slack_escalation_channel: str = _env("SLACK_ESCALATION_CHANNEL", "#onboarding-escalations")

    human_escalation_email: str = _env("HUMAN_ESCALATION_EMAIL", "cs-leads@novacrm.example")

    audit_log_path: str = _env("AUDIT_LOG_PATH", "audit.jsonl")
    escalation_log_path: str = _env("ESCALATION_LOG_PATH", "escalations.jsonl")


settings = Settings()
