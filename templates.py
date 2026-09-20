"""The two onboarding templates Priya described, encoded so the agent can build them
directly when Rocketlane template IDs are not configured.

Four phases, fifteen tasks. Enterprise runs 30 days with a dedicated CSM; Growth
runs 14 days with a pooled CSM. Task due dates are expressed as a fraction of the
plan length so the same plan scales to either timeline.

The one task that matters most is the last one in Data Migration: "Customer
verifies migrated data". Priya said migration tasks were marked done without the
customer actually checking. So verification is a SEPARATE task, assigned to the
customer contact, and Go-Live depends on it. Marking "migrate" done no longer
implies "verified".
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from models import PlanTier


@dataclass(frozen=True)
class TaskTemplate:
    name: str
    phase: str
    due_fraction: float             # 0.0 .. 1.0 of the onboarding window
    assignee: str = "csm"           # "csm" | "customer" | "engineer"
    description: str = ""


@dataclass(frozen=True)
class PlanTemplate:
    tier: PlanTier
    onboarding_days: int
    csm_model: str
    phases: tuple[str, ...]
    tasks: tuple[TaskTemplate, ...]


PHASES = ("Kickoff", "Data Migration", "Configuration", "Go-Live")

_TASKS = (
    # Kickoff
    TaskTemplate("Internal kickoff and account review", "Kickoff", 0.05, "csm"),
    TaskTemplate("Schedule customer kickoff call", "Kickoff", 0.07, "csm"),
    TaskTemplate("Customer kickoff call held", "Kickoff", 0.12, "csm"),
    TaskTemplate("Success criteria agreed and documented", "Kickoff", 0.15, "csm"),
    # Data Migration
    TaskTemplate("Customer provides source data export", "Data Migration", 0.25, "customer"),
    TaskTemplate("Map source fields to NovaCRM objects", "Data Migration", 0.35, "engineer"),
    TaskTemplate("Run migration into sandbox", "Data Migration", 0.45, "engineer"),
    TaskTemplate("Customer verifies migrated data", "Data Migration", 0.52, "customer",
                 "Customer confirms record counts and spot-checks 20 records. Go-Live is blocked until this task is complete."),
    # Configuration
    TaskTemplate("Configure pipelines and stages", "Configuration", 0.60, "engineer"),
    TaskTemplate("Configure users, roles and SSO", "Configuration", 0.68, "engineer"),
    TaskTemplate("Configure integrations (email, calendar)", "Configuration", 0.75, "engineer"),
    TaskTemplate("Admin training session delivered", "Configuration", 0.82, "csm"),
    # Go-Live
    TaskTemplate("Production cutover", "Go-Live", 0.90, "engineer"),
    TaskTemplate("Hypercare check-in", "Go-Live", 0.96, "csm"),
    TaskTemplate("Handoff summary written and support team briefed", "Go-Live", 1.00, "csm"),
)

ENTERPRISE = PlanTemplate(PlanTier.ENTERPRISE, 30, "dedicated", PHASES, _TASKS)
GROWTH = PlanTemplate(PlanTier.GROWTH, 14, "pooled", PHASES, _TASKS)


def template_for(tier: PlanTier) -> PlanTemplate:
    if tier == PlanTier.ENTERPRISE:
        return ENTERPRISE
    if tier == PlanTier.GROWTH:
        return GROWTH
    raise ValueError(f"no onboarding template for tier {tier!r}; only a confirmed enterprise or growth tier has one")


def phase_windows(start: date, days: int) -> dict[str, tuple[date, date]]:
    """Split the onboarding window into four consecutive phase windows."""
    bounds = [0.0, 0.15, 0.55, 0.85, 1.0]
    out = {}
    for i, name in enumerate(PHASES):
        s = start + timedelta(days=round(days * bounds[i]))
        e = start + timedelta(days=round(days * bounds[i + 1]))
        if e <= s:
            e = s + timedelta(days=1)
        out[name] = (s, e)
    return out


def task_due(start: date, days: int, t: TaskTemplate) -> date:
    return start + timedelta(days=max(1, round(days * t.due_fraction)))
