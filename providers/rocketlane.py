"""Rocketlane API client, plus an in-memory mock with the same surface.

Two ways to create the onboarding plan:
  1. From a Rocketlane template ID (sources=[{templateId, startDate}]) when one is configured.
  2. Built explicitly from templates.py: four phases and fifteen tasks with due dates
     derived from the tier's timeline.
Path 2 is the default because I could not find a templates endpoint in Rocketlane's
public API reference, so a template ID has to be read from the UI. Building explicitly
also proves the plan matches Priya's description task for task.

Existence check before creation: look the company up by name, verify the name actually
matches (never trust a server-side filter blindly), then look for a non-archived project
whose customer is that company. Creating a second onboarding project for the same
customer is exactly the kind of mistake the CS team is trying to stop.

Retries: 429/5xx/connection errors retry with exponential backoff up to `max_retries`,
then raise RocketlaneUnavailable. Any 4xx raises RocketlaneError immediately: retrying a
bad request does not make it good. The orchestrator escalates either way.

Partial creation: the project POST succeeds and a later phase or task POST fails. That
is reported as RocketlanePartial with the project id so a human can finish or delete it,
and a re-sent email will find the project and escalate rather than create a twin.
"""
from __future__ import annotations
import time
from datetime import date, timedelta
from typing import Callable, Optional
import requests
from models import PlanTier, ProjectSpec, RocketlaneProject
from templates import template_for, phase_windows, task_due


class RocketlaneError(RuntimeError):
    """A definitive failure (4xx). Not retried."""


class RocketlaneUnavailable(RocketlaneError):
    """Raised after retries are exhausted on 429/5xx/network errors."""


class RocketlanePartial(RocketlaneError):
    """Project exists but phases/tasks did not all get created."""
    def __init__(self, project_id: int, message: str):
        super().__init__(message)
        self.project_id = project_id


class RocketlaneClient:
    name = "base"

    def find_existing_project(self, customer_name: str) -> Optional[RocketlaneProject]:
        raise NotImplementedError

    def create_onboarding_project(self, spec: ProjectSpec, *, owner_email: str,
                                  template_id: Optional[str] = None) -> RocketlaneProject:
        raise NotImplementedError


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


class MockRocketlane(RocketlaneClient):
    name = "mock"

    def __init__(self, existing: Optional[dict[str, RocketlaneProject]] = None, fail_times: int = 0,
                 lookup_fails: bool = False):
        self.projects: dict[str, RocketlaneProject] = {_norm(k): v for k, v in (existing or {}).items()}
        self.phases: dict[int, list[dict]] = {}
        self.tasks: dict[int, list[dict]] = {}
        self.fail_times = fail_times          # simulate "API down" for the first N create calls
        self.lookup_fails = lookup_fails      # simulate "API down" during the duplicate check
        self._next_id = 1000
        self.create_calls = 0

    def find_existing_project(self, customer_name):
        if self.lookup_fails:
            raise RocketlaneUnavailable("simulated 503 from Rocketlane during lookup")
        return self.projects.get(_norm(customer_name))

    def create_onboarding_project(self, spec, *, owner_email, template_id=None):
        self.create_calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RocketlaneUnavailable("simulated 503 from Rocketlane")
        self._next_id += 1
        pid = self._next_id
        proj = RocketlaneProject(project_id=pid, project_name=spec.project_name, url=None, created=True)
        self.projects[_norm(spec.customer_name)] = proj
        if template_id:
            self.phases[pid], self.tasks[pid] = [{"from_template": template_id}], []
        else:
            self.phases[pid], self.tasks[pid] = _build_plan_payloads(spec)
        return proj


class RocketlaneAPI(RocketlaneClient):
    name = "rocketlane"
    BASE = "https://api.rocketlane.com/api/1.0"

    def __init__(self, api_key: str, max_retries: int = 3, backoff: float = 1.5,
                 on_call: Optional[Callable[[str, str, int, str], None]] = None):
        self.api_key, self.max_retries, self.backoff = api_key, max_retries, backoff
        self.on_call = on_call or (lambda method, path, status, note: None)   # audit hook
        self.unowned_tasks: list[tuple[int, str]] = []   # (project_id, task) where the customer assignee was rejected

    def _h(self):
        return {"api-key": self.api_key, "accept": "application/json", "content-type": "application/json"}

    def _req_once(self, method: str, path: str, **kw):
        """One attempt, same classification as _req, no retry loop."""
        try:
            r = requests.request(method, f"{self.BASE}{path}", headers=self._h(), timeout=30, **kw)
        except requests.RequestException as e:
            self.on_call(method, path, 0, "network error, single attempt")
            raise RocketlaneUnavailable(f"network error on {method} {path}: {e}") from e
        if r.status_code in (429, 500, 502, 503, 504):
            self.on_call(method, path, r.status_code, "retryable, single attempt")
            raise RocketlaneUnavailable(f"{r.status_code} from {method} {path}: {r.text[:200]}")
        if r.status_code >= 400:
            self.on_call(method, path, r.status_code, "definitive failure, not retried")
            raise RocketlaneError(f"{r.status_code} from {method} {path}: {r.text[:300]}")
        self.on_call(method, path, r.status_code, "ok")
        return r.json() if r.text.strip() else {}

    def _req(self, method: str, path: str, **kw):
        last = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = requests.request(method, f"{self.BASE}{path}", headers=self._h(), timeout=30, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    last = RocketlaneUnavailable(f"{r.status_code} from {method} {path}: {r.text[:200]}")
                    self.on_call(method, path, r.status_code, f"retryable, attempt {attempt}/{self.max_retries}")
                elif r.status_code >= 400:
                    self.on_call(method, path, r.status_code, "definitive failure, not retried")
                    raise RocketlaneError(f"{r.status_code} from {method} {path}: {r.text[:300]}")
                else:
                    self.on_call(method, path, r.status_code, "ok")
                    return r.json() if r.text.strip() else {}
            except requests.RequestException as e:
                last = RocketlaneUnavailable(f"network error on {method} {path}: {e}")
                self.on_call(method, path, 0, f"network error, attempt {attempt}/{self.max_retries}")
            if attempt < self.max_retries:
                time.sleep(self.backoff ** attempt)
        raise last or RocketlaneUnavailable("unknown")

    def _pages(self, path: str, params: dict, max_pages: int = 10) -> list[dict]:
        """Follow Rocketlane's page tokens so a match on page two is not read as 'no match'."""
        out, token = [], None
        for _ in range(max_pages):
            q = {**params, "pageSize": 100}
            if token:
                q["pageToken"] = token
            resp = self._req("GET", path, params=q)
            out.extend(resp.get("data", []) or [])
            token = (resp.get("pagination") or {}).get("nextPageToken") or resp.get("nextPageToken")
            if not token:
                break
        return out

    def find_existing_project(self, customer_name):
        comps = self._pages("/companies", {"companyName.eq": customer_name})
        match = next((c for c in comps if _norm(c.get("companyName")) == _norm(customer_name)), None)
        if match is None:
            return None
        cid = match["companyId"]
        projs = self._pages("/projects", {"customerId.eq": cid})
        # A customer may have unrelated live projects (a renewal, a services engagement). Only an
        # onboarding project blocks a new onboarding; ours are always named "<customer> onboarding (...)".
        live = [p for p in projs
                if not p.get("archived")
                and (p.get("customer") or {}).get("companyId") == cid
                and "onboarding" in str(p.get("projectName", "")).lower()]
        if not live:
            return None
        p = live[0]
        return RocketlaneProject(project_id=p["projectId"], project_name=p.get("projectName", ""), created=False,
                                 url=p.get("url") or p.get("projectUrl"))

    def _create_once_or_find(self, body: dict, spec: ProjectSpec) -> dict:
        """POST /projects is not idempotent. A timeout after the server committed would, on a blind
        retry, create the twin the duplicate check exists to prevent. So: one attempt per try, and
        before each retry look the project up; if it now exists, that is our project."""
        last = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._req_once("POST", "/projects", json=body)
            except RocketlaneUnavailable as e:
                last = e
                self.on_call("POST", "/projects", 0, f"unavailable on attempt {attempt}/{self.max_retries}; checking whether it was created before retrying")
                existing = self.find_existing_project(spec.customer_name)
                if existing is not None and _norm(existing.project_name) == _norm(spec.project_name):
                    self.on_call("POST", "/projects", 0, f"found project {existing.project_id} created by the lost request; not retrying")
                    return {"projectId": existing.project_id, "projectName": existing.project_name, "url": existing.url}
                if attempt < self.max_retries:
                    time.sleep(self.backoff ** attempt)
        raise last or RocketlaneUnavailable("project creation failed")

    def create_onboarding_project(self, spec, *, owner_email, template_id=None):
        if not owner_email:
            raise RocketlaneError("ROCKETLANE_OWNER_EMAIL is required to create a project")
        body = {
            "projectName": spec.project_name,
            "customer": {"companyName": spec.customer_name},
            "autoCreateCompany": True,
            "owner": {"emailId": owner_email},
            "startDate": spec.start_date.isoformat(),
            "dueDate": spec.due_date.isoformat(),
            "teamMembers": {"customers": [{"emailId": spec.customer_contact_email}]},
        }
        if template_id:
            body["sources"] = [{"templateId": int(template_id), "startDate": spec.start_date.isoformat()}]
        created = self._create_once_or_find(body, spec)
        if "projectId" not in created:
            raise RocketlaneError(f"project POST returned no projectId: {str(created)[:200]}")
        pid = created["projectId"]
        url = created.get("url") or created.get("projectUrl")
        if template_id:
            return RocketlaneProject(project_id=pid, project_name=created.get("projectName", spec.project_name), created=True, url=url)
        phases, tasks = _build_plan_payloads(spec)
        done = 0
        try:
            phase_ids = {}
            for ph in phases:
                resp = self._req("POST", "/phases", json={**ph, "project": {"projectId": pid}})
                phase_ids[ph["phaseName"]] = resp["phaseId"]
                done += 1
            for t in tasks:
                payload = {k: v for k, v in t.items() if k != "_phase"}
                payload.update({"project": {"projectId": pid}, "phase": {"phaseId": phase_ids[t["_phase"]]}})
                try:
                    self._req("POST", "/tasks", json=payload)
                except RocketlaneError as e:
                    # Assigning a customer contact may be rejected on workspaces where customer users
                    # cannot hold tasks. Create the task without the assignee rather than lose it,
                    # say so in the description, and put it in the audit trail: this is the task
                    # Priya's worst incident was about, so an unowned copy must never be silent.
                    if "assignees" in payload and not isinstance(e, RocketlaneUnavailable):
                        payload.pop("assignees")
                        payload["taskDescription"] = (payload.get("taskDescription") or "") + \
                            f"<p>Owner: customer contact {spec.customer_contact_email} (assignment rejected by workspace settings).</p>"
                        self.on_call("POST", "/tasks", 0, f"ASSIGNEE DROPPED on '{t['taskName']}' for project {pid}: "
                                                          f"workspace rejected the customer assignee ({str(e)[:120]}); task created unowned, needs a person")
                        self._req("POST", "/tasks", json=payload)
                        self.unowned_tasks.append((pid, t["taskName"]))
                    else:
                        raise
                done += 1
        except Exception as e:  # noqa: BLE001 - whatever failed, the project already exists and must not be lost
            raise RocketlanePartial(pid, f"project {pid} created but only {done}/{len(phases)+len(tasks)} phases+tasks: {type(e).__name__}: {e}") from e
        return RocketlaneProject(project_id=pid, project_name=created.get("projectName", spec.project_name), created=True, url=url)


def _build_plan_payloads(spec: ProjectSpec) -> tuple[list[dict], list[dict]]:
    """Turn a PlanTemplate into Rocketlane phase and task payloads for the spec's dates.
    Only documented fields are sent; optional ones only when non-empty."""
    tpl = template_for(spec.plan_tier)
    windows = phase_windows(spec.start_date, tpl.onboarding_days)
    phases = [{"phaseName": name, "startDate": s.isoformat(), "dueDate": e.isoformat()}
              for name, (s, e) in windows.items()]
    tasks = []
    for t in tpl.tasks:
        due = task_due(spec.start_date, tpl.onboarding_days, t)
        payload = {"taskName": t.name, "_phase": t.phase, "dueDate": due.isoformat(),
                   "startDate": windows[t.phase][0].isoformat()}
        if t.description:
            payload["taskDescription"] = f"<p>{t.description}</p>"
        if t.assignee == "customer":
            payload["assignees"] = {"members": [{"emailId": spec.customer_contact_email}]}
        tasks.append(payload)
    return phases, tasks


def build_spec(customer_name: str, contact_email: str, tier: PlanTier, start: date) -> ProjectSpec:
    tpl = template_for(tier)
    return ProjectSpec(customer_name=customer_name, customer_contact_email=contact_email, plan_tier=tier,
                       onboarding_days=tpl.onboarding_days, csm_model=tpl.csm_model, start_date=start,
                       due_date=start + timedelta(days=tpl.onboarding_days),
                       project_name=f"{customer_name} onboarding ({tier.value.title()}, {tpl.onboarding_days}-day)")


def build_rocketlane(settings, on_call=None) -> RocketlaneClient:
    if settings.rocketlane_api_key:
        return RocketlaneAPI(settings.rocketlane_api_key, max_retries=settings.rocketlane_max_retries, on_call=on_call)
    return MockRocketlane()
