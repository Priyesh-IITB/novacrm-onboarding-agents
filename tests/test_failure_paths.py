"""Part 4, test 4 (continued): failure paths that the mocks alone do not exercise.

These tests drive the real RocketlaneAPI client against a patched `requests.request`
so the retry/backoff/4xx logic is tested, and cover the two failures that can happen
*after* something has already been created: a Slack failure once the project exists,
and a partial project (project created, phases/tasks not). In both of those the
system must escalate with the project id so a human can finish by hand rather than
leave an orphan nobody knows about."""
import json, os, unittest
from unittest.mock import patch, MagicMock
from tests.helpers import make_system, email, GOOD_BODY, START
from providers.rocketlane import (RocketlaneAPI, MockRocketlane, RocketlaneError, RocketlaneUnavailable,
                                  RocketlanePartial, build_spec)
from providers.slack import MockSlack, SlackError
from models import PlanTier


def _resp(status, payload=None, text=None):
    r = MagicMock()
    r.status_code = status
    r.text = text if text is not None else (json.dumps(payload) if payload is not None else "")
    r.json.return_value = payload if payload is not None else {}
    return r


def _escalations(m):
    p = os.path.join(m["dir"], "esc.jsonl")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return [json.loads(l) for l in f if l.strip()]


class RocketlaneClientRetries(unittest.TestCase):
    """The HTTP client: 5xx/429 retried with backoff then raised as Unavailable;
    4xx raised immediately as a definitive error; network errors retried."""

    def setUp(self):
        self.calls = []
        self.api = RocketlaneAPI("k", max_retries=3, backoff=0.0,
                                 on_call=lambda m, p, s, n: self.calls.append((m, p, s, n)))

    @patch("providers.rocketlane.time.sleep", lambda s: None)
    @patch("providers.rocketlane.requests.request")
    def test_503_is_retried_then_unavailable(self, req):
        req.return_value = _resp(503, text="upstream down")
        with self.assertRaises(RocketlaneUnavailable):
            self.api._req("GET", "/companies")
        self.assertEqual(req.call_count, 3, "exactly max_retries attempts")
        self.assertEqual([c[3] for c in self.calls],
                         ["retryable, attempt 1/3", "retryable, attempt 2/3", "retryable, attempt 3/3"])

    @patch("providers.rocketlane.time.sleep", lambda s: None)
    @patch("providers.rocketlane.requests.request")
    def test_recovers_when_a_retry_succeeds(self, req):
        req.side_effect = [_resp(502), _resp(200, {"data": []})]
        self.assertEqual(self.api._req("GET", "/companies"), {"data": []})
        self.assertEqual(req.call_count, 2)

    @patch("providers.rocketlane.requests.request")
    def test_4xx_is_not_retried(self, req):
        req.return_value = _resp(400, text='{"message":"owner.emailId is not a workspace member"}')
        with self.assertRaises(RocketlaneError) as cm:
            self.api._req("POST", "/projects", json={})
        self.assertNotIsInstance(cm.exception, RocketlaneUnavailable)
        self.assertEqual(req.call_count, 1, "a definitive 4xx must not be retried")
        self.assertIn("workspace member", str(cm.exception))

    @patch("providers.rocketlane.time.sleep", lambda s: None)
    @patch("providers.rocketlane.requests.request")
    def test_network_error_is_retried(self, req):
        import requests as _r
        req.side_effect = [_r.ConnectionError("dns"), _r.Timeout("slow"), _resp(200, {"projectId": 7})]
        self.assertEqual(self.api._req("POST", "/projects", json={})["projectId"], 7)
        self.assertEqual(req.call_count, 3)

    @patch("providers.rocketlane.requests.request")
    def test_api_key_goes_in_header_never_in_url_or_params(self, req):
        key = "rl_live_9f3c2a7e_SECRET"
        api = RocketlaneAPI(key, max_retries=1, backoff=0.0)
        req.return_value = _resp(200, {"data": []})
        api._req("GET", "/companies", params={"companyName.eq": "Acme"})
        args, kw = req.call_args
        self.assertEqual(kw["headers"]["api-key"], key)
        self.assertNotIn(key, args[1])
        self.assertNotIn(key, str(kw.get("params")))
        self.assertNotIn(key, str(kw.get("json")))

    def test_owner_email_is_required(self):
        spec = build_spec("Acme Foods", "ops@acme.example", PlanTier.GROWTH, START)
        with self.assertRaises(RocketlaneError):
            self.api.create_onboarding_project(spec, owner_email="")

    @patch("providers.rocketlane.requests.request")
    def test_duplicate_lookup_matches_company_name_exactly(self, req):
        # Rocketlane returns 'Acme Foods Europe' for a companyName.eq query on 'Acme Foods'
        # (observed behaviour on some workspaces) - the client must not treat that as a match.
        req.return_value = _resp(200, {"data": [{"companyId": 1, "companyName": "Acme Foods Europe"}]})
        self.assertIsNone(self.api.find_existing_project("Acme Foods"))

    @patch("providers.rocketlane.requests.request")
    def test_duplicate_lookup_ignores_archived_projects(self, req):
        req.side_effect = [
            _resp(200, {"data": [{"companyId": 1, "companyName": "Acme Foods"}]}),
            _resp(200, {"data": [{"projectId": 9, "projectName": "old", "archived": True, "customer": {"companyId": 1}}]}),
        ]
        self.assertIsNone(self.api.find_existing_project("Acme Foods"))

    @patch("providers.rocketlane.requests.request")
    def test_partial_create_reports_project_id(self, req):
        """Project POST succeeds, first phase POST 400s: the caller must learn the project id
        so it can be finished by hand or cleaned up, not just 'it failed'."""
        req.side_effect = [_resp(200, {"projectId": 42, "projectName": "Acme Foods onboarding"}),
                           _resp(400, text="phase rejected")]
        spec = build_spec("Acme Foods", "ops@acme.example", PlanTier.GROWTH, START)
        with self.assertRaises(RocketlanePartial) as cm:
            self.api.create_onboarding_project(spec, owner_email="pm@novacrm.example")
        self.assertEqual(cm.exception.project_id, 42)
        self.assertIn("0/", str(cm.exception))


class CreateIsNotBlindlyRetried(unittest.TestCase):
    """POST /projects can succeed server-side and still time out on the way back. A blind retry
    would create the twin the duplicate check exists to prevent."""

    @patch("providers.rocketlane.time.sleep", lambda s: None)
    @patch("providers.rocketlane.requests.request")
    def test_lost_response_is_found_not_recreated(self, req):
        api = RocketlaneAPI("k", max_retries=3, backoff=0.0)
        spec = build_spec("Acme Foods", "ops@acme.example", PlanTier.GROWTH, START)
        import requests as _r
        req.side_effect = [
            _r.Timeout("response lost"),                                                          # POST /projects (server committed)
            _resp(200, {"data": [{"companyId": 1, "companyName": "Acme Foods"}]}),                 # lookup: companies
            _resp(200, {"data": [{"projectId": 77, "projectName": spec.project_name, "customer": {"companyId": 1}}]}),  # lookup: projects
        ]
        api = RocketlaneAPI("k", max_retries=3, backoff=0.0)
        created = api._create_once_or_find({"projectName": spec.project_name}, spec)
        self.assertEqual(created["projectId"], 77)
        self.assertEqual(sum(1 for c in req.call_args_list if c.args[0] == "POST"), 1, "exactly one POST; never a second create")

    @patch("providers.rocketlane.time.sleep", lambda s: None)
    @patch("providers.rocketlane.requests.request")
    def test_retries_create_when_lookup_shows_nothing(self, req):
        spec = build_spec("Acme Foods", "ops@acme.example", PlanTier.GROWTH, START)
        req.side_effect = [
            _resp(503),                                    # POST /projects
            _resp(200, {"data": []}),                      # lookup: no company
            _resp(200, {"projectId": 5, "projectName": spec.project_name}),   # POST /projects again
        ]
        api = RocketlaneAPI("k", max_retries=3, backoff=0.0)
        self.assertEqual(api._create_once_or_find({}, spec)["projectId"], 5)

    @patch("providers.rocketlane.requests.request")
    def test_unexpected_phase_response_is_still_partial_with_project_id(self, req):
        """A 200 with an unexpected body (no phaseId) is not a RocketlaneError, but the project
        exists, so it must still surface as Partial carrying the id."""
        req.side_effect = [_resp(200, {"projectId": 42}), _resp(200, {"id": 7})]
        api = RocketlaneAPI("k", max_retries=1, backoff=0.0)
        spec = build_spec("Acme Foods", "ops@acme.example", PlanTier.GROWTH, START)
        with self.assertRaises(RocketlanePartial) as cm:
            api.create_onboarding_project(spec, owner_email="pm@novacrm.example")
        self.assertEqual(cm.exception.project_id, 42)

    @patch("providers.rocketlane.requests.request")
    def test_unrelated_live_project_does_not_block_onboarding(self, req):
        req.side_effect = [
            _resp(200, {"data": [{"companyId": 1, "companyName": "Acme Foods"}]}),
            _resp(200, {"data": [{"projectId": 9, "projectName": "Acme Foods renewal services", "customer": {"companyId": 1}}]}),
        ]
        api = RocketlaneAPI("k", max_retries=1, backoff=0.0)
        self.assertIsNone(api.find_existing_project("Acme Foods"))


class ProviderSafety(unittest.TestCase):
    def test_named_voice_provider_without_key_is_refused_not_mocked(self):
        from providers.voice import build_voice, VoiceMisconfigured
        from tests.helpers import settings_for_tests
        with self.assertRaises(VoiceMisconfigured):
            build_voice(settings_for_tests(voice_provider="bland", bland_api_key=""))
        with self.assertRaises(VoiceMisconfigured):
            build_voice(settings_for_tests(voice_provider="twilio"))
        self.assertEqual(build_voice(settings_for_tests(voice_provider="mock")).name, "mock")

    def test_escalation_redacts_secrets_in_slack_and_email_too(self):
        from tests.helpers import make_system
        sysm, m = make_system()
        sysm.escalator.escalate(reason="test", severity="low", correlation_id="x",
                                context={"api_key": "sk-live-SUPERSECRET", "error": "503 from GET /x?key=sk-live-SUPERSECRET"})
        posted = " ".join(x["text"] for x in m["slack"].messages)
        self.assertNotIn("SUPERSECRET", posted)
        self.assertNotIn("SUPERSECRET", " ".join(x["body"] for x in m["mail"].sent))

    def test_channel_name_keeps_the_tier_for_long_customer_names(self):
        from providers.slack import slack_channel_name
        name = slack_channel_name("Northwind Consolidated Logistics and Freight Forwarding Incorporated of Delaware", "enterprise")
        self.assertTrue(name.endswith("-enterprise"))
        self.assertLessEqual(len(name), 80)

    @patch("providers.slack.requests.post")
    def test_lookup_by_email_is_form_encoded_and_invited_only_after_invite(self, post):
        from providers.slack import SlackAPI
        ok = MagicMock(); ok.status_code = 200; ok.json.return_value = {"ok": True, "user": {"id": "U1"}}
        bad = MagicMock(); bad.status_code = 200; bad.json.return_value = {"ok": False, "error": "cant_invite"}
        post.side_effect = [ok, bad]
        out = SlackAPI("xoxb-test").invite_by_email("C1", ["ae@novacrm.example"])
        first_call = post.call_args_list[0]
        self.assertIn("data", first_call.kwargs, "users.lookupByEmail must be form-encoded, not JSON")
        self.assertNotIn("json", first_call.kwargs)
        self.assertNotEqual(out["ae@novacrm.example"], "invited")

    def test_voice_audit_names_the_provider(self):
        from tests.helpers import make_system, email, GOOD_BODY
        sysm, m = make_system()
        sysm.handle(email(GOOD_BODY, subject="Closed Won: Acme Foods"), start_date=START)
        with open(os.path.join(m["dir"], "audit.jsonl")) as f:
            entries = [json.loads(l) for l in f if l.strip()]
        att = [e for e in entries if e["action"] == "voice_confirmation_attempt"]
        self.assertTrue(att and att[0]["inputs"]["provider"] == "mock")
        self.assertIn("voice_call_placing", [e["action"] for e in entries])
        self.assertLess([e["action"] for e in entries].index("voice_call_placing"),
                        [e["action"] for e in entries].index("voice_confirmation_attempt"))


class FailuresAfterSomethingExists(unittest.TestCase):
    def test_lookup_failure_escalates_without_calling_the_ae(self):
        rl = MockRocketlane(lookup_fails=True)
        sysm, m = make_system(rocketlane=rl)
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "escalated")
        self.assertEqual(rl.create_calls, 0)
        self.assertEqual(m["voice"].calls, [], "do not phone the AE if we cannot even check for duplicates")
        self.assertEqual(len(_escalations(m)), 1)

    def test_slack_failure_after_project_creation_escalates_with_project_id(self):
        class BrokenSlack(MockSlack):
            def create_channel(self, name):
                raise SlackError("channels:manage scope missing")
        slack = BrokenSlack()
        sysm, m = make_system(slack=slack)
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "escalated")
        self.assertIsNotNone(out.project, "the project that was created is reported, not lost")
        self.assertEqual(m["rocketlane"].create_calls, 1)
        esc = _escalations(m)
        self.assertEqual(len(esc), 1)
        self.assertEqual(esc[0]["context"]["project_id"], out.project.project_id)

    def test_partial_project_escalates_with_project_id(self):
        class PartialRL(MockRocketlane):
            def create_onboarding_project(self, spec, *, owner_email, template_id=None):
                self.create_calls += 1
                raise RocketlanePartial(4242, "project 4242 created but only 2/19 phases+tasks: 400 from POST /tasks")
        sysm, m = make_system(rocketlane=PartialRL())
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "escalated")
        self.assertIn("4242", out.reason)
        self.assertEqual(len(m["slack"].channels), 0, "no customer channel for a half-built project")
        esc = _escalations(m)
        self.assertEqual(esc[0]["context"]["project_id"], 4242)
        self.assertEqual(esc[0]["severity"], "high")

    def test_definitive_4xx_escalates_without_retry(self):
        class Rejecting(MockRocketlane):
            def create_onboarding_project(self, spec, *, owner_email, template_id=None):
                self.create_calls += 1
                raise RocketlaneError("400 from POST /projects: owner is not a member")
        rl = Rejecting()
        sysm, m = make_system(rocketlane=rl)
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "escalated")
        self.assertEqual(rl.create_calls, 1)
        self.assertIn("owner is not a member", _escalations(m)[0]["context"].get("error", "") + out.reason)

    def test_unexpected_exception_is_escalated_not_swallowed(self):
        class Weird(MockRocketlane):
            def find_existing_project(self, customer_name):
                raise KeyError("unexpected shape")
        sysm, m = make_system(rocketlane=Weird())
        out = sysm.handle(email(GOOD_BODY), start_date=START)
        self.assertEqual(out.status, "escalated")
        self.assertEqual(len(_escalations(m)), 1)
