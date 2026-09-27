"""Stdlib client for SystemOne-compatible endpoints (POST /v1/systemone; TypeSafe by default).

One keep-alive HTTP(S) connection per process, a deadline-aware socket timeout on every request, retries with
backoff on 429/529/503, one reconnect on a stale keep-alive, and strict answer validation.
`validate_choice` is ported from browser-use/jev-ultrafast jev_ultrafast/model.py (MIT). See NOTICE.
"""

import http.client
import json
import math
import re
import ssl
import time
from dataclasses import dataclass, field

from . import config

PATH = "/v1/systemone"
MAX_SOCKET_TIMEOUT = 25.0
RETRY_STATUSES = {429, 529, 503}
MAX_RETRIES = 3
LOG_LIMIT = 50
_now = time.monotonic
_LIMIT_WORDS = re.compile(r"max_tokens|token|context|length|too (long|large)", re.I)
_RECONNECT_ERRORS = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, ssl.SSLError,
                     http.client.CannotSendRequest, http.client.ResponseNotReady)


class ServiceError(RuntimeError):
    """TypeSafe or network failure after retries; no action was executed."""


class RequestTooLarge(ServiceError):
    """The request exceeded a model context/length limit."""


class MissingKey(ServiceError):
    """The selected SystemOne bearer key is not configured."""


@dataclass
class Answer:
    answers: dict
    model: str
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def validate_noul(answer):
    try:
        value = answer["noul"]
        valid = type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return float(value)


_SECRET_PATTERNS = [
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
]


def redact(obj):
    """Replace Authorization/Bearer values and sk-… tokens with <redacted>, recursively."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if str(k).lower() in {"authorization", "x-api-key", "api_key"} else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact(v) for v in obj)
    if isinstance(obj, str):
        for pattern in _SECRET_PATTERNS:
            obj = pattern.sub("<redacted>", obj)
        return obj
    return obj


def _detail(body):
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return str(body)[:300]
    return json.dumps(data.get("detail", data) if isinstance(data, dict) else data)[:300]


class Client:
    def __init__(self, api_key=None, model=None, *, sleep=time.sleep):
        endpoint = config.jev_endpoint()
        key = config.jev_api_key(api_key)
        if not key and config.jev_auth() != "none":
            raise MissingKey(config.key_detail())
        self._key = key
        self._host = endpoint.netloc
        self._path = endpoint.path.rstrip("/") + PATH
        self._https = endpoint.scheme == "https"
        self._default_endpoint = config.jev_default_endpoint()
        self._service = "TypeSafe" if self._default_endpoint else "Jev endpoint"
        self._key_env = config.jev_api_key_env() if key else None
        self.model = model or config.get("jev.model")
        self._sleep = sleep
        self._conn = None
        self.log = []  # redacted request bodies only; headers are never stored

    def __repr__(self):
        return f"Client(model={self.model!r})"

    def _connection(self, timeout):
        if self._conn is None:
            connection = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
            self._conn = connection(self._host, timeout=timeout)
        self._conn.timeout = timeout
        if getattr(self._conn, "sock", None) is not None:
            self._conn.sock.settimeout(timeout)
        return self._conn

    def _drop(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def _timeout(self, deadline):
        if deadline is None:
            return MAX_SOCKET_TIMEOUT
        remaining = deadline - _now()
        if remaining <= 0:
            raise ServiceError("deadline reached before the TypeSafe request")
        return min(MAX_SOCKET_TIMEOUT, remaining)

    def _roundtrip(self, payload, deadline):
        """One HTTP exchange, reconnecting once if a reused keep-alive connection was closed."""
        reused = self._conn is not None
        for attempt in range(2):
            conn = self._connection(self._timeout(deadline))
            try:
                headers = {"Content-Type": "application/json"}
                if self._key:
                    headers["Authorization"] = f"Bearer {self._key}"
                conn.request("POST", self._path, body=payload, headers=headers)
                response = conn.getresponse()
                return response.status, response.read()
            except _RECONNECT_ERRORS as exc:
                self._drop()
                if attempt == 0 and reused:
                    continue
                raise ServiceError(f"{self._service} connection failed ({type(exc).__name__}); no action executed.") from None
            except (TimeoutError, OSError) as exc:
                self._drop()
                raise ServiceError(f"{self._service} connection failed ({type(exc).__name__}); no action executed.") from None
        raise ServiceError(f"{self._service} connection failed; no action executed.")

    def ask(self, state, questions, *, deadline=None):
        body = {"state": state, "model": self.model, "questions": questions}
        payload = json.dumps(body)
        self.log.append({"body": redact(body)})
        del self.log[:-LOG_LIMIT]
        started = time.perf_counter()
        for attempt in range(MAX_RETRIES + 1):
            status, raw = self._roundtrip(payload, deadline)
            if status in RETRY_STATUSES and attempt < MAX_RETRIES:
                self._sleep(0.5 * 2**attempt)
                continue
            if status in (401, 403):
                hint = (f"check {self._key_env} in the browser-harness agent-workspace .env"
                        if self._key else "check jev.auth and jev.api_key_env")
                raise ServiceError(f"{self._service} rejected authentication (HTTP {status}); {hint}. Not retried.")
            if status in (400, 413, 422):
                detail = redact(_detail(raw))
                too_large = bool(_LIMIT_WORDS.search(detail))
                # Custom servers may echo private URLs or arbitrary bearer tokens in error bodies.
                if not self._default_endpoint:
                    detail = "response detail withheld for a custom endpoint"
                elif self._key:
                    detail = detail.replace(self._key, "<redacted>")
                if too_large:
                    raise RequestTooLarge(f"{self._service} request too large: {detail}")
                raise ServiceError(f"{self._service} rejected the request (HTTP {status}): {detail}")
            if status != 200:
                raise ServiceError(f"{self._service} returned HTTP {status}; no action executed.")
            try:
                data = json.loads(raw)
                answers = data["answers"]
            except (ValueError, KeyError, TypeError):
                raise ServiceError(f"{self._service} returned an unreadable body; no action executed.") from None
            return Answer(answers=answers, model=data.get("model", self.model), usage=data.get("usage", {}),
                          latency_ms=round((time.perf_counter() - started) * 1000))
        raise ServiceError(f"{self._service} unavailable after retries; no action executed.")
