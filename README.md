# NovaCRM onboarding automation

Two agents and one Rocketlane automation that turn a closed-deal email into a live onboarding:
project in Rocketlane with the right template and timeline, shared Slack channel with a welcome
written for the plan tier, and overdue escalation that fires on the platform's clock.

The design rule: **the system never guesses.** Missing input gets a question. An unconfirmed
plan tier gets a retry and then a person. An unreachable API gets retries and then a person.

## Run it in ten seconds, no accounts

```
pip install -r requirements.txt
python run.py --demo positive
python run.py --demo negative
python -m unittest discover -s tests -t . -v
```

## Run it for real

Copy `.env.example` to `.env` and fill in:
- Gmail address and an App Password (2-Step Verification must be on)
- Bland AI key (free tier) and the AE phone directory
- Rocketlane API key (Settings > API) and the owner's email
- Optional: Slack bot token (scopes: `channels:manage`, `chat:write`, `chat:write.public`, `users:read.email`;
  `chat:write.public` is what lets the bot post escalations to a channel it was never invited to);
  Rocketlane template IDs (read from the UI; the API reference has no templates endpoint)

Then `python preflight.py --dry-run` to confirm every credential answers and to create-and-delete a
throwaway Rocketlane project (proves the create path before a phone call depends on it),
`python call_test.py "+1YOURNUMBER"` to hear the voice agent once on its own,
`python slack_test.py` to prove the bot token in a throwaway channel it archives afterwards,
then `python run.py --watch`. The live run refuses to start while any of inbox, voice or Rocketlane
is still a mock, or while `ALLOWED_SENDER_DOMAINS` is blank (`--allow-mocks` overrides both for a throwaway test).

If the phone that answers runs handset or carrier call screening, set `BLAND_VOICEMAIL_ACTION=ignore`:
the screening prompt reads as a voicemail greeting to the provider's detector, which hangs up before
the AE ever speaks. The production default is `hangup`.

`docs/make_diagram.py` regenerates the workflow figure and needs matplotlib, which is not a runtime dependency. Send a deal email to the inbox and watch the audit log.

## Layout

```
preflight.py             checks each real credential before the live run
call_test.py             places one real voice call in isolation, and says which layer read the tier
slack_test.py            creates, posts to and archives a throwaway channel, to prove the bot token
agents/intake.py         Agent 1: parse, validate or ask, duplicate check, voice-confirm tier, create project
agents/communication.py  Agent 2: Slack channel, topic, personalised welcome
agents/orchestrator.py   the loop and every failure branch
providers/               llm, email_source, voice (Bland, Vapi, mock), rocketlane, slack
templates.py             the two onboarding plans: 4 phases, 15 tasks, 30-day vs 14-day
audit.py                 JSONL audit log: timestamp, inputs, outputs, rationale
escalation.py            durable escalation queue, mirrored to Slack and email
tests/                   69 tests: happy path, validation, template accuracy, edge cases, failure paths,
                         provider safety, and the three real calls of 19 Sep pinned as fixtures
docs/                    Part 1 analysis, Rocketlane automation setup
```

## Guardrails, where they live

| Guardrail | Where |
|---|---|
| Required fields present and valid, else ask | `agents/intake.py::parse`, `models.py::DealNotification` |
| A model can extract, never invent: LLM values must occur in the email text | `agents/intake.py::_appears_in` |
| First labelled line wins; a signature's "Company:" cannot overwrite the customer | `agents/intake.py::parse_labelled` |
| Tier only from an unambiguous voice answer, else retry then escalate | `agents/intake.py::confirm_tier`, `providers/voice.py` |
| A tier is read only when a person actually spoke, judged on the transcript rather than the provider's `answered_by` | `providers/voice.py::_ae_spoke` |
| Three independent ways to read the tier, and unclear when they disagree or are silent | `providers/voice.py::_read_tier` |
| No duplicate project | `agents/intake.py::existing_project` |
| Rocketlane retries with backoff, then escalate | `providers/rocketlane.py::RocketlaneAPI._req`, `agents/orchestrator.py` |
| Project creation is never blindly retried: a lost response is looked up before any second POST | `providers/rocketlane.py::_create_once_or_find` |
| A named real provider with no key refuses to start; the live run refuses mocks and an empty sender allow-list | `providers/voice.py::build_voice`, `run.py::_live_system` |
| The AE is never phoned twice for one email, even across a crash | `agents/orchestrator.py::_commit_before_dialling` |
| Customer owns data verification | `templates.py` |
| Partial creation or Slack failure after the project exists escalates with the project id | `agents/orchestrator.py` |
| Own replies and auto-replies are never re-processed (loop guard) | `providers/email_source.py::_is_auto`, `agents/intake.py::admissible` |
| Every action logged with rationale; secrets redacted | `audit.py`, every agent method |

## What the negative demo shows

`python run.py --demo negative` walks six failures in order: missing fields, malformed fields,
duplicate project, AE unreachable then ambiguous, Rocketlane 503, and Slack failing after the
project already exists. In every case the output says what happened, nothing is guessed, and
the escalation queue (`demo_negative_escalations.jsonl`) holds enough context for a person to
finish the job by hand.

## Reading the plan tier off a finished call

This is the part that field experience changed, so it is written down.

The provider's call record carries no single field saying which tag it chose. It carries
`dispositions`, a list that stays empty unless the tag model ran. An earlier version of this
adapter read a field name that does not exist, and every real call came back "unclear" while
looking healthy: status completed, transcript present, wrong answer. The check in `call_test.py`
is what caught it.

Two rules came out of that, and both are tested against transcripts of real calls
(`tests/test_voice_real_calls.py`):

**Did a person speak?** Decided from the transcript, not from `answered_by`. The provider
reports a voicemail pickup as a completed call, and reports `answered_by="unknown"` on plenty
of calls a human plainly held, so trusting that field discards real confirmations in one
direction and invents them in the other. Recorded greetings are recognised and do not count as
a person speaking.

**What did they say?** Three layers, most trustworthy first: a one-question post-call analysis,
then the `dispositions` tag if the tag model ran, then the transcript. The transcript layer
reads only the AE's own turns, never the agent's, because the agent names both plans while
asking the question. Naming both plans, or hedging next to the word, stays unclear. If the
analysis endpoint is unreachable the call still resolves from the transcript; if every layer is
silent the answer is unclear and the workflow escalates. Nothing guesses.

## What changes in production

- Slack: the mock becomes `SlackAPI` (already written) with the scopes above; external customers go through Slack Connect.
- Dedicated vs pooled CSM is a staffing fact, not copy: `ROCKETLANE_ENTERPRISE_CSM_EMAIL` and `ROCKETLANE_GROWTH_CSM_EMAIL` become the project owner per tier (both default to the workspace owner on a trial).
- Voice: `BlandVoice` or `VapiVoice` with a paid number and a caller id the AEs recognise, which removes most of the screening problem; the tier is still only ever taken from the call, never from the email.
- Inbox: the IMAP poller becomes a Gmail push subscription (Pub/Sub) so there is no polling delay.
- Processed-message state moves from a local file to a small table so two workers cannot both take the same email.
- The AE phone directory moves from `.env` to the HR system or Salesforce user records.
