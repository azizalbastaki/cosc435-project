"""Read-only adaptation of the Student Development Guide Section 6.2 scaffold."""

import json
import random
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

ENDPOINTS = {
    "portfolio.php", "policies.php", "market_prices.php", "theory.php",
    "market_model.php", "market_news.php", "transactions.php",
}


class UncertainWrite(RuntimeError):
    """The request may have reached Ledger. Reconcile; never resend blindly."""


class ApiError(RuntimeError):
    def __init__(self, status, message, code=None):
        self.status, self.code = status, code
        label = f" ({code})" if code else ""
        super().__init__(f"Ledger HTTP {status}{label}: {message}")


class Cooldown(RuntimeError):
    def __init__(self, seconds, status=None):
        super().__init__("Wait before the next Ledger request")
        self.seconds, self.status = seconds, status


def validate_base(base):
    try:
        url = urlsplit(base)
        valid = (url.scheme == "https" and url.hostname
                 and url.username is None and url.password is None
                 and "?" not in base and "#" not in base
                 and not any(c.isspace() or ord(c) < 32 for c in base)
                 and "\\" not in base)
        url.port  # Reject malformed ports before any request.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Use the course's canonical HTTPS API base URL")
    return base.rstrip("/")


def retry_seconds(value):
    try:
        if value.isdigit():
            return max(1, int(value))
        return max(1, parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return 15


class LedgerClient:
    def __init__(self, base, key, *, role="trader", allow_writes=False):
        if role not in {"trader", "guardian"}:
            raise ValueError("Choose trader or guardian role")
        self.role = role
        self.allow_writes = allow_writes
        self.endpoints = ENDPOINTS | ({"trade_pending.php"} if role == "guardian" else set())
        self.base = validate_base(base)
        if (not key.strip() or key.strip().startswith("PASTE_")
                or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            raise ValueError(f"Configure the complete {role}-scoped API key")
        self.key = key
        self.session = requests.Session()
        # Avoid implicit .netrc credentials and environment proxy configuration.
        self.session.trust_env = False
        self.next_allowed = self.next_request_at = 0.0

    def close(self):
        self.session.close()

    def request(self, method, endpoint, *, params=None, body=None):
        write_endpoint = "trade_propose.php" if self.role == "trader" else "trade_review.php"
        is_write = method == "POST"
        if is_write:
            if not self.allow_writes or endpoint != write_endpoint or params:
                raise ValueError("Writes require explicit opt-in and the role's own endpoint")
            if not isinstance(body, dict):
                raise ValueError("POST requires a JSON object")
        elif method != "GET" or endpoint not in self.endpoints or body is not None:
            raise ValueError("This role permits only its documented read-only endpoints")
        headers = {"X-Api-Key": self.key}
        data = None
        if is_write:
            data = json.dumps(body, allow_nan=False, ensure_ascii=False).encode("utf-8")
            if len(data) >= 32768:
                raise ValueError("JSON body must be below 32 KiB")
            headers["Content-Type"] = "application/json"
        attempts = 1 if is_write else 3
        for attempt in range(attempts):
            delay = self.next_allowed - time.monotonic()
            if delay > 0:
                raise Cooldown(delay)
            spacing = self.next_request_at - time.monotonic()
            if spacing > 0:
                time.sleep(spacing)
            self.next_request_at = time.monotonic() + 1.1
            try:
                response = self.session.request(
                    method, f"{self.base}/{endpoint}",
                    headers=headers, params=params, data=data,
                    timeout=(5, 15), allow_redirects=False,
                )
            except requests.RequestException:
                if is_write:
                    raise UncertainWrite("Write response lost; reconcile before another attempt") from None
                if attempt + 1 == attempts:
                    raise ApiError(0, "Read failed; check connectivity and try later") from None
                time.sleep(2 ** attempt + random.uniform(0, 1))
                continue
            status = response.status_code
            if status in {429, 503}:
                seconds = retry_seconds(response.headers.get("Retry-After", "15"))
                self.next_allowed = time.monotonic() + seconds + random.uniform(0, 2)
                if is_write and status == 503:
                    raise UncertainWrite("Service unavailable; wait for cooldown and reconcile")
                raise Cooldown(self.next_allowed - time.monotonic(), status)
            if 300 <= status < 400:
                raise ApiError(status, "Redirect refused; check API base URL")
            if is_write and status >= 500:
                raise UncertainWrite("Server error; reconcile before another attempt")
            try:
                result = response.json()
            except ValueError:
                if is_write:
                    raise UncertainWrite("Unreadable write response; reconcile first") from None
                raise ApiError(status, "Expected JSON; check API base URL or hosting layer") from None
            if not isinstance(result, dict):
                if is_write:
                    raise UncertainWrite("Unexpected write response; reconcile first")
                raise ApiError(status, "Expected a JSON object")
            if not response.ok or "error" in result:
                # Never include server messages/bodies; even error codes are untrusted.
                code = result.get("error")
                if (not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code)
                        or self.key in code):
                    code = "unavailable"
                message = "API read failed"
                if status == 401:
                    message = "Use the full original key; replace it if revoked or lost"
                elif status == 403:
                    message = f"Use a {self.role}-scoped key; do not substitute a full key"
                raise ApiError(status, message, code)
            return result

    def get(self, endpoint, **params):
        return self.request("GET", endpoint, params=params)

    def post(self, endpoint, body):
        return self.request("POST", endpoint, body=body)
