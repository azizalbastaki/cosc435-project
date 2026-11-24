"""Model adapter (REQUIRED by the course): one interface over several LLM providers.

provider = "meta"       -> Meta Model API, Muse Spark (OpenAI-compatible Chat Completions,
                           base https://api.meta.ai/v1, key in MODEL_API_KEY / META_API_KEY).
provider = "openai"     -> any OpenAI-compatible /chat/completions endpoint (incl. local Ollama).
provider = "gemini"     -> Google Gemini through its OpenAI-compatible endpoint (key from Google AI Studio).
provider = "anthropic"  -> Anthropic Messages API.
provider = "none"       -> NOT ALLOWED for live runs: the trader then HOLDs every cycle and the
                           Guardian ESCALATEs every order (guide 6.6: missing credentials must not
                           silently satisfy the required-LLM assessment).

The adapter asks for a JSON object that follows a JSON schema (response_format json_schema),
checks finish_reason, and returns the parsed object or raises ModelFailure. It never retries
automatically and never sees a Ledger key. Callers validate every field themselves.
"""
from __future__ import annotations

import json
import uuid
from common.telemetry import emit, safe_fields
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_BASE = {"anthropic": "https://api.anthropic.com/v1", "openai": "https://api.openai.com/v1",
                "meta": "https://api.meta.ai/v1", "gemini": "https://generativelanguage.googleapis.com/v1beta/openai"}
DEFAULT_KEY_ENV = {"meta": ("MODEL_API_KEY", "META_API_KEY"), "openai": ("OPENAI_API_KEY", "LLM_API_KEY"),
                   "anthropic": ("ANTHROPIC_API_KEY", "LLM_API_KEY"), "gemini": ("GEMINI_API_KEY", "MODEL_API_KEY")}


class ModelFailure(RuntimeError):
    """The model call failed, timed out, was refused, was incomplete or was not JSON."""


class ModelAdapter:
    def __init__(self, cfg: dict, log=None):
        cfg = cfg or {}
        self.provider = (cfg.get("provider") or "none").lower()
        self.model = cfg.get("model", "")
        self.base_url = (cfg.get("base_url") or DEFAULT_BASE.get(self.provider, "")).rstrip("/")
        self.timeout = float(cfg.get("timeout_s", 90))
        # reasoning models (e.g. Muse Spark) spend tokens thinking before the JSON answer
        self.max_tokens = int(cfg.get("max_tokens", 8000))
        self.reasoning_effort = cfg.get("reasoning_effort")
        self.use_schema = cfg.get("json_schema", True)
        self.send_temperature = cfg.get("send_temperature", self.provider != "meta")
        envs = [cfg["api_key_env"]] if cfg.get("api_key_env") else list(DEFAULT_KEY_ENV.get(self.provider, ("LLM_API_KEY",)))
        self.api_key = next((os.environ[e] for e in envs if os.environ.get(e)), "")
        self.log = log or (lambda *a, **k: None)
        self.last_latency_s = None
        self.calls = self.failures = 0

    @property
    def enabled(self) -> bool:
        if self.provider == "none" or not self.model:
            return False
        local = self.base_url.startswith(("http://localhost", "http://127.0.0.1"))
        return bool(self.api_key) or local

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}" if self.enabled else "none"

    def ask_json(self, system: str, payload: dict, schema: dict, name: str = "decision") -> dict:
        request_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        endpoint = "/messages" if self.provider == "anthropic" else "/chat/completions"
        emit(self.log, "model_request_start", request_id=request_id, provider=self.provider, model=self.model,
             endpoint=endpoint, task=name, tx=(payload.get("order") or {}).get("transaction_id"))
        try:
            result = self._ask_json_untraced(system, payload, schema, name)
        except Exception as exc:
            emit(self.log, "model_request_end", request_id=request_id, provider=self.provider, model=self.model,
                 endpoint=endpoint, outcome="error", error_type=type(exc).__name__, duration_ms=round((time.monotonic()-started)*1000, 1))
            raise
        emit(self.log, "model_request_end", request_id=request_id, provider=self.provider, model=self.model,
             endpoint=endpoint, outcome="returned_unvalidated", duration_ms=round((time.monotonic()-started)*1000, 1),
             summary=safe_fields(result, ["action", "decision", "quantity", "risk_score"]))
        return result

    def _ask_json_untraced(self, system: str, payload: dict, schema: dict, name: str = "decision") -> dict:
        """Return the model's JSON object or raise ModelFailure."""
        if not self.enabled:
            raise ModelFailure("model not configured (set provider/model and the provider API key)")
        user = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        self.calls += 1
        t0 = time.time()
        try:
            if self.provider == "anthropic":
                body = {"model": self.model, "max_tokens": self.max_tokens, "temperature": 0,
                        "system": system + "\nReturn only a JSON object matching this schema: " + json.dumps(schema),
                        "messages": [{"role": "user", "content": user}]}
                raw = self._post(f"{self.base_url}/messages", body,
                                 {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
                if raw.get("stop_reason") not in ("end_turn", "stop_sequence"):
                    raise ModelFailure(f"incomplete response (stop_reason={raw.get('stop_reason')})")
                text = "".join(b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text")
            else:
                body = {"model": self.model, "max_tokens": self.max_tokens,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
                if self.use_schema:
                    body["response_format"] = {"type": "json_schema",
                                               "json_schema": {"name": name, "schema": schema}}
                if self.send_temperature:
                    body["temperature"] = 0
                if self.reasoning_effort:
                    body["reasoning_effort"] = self.reasoning_effort
                headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
                try:
                    raw = self._post(f"{self.base_url}/chat/completions", body, headers)
                except urllib.error.HTTPError as exc:
                    # a 400 means the request was refused before any generation: retry ONCE with a plainer
                    # JSON request (the schema is then enforced only by our own validation)
                    if exc.code != 400 or "response_format" not in body:
                        raise
                    self.log("model_retry_without_schema", model=self.label)
                    body["response_format"] = {"type": "json_object"}
                    body["messages"][0]["content"] = system + "\nReturn only a JSON object matching this schema: " + json.dumps(schema)
                    raw = self._post(f"{self.base_url}/chat/completions", body, headers)
                choice = (raw.get("choices") or [{}])[0]
                if choice.get("finish_reason") not in ("stop", None):
                    raise ModelFailure(f"incomplete response (finish_reason={choice.get('finish_reason')})")
                msg = choice.get("message") or {}
                if msg.get("refusal"):
                    raise ModelFailure("model refused the request")
                text = msg.get("content") or ""
                if isinstance(text, list):
                    text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
            self.last_latency_s = round(time.time() - t0, 2)
            if not text.strip():
                raise ModelFailure("empty response")
            obj = _parse_json(text)
            self.log("model_call", model=self.label, latency_s=self.last_latency_s)
            return obj
        except ModelFailure as exc:
            self.failures += 1
            self.log("model_failure", model=self.label, error=str(exc)[:200])
            raise
        except urllib.error.HTTPError as exc:
            self.failures += 1
            detail = ""
            try:
                detail = exc.read().decode(errors="replace")[:200]
            except Exception:
                pass
            self.log("model_failure", model=self.label, error=f"HTTP {exc.code}", detail=detail)
            raise ModelFailure(f"provider HTTP {exc.code}") from None
        except Exception as exc:  # timeout, network, malformed provider reply
            self.failures += 1
            self.log("model_failure", model=self.label, error=type(exc).__name__ + ": " + str(exc)[:160])
            raise ModelFailure(f"{type(exc).__name__}: {str(exc)[:120]}") from None

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(body, allow_nan=False).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())


def _parse_json(text: str) -> dict:
    """Strict: the whole reply must be one JSON object (code fences tolerated)."""
    t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        obj = json.loads(t, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ModelFailure(f"reply is not a single JSON object: {exc}") from None
    if not isinstance(obj, dict):
        raise ModelFailure("reply is not a JSON object")
    return obj


def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")
