"""CLI.

  python run.py --watch            poll the real inbox forever (needs .env)
  python run.py --once             process whatever is unseen right now, then exit
  python run.py --demo positive    scripted happy path with mocks (no accounts needed)
  python run.py --demo negative    scripted failure paths with mocks
  python run.py --demo live        real inbox + real voice + real Rocketlane, single pass, verbose

The demo modes exist so the video can show both scenarios deterministically and
so a reviewer can run the whole thing in ten seconds with zero credentials.
"""
from __future__ import annotations
import argparse
import json
import sys
from config import settings
from agents.orchestrator import build_system
from providers.email_source import InboundEmail, MockEmailSource
from providers.voice import MockVoice
from providers.rocketlane import MockRocketlane
from providers.slack import MockSlack
from providers.llm import MockLLM
from models import RocketlaneProject

GOOD_EMAIL = InboundEmail(
    message_id="<demo-happy-1@novacrm>", sender="Alex Rivera <alex.rivera@novacrm.example>",
    subject="Closed Won: Brightline Logistics",
    body="Customer: Brightline Logistics\nContact email: maria.chen@brightline.example\nAE: Alex Rivera\n"
         "Opportunity: https://novacrm.lightning.force.com/lightning/r/Opportunity/006ABC123/view\n\nSigned today, kickoff ASAP.",
)
BAD_EMAIL_MISSING = InboundEmail(
    message_id="<demo-bad-1@novacrm>", sender="Alex Rivera <alex.rivera@novacrm.example>",
    subject="Closed Won: Harbor & Finch",
    body="We closed Harbor & Finch today. Opportunity: https://novacrm.lightning.force.com/lightning/r/Opportunity/006XYZ/view\nLet's get them started.",
)
BAD_EMAIL_MALFORMED = InboundEmail(
    message_id="<demo-bad-2@novacrm>", sender="Alex Rivera <alex.rivera@novacrm.example>",
    subject="Closed Won: Quill Apparel",
    body="Customer: Quill Apparel\nContact email: not-an-email\nAE: Alex Rivera\nOpportunity: see salesforce",
)
GROWTH_EMAIL = InboundEmail(
    message_id="<demo-growth-1@novacrm>", sender="Dana Okafor <dana.okafor@novacrm.example>",
    subject="Closed Won: Pinecrest Dental",
    body="Customer: Pinecrest Dental\nContact email: office@pinecrest.example\nAE: Dana Okafor\n"
         "Opportunity: https://novacrm.lightning.force.com/lightning/r/Opportunity/006PINE/view",
)


def _print(outcome, audit_path):
    print(json.dumps(outcome.model_dump(mode="json", exclude_none=True), indent=2, default=str))
    print(f"(audit trail appended to {audit_path})\n")


def demo(kind: str):
    audit_path, esc_path = f"demo_{kind}_audit.jsonl", f"demo_{kind}_escalations.jsonl"
    for p in (audit_path, esc_path):
        open(p, "w").close()
    directory = {"Alex Rivera": "+15550100001", "Dana Okafor": "+15550100002"}
    st = settings.__class__(**{**settings.__dict__, "ae_phone_directory": directory, "voice_max_attempts": 2,
                               "voice_retry_delay_seconds": 0, "allowed_sender_domains": (), "gmail_address": ""})
    if kind == "positive":
        voice = MockVoice([("completed", "enterprise"), ("completed", "growth")])
        rl, slack, mail = MockRocketlane(), MockSlack(), MockEmailSource([GOOD_EMAIL, GROWTH_EMAIL])
        sysm = build_system(st, llm=MockLLM(), voice=voice, rocketlane=rl, slack=slack, email_source=mail,
                            audit_path=audit_path, escalation_path=esc_path)
        print("=== POSITIVE: Enterprise deal, then a Growth deal ===")
        for o in sysm.run_once():
            _print(o, audit_path)
        if rl.tasks:
            pid = max(rl.tasks)
            print(f"Rocketlane mock now holds {len(rl.phases[pid])} phases and {len(rl.tasks[pid])} tasks for the last project.")
        print("Slack mock channels:", {c["name"]: c["topic"][:60] for c in slack.channels.values()})
    elif kind == "negative":
        existing = {"brightline logistics": RocketlaneProject(project_id=777, project_name="Brightline Logistics onboarding", created=False)}
        voice = MockVoice([("no-answer", ""), ("completed", "unclear")])       # both attempts fail to confirm
        rl, slack = MockRocketlane(existing=existing), MockSlack()
        mail = MockEmailSource([])
        sysm = build_system(st, llm=MockLLM(), voice=voice, rocketlane=rl, slack=slack, email_source=mail,
                            audit_path=audit_path, escalation_path=esc_path)
        scenarios = [
            ("NEGATIVE 1: missing fields (no customer contact, no AE line) -> clarification email to the AE, nothing created", BAD_EMAIL_MISSING),
            ("NEGATIVE 2: malformed contact email and opportunity link -> clarification email, nothing created", BAD_EMAIL_MALFORMED),
            ("NEGATIVE 3: customer already has an onboarding project -> escalated, AE not called, nothing created", GOOD_EMAIL),
            ("NEGATIVE 4: AE does not answer, retry gets an ambiguous answer -> escalated, tier never assumed", GROWTH_EMAIL),
        ]
        for title, em in scenarios:
            print(f"=== {title} ===")
            _print(sysm.handle(em), audit_path)
        print("Emails the agent sent (clarifications go to the AE, escalations to the CS leads address):")
        for m in mail.sent:
            print(f"  to={m['to']}  subject={m['subject']!r}")
        print()

        print("=== NEGATIVE 5: Rocketlane returns 503 on project creation -> escalated (high), nothing half-built ===")
        print("    (HTTP retries with backoff live inside RocketlaneAPI._req and are covered by tests/test_failure_paths.py;"
              " the mock raises Unavailable directly to keep the demo fast.)")
        rl2 = MockRocketlane(fail_times=5)
        sysm2 = build_system(st, llm=MockLLM(), voice=MockVoice([("completed", "enterprise")]), rocketlane=rl2, slack=slack,
                             email_source=MockEmailSource([]), audit_path=audit_path, escalation_path=esc_path)
        _print(sysm2.handle(GOOD_EMAIL), audit_path)

        print("=== NEGATIVE 6: project created, then Slack fails -> escalated with the project id so a person finishes the channel ===")
        class _BrokenSlack(MockSlack):
            def create_channel(self, name):
                raise RuntimeError("Slack: missing_scope channels:manage")
        sysm3 = build_system(st, llm=MockLLM(), voice=MockVoice([("completed", "growth")]), rocketlane=MockRocketlane(),
                             slack=_BrokenSlack(), email_source=MockEmailSource([]), audit_path=audit_path, escalation_path=esc_path)
        _print(sysm3.handle(GROWTH_EMAIL), audit_path)

        with open(esc_path) as f:
            n = sum(1 for l in f if l.strip())
        print(f"Escalation queue: {esc_path} ({n} entries, each with severity, reason and full context)")
    elif kind == "live":
        sysm = _live_system(allow_mocks=False)
        for o in sysm.run_once():
            _print(o, settings.audit_log_path)
    else:
        sys.exit("demo must be positive | negative | live")


def _live_system(allow_mocks: bool):
    """Build against the real inbox. Refuse to run with a mock in the loop unless told to,
    because a mock voice confirms 'enterprise' without ringing anyone and a mock Rocketlane
    reports 'completed' while creating nothing. Also refuse an empty sender allow-list: anyone
    who can land a 'Closed Won' subject in the inbox would otherwise trigger a real phone call."""
    sysm = build_system(settings)
    providers = {"llm": sysm.intake.llm.name, "voice": sysm.intake.voice.name,
                 "rocketlane": sysm.intake.rocketlane.name, "slack": sysm.comm.slack.name,
                 "inbox": sysm.email_source.name}
    print("Providers:", providers)
    mocks = [k for k, v in providers.items() if v == "mock" and k in ("voice", "rocketlane", "inbox")]
    if mocks and not allow_mocks:
        sys.exit(f"Refusing the live run: {', '.join(mocks)} would be mocked. Fill in .env, or pass --allow-mocks to run anyway.")
    if not settings.allowed_sender_domains and not allow_mocks:
        sys.exit("Refusing the live run: ALLOWED_SENDER_DOMAINS is blank, so any sender could trigger a phone call. "
                 "Set it (e.g. your own email domain), or pass --allow-mocks for a throwaway test.")
    return sysm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--demo", choices=["positive", "negative", "live"])
    ap.add_argument("--allow-mocks", action="store_true", help="let --once/--watch run with mock providers or no sender allow-list")
    a = ap.parse_args()
    if a.demo:
        demo(a.demo)
    elif a.once:
        for o in _live_system(a.allow_mocks).run_once():
            _print(o, settings.audit_log_path)
    elif a.watch:
        _live_system(a.allow_mocks).watch()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
