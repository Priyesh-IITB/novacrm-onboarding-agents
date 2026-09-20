# Code walkthrough

Read in this order. Each section says what the file does, then the one decision in it worth
asking about.

## Start here: one email, end to end

`run.py --demo positive` runs the whole thing with no accounts. The path through the code is:

```
providers/email_source.py   an email arrives and is admitted, or rejected as auto-reply
agents/intake.py            parse -> validate -> duplicate check -> phone the AE -> create project
providers/voice.py          place the call, decide whether a person spoke, read the tier
providers/rocketlane.py     create project, phases and tasks, idempotently
agents/communication.py     create the Slack channel, set topic, post the tier-specific welcome
agents/orchestrator.py      the loop, and every branch that ends at a person instead
escalation.py               the durable queue a human works from
audit.py                    every decision, with its rationale, appended as JSONL
```

## The files, and the decision in each

**`agents/intake.py`** Parses the deal email. Labelled lines first, LLM only for free text.
*The decision:* `parse_labelled` takes the FIRST match for each field, so a signature line reading
`Company: NovaCRM Inc.` cannot overwrite the customer the AE actually typed. And `_appears_in`
discards any value the model returns that does not occur verbatim in the email, so the model can
extract but never invent.

**`providers/voice.py`** Places the call and comes back with enterprise, growth, or unclear.
*The decision, and the one to ask about:* the tier is not taken from the provider's own
classification. `_ae_spoke` decides from the transcript whether a person actually talked, because
the provider reports a voicemail pickup as a completed call and reports `answered_by="unknown"` on
calls a human plainly held. Then `_read_tier` tries three independent readings in order of
reliability: a post-call analysis question, the disposition tag, and the AE's own turns in the
transcript. Disagreement or silence resolves to unclear. See `tests/test_voice_real_calls.py`:
those fixtures are three real calls, two of which broke earlier versions of this file in opposite
directions.

**`providers/rocketlane.py`** The API client. *The decision:* `_create_once_or_find` never blindly
retries a create. If the response to a POST is lost, it looks the project up before it would send a
second one, because a duplicated onboarding project is worse than a failed one. Partial creation
raises `RocketlanePartial` carrying the project id, so the escalation tells a person what already
exists.

**`agents/orchestrator.py`** The loop and the failure branches. *The decision:*
`_commit_before_dialling` marks the message processed and writes an audit entry BEFORE the first
dial, so a crash mid-call cannot ring the AE twice on the next poll.

**`templates.py`** The 4 phases and 15 tasks, 30-day Enterprise and 14-day Growth. *The decision:*
"Customer verifies migrated data" is a separate task assigned to the customer contact, not a
checkbox on the migration task, because the brief says a done-but-unverified migration caused real
escalations.

**`agents/communication.py`** Channel, topic, welcome. *The decision:* the `channel_created` audit
entry is written immediately after creation, before the topic and welcome, so a failure halfway
through still leaves a record that the channel exists.

**`run.py`** Demos and the live runner. *The decision:* `_live_system` refuses to start if the
inbox, the voice provider or Rocketlane is mocked, or if the sender allow-list is empty. A mock
that answers "Enterprise" without ringing anyone is the one failure the brief forbids, so it
cannot happen by misconfiguration.

## The tests

69 of them, under a second, no network.

- `test_happy_path.py` the whole flow, both tiers
- `test_intake_validation.py` missing fields, malformed fields, invented LLM values, signature lines
- `test_template_accuracy.py` the 15 tasks, the two timelines, the verification task's owner
- `test_edge_cases.py` unknown AE, duplicate customer, auto-replies
- `test_failure_paths.py` retries, non-idempotent creates, provider safety, partial creation
- `test_voice_tier_reading.py` the three reading layers and every case that must stay unclear
- `test_voice_real_calls.py` three real call transcripts, pinned as fixtures

## Operator tooling

Written because setup time is where a take-home actually goes wrong.

- `preflight.py --dry-run` checks every credential and names the wrong `.env` line. Creates and
  deletes a throwaway Rocketlane project, proving the create path before a call depends on it.
- `call_test.py "+1..."` one real call, in isolation, printing which layer read the tier.
- `slack_test.py` creates, posts to and archives a throwaway channel to prove the bot token.

## What is real and what is mocked

Real: Gmail IMAP, the voice call, the Rocketlane API, the Rocketlane overdue automation.
Mocked unless a token is supplied: Slack. The mock records channel, topic and message so the
personalisation stays visible and testable, and `slack_test.py` exercises the real path.
