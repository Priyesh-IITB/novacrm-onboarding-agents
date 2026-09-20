"""Typed contracts between agents. If a value is not one of these, it does not
cross an agent boundary. That is the first guardrail."""
from __future__ import annotations
from datetime import date
from enum import Enum
from typing import Optional
import re
from pydantic import BaseModel, Field, field_validator

_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


class PlanTier(str, Enum):
    ENTERPRISE = "enterprise"
    GROWTH = "growth"
    UNCLEAR = "unclear"      # the only allowed non-answer; never silently mapped to a tier


class DealNotification(BaseModel):
    """What the Intake Agent must extract from an AE's email before anything else happens.

    Every field below is REQUIRED. Missing any of them means the email is
    incomplete and the agent asks for clarification instead of guessing.
    """
    customer_name: str = Field(min_length=2)
    customer_contact_email: str
    ae_name: str = Field(min_length=2)
    opportunity_link: str = Field(min_length=8)
    source_message_id: str
    ae_email: Optional[str] = None            # nice to have: where clarification requests go
    raw_subject: str = ""

    @field_validator("customer_contact_email")
    @classmethod
    def _looks_like_an_email(cls, v: str) -> str:
        v = (v or "").strip().strip("<>")
        if not _EMAIL.match(v):
            raise ValueError("customer_contact_email is not a valid email address")
        return v

    @field_validator("opportunity_link")
    @classmethod
    def _looks_like_a_link(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("opportunity_link must be a URL")
        return v


class VoiceCallResult(BaseModel):
    provider: str
    call_id: str
    status: str                     # completed | no-answer | busy | failed | mock
    plan_tier: PlanTier
    transcript: str = ""
    raw: dict = Field(default_factory=dict)


class ProjectSpec(BaseModel):
    """Fully resolved plan for a Rocketlane project. Only built after the tier is confirmed."""
    customer_name: str
    customer_contact_email: str
    plan_tier: PlanTier
    onboarding_days: int
    csm_model: str                  # "dedicated" | "pooled"
    start_date: date
    due_date: date
    project_name: str


class RocketlaneProject(BaseModel):
    project_id: int
    project_name: str
    url: Optional[str] = None
    created: bool = True            # False when an existing project was found and reused


class SlackChannelResult(BaseModel):
    channel_id: str
    channel_name: str
    topic: str
    welcome_message: str


class OnboardingOutcome(BaseModel):
    """The orchestrator's single return value per email. Every branch ends here."""
    status: str                     # completed | escalated | rejected | skipped
    reason: str
    deal: Optional[DealNotification] = None
    tier: Optional[PlanTier] = None
    project: Optional[RocketlaneProject] = None
    slack: Optional[SlackChannelResult] = None
