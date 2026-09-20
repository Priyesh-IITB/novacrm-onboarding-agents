"""Part 4, test 3: template accuracy. Enterprise -> 30-day, dedicated CSM.
Growth -> 14-day, pooled CSM. Same 4 phases and 15 tasks, different dates."""
import unittest
from datetime import timedelta
from tests.helpers import make_system, email, GOOD_BODY, START
from providers.voice import MockVoice
from providers.rocketlane import build_spec
from models import PlanTier
from templates import ENTERPRISE, GROWTH, template_for


class TemplateAccuracy(unittest.TestCase):
    def test_enterprise_template(self):
        sysm, m = make_system(voice=MockVoice([("completed", "enterprise")]))
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        spec = build_spec("Acme Foods", "ops@acme.example", PlanTier.ENTERPRISE, START)
        self.assertEqual(spec.onboarding_days, 30)
        self.assertEqual(spec.csm_model, "dedicated")
        self.assertEqual(spec.due_date, START + timedelta(days=30))
        self.assertIn("dedicated CSM", out.slack.topic)
        tasks = m["rocketlane"].tasks[out.project.project_id]
        self.assertEqual(len(tasks), 15)
        self.assertEqual({t["_phase"] for t in tasks}, {"Kickoff", "Data Migration", "Configuration", "Go-Live"})
        last_due = max(t["dueDate"] for t in tasks)
        self.assertEqual(last_due, (START + timedelta(days=30)).isoformat())

    def test_growth_template(self):
        sysm, m = make_system(voice=MockVoice([("completed", "growth")]))
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.tier, PlanTier.GROWTH)
        self.assertIn("Growth, 14-day", out.project.project_name)
        self.assertIn("pooled CSM", out.slack.topic)
        self.assertIn("pooled CSM team", out.slack.welcome_message)
        tasks = m["rocketlane"].tasks[out.project.project_id]
        last_due = max(t["dueDate"] for t in tasks)
        self.assertEqual(last_due, (START + timedelta(days=14)).isoformat())

    def test_customer_owns_data_verification(self):
        """Priya's escalations came from migration marked done without verification.
        The verification task is assigned to the customer, not the CSM."""
        sysm, m = make_system()
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        tasks = m["rocketlane"].tasks[out.project.project_id]
        verify = next(t for t in tasks if t["taskName"] == "Customer verifies migrated data")
        self.assertEqual(verify["assignees"]["members"][0]["emailId"], "ops@acme.example")
        self.assertIn("blocked until", verify["taskDescription"])

    def test_no_template_for_unclear(self):
        with self.assertRaises(ValueError):
            template_for(PlanTier.UNCLEAR)

    def test_templates_share_structure(self):
        self.assertEqual(ENTERPRISE.phases, GROWTH.phases)
        self.assertEqual(len(ENTERPRISE.tasks), 15)
        self.assertEqual(len(GROWTH.tasks), 15)
