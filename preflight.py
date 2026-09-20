"""Preflight: check every real credential in .env before the live run.

  python preflight.py              read-only checks
  python preflight.py --dry-run    also create a throwaway Rocketlane project (customer
                                   "Preflight Dry Run Co"), one phase, one task, then delete it

Each check is read-only and takes a second or two. It prints one line per
integration with what it found, and never prints a secret. Run this before
`python run.py --demo live` so a typo in .env shows up here, not halfway through
a phone call.
"""
from __future__ import annotations
import imaplib
import sys
import requests
from config import settings


def _ok(name, detail):
    print(f"  OK    {name:<12} {detail}")


def _skip(name, detail):
    print(f"  --    {name:<12} {detail}")


def _fail(name, detail):
    print(f"  FAIL  {name:<12} {detail}")
    return 1


def check_gmail() -> int:
    if not settings.gmail_address or not settings.gmail_app_password:
        _skip("gmail", "GMAIL_ADDRESS / GMAIL_APP_PASSWORD blank -> mock inbox")
        return 0
    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        conn.login(settings.gmail_address, settings.gmail_app_password)
        typ, data = conn.select("INBOX", readonly=True)
        n = int(data[0]) if typ == "OK" and data and data[0] else -1
        typ, data = conn.uid("search", None, "UNSEEN", "SUBJECT", f'"{settings.deal_subject_pattern}"')
        unseen = len(data[0].split()) if typ == "OK" and data and data[0] else 0
        conn.logout()
        _ok("gmail", f"logged in as {settings.gmail_address}; INBOX has {n} messages, "
                     f"{unseen} unseen matching '{settings.deal_subject_pattern}'")
        return 0
    except imaplib.IMAP4.error as e:
        return _fail("gmail", f"IMAP login rejected: {e}. Is 2-Step Verification on and is this an App Password, "
                              f"not your normal password?")
    except Exception as e:  # noqa: BLE001
        return _fail("gmail", f"{type(e).__name__}: {e}")


def check_voice() -> int:
    p = settings.voice_provider
    if p == "mock":
        _skip("voice", "VOICE_PROVIDER=mock -> no real call; the mock answers 'enterprise' to everything")
        return 0
    if (p == "bland" and not settings.bland_api_key) or (p == "vapi" and not (settings.vapi_api_key and settings.vapi_phone_number_id)):
        return _fail("voice", f"VOICE_PROVIDER={p} but its credentials are blank. This is the tier guardrail; "
                              f"it is never downgraded to the mock silently. Set the key or set VOICE_PROVIDER=mock.")
    if not settings.ae_phone_directory:
        return _fail("voice", "AE_PHONE_DIRECTORY is empty; the agent cannot call anyone. "
                              'Set it like {"Alex Rivera": "+1XXXXXXXXXX"} with your own number.')
    bad = [k for k, v in settings.ae_phone_directory.items()
           if not (str(v).startswith("+") and str(v)[1:].isdigit() and 8 <= len(str(v)) <= 16)]
    if bad:
        return _fail("voice", f"phone numbers must be E.164, a + followed by digits only (the +1XXXXXXXXXX "
                              f"placeholder is still in .env?): {bad}")
    try:
        if p == "bland":
            r = requests.get("https://api.bland.ai/v1/me", headers={"authorization": settings.bland_api_key}, timeout=20)
            if r.status_code == 401:
                return _fail("voice", "Bland rejected the API key (401)")
            if r.status_code >= 400:
                return _fail("voice", f"Bland /v1/me returned {r.status_code}: {r.text[:120]}")
            body = r.json() if r.text.strip() else {}
            bal = (body.get("billing") or {}).get("current_balance", body.get("current_balance"))
            _ok("voice", f"Bland key accepted; balance={bal if bal is not None else 'unknown'}; "
                         f"directory has {len(settings.ae_phone_directory)} AE(s)"
                         + ("" if settings.bland_from_number else " (no BLAND_FROM_NUMBER, Bland picks the caller id)"))
            return 0
        if p == "vapi":
            r = requests.get("https://api.vapi.ai/phone-number", headers={"Authorization": f"Bearer {settings.vapi_api_key}"}, timeout=20)
            if r.status_code == 401:
                return _fail("voice", "Vapi rejected the API key (401)")
            nums = r.json() if r.status_code == 200 else []
            ids = [n.get("id") for n in nums] if isinstance(nums, list) else []
            if settings.vapi_phone_number_id and settings.vapi_phone_number_id not in ids:
                return _fail("voice", f"VAPI_PHONE_NUMBER_ID not found among your numbers ({len(ids)} listed)")
            _ok("voice", f"Vapi key accepted; {len(ids)} phone number(s); remember free Vapi numbers are inbound-only")
            return 0
    except Exception as e:  # noqa: BLE001
        return _fail("voice", f"{type(e).__name__}: {e}")
    return _fail("voice", f"unknown VOICE_PROVIDER '{p}' (mock | bland | vapi)")


def check_rocketlane() -> int:
    if not settings.rocketlane_api_key:
        _skip("rocketlane", "ROCKETLANE_API_KEY blank -> mock Rocketlane (nothing is created anywhere)")
        return 0
    if not settings.rocketlane_owner_email:
        return _fail("rocketlane", "ROCKETLANE_OWNER_EMAIL is required (the login email of your workspace user)")
    h = {"api-key": settings.rocketlane_api_key, "accept": "application/json"}
    try:
        r = requests.get("https://api.rocketlane.com/api/1.0/projects", headers=h, params={"pageSize": 1}, timeout=30)
        if r.status_code in (401, 403):
            return _fail("rocketlane", f"API key rejected ({r.status_code}). Settings > API in your workspace.")
        if r.status_code >= 400:
            return _fail("rocketlane", f"GET /projects returned {r.status_code}: {r.text[:150]}")
        data = r.json() if r.text.strip() else {}
        n = len(data.get("data", []) or [])   # 0 or 1 with pageSize=1; the point is the key works
        r2 = requests.get("https://api.rocketlane.com/api/1.0/users", headers=h,
                          params={"email.eq": settings.rocketlane_owner_email, "pageSize": 5}, timeout=30)
        users = (r2.json().get("data", []) if r2.status_code == 200 and r2.text.strip() else None)
        want = settings.rocketlane_owner_email.lower()
        if users is None:
            owner_ok = None
        else:
            emails = [str(u.get("email") or u.get("emailId") or "").lower() for u in users]
            if want in emails:
                owner_ok = True
            elif emails and any(e != want for e in emails):
                owner_ok = None     # the filter was not honoured (unrelated users came back), so absence proves nothing
            else:
                owner_ok = False    # filtered query, empty result
        note = ("owner email found among workspace users" if owner_ok else
                "owner email NOT found among workspace users; project creation will 400" if owner_ok is False else
                "could not list users to confirm the owner email")
        tpl = []
        if settings.rocketlane_template_id_enterprise:
            tpl.append(f"enterprise template {settings.rocketlane_template_id_enterprise}")
        if settings.rocketlane_template_id_growth:
            tpl.append(f"growth template {settings.rocketlane_template_id_growth}")
        _ok("rocketlane", f"key accepted ({'a project is' if n else 'no projects'} visible yet); {note}; "
                          + (", ".join(tpl) if tpl else "no template ids, the agent builds phases and tasks itself"))
        return 0 if owner_ok is not False else 1
    except Exception as e:  # noqa: BLE001
        return _fail("rocketlane", f"{type(e).__name__}: {e}")


def check_slack() -> int:
    if not settings.slack_bot_token:
        _skip("slack", "SLACK_BOT_TOKEN blank -> mock Slack (channel, topic and welcome are printed, not posted)")
        return 0
    try:
        r = requests.post("https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {settings.slack_bot_token}"}, timeout=20)
        body = r.json()
        if not body.get("ok"):
            return _fail("slack", f"auth.test failed: {body.get('error')}")
        scopes = r.headers.get("x-oauth-scopes", "")
        need = {"channels:manage", "chat:write"}
        have = {x.strip() for x in scopes.split(",")}
        missing = sorted(s for s in need if s not in have)
        detail = f"bot '{body.get('user')}' in workspace '{body.get('team')}'"
        if missing:
            return _fail("slack", f"{detail}; token is missing scopes {missing}")
        extra = []
        if "chat:write.public" not in have:
            extra.append("no chat:write.public, so escalations post only to channels the bot has joined")
        if "users:read.email" not in have:
            extra.append("no users:read.email, so every invite-by-email will be recorded as missing_scope in the audit log")
        _ok("slack", detail + ("; " + "; ".join(extra) if extra else "; all optional scopes present"))
        return 0
    except Exception as e:  # noqa: BLE001
        return _fail("slack", f"{type(e).__name__}: {e}")


def check_llm() -> int:
    p = settings.llm_provider
    if p == "mock":
        _skip("llm", "LLM_PROVIDER=mock -> labelled emails parse deterministically; free-text emails will be rejected")
        return 0
    key = settings.gemini_api_key if p == "gemini" else settings.anthropic_api_key if p == "anthropic" else ""
    if not key:
        return _fail("llm", f"LLM_PROVIDER={p} but its API key is blank")
    try:
        if p == "gemini":
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": key},
                             params={"pageSize": 1}, timeout=20)
        else:
            r = requests.get("https://api.anthropic.com/v1/models", headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                             params={"limit": 1}, timeout=20)
        if r.status_code in (401, 403, 400):
            return _fail("llm", f"{p} rejected the key ({r.status_code})")
        _ok("llm", f"{p} key accepted")
        return 0
    except Exception as e:  # noqa: BLE001
        return _fail("llm", f"{type(e).__name__}: {e}")


def dry_run_rocketlane() -> int:
    """Prove the create path end to end against the real workspace before a phone call depends on
    it: POST /projects with the same body shape the agent sends, one phase, one task, then
    DELETE. If the delete is refused the project id is printed so it can be removed by hand."""
    if not settings.rocketlane_api_key:
        return _fail("dry-run", "ROCKETLANE_API_KEY is blank")
    from datetime import date, timedelta
    from providers.rocketlane import RocketlaneAPI, RocketlaneError
    import requests as _rq
    api = RocketlaneAPI(settings.rocketlane_api_key, max_retries=1)
    start = date.today()
    name = f"Preflight dry run {start.isoformat()} (delete me)"
    body = {"projectName": name, "customer": {"companyName": "Preflight Dry Run Co"}, "autoCreateCompany": True,
            "owner": {"emailId": settings.rocketlane_owner_email},
            "startDate": start.isoformat(), "dueDate": (start + timedelta(days=3)).isoformat()}
    try:
        created = api._req_once("POST", "/projects", json=body)
    except RocketlaneError as e:
        return _fail("dry-run", f"POST /projects rejected: {e}")
    pid = created.get("projectId")
    if not pid:
        return _fail("dry-run", f"POST /projects returned no projectId: {str(created)[:200]}")
    steps = []
    try:
        ph = api._req_once("POST", "/phases", json={"phaseName": "Kickoff", "startDate": start.isoformat(),
                                                    "dueDate": (start + timedelta(days=1)).isoformat(), "project": {"projectId": pid}})
        steps.append(f"phase {ph.get('phaseId')}")
        tk = api._req_once("POST", "/tasks", json={"taskName": "Preflight task", "startDate": start.isoformat(),
                                                   "dueDate": (start + timedelta(days=1)).isoformat(),
                                                   "project": {"projectId": pid}, "phase": {"phaseId": ph.get("phaseId")}})
        steps.append(f"task {tk.get('taskId')}")
    except RocketlaneError as e:
        steps.append(f"FAILED at {'task' if steps else 'phase'}: {e}")
    try:
        r = _rq.delete(f"{api.BASE}/projects/{pid}", headers=api._h(), timeout=30)
        deleted = r.status_code in (200, 204)
    except Exception as e:  # noqa: BLE001
        deleted, r = False, None
    if any("FAILED" in x for x in steps):
        return _fail("dry-run", f"project {pid} created; " + "; ".join(steps) + (" ; deleted" if deleted else f" ; NOT deleted, remove project {pid} by hand"))
    _ok("dry-run", f"project {pid} created with {', '.join(steps)}; " + ("deleted again" if deleted else f"delete refused (HTTP {getattr(r, 'status_code', '?')}), remove project {pid} by hand"))
    return 0


def main() -> int:
    dry = "--dry-run" in sys.argv
    print("Preflight for the live run (read-only checks, no secrets printed)\n")
    failures = check_gmail() + check_llm() + check_voice() + check_rocketlane() + check_slack()
    if dry:
        failures += dry_run_rocketlane()
    mocked = [n for n, real in (("inbox", settings.gmail_address and settings.gmail_app_password),
                                ("voice", settings.voice_provider != "mock"),
                                ("rocketlane", settings.rocketlane_api_key)) if not real]
    print()
    if failures:
        print(f"{failures} check(s) failed. Fix .env and run again.")
        return 1
    if mocked:
        print(f"WARNING: {', '.join(mocked)} would be MOCKED. A live run would show 'completed' without ringing anyone "
              f"or creating anything. Fill in .env before recording; `run.py --once/--watch` refuse to start like this.")
        return 2
    print("All real integrations answered. `python run.py --demo live` is safe to run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
