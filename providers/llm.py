"""LLM access for one job only: turning a free-text deal email into the fields in
DealNotification. The deterministic parser in agents/intake.py runs FIRST; the
model is consulted only when the email is unstructured. Either way the result
goes through the same pydantic validation, so the model can never smuggle a
guessed field past the guardrail.

Both real providers are called over plain HTTPS with `requests`, no SDK, so the
project has three dependencies and runs anywhere.
"""
from __future__ import annotations
import json
import re
from typing import Optional
import requests

EXTRACTION_INSTRUCTIONS = (
    "You extract structured fields from a sales email announcing a closed deal. "
    "Return ONLY a JSON object with keys: customer_name, customer_contact_email, ae_name, "
    "opportunity_link, ae_email. Use null for any field that is not explicitly present. "
    "Never invent a value. Never infer the plan tier; it is not your job."
)

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _extract_json(text: str) -> dict:
    m = _JSON_RE.search(text or "")
    if not m:
        raise ValueError("model returned no JSON object")
    return json.loads(m.group(0))


class LLMProvider:
    name = "base"

    def extract_deal_fields(self, subject: str, body: str) -> dict:
        raise NotImplementedError


class MockLLM(LLMProvider):
    """Used by tests and by the negative-path demo. Returns exactly what it is told to."""
    name = "mock"

    def __init__(self, canned: Optional[dict] = None):
        self.canned = canned or {}

    def extract_deal_fields(self, subject: str, body: str) -> dict:
        return dict(self.canned)


class GeminiLLM(LLMProvider):
    name = "gemini"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        self.api_key, self.model = api_key, model

    def extract_deal_fields(self, subject: str, body: str) -> dict:
        payload = {
            "contents": [{"parts": [{"text": f"{EXTRACTION_INSTRUCTIONS}\n\nSUBJECT: {subject}\n\nBODY:\n{body}"}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        # Key goes in a header, never the URL, so it cannot appear in an exception message or a log line.
        r = requests.post(self.URL.format(model=self.model), headers={"x-goog-api-key": self.api_key}, json=payload, timeout=30)
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return _extract_json(text)


class AnthropicLLM(LLMProvider):
    name = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5"):
        self.api_key, self.model = api_key, model

    def extract_deal_fields(self, subject: str, body: str) -> dict:
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        payload = {"model": self.model, "max_tokens": 400, "temperature": 0,
                   "system": EXTRACTION_INSTRUCTIONS,
                   "messages": [{"role": "user", "content": f"SUBJECT: {subject}\n\nBODY:\n{body}"}]}
        r = requests.post(self.URL, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
        return _extract_json(text)


def build_llm(settings) -> LLMProvider:
    """A named provider without its key is a configuration error, not a silent downgrade.
    (The mock LLM returns nothing, so free-text emails would all be rejected and it would
    look like the AEs were writing bad emails.)"""
    p = settings.llm_provider
    if p == "mock":
        return MockLLM()
    if p == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY is blank; set it or use LLM_PROVIDER=mock")
        return GeminiLLM(settings.gemini_api_key)
    if p == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is blank; set it or use LLM_PROVIDER=mock")
        return AnthropicLLM(settings.anthropic_api_key)
    raise RuntimeError(f"unknown LLM_PROVIDER {p!r}; use mock, gemini or anthropic")
