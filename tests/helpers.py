"""Shared fixtures. Every test builds a fresh system with mocks and a throwaway audit log."""
from __future__ import annotations
import os
import tempfile
from datetime import date
from config import Settings
from agents.orchestrator import build_system
from providers.email_source import InboundEmail, MockEmailSource
from providers.voice import MockVoice
from providers.rocketlane import MockRocketlane
from providers.slack import MockSlack
from providers.llm import MockLLM

START = date(2026, 10, 1)
DIRECTORY = {"Alex Rivera": "+15550100001", "Dana Okafor": "+15550100002"}


def settings_for_tests(**over) -> Settings:
    # Every environment-derived field is pinned, so a filled-in .env on the developer's machine
    # cannot change what the tests see.
    base = dict(llm_provider="mock", gemini_api_key="", anthropic_api_key="",
                gmail_address="", gmail_app_password="", deal_subject_pattern="Closed Won", poll_seconds=1,
                allowed_sender_domains=(), processed_ids_path=os.path.join(tempfile.mkdtemp(), "processed.txt"),
                voice_provider="mock", bland_api_key="", bland_from_number="", vapi_api_key="", vapi_phone_number_id="",
                ae_phone_directory=DIRECTORY, voice_max_attempts=2, voice_retry_delay_seconds=0,
                rocketlane_api_key="", rocketlane_owner_email="pm@novacrm.example",
                rocketlane_enterprise_csm_email="", rocketlane_growth_csm_email="",
                rocketlane_template_id_enterprise="", rocketlane_template_id_growth="", rocketlane_max_retries=3,
                slack_bot_token="", slack_escalation_channel="#onboarding-escalations",
                human_escalation_email="cs-leads@novacrm.example",
                audit_log_path="unused.jsonl", escalation_log_path="unused.jsonl")
    base.update(over)
    return Settings(**base)


def email(body: str, *, subject="Closed Won: Acme", sender="Alex Rivera <alex.rivera@novacrm.example>", mid="<t1@novacrm>"):
    return InboundEmail(message_id=mid, subject=subject, body=body, sender=sender)


GOOD_BODY = ("Customer: Acme Foods\nContact email: ops@acme.example\nAE: Alex Rivera\n"
             "Opportunity: https://novacrm.lightning.force.com/lightning/r/Opportunity/006ACME/view\n")


def make_system(*, voice=None, rocketlane=None, slack=None, mail=None, llm=None, settings=None):
    d = tempfile.mkdtemp()
    st = settings or settings_for_tests()
    voice = voice or MockVoice([("completed", "enterprise")])
    rocketlane = rocketlane if rocketlane is not None else MockRocketlane()
    slack = slack or MockSlack()
    mail = mail or MockEmailSource()
    llm = llm or MockLLM()
    sysm = build_system(st, llm=llm, voice=voice, rocketlane=rocketlane, slack=slack, email_source=mail,
                        audit_path=os.path.join(d, "audit.jsonl"), escalation_path=os.path.join(d, "esc.jsonl"))
    return sysm, dict(voice=voice, rocketlane=rocketlane, slack=slack, mail=mail, dir=d)
