"""Part 4, test 1: happy path. Enterprise email -> parse -> voice confirms -> Rocketlane
project with the 30-day template -> personalised Slack channel."""
import json, os, unittest
from tests.helpers import make_system, email, GOOD_BODY, START
from providers.voice import MockVoice
from models import PlanTier


class HappyPath(unittest.TestCase):
    def test_enterprise_deal_end_to_end(self):
        sysm, m = make_system(voice=MockVoice([("completed", "enterprise")]))
        out = sysm.handle(email(GOOD_BODY), start_date=START)

        self.assertEqual(out.status, "completed")
        self.assertEqual(out.tier, PlanTier.ENTERPRISE)
        # voice call actually happened, to the right AE, before any project existed
        self.assertEqual(len(m["voice"].calls), 1)
        self.assertEqual(m["voice"].calls[0]["ae_phone"], "+15550100001")
        # project: right name, 30-day due date, 4 phases, 15 tasks
        self.assertTrue(out.project.created)
        self.assertIn("Enterprise, 30-day", out.project.project_name)
        pid = out.project.project_id
        self.assertEqual(len(m["rocketlane"].phases[pid]), 4)
        self.assertEqual(len(m["rocketlane"].tasks[pid]), 15)
        # slack: name, topic and welcome all carry the tier and the customer
        self.assertEqual(out.slack.channel_name, "onb-acme-foods-enterprise")
        self.assertIn("Enterprise plan, 30 days, dedicated CSM", out.slack.topic)
        self.assertIn("dedicated Customer Success Manager", out.slack.welcome_message)
        self.assertIn("Acme Foods", out.slack.welcome_message)
        posted = [x for x in m["slack"].messages if x["channel"] == out.slack.channel_id]
        self.assertEqual(len(posted), 1)
        # audit: every step present, every entry has a rationale
        with open(os.path.join(m["dir"], "audit.jsonl")) as f:
            entries = [json.loads(l) for l in f]
        actions = [e["action"] for e in entries]
        for a in ("email_received", "parsed_deal", "duplicate_check", "voice_confirmation_attempt",
                  "project_created", "channel_created", "onboarding_started"):
            self.assertIn(a, actions)
        self.assertTrue(all(e["rationale"] for e in entries))
        self.assertTrue(all(e["ts"] for e in entries))

    def test_nothing_escalated_on_happy_path(self):
        sysm, m = make_system()
        sysm.handle(email(GOOD_BODY), start_date=START)
        p = os.path.join(m["dir"], "esc.jsonl")
        self.assertTrue(not os.path.exists(p) or os.path.getsize(p) == 0)
