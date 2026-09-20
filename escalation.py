"""Human escalation. The agents never guess; when they cannot proceed they hand
the case to a person with everything needed to pick it up.

Escalations are written to a durable JSONL queue first, then mirrored to Slack
and email on a best-effort basis. The file is the source of truth so that a
Slack outage cannot lose an escalation.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any, Optional
from audit import _safe


class Escalator:
    def __init__(self, path: str, audit, slack=None, mailer=None, channel: str = "", email: str = ""):
        self.path, self.audit, self.slack, self.mailer = path, audit, slack, mailer
        self.channel, self.email = channel, email

    def escalate(self, *, reason: str, context: dict[str, Any], correlation_id: Optional[str] = None,
                 severity: str = "medium") -> dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "severity": severity,
            "reason": reason,
            "correlation_id": correlation_id,
            "context": _safe(context),
            "status": "open",
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        self.audit.record("escalator", "escalate_to_human", inputs=entry["context"], outputs={"reason": reason},
                          rationale="Agent could not proceed safely; handing to a person rather than guessing.",
                          level="warning", correlation_id=correlation_id)
        # Slack and email get the same redacted context as the file. Those two are what people read.
        text = f":rotating_light: Onboarding escalation ({severity}): {reason}\n```{json.dumps(entry['context'], default=str, indent=1)[:1500]}```"
        if self.slack is not None and self.channel:
            try:
                self.slack.post_message(self.channel, text)
            except Exception as e:  # noqa: BLE001 - best effort by design
                self.audit.record("escalator", "slack_notify_failed", outputs=str(e),
                                  rationale="Slack mirror failed; the JSONL queue still holds the escalation.",
                                  level="warning", correlation_id=correlation_id)
        if self.mailer is not None and self.email:
            try:
                self.mailer.send(self.email, f"[Onboarding escalation] {reason}", text, None)
            except Exception as e:  # noqa: BLE001
                self.audit.record("escalator", "email_notify_failed", outputs=str(e),
                                  rationale="Email mirror failed; the JSONL queue still holds the escalation.",
                                  level="warning", correlation_id=correlation_id)
        return entry
