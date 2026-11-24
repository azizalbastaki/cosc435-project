"""Ledger API v1 client (standard library only), following Student Development Guide 6.2.

* HTTPS base URL only (loopback HTTP allowed for local tests); URLs with credentials, query or
  fragment are rejected; redirects are refused so the key is never forwarded elsewhere.
* Only the 11 documented endpoints; key only in the X-Api-Key header; never logged.
* JSON bodies < 32 KiB with NaN/inf rejected; requests spaced >= 1.1 s apart.
* 429/503 -> cooldown from Retry-After plus jitter, respected by every later request of this client.
* A GET may be retried after a backoff (it cannot create an order). A POST is NEVER resent:
  a lost response, a 5xx or a 503 raises UncertainWrite and the caller must reconcile with history.
* Non-JSON (HTML) replies are reported without echoing the body.
"""
from __future__ import annotations

import json
import uuid
from common.telemetry import emit, response_summary, safe_fields
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINTS = {
    "market_prices.php", "market_news.php", "portfolio.php", "policies.php",
    "trade_propose.php", "trade_pending.php", "trade_review.php",
    "transactions.php", "guardian_logs.php", "theory.php", "market_model.php",
}


class LedgerError(RuntimeError):
    def __init__(self, status: int, payload, message: str | None = None):
        self.status, self.payload = status, payload
        self.code = payload.get("error") if isinstance(payload, dict) else None
        super().__init__(message or f"Ledger HTTP {status}: {self.code or str(payload)[:120]}")


class Cooldown(LedgerError):
    """Ledger asked us to slow down (429/503). Wait `seconds` before the next request."""

    def __init__(self, seconds: float, status: int = 429):
        super().__init__(status, {"error": "cooldown"}, f"Ledger cooldown: wait {seconds:.0f}s (HTTP {status})")
        self.seconds = seconds


class UncertainWrite(LedgerError):
    """A POST may or may not have been applied. Reconcile with history before any further write."""

    def __init__(self, message: str):
        super().__init__(0, {"error": "uncertain_write"}, message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _validate_base(base_url: str) -> str:
    u = urllib.parse.urlsplit(base_url)
    loopback = u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")
    if (u.scheme != "https" and not loopback) or not u.hostname or u.username or u.password \
            or u.query or u.fragment:
        raise ValueError("Use the course's canonical HTTPS API base URL (no credentials, query or fragment)")
    return base_url.rstrip("/") + "/"


class LedgerClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0, spacing_s: float = 1.1,
                 get_attempts: int = 3, sleep=time.sleep, clock=time.monotonic, log=None):
        if not api_key or not api_key.strip() or api_key.startswith(("PLACEHOLDER", "PASTE_")):
            raise ValueError("Configure this role's complete API key (environment variable, never the code).")
        self.base = _validate_base(base_url)
        self._key = api_key.strip()
        self.timeout, self.spacing_s, self.get_attempts = timeout, spacing_s, get_attempts
        self._sleep, self._clock = sleep, clock
        self.log = log or (lambda *a, **k: None)
        self._next_request_at = 0.0
        self._cooldown_until = 0.0
        self._opener = urllib.request.build_opener(_NoRedirect())

    # ------------------------------------------------------------------ transport
    def _request(self, method: str, endpoint: str, params: dict | None = None, body: dict | None = None) -> dict:
        request_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        emit(self.log, "api_start", request_id=request_id, service="Ledger", method=method,
             endpoint=endpoint if endpoint in ENDPOINTS else "invalid endpoint",
             request=safe_fields(body, ["transaction_id", "symbol", "action", "quantity", "decision", "theory_id", "theory_version"]))
        self._last_http_status = None
        try:
            result = self._request_untraced(method, endpoint, params, body)
        except Exception as exc:
            outcome = "uncertain" if isinstance(exc, UncertainWrite) else "cooldown" if isinstance(exc, Cooldown) else "error"
            emit(self.log, "api_end", request_id=request_id, service="Ledger", endpoint=endpoint if endpoint in ENDPOINTS else "invalid endpoint",
                 method=method, outcome=outcome, http_status=getattr(exc, "status", None) or self._last_http_status,
                 duration_ms=round((time.monotonic()-started)*1000, 1), error_type=type(exc).__name__)
            raise
        emit(self.log, "api_end", request_id=request_id, service="Ledger", endpoint=endpoint, method=method,
             outcome="returned", http_status=self._last_http_status,
             duration_ms=round((time.monotonic()-started)*1000, 1), summary=response_summary(endpoint, result))
        return result

    def _request_untraced(self, method: str, endpoint: str, params: dict | None = None, body: dict | None = None) -> dict:
        if endpoint not in ENDPOINTS or method not in ("GET", "POST"):
            raise ValueError("Use a documented endpoint and method")
        wait = self._cooldown_until - self._clock()
        if wait > 0:
            if method == "POST":
                raise Cooldown(wait)
            self.log("cooldown_wait", endpoint=endpoint, wait_s=round(wait, 1))
            self._sleep(wait)
        url = self.base + endpoint + ("?" + urllib.parse.urlencode(params) if params else "")
        data = None
        headers = {"X-Api-Key": self._key, "Accept": "application/json", "User-Agent": "cosc435-ledger/7"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(data) > 32768:
                raise ValueError("JSON request exceeds 32 KiB")
            headers["Content-Type"] = "application/json"
        attempts = self.get_attempts if method == "GET" else 1
        for attempt in range(attempts):
            spacing = self._next_request_at - self._clock()
            if spacing > 0:
                self._sleep(spacing)
            self._next_request_at = self._clock() + self.spacing_s
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    status, raw, ctype = resp.status, resp.read(), resp.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                status, raw, ctype = exc.code, exc.read() if exc.fp else b"", exc.headers.get("Content-Type", "") if exc.headers else ""
                self._last_http_status = status
                if status in (429, 503):
                    ra = (exc.headers.get("Retry-After") if exc.headers else None) or "15"
                    secs = max(1, int(ra)) if ra.isdigit() else 15
                    self._cooldown_until = self._clock() + secs + random.uniform(0, 2)
                    self.log("rate_limited", endpoint=endpoint, status=status, wait_s=secs)
                    if method == "POST":
                        if status == 503:
                            raise UncertainWrite("Ledger returned 503 to a write: wait and reconcile") from None
                        raise Cooldown(self._cooldown_until - self._clock(), status) from None
                    if attempt + 1 < attempts:
                        self._sleep(self._cooldown_until - self._clock())
                        continue
                    raise Cooldown(self._cooldown_until - self._clock(), status) from None
                if 300 <= status < 400:
                    raise LedgerError(status, {"error": "redirect_refused"}, "Redirect refused: check the API base URL") from None
                if method == "POST" and status >= 500:
                    raise UncertainWrite(f"Ledger HTTP {status} on a write: reconcile before another write") from None
                if method == "GET" and status >= 500 and attempt + 1 < attempts:
                    self._sleep(2 ** attempt + random.uniform(0, 1))
                    continue
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                if method == "POST":
                    raise UncertainWrite("Write response lost (network): reconcile first") from exc
                if attempt + 1 == attempts:
                    raise LedgerError(0, {"error": "network"}, "Read failed; try a later cycle") from exc
                self._sleep(2 ** attempt + random.uniform(0, 1))
                continue
            self._last_http_status = status
            return self._parse(method, status, raw, ctype)
        raise LedgerError(0, {"error": "retries_exhausted"})

    @staticmethod
    def _parse(method: str, status: int, raw: bytes, ctype: str) -> dict:
        try:
            result = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            if method == "POST" and 200 <= status < 300:
                raise UncertainWrite("Write returned an unreadable result: reconcile first") from None
            hint = " (HTML received: the base URL or hosting layer may be wrong)" if "html" in ctype.lower() else ""
            raise LedgerError(status, {"error": "non_json"}, f"Ledger HTTP {status}: expected JSON{hint}") from None
        if not 200 <= status < 300:
            # a restricted action is a logged rejection that still carries a JSON body
            if isinstance(result, dict) and result.get("status") == "REJECTED" and method == "POST":
                return result
            raise LedgerError(status, result)
        if not isinstance(result, dict):
            if method == "POST":
                raise UncertainWrite("Write returned an unexpected result: reconcile first")
            raise LedgerError(status, {"error": "not_object"}, "Expected a JSON object")
        return result

    def get(self, endpoint: str, **params) -> dict:
        return self._request("GET", endpoint, params=params or None)

    def post(self, endpoint: str, body: dict) -> dict:
        return self._request("POST", endpoint, body=body)

    # ------------------------------------------------------------------ typed helpers
    def market_model(self) -> dict: return self.get("market_model.php")["model"]
    def portfolio(self) -> dict: return self.get("portfolio.php")
    def theory(self) -> dict: return self.get("theory.php").get("theory") or {}
    def policy(self) -> dict: return self.get("policies.php")
    def news(self) -> list: return self.get("market_news.php").get("news", [])

    def quotes(self, symbols: list[str]) -> dict:
        r = self.get("market_prices.php", symbols=",".join(symbols))
        return {p["symbol"]: p for p in r.get("prices", [])}

    def transactions(self, status: str | None = None, limit: int = 200) -> list:
        params = {"limit": limit}
        if status:
            params["status"] = status
        return self.get("transactions.php", **params).get("transactions", [])

    def pending(self, limit: int = 2) -> dict: return self.get("trade_pending.php", limit=limit)
    def guardian_logs(self, limit: int = 50) -> list: return self.get("guardian_logs.php", limit=limit).get("guardian_logs", [])

    def propose(self, symbol: str, action: str, quantity: str, reasoning: str,
                theory_id: int | None, theory_version: int | None) -> dict:
        return self.post("trade_propose.php", {
            "symbol": symbol, "action": action, "quantity": quantity, "reasoning": reasoning,
            "theory_id": theory_id, "theory_version": theory_version})

    def review(self, transaction_id: int, decision: str, reason: str, risk_score: float,
               modified_quantity: str | None = None, rules_triggered: list | None = None) -> dict:
        body = {"transaction_id": transaction_id, "decision": decision, "reason": reason,
                "risk_score": round(float(risk_score), 2), "rules_triggered": (rules_triggered or [])[:100]}
        if modified_quantity is not None:
            body["modified_quantity"] = modified_quantity
        return self.post("trade_review.php", body)


def startup_check(client: LedgerClient, role: str) -> str:
    """Small read to verify credentials (guide 6.8); returns a human explanation on failure."""
    endpoint = "portfolio.php" if role == "trader" else "policies.php"
    try:
        client.get(endpoint)
        return ""
    except Cooldown as exc:
        return f"Ledger is rate-limiting: wait {exc.seconds:.0f}s and start again."
    except LedgerError as exc:
        if exc.status == 401:
            return "401: the key is missing, invalid or revoked; paste the full original key (it is shown once)."
        if exc.status == 403:
            return f"403 {exc.code}: this key does not have the '{role}' scope; create a {role}-scoped key."
        return f"Startup check failed: {exc}"
