"""Muse structured JSON transport. No Ledger requests or trading decisions."""

import json
import re

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from openai import APIError, APITimeoutError, OpenAI

from configuration import MUSE_FIELDS, read_config, unique_object


class ModelError(RuntimeError):
    """Safe model failure. Trader must HOLD; Guardian must not approve on failure."""

    def __init__(self, message, *, category="unknown", status=None):
        super().__init__(message)
        self.category = category
        self.status = status if type(status) is int and 100 <= status <= 599 else None


    def safe_summary(self):
        """Fixed diagnostic text; never include provider bodies or model output."""
        if self.category == "response":
            return "Muse reply was incomplete, refused, or failed the JSON schema checks."
        if self.category == "timeout":
            return "Muse request timed out. No automatic retry was attempted."
        if self.category == "request":
            if self.status is not None:
                return f"Muse provider request failed (HTTP {self.status})."
            return "Muse request failed without an HTTP status; check connectivity."
        return "Muse request or structured response failed."

def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def reject_credential_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if any(word in normalized for word in ("apikey", "authorization", "password", "secret", "token")):
                raise ModelError("Credential-like fields cannot be sent to the model")
            reject_credential_fields(item)
    elif isinstance(value, list):
        for item in value:
            reject_credential_fields(item)


class MuseAdapter:
    def __init__(self, *, api_key, base_url, model, forbidden_secrets=()):
        # Restrict this adapter to Meta; never send a configured key to another host.
        if base_url.rstrip("/") != "https://api.meta.ai/v1":
            raise ValueError("META_BASE_URL must be https://api.meta.ai/v1")
        if (not api_key or api_key.startswith("PASTE_")
                or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
            raise ValueError("Configure the complete META_API_KEY")
        if not re.fullmatch(r"muse-[a-z0-9.-]+", model):
            raise ValueError("Configure a valid Muse model identifier")
        self.model = model
        self._secrets = tuple(s for s in (api_key, *forbidden_secrets) if s)
        self._client = OpenAI(
            api_key=api_key, base_url=base_url.rstrip("/"),
            timeout=30.0, max_retries=0,
            http_client=httpx.Client(trust_env=False, follow_redirects=False),
        )

    @classmethod
    def from_config(cls, path, *, role="trader"):
        data = read_config(path, role=role)
        if not MUSE_FIELDS <= data.keys() or data["LLM_PROVIDER"] != "meta_muse":
            raise ValueError("Configure the four Muse fields with LLM_PROVIDER=meta_muse")
        return cls(api_key=data["META_API_KEY"], base_url=data["META_BASE_URL"],
                   model=data["MUSE_MODEL"],
                   forbidden_secrets=(data.get("TRADER_API_KEY", ""), data.get("GUARDIAN_API_KEY", "")))

    def close(self):
        self._client.close()

    def generate(self, system_prompt, payload, schema):
        """Return locally schema-validated JSON, or ModelError; never retry.

        Payloads must be explicitly constructed agent evidence, never config dicts.
        Schema validity alone is not order safety: run local business rules next.
        """
        try:
            if not isinstance(system_prompt, str) or not system_prompt.strip() or not isinstance(payload, dict):
                raise ValueError
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError
            Draft202012Validator.check_schema(schema)
            reject_credential_fields(payload)
            serialized = json.dumps(payload, allow_nan=False, ensure_ascii=False)
            outbound = json.dumps([system_prompt, payload, schema], allow_nan=False, ensure_ascii=False)
            if any(secret in outbound for secret in self._secrets):
                raise ModelError("Credentials cannot be sent in model input")
        except (ValueError, TypeError, SchemaError):
            raise ModelError("Invalid model prompt, payload, or schema") from None
        try:
            reply = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system_prompt},
                          {"role": "user", "content": serialized}],
                response_format={"type": "json_schema", "json_schema": {
                    "name": "agent_response", "strict": True, "schema": schema}},
                max_completion_tokens=2048,
            )
        except APIError as error:
            category = "timeout" if isinstance(error, APITimeoutError) else "request"
            raise ModelError("Muse request failed; check access, billing, model, and connectivity",
                             category=category, status=getattr(error, "status_code", None)) from None
        try:
            if len(reply.choices) != 1:
                raise ValueError
            choice = reply.choices[0]
            if choice.finish_reason != "stop" or choice.message.refusal:
                raise ValueError
            content = choice.message.content
            if not isinstance(content, str) or not content.strip():
                raise ValueError
            result = json.loads(content, parse_constant=reject_constant, object_pairs_hook=unique_object)
            if not isinstance(result, dict):
                raise ValueError
            # Also reject exponent-overflow floats (e.g. JSON 1e999).
            checked = json.dumps(result, allow_nan=False, ensure_ascii=False)
            if any(secret in checked for secret in self._secrets):
                raise ValueError
            Draft202012Validator(schema).validate(result)
            return result
        except (ValueError, TypeError, AttributeError, ValidationError):
            raise ModelError("Muse returned an incomplete, refused, or invalid structured response",
                             category="response") from None
