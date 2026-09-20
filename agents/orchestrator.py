"""The orchestrator owns the loop and the failure branches. Each agent does one
job; the orchestrator decides what a failure means.

Branches, per email:
  skipped    - not an admissible trigger (autoresponder, wrong sender domain, our own mail)
  rejected   - incomplete or malformed email; clarification sent to the AE
  escalated  - existing project, tier unconfirmed, no phone on file, Rocketlane down or
               rejecting, partial creation, Slack failure after creation, or any unexpected error
  completed  - project created and Slack channel opened

The rule: every path that is not "completed" ends with either a question to the AE
or an escalation to a person. Nothing is silently dropped, and an unexpected
exception is an escalation, not a crash.
"""
from __future__ import annotations
import time
import traceback
from datetime import date
from typing import Optional
from models import OnboardingOutcome, PlanTier
from providers.rocketlane import RocketlaneError, RocketlanePartial, RocketlaneUnavailable


class Orchestrator:
    name = "orchestrator"

    def __init__(self, *, intake, communication, email_source, audit, escalator, settings):
        self.intake, self.comm, self.email_source = intake, communication, email_source
        self.audit, self.escalator, self.settings = audit, escalator, settings

    def _commit_before_dialling(self, inbound):
        """A phone call to a person cannot be undone. Flag the message as processed BEFORE the
        first dial, so a crash mid-call cannot make the next poll ring the AE again. The trade-off
        (a crash loses the email rather than re-calling) is the right one, and the audit entry
        written here is what a person needs to pick the case up."""
        try:
            self.email_source.mark_processed(inbound)
        except Exception as e:  # noqa: BLE001
            self.audit.record(self.name, "mark_processed_failed", outputs=str(e),
                              rationale="Could not flag the message before dialling; the processed-id file still applies.",
                              level="warning", correlation_id=inbound.message_id)
        self.audit.record(self.name, "voice_call_placing", inputs={"subject": inbound.subject},
                          rationale="About to place a real call to the AE; recorded before dialling so an interrupted run leaves a trace.",
                          correlation_id=inbound.message_id)

    def handle(self, inbound, *, start_date: Optional[date] = None) -> OnboardingOutcome:
        cid = inbound.message_id
        self.audit.record(self.name, "email_received", inputs={"subject": inbound.subject, "from": inbound.sender},
                          rationale="New message from the monitored inbox (the subject filter is applied at fetch time).", correlation_id=cid)

        why_not = self.intake.admissible(inbound)
        if why_not:
            self.audit.record(self.name, "email_skipped", outputs={"reason": why_not},
                              rationale="Guardrail: only genuine AE deal emails may trigger calls and project creation.",
                              level="warning", correlation_id=cid)
            return OnboardingOutcome(status="skipped", reason=why_not)

        deal, missing = self.intake.parse(inbound)
        if deal is None:
            try:
                self.intake.request_clarification(inbound, missing)
            except Exception as e:  # noqa: BLE001
                self._escalate("Could not send clarification request to AE", "medium", cid,
                               {"subject": inbound.subject, "missing": missing, "error": str(e)})
            return OnboardingOutcome(status="rejected", reason=f"missing or invalid fields: {', '.join(missing)}")

        tier: Optional[PlanTier] = None
        project = None
        try:
            existing = self.intake.existing_project(deal)
            if existing is not None:
                self._escalate("Customer already has an onboarding project", "medium", cid,
                               {"deal": deal.model_dump(mode="json"), "existing": existing.model_dump()})
                return OnboardingOutcome(status="escalated", reason="existing project found", deal=deal, project=existing)

            self._commit_before_dialling(inbound)
            try:
                tier, calls = self.intake.confirm_tier(deal)
            except self.intake.NoPhoneOnFile:
                self._escalate("No phone number on file for the AE; tier cannot be confirmed", "medium", cid,
                               {"deal": deal.model_dump(mode="json"), "ae_name": deal.ae_name})
                return OnboardingOutcome(status="escalated", reason="ae phone missing", deal=deal)
            if tier == PlanTier.UNCLEAR:
                self._escalate("Plan tier not confirmed by AE after voice attempts", "medium", cid,
                               {"deal": deal.model_dump(mode="json"),
                                "attempts": [c.model_dump(mode="json", exclude={"raw"}) for c in calls]})
                return OnboardingOutcome(status="escalated", reason="tier unconfirmed", deal=deal, tier=tier)

            project = self.intake.create_project(deal, tier, start=start_date)

            try:
                slack = self.comm.run(deal, tier, project)
            except Exception as e:  # noqa: BLE001 - the project exists; a person must finish the channel
                self._escalate("Rocketlane project created but Slack channel failed", "medium", cid,
                               {"deal": deal.model_dump(mode="json"), "tier": tier.value,
                                "project_id": project.project_id, "project": project.model_dump(), "error": str(e)})
                return OnboardingOutcome(status="escalated", reason="slack failed after project creation",
                                         deal=deal, tier=tier, project=project)

            self.audit.record(self.name, "onboarding_started",
                              outputs={"project_id": project.project_id, "channel": slack.channel_name},
                              rationale="All guardrails passed; onboarding is live in Rocketlane and Slack.", correlation_id=cid)
            return OnboardingOutcome(status="completed", reason="ok", deal=deal, tier=tier, project=project, slack=slack)

        except RocketlanePartial as e:
            self._escalate("Rocketlane project partially created; needs a person to finish or delete it", "high", cid,
                           {"deal": deal.model_dump(mode="json"), "tier": tier.value if tier else None,
                            "project_id": e.project_id, "error": str(e)})
            return OnboardingOutcome(status="escalated", reason=f"rocketlane partial creation (project {e.project_id})",
                                     deal=deal, tier=tier)
        except RocketlaneUnavailable as e:
            stage = "project creation" if tier else "duplicate check"
            self._escalate(f"Rocketlane unavailable during {stage} (retries exhausted)", "high", cid,
                           {"deal": deal.model_dump(mode="json"), "tier": tier.value if tier else None, "error": str(e)})
            return OnboardingOutcome(status="escalated", reason=f"rocketlane unavailable ({stage})", deal=deal, tier=tier)
        except RocketlaneError as e:
            stage = "project creation" if tier else "duplicate check"
            self._escalate(f"Rocketlane rejected the request during {stage}", "high", cid,
                           {"deal": deal.model_dump(mode="json"), "tier": tier.value if tier else None, "error": str(e)})
            return OnboardingOutcome(status="escalated", reason=f"rocketlane error ({stage})", deal=deal, tier=tier)
        except Exception as e:  # noqa: BLE001 - last line of defence: an unknown failure is still an escalation
            self._escalate("Unexpected error while onboarding", "high", cid,
                           {"deal": deal.model_dump(mode="json"), "tier": tier.value if tier else None,
                            "project": project.model_dump() if project else None,
                            "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]})
            return OnboardingOutcome(status="escalated", reason=f"unexpected error: {type(e).__name__}", deal=deal, tier=tier)

    def _escalate(self, reason, severity, cid, context):
        self.escalator.escalate(reason=reason, severity=severity, context=context, correlation_id=cid)

    def run_once(self) -> list[OnboardingOutcome]:
        outcomes = []
        for m in self.email_source.fetch_new():
            outcome = self.handle(m)
            outcomes.append(outcome)
            try:
                self.email_source.mark_processed(m)     # only after the outcome is decided and recorded
            except Exception as e:  # noqa: BLE001
                self.audit.record(self.name, "mark_processed_failed", outputs=str(e),
                                  rationale="Could not flag the message; the processed-id file still prevents a second run.",
                                  level="warning", correlation_id=m.message_id)
        return outcomes

    def watch(self, poll_seconds: Optional[int] = None) -> None:
        interval = poll_seconds or self.settings.poll_seconds
        self.audit.record(self.name, "watch_started", inputs={"poll_seconds": interval},
                          rationale="Polling the inbox; production would use Gmail push via Pub/Sub instead.")
        while True:
            try:
                for outcome in self.run_once():
                    print(f"[{outcome.status}] {outcome.reason}")
            except Exception as e:  # noqa: BLE001 - a bad poll (IMAP hiccup) must not kill the loop
                self.audit.record(self.name, "poll_error", outputs=str(e),
                                  rationale="Inbox fetch failed; will retry on the next interval.", level="error")
            time.sleep(interval)


def build_system(settings, *, llm=None, voice=None, rocketlane=None, slack=None, email_source=None,
                 audit_path=None, escalation_path=None):
    """Wire everything. Any provider can be injected (tests do), otherwise built from settings."""
    from audit import AuditLog
    from escalation import Escalator
    from providers.llm import build_llm
    from providers.voice import build_voice
    from providers.rocketlane import build_rocketlane
    from providers.slack import build_slack
    from providers.email_source import build_email_source
    from agents.intake import IntakeRoutingAgent
    from agents.communication import CommunicationAgent

    audit = AuditLog(audit_path or settings.audit_log_path)

    def rl_audit(method, path, status, note):
        audit.record("rocketlane_client", "api_call", inputs={"method": method, "path": path},
                     outputs={"http_status": status, "note": note},
                     rationale="Every Rocketlane call recorded, including retries, so a partial build can be reconstructed.",
                     level="info" if status < 400 and status != 0 else "warning")

    def slack_audit(method, ok, note):
        audit.record("slack_client", "api_call", inputs={"method": method}, outputs={"ok": ok, "note": note},
                     rationale="Every Slack call recorded so a half-built channel can be found and finished.",
                     level="info" if ok else "warning")

    llm = llm or build_llm(settings)
    voice = voice or build_voice(settings)
    rocketlane = rocketlane or build_rocketlane(settings, on_call=rl_audit)
    slack = slack or build_slack(settings, on_call=slack_audit)
    email_source = email_source or build_email_source(settings)
    escalator = Escalator(escalation_path or settings.escalation_log_path, audit, slack=slack, mailer=email_source,
                          channel=settings.slack_escalation_channel, email=settings.human_escalation_email)
    intake = IntakeRoutingAgent(llm=llm, voice=voice, rocketlane=rocketlane, email_source=email_source,
                                audit=audit, settings=settings)
    comm = CommunicationAgent(slack=slack, audit=audit)
    return Orchestrator(intake=intake, communication=comm, email_source=email_source, audit=audit,
                        escalator=escalator, settings=settings)
