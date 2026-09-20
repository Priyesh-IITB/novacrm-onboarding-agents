"""Voice AI: place a real outbound call to the AE and come back with exactly one of
three answers: enterprise, growth, or unclear.

"Unclear" is a first-class result, not an error. The Intake Agent decides what
to do with it (retry, then escalate). The provider's only job is to never turn
an unclear answer into a tier.

Bland is the default because a single POST places the call and its `dispositions`
feature makes the model choose exactly one tag from a closed list, which maps
one-to-one onto PlanTier. Vapi is wired as an alternative; its free numbers are
inbound-only, so it needs an imported Twilio number for outbound.
"""
from __future__ import annotations
import time
from typing import Optional
import requests
from models import PlanTier, VoiceCallResult

CALL_TASK = (
    "You are calling {ae_name}, an account executive at NovaCRM, on behalf of the Customer Success team. "
    "A deal for the customer '{customer}' just closed and the onboarding system needs one fact before it can "
    "start: is this customer on the Enterprise plan or the Growth plan? Greet them briefly, say why you are "
    "calling, and ask which of the two plans the customer is on. If they say Enterprise, confirm 'Enterprise, "
    "thank you'. If they say Growth, confirm 'Growth, thank you'. If they are unsure, say anything else, or want "
    "to check, do NOT guess: say you will have a colleague follow up, thank them, and end the call. Keep the whole "
    "call under 90 seconds."
)
FIRST_SENTENCE = "Hi {ae_name}, this is the NovaCRM onboarding assistant calling about the {customer} deal. Do you have twenty seconds?"


def _to_tier(tag: Optional[str]) -> PlanTier:
    t = (tag or "").strip().lower()
    if t == "enterprise":
        return PlanTier.ENTERPRISE
    if t == "growth":
        return PlanTier.GROWTH
    return PlanTier.UNCLEAR


# Recorded greetings a machine plays. A turn that is only this is not the AE speaking, however
# the provider labelled the pickup.
_MACHINE_GREETING = (
    "forwarded to voice mail", "forwarded to voicemail", "please record your message",
    "at the tone", "after the tone", "is not available", "leave a message",
    "record your name and reason", "i'll see if this person is available",
    "the person you're trying to reach", "the person you are trying to reach",
    "has a voice mailbox", "please leave your message",
)


def _ae_turns(data: dict) -> list[str]:
    """The human side of the transcript, as a list of what they actually said."""
    out = []
    for turn in (data.get("transcripts") or []):
        if not isinstance(turn, dict):
            continue
        who = str(turn.get("user") or turn.get("role") or "").lower()
        if who in ("user", "human", "customer"):
            text = str(turn.get("text") or "").strip()
            if text:
                out.append(text)
    return out


def _ae_spoke(data: dict) -> bool:
    """True when a person held a conversation with the agent. One word ("Yes.") counts; a
    recorded greeting does not, and neither does silence."""
    for text in _ae_turns(data):
        low = text.lower()
        if any(g in low for g in _MACHINE_GREETING):
            continue
        if len(low.split()) >= 1 and any(c.isalpha() for c in low):
            return True
    return False


def _tier_from_transcript(data: dict) -> PlanTier:
    """Last resort, and deliberately strict. Only the AE's OWN turns are read, never the agent's,
    because the agent says both words while asking the question. If the AE named both plans, or
    hedged next to the word, that is not a confirmation and stays unclear."""
    joined = " ".join(_ae_turns(data)).lower()
    if not joined:
        return PlanTier.UNCLEAR
    if any(h in joined for h in ("not sure", "no idea", "let me check", "i'll check", "i will check",
                                 "don't know", "do not know", "check and", "get back to you")):
        return PlanTier.UNCLEAR
    ent, gro = "enterprise" in joined, "growth" in joined
    if ent and not gro:
        return PlanTier.ENTERPRISE
    if gro and not ent:
        return PlanTier.GROWTH
    return PlanTier.UNCLEAR


class VoiceProvider:
    name = "base"

    def confirm_plan_tier(self, *, ae_name: str, ae_phone: str, customer: str) -> VoiceCallResult:
        raise NotImplementedError


class MockVoice(VoiceProvider):
    """Scriptable: a list of outcomes consumed one per call. Lets tests express
    'no answer, then unclear, then enterprise' as data."""
    name = "mock"

    def __init__(self, script: Optional[list[tuple[str, str]]] = None):
        # each item: (status, disposition) e.g. ("completed","enterprise"), ("no-answer",""), ("completed","unclear")
        self.script = list(script or [("completed", "enterprise")])
        self.calls: list[dict] = []

    def confirm_plan_tier(self, *, ae_name, ae_phone, customer) -> VoiceCallResult:
        status, tag = self.script.pop(0) if self.script else ("no-answer", "")
        self.calls.append({"ae_name": ae_name, "ae_phone": ae_phone, "customer": customer, "status": status, "tag": tag})
        return VoiceCallResult(provider="mock", call_id=f"mock-{len(self.calls)}", status=status,
                               plan_tier=_to_tier(tag) if status == "completed" else PlanTier.UNCLEAR,
                               transcript=f"[mock] AE said: {tag or '(no answer)'}", raw={"status": status, "tag": tag})


class BlandVoice(VoiceProvider):
    name = "bland"
    BASE = "https://api.bland.ai/v1"

    def __init__(self, api_key: str, from_number: str = "", poll_seconds: int = 5, max_wait_seconds: int = 240,
                 voicemail_action: str = "hangup"):
        self.api_key, self.from_number = api_key, from_number
        self.poll_seconds, self.max_wait = poll_seconds, max_wait_seconds
        # "hangup" is right in production: talking to a voicemail box cannot confirm a tier.
        # "ignore" exists for the case where the callee runs carrier or handset call screening,
        # whose spoken prompt trips voicemail detection on a call a human is about to take.
        self.voicemail_action = voicemail_action or "hangup"

    def _h(self):
        # Bland documents the bare key in the authorization header (no "Bearer" scheme).
        return {"authorization": self.api_key, "Content-Type": "application/json"}

    def confirm_plan_tier(self, *, ae_name, ae_phone, customer) -> VoiceCallResult:
        body = {
            "phone_number": ae_phone,
            "task": CALL_TASK.format(ae_name=ae_name, customer=customer),
            "first_sentence": FIRST_SENTENCE.format(ae_name=ae_name, customer=customer),
            "voice": "Karen",
            "max_duration": 3,
            "record": False,
            "dispositions": ["enterprise", "growth", "unclear"],
            "summary_prompt": "State which plan tier the AE confirmed: enterprise, growth, or unclear.",
            "voicemail": {"action": self.voicemail_action, "sensitive": False},
        }
        if self.from_number:
            body["from"] = self.from_number
        r = requests.post(f"{self.BASE}/calls", headers=self._h(), json=body, timeout=30)
        r.raise_for_status()
        created = r.json() if r.text.strip() else {}
        # Bland can answer HTTP 200 with {"status": "error", "message": ...}. Surface the message,
        # not a KeyError, so the audit trail says why no call was placed.
        if created.get("status") == "error" or "call_id" not in created:
            raise RuntimeError(f"Bland did not place the call: {created.get('message') or str(created)[:200]}")
        call_id = created["call_id"]
        deadline = time.time() + self.max_wait
        data = {}
        while time.time() < deadline:
            time.sleep(self.poll_seconds)
            g = requests.get(f"{self.BASE}/calls/{call_id}", headers=self._h(), timeout=30)
            g.raise_for_status()
            data = g.json()
            if data.get("completed") or data.get("status") in ("completed", "failed", "busy", "no-answer", "canceled"):
                break
        status = data.get("status", "unknown")
        answered_by = (data.get("answered_by") or "").lower()
        # Whether a tier may be read at all is decided by one question: did a person talk to the
        # agent? Bland's own fields do not answer it reliably. It reports a voicemail pickup as a
        # "completed" call, and it reports answered_by="unknown" on plenty of calls a human
        # answered and completed, so trusting either field alone throws away real confirmations
        # in one direction or invents them in the other. The transcript is the evidence.
        spoke = _ae_spoke(data)
        if answered_by == "voicemail":
            status = "voicemail"
        elif not spoke:
            status = "no-answer"
        if spoke and answered_by != "voicemail":
            tier, how = self._read_tier(call_id, data)
        else:
            tier, how = PlanTier.UNCLEAR, f"status={status}, no conversation with the AE to read"
        data["_tier_source"] = how
        return VoiceCallResult(provider="bland", call_id=call_id, status=status, plan_tier=tier,
                               transcript=data.get("concatenated_transcript", "") or "", raw=data)

    # --- reading the tier out of a finished call -------------------------------------------------
    # Bland does not return a single "which tag did you pick" field on the call record. It returns
    # `dispositions`, which is a list and is empty unless the tag model ran, so a decision has to be
    # read deliberately. Three layers, most trustworthy first, and every one of them may return
    # UNCLEAR, which is a legitimate answer that sends the workflow to a human.
    def _read_tier(self, call_id: str, data: dict) -> tuple[PlanTier, str]:
        tier = _to_tier(self._ask_analysis(call_id))
        if tier is not PlanTier.UNCLEAR:
            return tier, "post-call analysis"
        for tag in (data.get("dispositions") or []):
            t = _to_tier(tag if isinstance(tag, str) else (tag or {}).get("name"))
            if t is not PlanTier.UNCLEAR:
                return t, "disposition tag"
        t = _tier_from_transcript(data)
        if t is not PlanTier.UNCLEAR:
            return t, "transcript of the AE's own words"
        return PlanTier.UNCLEAR, "no layer found a tier"

    def _ask_analysis(self, call_id: str) -> Optional[str]:
        """One question, one word back. A failure here is not fatal: the caller falls through to
        the next layer rather than inventing a tier."""
        try:
            r = requests.post(f"{self.BASE}/calls/{call_id}/analyze", headers=self._h(), timeout=45,
                              json={"goal": "Determine which plan tier the account executive confirmed.",
                                    "questions": [["Which plan tier did the account executive state? Answer with "
                                                   "exactly one word: enterprise, growth, or unclear. Answer "
                                                   "unclear unless they said one of the two plainly.",
                                                   "enterprise, growth, or unclear"]]})
            if r.status_code >= 400:
                return None
            answers = (r.json() or {}).get("answers") or []
            return answers[0] if answers and isinstance(answers[0], str) else None
        except Exception:  # noqa: BLE001
            return None


class VapiVoice(VoiceProvider):
    name = "vapi"
    BASE = "https://api.vapi.ai"

    def __init__(self, api_key: str, phone_number_id: str, poll_seconds: int = 5, max_wait_seconds: int = 240):
        self.api_key, self.phone_number_id = api_key, phone_number_id
        self.poll_seconds, self.max_wait = poll_seconds, max_wait_seconds

    def _h(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def confirm_plan_tier(self, *, ae_name, ae_phone, customer) -> VoiceCallResult:
        body = {
            "phoneNumberId": self.phone_number_id,
            "customer": {"number": ae_phone, "name": ae_name},
            "assistant": {
                "firstMessage": FIRST_SENTENCE.format(ae_name=ae_name, customer=customer),
                "model": {"provider": "openai", "model": "gpt-4o", "temperature": 0.2,
                          "messages": [{"role": "system", "content": CALL_TASK.format(ae_name=ae_name, customer=customer)}]},
                "voice": {"provider": "azure", "voiceId": "andrew"},
                "analysisPlan": {"structuredDataPlan": {"enabled": True, "schema": {
                    "type": "object", "properties": {"plan_tier": {"type": "string", "enum": ["enterprise", "growth", "unclear"]}},
                    "required": ["plan_tier"]}}},
            },
        }
        r = requests.post(f"{self.BASE}/call", headers=self._h(), json=body, timeout=30)
        r.raise_for_status()
        call_id = r.json()["id"]
        deadline = time.time() + self.max_wait
        data, ended_at = {}, None
        while time.time() < deadline:
            time.sleep(self.poll_seconds)
            g = requests.get(f"{self.BASE}/call/{call_id}", headers=self._h(), timeout=30)
            g.raise_for_status()
            data = g.json()
            if data.get("status") == "ended":
                ended_at = ended_at or time.time()
                # analysis is generated shortly after the call ends; wait a bounded grace period for it
                if data.get("analysis") or time.time() - ended_at > 30:
                    break
        ended = data.get("status") == "ended"
        reason = (data.get("endedReason") or "").lower()
        NOT_A_CONVERSATION = ("customer-did-not-answer", "customer-busy", "voicemail", "silence-timed-out",
                              "no-answer", "busy", "customer-did-not-give-microphone-permission")
        if ended and not any(k in reason for k in NOT_A_CONVERSATION):
            status = "completed"
        elif ended:
            status = "no-answer" if ("answer" in reason or "busy" in reason) else reason
        else:
            status = "timeout"
        tag = ((data.get("analysis") or {}).get("structuredData") or {}).get("plan_tier")
        tier = _to_tier(tag) if status == "completed" else PlanTier.UNCLEAR
        return VoiceCallResult(provider="vapi", call_id=call_id, status=status, plan_tier=tier,
                               transcript=data.get("transcript", "") or "", raw=data)


class VoiceMisconfigured(RuntimeError):
    """A named real provider without its credentials. Never silently downgraded to the mock,
    because the mock answers 'enterprise' without ringing anyone."""


def build_voice(settings) -> VoiceProvider:
    p = settings.voice_provider
    if p == "mock":
        return MockVoice()
    if p == "bland":
        if not settings.bland_api_key:
            raise VoiceMisconfigured("VOICE_PROVIDER=bland but BLAND_API_KEY is blank. Set the key, or set VOICE_PROVIDER=mock explicitly.")
        return BlandVoice(settings.bland_api_key, settings.bland_from_number,
                          voicemail_action=getattr(settings, "bland_voicemail_action", "hangup"))
    if p == "vapi":
        if not (settings.vapi_api_key and settings.vapi_phone_number_id):
            raise VoiceMisconfigured("VOICE_PROVIDER=vapi needs both VAPI_API_KEY and VAPI_PHONE_NUMBER_ID. Or set VOICE_PROVIDER=mock explicitly.")
        return VapiVoice(settings.vapi_api_key, settings.vapi_phone_number_id)
    raise VoiceMisconfigured(f"unknown VOICE_PROVIDER {p!r}; use mock, bland or vapi")
