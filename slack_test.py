"""Prove the Slack bot token works, in isolation, then clean up after itself.

  python slack_test.py                       # creates a throwaway channel, posts, archives it
  python slack_test.py --keep                # leaves the channel so you can look at it on camera
  python slack_test.py --invite you@work.com # also exercises the invite-by-email path

This does exactly what the Communication Agent does on a real onboarding, against a channel
named for today rather than a customer: create, set topic, post the welcome, optionally invite,
then archive. Every API call is printed with its result, so a missing scope names itself here
rather than halfway through a live run.

Slack is optional for the assignment. Leave SLACK_BOT_TOKEN blank and the mock prints the same
channel, topic and welcome text instead of posting them.
"""
from __future__ import annotations
import argparse
import sys
from datetime import date
from config import settings
from providers.slack import build_slack, slack_channel_name, SlackError


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="do not archive the channel afterwards")
    ap.add_argument("--invite", action="append", default=[], metavar="EMAIL",
                    help="also try inviting this address (repeatable)")
    a = ap.parse_args()

    calls: list[str] = []
    slack = build_slack(settings, on_call=lambda m, ok, note: calls.append(f"  {'ok  ' if ok else 'FAIL'} {m:<26} {note}"))
    if slack.name == "mock":
        print("SLACK_BOT_TOKEN is blank, so this is the mock. Nothing will be posted to a real "
              "workspace.\nThat is a supported configuration: the brief allows a mocked Slack as "
              "long as you say what changes in production.\n")

    name = slack_channel_name(f"token test {date.today().isoformat()}", "growth")
    print(f"Creating #{name} ...")
    try:
        channel_id = slack.create_channel(name)
        slack.set_topic(channel_id, "Throwaway channel from slack_test.py. Safe to archive.")
        slack.post_message(channel_id, "If you can read this, the bot token, the channel scope "
                                       "and chat:write all work.")
        if a.invite:
            print(f"Inviting {a.invite} ...")
            for email, result in slack.invite_by_email(channel_id, a.invite).items():
                print(f"  {email}: {result}")
                if result == "not_in_workspace":
                    print("    (expected for an address that is not a member; the agent records "
                          "this and carries on rather than failing the onboarding)")
    except SlackError as e:
        print("\n".join(calls))
        print(f"\nFAILED: {e}")
        print("Common causes: the token is not a bot token (needs the xoxb- one, not xoxp-), the "
              "app was never installed to the workspace, or channels:manage is missing. "
              "`python preflight.py` lists the scopes the token actually carries.")
        return 1

    print("\n".join(calls))
    print(f"\nChannel id: {channel_id}")
    if a.keep:
        print("Left in place (--keep). Archive it yourself when you are done.")
    else:
        try:
            slack.archive_channel(channel_id)
            print("Archived, so nothing is left behind.")
        except SlackError as e:
            print(f"Could not archive it ({e}); archive #{name} by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
