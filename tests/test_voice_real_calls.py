"""The three real calls placed on 19 Sep 2026, pinned as fixtures.

Every one of them is a transcript that actually came back from Bland. Two of them broke an
earlier version of this adapter, in opposite directions:

  call 1  the AE answered and said "enterprise", and the adapter reported no-answer and threw
          the confirmation away, because Bland set answered_by="unknown" on a call a human
          plainly held. Refusing a tier you were given is as wrong as inventing one.
  call 2  the AE did not know and asked to talk tomorrow. Unclear is the correct answer here
          and the workflow must escalate rather than pick a plan.
  call 3  a real voicemail box picked up and read its greeting. No tier may come out of that,
          whatever words appear in the transcript.
"""
import unittest
from unittest.mock import patch
from models import PlanTier
from providers.voice import BlandVoice, _ae_spoke


def _turns(*pairs):
    return [{"user": who, "text": text} for who, text in pairs]


# --- call 931eb710: human answered, confirmed Enterprise, answered_by reported as "unknown" ---
CALL_CONFIRMED = {
    "call_id": "931eb710", "status": "completed", "answered_by": "unknown",
    "call_ended_by": "ASSISTANT", "dispositions": [], "analysis": None,
    "transcripts": _turns(
        ("assistant", "Hi Alex Rivera, this is the NovaCRM onboarding assistant calling about the "
                      "Brightline Logistics deal. Do you have twenty seconds?"),
        ("user", "Yes."),
        ("assistant", "Great. I just need to know: is Brightline Logistics on the Enterprise plan "
                      "or the Growth plan?"),
        ("user", "It's in on the enterprise."),
        ("assistant", "Enterprise, thank you."),
        ("user", "Yes. Thank you."),
        ("assistant", "Have a great day.")),
}

# --- call 1e4ed1d9: human answered, did not know, deferred to tomorrow ---
CALL_DEFERRED = {
    "call_id": "1e4ed1d9", "status": "completed", "answered_by": "human",
    "call_ended_by": "ASSISTANT", "dispositions": [], "analysis": None,
    "transcripts": _turns(
        ("assistant", "Hi Alex Rivera, this is the NovaCRM onboarding assistant calling about the "
                      "Brightline Logistics deal. Do you have twenty seconds?"),
        ("user", "I don't know what are you talking about."),
        ("assistant", "I apologize, Alex. We just closed a deal for Brightline Logistics and need to "
                      "confirm if they are on the Enterprise plan or the Growth plan to start their "
                      "onboarding. Would you happen to know which one it is?"),
        ("user", "But I don't know why are you calling me. This was closed before. Can we talk tomorrow?"),
        ("assistant", "Okay, I will have a colleague follow up with you tomorrow. Thank you for your time."),
        ("user", "Are you there?")),
}

# --- call 41f3e67d: carrier voicemail answered and read its greeting ---
CALL_VOICEMAIL = {
    "call_id": "41f3e67d", "status": "completed", "answered_by": "voicemail",
    "call_ended_by": "ASSISTANT", "dispositions": [], "analysis": None,
    "transcripts": _turns(
        ("assistant", "Hi Alex Rivera, this is the NovaCRM onboarding assistant calli-"),
        ("user", "Your call has been forwarded to voice mail. The person you're trying to reach is "
                 "not available. At the tone, please record your message. When you have finished "
                 "recording, you may hang up."),
        ("assistant", "Goodbye")),
}


class DidAPersonSpeak(unittest.TestCase):
    def test_a_human_conversation_counts_even_when_the_provider_says_unknown(self):
        self.assertTrue(_ae_spoke(CALL_CONFIRMED))

    def test_a_human_who_does_not_know_still_counts_as_a_conversation(self):
        self.assertTrue(_ae_spoke(CALL_DEFERRED))

    def test_a_recorded_greeting_is_not_a_person_speaking(self):
        self.assertFalse(_ae_spoke(CALL_VOICEMAIL))

    def test_handset_call_screening_is_not_a_person_speaking(self):
        screened = dict(CALL_VOICEMAIL, answered_by="unknown", transcripts=_turns(
            ("assistant", "Hi Alex Rivera, this is the NovaCRM onboarding assistant -"),
            ("user", "Hi. If you record your name and reason for calling, I'll see if this person "
                     "is available.")))
        self.assertFalse(_ae_spoke(screened))

    def test_silence_is_not_a_person_speaking(self):
        self.assertFalse(_ae_spoke({"transcripts": _turns(("assistant", "Hello?"))}))


class TheThreeCallsResolveCorrectly(unittest.TestCase):
    def setUp(self):
        self.v = BlandVoice("k")
        # No network in tests: the analysis layer returns nothing, so these exercise the
        # transcript layer, which is the one that has to hold when the provider is unhelpful.
        self.p = patch.object(BlandVoice, "_ask_analysis", return_value=None)
        self.p.start()
        self.addCleanup(self.p.stop)

    def _resolve(self, data):
        answered_by = (data.get("answered_by") or "").lower()
        spoke = _ae_spoke(data)
        if spoke and answered_by != "voicemail":
            return self.v._read_tier(data["call_id"], data)
        return PlanTier.UNCLEAR, "no conversation"

    def test_call_1_the_confirmation_is_kept(self):
        tier, how = self._resolve(CALL_CONFIRMED)
        self.assertIs(tier, PlanTier.ENTERPRISE)
        self.assertEqual(how, "transcript of the AE's own words")

    def test_call_2_a_deferral_escalates_rather_than_guessing(self):
        tier, _ = self._resolve(CALL_DEFERRED)
        self.assertIs(tier, PlanTier.UNCLEAR)

    def test_call_3_a_voicemail_yields_nothing(self):
        tier, _ = self._resolve(CALL_VOICEMAIL)
        self.assertIs(tier, PlanTier.UNCLEAR)


if __name__ == "__main__":
    unittest.main()
