"""Part 4, test 4: edge cases. Rocketlane down; existing project; AE does not answer;
ambiguous tier. In every case: nothing is created, a human is told why."""
import json, os, unittest
from tests.helpers import make_system, email, GOOD_BODY, START
from providers.voice import MockVoice
from providers.rocketlane import MockRocketlane
from models import RocketlaneProject, PlanTier


def _escalations(m):
    with open(os.path.join(m["dir"], "esc.jsonl")) as f:
        return [json.loads(l) for l in f if l.strip()]


class EdgeCases(unittest.TestCase):
    def test_rocketlane_down_retries_then_escalates(self):
        rl = MockRocketlane(fail_times=10)
        sysm, m = make_system(rocketlane=rl)
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "escalated")
        self.assertIn("rocketlane unavailable", out.reason)
        self.assertGreaterEqual(rl.create_calls, 1)
        esc = _escalations(m)
        self.assertEqual(len(esc), 1)
        self.assertEqual(esc[0]["severity"], "high")
        self.assertEqual(len(m["slack"].channels), 0, "no channel without a project")
        # the escalation was mirrored to the escalation channel
        self.assertTrue(any("escalation" in x["text"].lower() for x in m["slack"].messages))

    def test_existing_project_is_not_duplicated(self):
        rl = MockRocketlane(existing={"acme foods": RocketlaneProject(project_id=5, project_name="Acme Foods onboarding", created=False)})
        sysm, m = make_system(rocketlane=rl)
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "escalated")
        self.assertEqual(out.reason, "existing project found")
        self.assertEqual(rl.create_calls, 0)
        self.assertEqual(m["voice"].calls, [], "no call when we already know we cannot proceed")
        self.assertEqual(_escalations(m)[0]["context"]["existing"]["project_id"], 5)

    def test_ae_does_not_answer_twice(self):
        voice = MockVoice([("no-answer", ""), ("no-answer", "")])
        sysm, m = make_system(voice=voice)
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "escalated")
        self.assertEqual(out.tier, PlanTier.UNCLEAR)
        self.assertEqual(len(voice.calls), 2, "retried exactly max_attempts times")
        self.assertEqual(m["rocketlane"].create_calls, 0)
        self.assertEqual(len(_escalations(m)[0]["context"]["attempts"]), 2)

    def test_ambiguous_then_clear_succeeds_on_retry(self):
        voice = MockVoice([("completed", "unclear"), ("completed", "growth")])
        sysm, m = make_system(voice=voice)
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "completed")
        self.assertEqual(out.tier, PlanTier.GROWTH)
        self.assertEqual(len(voice.calls), 2)

    def test_ambiguous_twice_escalates_and_never_assumes(self):
        voice = MockVoice([("completed", "unclear"), ("completed", "unclear")])
        sysm, m = make_system(voice=voice)
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "escalated")
        self.assertEqual(m["rocketlane"].create_calls, 0)

    def test_voice_provider_exception_is_handled(self):
        class Boom(MockVoice):
            def confirm_plan_tier(self, **kw):
                raise RuntimeError("voice API 500")
        sysm, m = make_system(voice=Boom())
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "escalated")
        self.assertEqual(m["rocketlane"].create_calls, 0)

    def test_unknown_ae_phone_escalates(self):
        sysm, m = make_system()
        out = sysm.handle(email(GOOD_BODY.replace("AE: Alex Rivera", "AE: Someone New")), start_date=START)
        self.assertEqual(out.status, "escalated")
        self.assertEqual(m["voice"].calls, [])

    def test_slack_name_collision_is_suffixed(self):
        sysm, m = make_system()
        m["slack"].create_channel("onb-acme-foods-enterprise")
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "completed")
        self.assertTrue(out.slack.channel_name.startswith("onb-acme-foods-enterprise-"))
