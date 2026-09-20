"""Place ONE real voice call in isolation, with no inbox and no Rocketlane involved.

  python call_test.py "+1XXXXXXXXXX"            # calls that number as "Alex Rivera" about "Brightline Logistics"
  python call_test.py "+1XXXXXXXXXX" --ae "Dana Okafor" --customer "Pinecrest Dental"

Use this to prove the voice provider works before recording. It prints the call
status, what the provider decided (enterprise / growth / unclear) and the transcript,
and writes the full provider response to call_test_last.json for the write-up.
Answer the phone and say "Enterprise", then run it again and say "let me check" to
see the unclear path. Do not answer at all to see the no-answer path.
"""
from __future__ import annotations
import argparse
import json
import sys
from config import settings
from providers.voice import build_voice


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phone", help="E.164 number to call, e.g. +15551234567 (your own phone)")
    ap.add_argument("--ae", default="Alex Rivera")
    ap.add_argument("--customer", default="Brightline Logistics")
    a = ap.parse_args()
    if not a.phone.startswith("+"):
        sys.exit("phone must be E.164, starting with +")
    voice = build_voice(settings)
    if voice.name == "mock":
        sys.exit("VOICE_PROVIDER is mock or its key is blank; set VOICE_PROVIDER=bland and BLAND_API_KEY in .env")
    print(f"Placing a {voice.name} call to {a.phone} as the NovaCRM onboarding assistant, asking {a.ae} about {a.customer}...")
    res = voice.confirm_plan_tier(ae_name=a.ae, ae_phone=a.phone, customer=a.customer)
    print(f"\ncall_id   : {res.call_id}")
    print(f"status    : {res.status}")
    print(f"decision  : {res.plan_tier.value}   (only 'enterprise' or 'growth' lets the agent proceed)")
    raw = res.raw if isinstance(res.raw, dict) else {}
    print(f"read from : {raw.get('_tier_source', '(n/a)')}")
    print(f"answered  : {raw.get('answered_by') or '(not reported)'}    ended by: {raw.get('call_ended_by') or '(n/a)'}")
    print(f"transcript: {res.transcript[:1200] or '(none)'}")
    with open("call_test_last.json", "w") as f:
        json.dump(raw or res.raw, f, indent=2, default=str)
    print("\nFull provider response written to call_test_last.json")

    if (raw.get("answered_by") or "").lower() == "voicemail":
        print("\nNOTE: the provider classified this pickup as VOICEMAIL and ended the call, so no tier "
              "could be confirmed. That is the correct behaviour for a real voicemail box. If a person "
              "actually answered, the cause is call screening on the handset or the carrier: its spoken "
              "prompt reads as a voicemail greeting. Turn screening off for the test, or set "
              "BLAND_VOICEMAIL_ACTION=ignore in .env.")
        return 3
    if res.plan_tier.value == "unclear" and res.status == "completed":
        print("\nNOTE: the call completed but no tier was confirmed. Check the transcript above: if the AE "
              "clearly said a plan and this still says unclear, the reading layers in providers/voice.py "
              "need looking at. If they hedged, unclear is the right answer and the agent escalates.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
