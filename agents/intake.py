"""Agent 1: Intake & Routing.

Responsibilities, in order, and it stops at the first one that fails:
  1. Parse the AE's email into a DealNotification. Deterministic parser first
     (labelled emails), LLM second (free text). Both results are validated by the
     same pydantic model.
  2. If any required field is missing: reply to the AE asking for exactly the
     missing fields, and stop. Never guess.
  3. Check Rocketlane for an existing onboarding project for this customer. If one
     exists: stop and escalate (a human decides whether this is a duplicate or a
     second engagement).
  4. Place a real voice call to the AE to confirm the plan tier. Accept only an
     unambiguous "enterprise" or "growth". On no-answer or "unclear", retry up to
     the configured limit, then escalate. Never assume a tier.
  5. Create the Rocketlane project with the tier's template and timeline.

Every step writes an audit entry with rationale.
"""
from __future__ import annotations
import re
import time
from datetime import date
from typing import Optional
from pydantic import ValidationError
from models import DealNotification, PlanTier, RocketlaneProject, VoiceCallResult
from providers.rocketlane import RocketlaneUnavailable, build_spec

REQUIRED = ("customer_name", "customer_contact_email", "ae_name", "opportunity_link")

# Labelled-line parser. Priya only said the email carries the customer name and the Salesforce
# link, not how it is formatted. Labelled lines are the cheapest format to parse reliably, so the
# clarification request asks the AE for exactly this shape. Free-text emails fall through to the LLM.
_LABELS = {
    "customer_name": r"^\s*(?:customer|customer name|account|company)\s*[:\-]\s*(.+?)\s*$",
    "customer_contact_email": r"^\s*(?:customer contact|contact email|contact|customer email)\s*[:\-]\s*<?([^\s>]+@[^\s>]+)>?\s*$",
    "ae_name": r"^\s*(?:ae|account executive|rep|owner)\s*[:\-]\s*(.+?)\s*$",
    "opportunity_link": r"^\s*(?:opportunity|opportunity link|salesforce|sfdc|opp)\s*[:\-]\s*(https?://\S+)\s*$",
    "ae_email": r"^\s*(?:ae email|rep email)\s*[:\-]\s*<?([^\s>]+@[^\s>]+)>?\s*$",
}
_URL_RE = re.compile(r"https?://[^\s>]+salesforce[^\s>]*|https?://[^\s>]+lightning\.force\.com[^\s>]*", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SUBJECT_RE = re.compile(r"^\s*(?:closed[\s-]*won|new deal|deal closed|signed)\s*[:\-]\s*(.+?)\s*$", re.I)


def parse_labelled(subject: str, body: str) -> dict:
    """First matching line wins for every field. A later 'Company: NovaCRM Inc.' in the AE's
    signature, or 'Account: 4471002' in a CRM footer, must never overwrite the customer name
    the AE actually typed. The subject ('Closed Won: <name>') is used only when the body has
    no customer label at all."""
    found: dict[str, Optional[str]] = {k: None for k in _LABELS}
    for line in body.splitlines():
        for key, pat in _LABELS.items():
            if found[key] is None:
                m = re.match(pat, line, re.I)
                if m:
                    found[key] = m.group(1).strip()
    if found["customer_name"] is None:
        m = _SUBJECT_RE.match(subject or "")
        if m:
            found["customer_name"] = m.group(1).strip()
    if found["opportunity_link"] is None:
        m = _URL_RE.search(body)
        if m:
            found["opportunity_link"] = m.group(0)
    return found


def _appears_in(value: str, text: str) -> bool:
    """True if the value occurs verbatim (case- and whitespace-insensitive) in the email text.
    Used to make sure a model can only extract, never invent."""
    norm = lambda s: " ".join(str(s or "").split()).lower()   # noqa: E731
    v = norm(value)
    return bool(v) and v in norm(text)


def _sender_email(sender: str) -> Optional[str]:
    m = _EMAIL_RE.search(sender or "")
    return m.group(0) if m else None


class IntakeRoutingAgent:
    name = "intake_routing_agent"

    def __init__(self, *, llm, voice, rocketlane, email_source, audit, settings):
        self.llm, self.voice, self.rocketlane = llm, voice, rocketlane
        self.email_source, self.audit, self.settings = email_source, audit, settings

    # ---- step 0: is this email allowed to trigger anything at all? ------------------------------
    def admissible(self, inbound) -> Optional[str]:
        """Return a reason string if the email must be ignored, else None."""
        if getattr(inbound, "auto_submitted", False):
            return "auto-submitted message (autoresponder or bounce)"
        sender = _sender_email(inbound.sender) or ""
        if sender and self.settings.gmail_address and sender.lower() == self.settings.gmail_address.lower():
            return "sent by the agent's own mailbox"
        allowed = self.settings.allowed_sender_domains
        if allowed:
            domain = sender.rsplit("@", 1)[-1].lower() if "@" in sender else ""
            if domain not in allowed:
                return f"sender domain '{domain}' is not on the allow-list"
        return None

    # ---- step 1 + 2: parse and validate ---------------------------------------------------------
    def parse(self, inbound) -> tuple[Optional[DealNotification], list[str]]:
        cid = inbound.message_id
        fields = parse_labelled(inbound.subject, inbound.body)
        source = "deterministic"
        if any(fields.get(k) is None for k in REQUIRED):
            try:
                llm_fields = self.llm.extract_deal_fields(inbound.subject, inbound.body) or {}
                self.audit.record(self.name, "llm_extraction", inputs={"subject": inbound.subject},
                                  outputs={"model_returned": llm_fields},
                                  rationale="Raw model output recorded before validation so accepted vs rejected values can be audited.",
                                  correlation_id=cid)
                email_text = f"{inbound.subject}\n{inbound.body}\n{inbound.sender}"
                invented = {}
                for k in _LABELS:
                    if fields.get(k) is None and llm_fields.get(k):
                        val = str(llm_fields[k]).strip()
                        # A well-formed value the email never contained is a hallucination, not a
                        # field. Format validation cannot catch it; this does.
                        if _appears_in(val, email_text):
                            fields[k] = val
                        else:
                            invented[k] = val
                if invented:
                    self.audit.record(self.name, "llm_values_rejected", outputs={"not_in_email": invented},
                                      rationale="Model returned values that do not occur in the email text; discarded as invented.",
                                      level="warning", correlation_id=cid)
                source = f"deterministic+{self.llm.name}"
            except Exception as e:  # noqa: BLE001
                self.audit.record(self.name, "llm_extraction_failed", outputs=str(e),
                                  rationale="LLM unavailable; validation will decide with what the parser found.",
                                  level="warning", correlation_id=cid)
        if not fields.get("ae_email"):
            fields["ae_email"] = _sender_email(inbound.sender)
        missing = [k for k in REQUIRED if not fields.get(k)]
        if missing:
            self.audit.record(self.name, "validation_failed", inputs={"subject": inbound.subject, "parsed": fields},
                              outputs={"missing": missing},
                              rationale=f"Required fields absent after {source} parse. Asking, not guessing.",
                              level="warning", correlation_id=cid)
            return None, missing
        try:
            deal = DealNotification(**{k: fields.get(k) for k in _LABELS}, source_message_id=cid, raw_subject=inbound.subject)
        except ValidationError as e:
            bad = sorted({err["loc"][0] for err in e.errors()})
            self.audit.record(self.name, "validation_failed", inputs=fields, outputs={"invalid": bad, "detail": e.errors()},
                              rationale="Fields present but malformed (bad email or link). Asking, not guessing.",
                              level="warning", correlation_id=cid)
            return None, [str(b) for b in bad]
        self.audit.record(self.name, "parsed_deal", inputs={"subject": inbound.subject}, outputs=deal,
                          rationale=f"All required fields present and valid via {source} parse.", correlation_id=cid)
        return deal, []

    def request_clarification(self, inbound, missing: list[str]) -> None:
        to = _sender_email(inbound.sender) or self.settings.human_escalation_email
        wanted = ", ".join(missing)
        body = ("Hi,\n\nThe onboarding system could not start a project from your deal email because these fields "
                f"were missing or unreadable: {wanted}.\n\nPlease reply with them in this format:\n"
                "Customer: <name>\nContact email: <email>\nAE: <your name>\nOpportunity: <Salesforce link>\n\n"
                "Nothing was created. Thanks.")
        self.email_source.send(to, f"Action needed: onboarding could not start for '{inbound.subject}'", body,
                               in_reply_to=inbound.message_id)
        self.audit.record(self.name, "clarification_requested", inputs={"to": to, "missing": missing},
                          rationale="Guardrail: incomplete input triggers a question, never a default value.",
                          correlation_id=inbound.message_id)

    # ---- step 3: duplicate check ----------------------------------------------------------------
    def existing_project(self, deal: DealNotification) -> Optional[RocketlaneProject]:
        try:
            found = self.rocketlane.find_existing_project(deal.customer_name)
        except RocketlaneUnavailable as e:
            self.audit.record(self.name, "duplicate_check_unavailable", outputs=str(e),
                              rationale="Cannot prove there is no existing project; treating as blocked.",
                              level="error", correlation_id=deal.source_message_id)
            raise
        self.audit.record(self.name, "duplicate_check", inputs={"customer": deal.customer_name},
                          outputs=found, rationale="Existing project means a human decides, not the agent.",
                          correlation_id=deal.source_message_id)
        return found

    # ---- step 4: confirm tier by voice ----------------------------------------------------------
    class NoPhoneOnFile(Exception):
        pass

    def confirm_tier(self, deal: DealNotification) -> tuple[PlanTier, list[VoiceCallResult]]:
        phone = self.settings.ae_phone_directory.get(deal.ae_name)
        results: list[VoiceCallResult] = []
        if not phone:
            self.audit.record(self.name, "ae_phone_missing", inputs={"ae_name": deal.ae_name},
                              rationale="No phone number on file for this AE; cannot confirm tier by voice.",
                              level="warning", correlation_id=deal.source_message_id)
            raise IntakeRoutingAgent.NoPhoneOnFile(deal.ae_name)
        for attempt in range(1, self.settings.voice_max_attempts + 1):
            if attempt > 1 and self.settings.voice_retry_delay_seconds > 0:
                time.sleep(self.settings.voice_retry_delay_seconds)   # do not redial a person five seconds later
            try:
                res = self.voice.confirm_plan_tier(ae_name=deal.ae_name, ae_phone=phone, customer=deal.customer_name)
            except Exception as e:  # noqa: BLE001 - provider outage is a retryable, loggable event
                res = VoiceCallResult(provider=self.voice.name, call_id=f"error-{attempt}", status="failed",
                                      plan_tier=PlanTier.UNCLEAR, transcript=f"provider error: {e}")
            results.append(res)
            self.audit.record(self.name, "voice_confirmation_attempt",
                              inputs={"attempt": attempt, "ae": deal.ae_name, "provider": self.voice.name},
                              outputs={"status": res.status, "plan_tier": res.plan_tier.value, "call_id": res.call_id,
                                       "transcript": res.transcript[:1500]},
                              rationale="Tier must come from the AE by voice; only enterprise or growth is accepted. "
                                        "Provider named so a mock can never pass for a real call in the log. Transcript kept so a human can check the classifier.",
                              correlation_id=deal.source_message_id)
            if res.status == "completed" and res.plan_tier in (PlanTier.ENTERPRISE, PlanTier.GROWTH):
                return res.plan_tier, results
        return PlanTier.UNCLEAR, results

    # ---- step 5: create project ------------------------------------------------------------------
    def create_project(self, deal: DealNotification, tier: PlanTier, start: Optional[date] = None) -> RocketlaneProject:
        spec = build_spec(deal.customer_name, str(deal.customer_contact_email), tier, start or date.today())
        template_id = (self.settings.rocketlane_template_id_enterprise if tier == PlanTier.ENTERPRISE
                       else self.settings.rocketlane_template_id_growth) or None
        # "Dedicated CSM" vs "pooled CSM" is a real staffing difference, not copy: the project owner
        # is the tier's CSM. Both default to the workspace owner on a one-person trial.
        owner = (self.settings.rocketlane_enterprise_csm_email if tier == PlanTier.ENTERPRISE
                 else self.settings.rocketlane_growth_csm_email) or self.settings.rocketlane_owner_email
        proj = self.rocketlane.create_onboarding_project(spec, owner_email=owner, template_id=template_id)
        self.audit.record(self.name, "project_created", inputs={"spec": spec, "owner_email": owner, "template_id": template_id},
                          outputs=proj,
                          rationale=f"Tier confirmed as {tier.value}: {spec.onboarding_days}-day plan, {spec.csm_model} CSM, owned by {owner}.",
                          correlation_id=deal.source_message_id)
        unowned = [t for pid, t in getattr(self.rocketlane, "unowned_tasks", []) if pid == proj.project_id]
        if unowned:
            self.audit.record(self.name, "tasks_created_unowned", outputs={"project_id": proj.project_id, "tasks": unowned},
                              rationale="Workspace rejected the customer assignee; these tasks exist but nobody owns them. Needs a person.",
                              level="warning", correlation_id=deal.source_message_id)
        return proj
