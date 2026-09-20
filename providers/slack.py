"""Slack: create the onboarding channel, set its topic, post the welcome, invite the people
we can. Real client uses the Web API over HTTPS with a bot token; the mock records calls.

Inviting: internal people (the AE, the CSM) are looked up by email and invited. The
customer contact is external to NovaCRM's workspace, so `conversations.invite` cannot add
them; that is Slack Connect in production. The agent records that it tried, so the gap is
visible in the audit log rather than silent.

All interpolated text is escaped so a customer named "<!channel> Acme" cannot ping the
whole workspace.
"""
from __future__ import annotations
import re
import requests

_NAME_RE = re.compile(r"[^a-z0-9_-]+")


def slack_channel_name(customer_name: str, tier: str) -> str:
    base = _NAME_RE.sub("-", customer_name.lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)
    # Truncate the customer part, never the tier, so a long name cannot turn "-enterprise" into "-e".
    room = 80 - len(f"onb--{tier}")
    base = base[:room].rstrip("-")
    return f"onb-{base}-{tier}" if base else f"onb-{tier}"


def slack_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackError(RuntimeError):
    pass


class SlackClient:
    name = "base"

    def create_channel(self, name: str, is_private: bool = False) -> str:
        raise NotImplementedError

    def set_topic(self, channel_id: str, topic: str) -> None:
        raise NotImplementedError

    def post_message(self, channel: str, text: str) -> None:
        raise NotImplementedError

    def invite_by_email(self, channel_id: str, emails: list[str]) -> dict[str, str]:
        """Returns {email: 'invited' | 'not_in_workspace' | error}."""
        raise NotImplementedError

    def archive_channel(self, channel_id: str) -> None:
        """Only slack_test.py calls this, to clean up after proving the token works. The agent
        never archives a channel it created: an onboarding channel outlives the onboarding."""
        raise NotImplementedError


class MockSlack(SlackClient):
    name = "mock"

    def __init__(self, workspace_emails: set[str] | None = None):
        self.channels: dict[str, dict] = {}
        self.messages: list[dict] = []
        self.invites: list[dict] = []
        self.workspace_emails = workspace_emails or set()
        self._n = 0

    def create_channel(self, name, is_private=False):
        if name in {c["name"] for c in self.channels.values()}:
            raise SlackError("name_taken")
        self._n += 1
        cid = f"C{self._n:06d}"
        self.channels[cid] = {"name": name, "topic": "", "members": []}
        return cid

    def set_topic(self, channel_id, topic):
        self.channels[channel_id]["topic"] = topic

    def post_message(self, channel, text):
        self.messages.append({"channel": channel, "text": text})

    def invite_by_email(self, channel_id, emails):
        out = {}
        for e in emails:
            if e in self.workspace_emails:
                self.channels[channel_id]["members"].append(e)
                out[e] = "invited"
            else:
                out[e] = "not_in_workspace"
            self.invites.append({"channel_id": channel_id, "email": e, "result": out[e]})
        return out

    def archive_channel(self, channel_id):
        self.channels.pop(channel_id, None)


class SlackAPI(SlackClient):
    name = "slack"
    BASE = "https://slack.com/api"

    # Slack accepts JSON bodies for write methods but users.lookupByEmail (a read) only takes
    # application/x-www-form-urlencoded; sending JSON there returns invalid_arguments.
    _FORM_ONLY = {"users.lookupByEmail"}

    def __init__(self, bot_token: str, on_call=None):
        self.token = bot_token
        self.on_call = on_call or (lambda method, ok, note: None)   # audit hook, same idea as the Rocketlane client

    def _call(self, method: str, **payload):
        headers = {"Authorization": f"Bearer {self.token}"}
        if method in self._FORM_ONLY:
            r = requests.post(f"{self.BASE}/{method}", headers=headers, data=payload, timeout=30)
        else:
            r = requests.post(f"{self.BASE}/{method}", headers={**headers, "Content-Type": "application/json"},
                              json=payload, timeout=30)
        if r.status_code == 429:
            self.on_call(method, False, "rate limited")
            raise SlackError(f"slack {method}: rate limited, retry after {r.headers.get('Retry-After')}s")
        try:
            data = r.json()
        except ValueError as e:
            self.on_call(method, False, f"non-JSON HTTP {r.status_code}")
            raise SlackError(f"slack {method}: non-JSON response HTTP {r.status_code}") from e
        if not data.get("ok"):
            self.on_call(method, False, str(data.get("error")))
            raise SlackError(f"slack {method}: {data.get('error')}")
        self.on_call(method, True, "ok")
        return data

    def create_channel(self, name, is_private=False):
        return self._call("conversations.create", name=name, is_private=is_private)["channel"]["id"]

    def set_topic(self, channel_id, topic):
        self._call("conversations.setTopic", channel=channel_id, topic=topic[:250])

    def post_message(self, channel, text):
        self._call("chat.postMessage", channel=channel, text=text)

    def invite_by_email(self, channel_id, emails):
        out: dict[str, str] = {}
        found: dict[str, str] = {}
        for e in emails:
            try:
                found[e] = self._call("users.lookupByEmail", email=e)["user"]["id"]
            except SlackError as err:
                out[e] = "not_in_workspace" if "users_not_found" in str(err) else str(err)
        if found:
            try:
                self._call("conversations.invite", channel=channel_id, users=",".join(found.values()))
                for e in found:
                    out[e] = "invited"          # only claimed once the invite call has succeeded
            except SlackError as err:
                if "already_in_channel" in str(err):
                    for e in found:
                        out[e] = "invited"
                else:
                    for e in found:
                        out[e] = str(err)
        return out

    def archive_channel(self, channel_id):
        self._call("conversations.archive", channel=channel_id)


def build_slack(settings, on_call=None) -> SlackClient:
    return SlackAPI(settings.slack_bot_token, on_call=on_call) if settings.slack_bot_token else MockSlack()
