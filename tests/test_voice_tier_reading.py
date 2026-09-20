"""The tier has to be READ out of a finished call, and the first real call proved why.

Bland's call record carries no single "which tag did you pick" field. It carries
`dispositions`, a list that stays empty unless the tag model ran. The first version of
this adapter read a field name that does not exist, so every real call came back
"unclear" while looking like a success. These tests pin the three layers and, more
importantly, pin the cases where the answer must stay unclear.

The fixtures below are the shape of a real Bland response, taken from a live call.
"""
import unittest
from unittest.mock import patch
from models import PlanTier
from providers.voice import BlandVoice, _tier_from_transcript


def _call(status="completed", answered_by="human", dispositions=None, turns=()):
    return {"call_id": "c1", "status": status, "completed": True, "answered_by": answered_by,
            "dispositions": list(dispositions or []), "disposition_ids": [], "analysis": None,
            "concatenated_transcript": " ".join(t.get("text", "") for t in turns),
            "transcripts": [dict(t) for t in turns]}


def _said(*texts):
    return [{"user": "user", "text": t} for t in texts]


class TranscriptFallback(unittest.TestCase):
    def test_the_ae_saying_enterprise_is_read(self):
        self.assertIs(_tier_from_transcript(_call(turns=_said("Yeah, they're on Enterprise."))),
                      PlanTier.ENTERPRISE)

    def test_only_the_ae_is_read_never_the_agent(self):
        # The agent names both plans in the question. If the agent's turns were read, every call
        # would look ambiguous at best and would resolve to whichever word came first at worst.
        d = _call(turns=[{"user": "assistant", "text": "Is it Enterprise or Growth?"},
                         {"user": "user", "text": "Growth."}])
        self.assertIs(_tier_from_transcript(d), PlanTier.GROWTH)

    def test_naming_both_plans_is_not_a_confirmation(self):
        self.assertIs(_tier_from_transcript(_call(turns=_said("Could be enterprise, could be growth."))),
                      PlanTier.UNCLEAR)

    def test_hedging_next_to_the_word_is_not_a_confirmation(self):
        for hedge in ("I think enterprise, let me check.", "Not sure, enterprise maybe?",
                      "Enterprise I believe, I'll check and get back to you."):
            with self.subTest(hedge=hedge):
                self.assertIs(_tier_from_transcript(_call(turns=_said(hedge))), PlanTier.UNCLEAR)

    def test_an_empty_transcript_is_unclear(self):
        self.assertIs(_tier_from_transcript(_call()), PlanTier.UNCLEAR)


class LayerOrder(unittest.TestCase):
    def setUp(self):
        self.v = BlandVoice("k")

    def test_analysis_wins_when_it_answers(self):
        with patch.object(BlandVoice, "_ask_analysis", return_value="growth"):
            tier, how = self.v._read_tier("c1", _call(dispositions=["enterprise"],
                                                      turns=_said("enterprise")))
        self.assertIs(tier, PlanTier.GROWTH)
        self.assertEqual(how, "post-call analysis")

    def test_disposition_tag_is_next(self):
        with patch.object(BlandVoice, "_ask_analysis", return_value=None):
            tier, how = self.v._read_tier("c1", _call(dispositions=["enterprise"]))
        self.assertIs(tier, PlanTier.ENTERPRISE)
        self.assertEqual(how, "disposition tag")

    def test_transcript_is_last(self):
        with patch.object(BlandVoice, "_ask_analysis", return_value=None):
            tier, how = self.v._read_tier("c1", _call(turns=_said("We're Growth.")))
        self.assertIs(tier, PlanTier.GROWTH)
        self.assertEqual(how, "transcript of the AE's own words")

    def test_every_layer_silent_means_unclear_not_a_guess(self):
        with patch.object(BlandVoice, "_ask_analysis", return_value=None):
            tier, how = self.v._read_tier("c1", _call())
        self.assertIs(tier, PlanTier.UNCLEAR)
        self.assertEqual(how, "no layer found a tier")

    def test_a_failing_analysis_endpoint_does_not_stop_the_call(self):
        # A 500 from the analysis endpoint must fall through, not raise: an unreachable
        # scoring service is not a reason to lose a confirmation the AE already gave.
        class _R:
            status_code = 500
            def json(self): return {}
        with patch("providers.voice.requests.post", return_value=_R()):
            tier, how = self.v._read_tier("c1", _call(turns=_said("Enterprise.")))
        self.assertIs(tier, PlanTier.ENTERPRISE)
        self.assertEqual(how, "transcript of the AE's own words")


class VoicemailIsNotAConversation(unittest.TestCase):
    def test_a_screened_or_voicemail_pickup_never_yields_a_tier(self):
        # The live failure: handset call screening speaks a greeting, Bland's detector calls it
        # voicemail and ends the call. Whatever is in that transcript, no tier may come out of it.
        v = BlandVoice("k")
        data = _call(answered_by="voicemail",
                     turns=_said("Hi. If you record your name and reason for calling..."))
        status = "voicemail"
        tier = v._read_tier("c1", data)[0] if status == "completed" else PlanTier.UNCLEAR
        self.assertIs(tier, PlanTier.UNCLEAR)


if __name__ == "__main__":
    unittest.main()
