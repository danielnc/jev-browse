import http.client
import json

import pytest

from jev_browse import typesafe
from jev_browse.typesafe import (
    Client,
    MissingKey,
    RequestTooLarge,
    ServiceError,
    redact,
    validate_choice,
    validate_noul,
)


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


# Ported from browser-use/jev-ultrafast tests/test_agent.py (MIT). See NOTICE.
@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        validate_choice(a, {"a", "b"})


@pytest.mark.parametrize("value", [1.2, -0.1, float("nan"), "0.5", None])
def test_noul_out_of_range_rejected(value):
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        validate_noul({"type": "noul", "noul": value})
    assert validate_noul({"type": "noul", "noul": 0.25}) == 0.25


class FakeSock:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, t):
        self.timeouts.append(t)


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode() if not isinstance(body, bytes) else body

    def read(self):
        return self._body


class FakeConn:
    """Scripted HTTPSConnection. `script` items are FakeResponse or exceptions raised from getresponse()."""

    created = []

    def __init__(self, host, timeout=None, script=None):
        self.host = host
        self.timeout = timeout
        self.sock = None
        self.requests = []
        self.script = script
        FakeConn.created.append(self)

    def request(self, method, path, body=None, headers=None):
        self.requests.append({"method": method, "path": path, "body": body, "headers": headers})
        if self.sock is None:
            self.sock = FakeSock()

    def getresponse(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            self.sock = None
            raise item
        return item

    def close(self):
        self.sock = None


OK = {"model": "jev-1.13.0", "answers": {"q": {"type": "noul", "noul": 0.9}},
      "usage": {"input_tokens": 10, "output_tokens": 2}}


@pytest.fixture
def conns(monkeypatch):
    script = []
    FakeConn.created = []
    monkeypatch.setattr(typesafe.http.client, "HTTPSConnection",
                        lambda host, timeout=None: FakeConn(host, timeout, script))
    return script


def client(**kw):
    return Client(api_key="test-key-not-real", sleep=lambda s: None, **kw)


def test_retries_429_529_503_then_succeeds(conns):
    conns += [FakeResponse(429, {}), FakeResponse(529, {}), FakeResponse(503, {}), FakeResponse(200, OK)]
    slept = []
    c = Client(api_key="k", sleep=slept.append)
    a = c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
    assert a.answers["q"]["noul"] == 0.9
    assert slept == [0.5, 1.0, 2.0]


def test_reconnects_after_server_closed_idle_connection(conns):
    conns += [FakeResponse(200, OK), http.client.RemoteDisconnected("closed"), FakeResponse(200, OK)]
    c = client()
    c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
    c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
    assert len(FakeConn.created) == 2


def test_socket_timeout_set_per_request_on_reused_connection(conns, monkeypatch):
    conns += [FakeResponse(200, OK), FakeResponse(200, OK)]
    now = [100.0]
    monkeypatch.setattr(typesafe, "_now", lambda: now[0])
    c = client()
    c.ask("s", {"q": {"type": "noul", "instructions": "x"}}, deadline=200.0)
    sock = FakeConn.created[0].sock
    c.ask("s", {"q": {"type": "noul", "instructions": "x"}}, deadline=110.0)
    assert sock.timeouts[-1] == pytest.approx(10.0)
    assert FakeConn.created[0].timeout == pytest.approx(10.0)


def test_422_context_limit_raises_request_too_large(conns):
    conns += [FakeResponse(400, {"detail": {"error_type": "max_tokens_exceeded"}})]
    with pytest.raises(RequestTooLarge):
        client().ask("s", {"q": {"type": "noul", "instructions": "x"}})
    conns += [FakeResponse(422, {"detail": "context length exceeded"})]
    with pytest.raises(RequestTooLarge):
        client().ask("s", {"q": {"type": "noul", "instructions": "x"}})


def test_422_other_is_service_error(conns):
    conns += [FakeResponse(422, {"detail": [{"loc": ["body", "questions", "q"], "msg": "field required"}]})]
    with pytest.raises(ServiceError) as e:
        client().ask("s", {"q": {"type": "noul", "instructions": "x"}})
    assert not isinstance(e.value, RequestTooLarge)
    assert "questions" in str(e.value)


def test_401_raises_service_error_without_key_in_message(conns):
    conns += [FakeResponse(401, {"detail": "bad key test-key-not-real"})]
    with pytest.raises(ServiceError) as e:
        client().ask("s", {"q": {"type": "noul", "instructions": "x"}})
    assert "test-key-not-real" not in str(e.value)
    assert "TYPESAFE_API_KEY" in str(e.value)
    assert len(FakeConn.created[0].requests) == 1  # never retried


def test_connection_reused_across_calls(conns):
    conns += [FakeResponse(200, OK), FakeResponse(200, OK)]
    c = client()
    c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
    c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
    assert len(FakeConn.created) == 1


def test_missing_key_raises_missing_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(MissingKey):
        Client()


def test_model_from_env(monkeypatch, conns):
    monkeypatch.setenv("JEV_BROWSE_MODEL", "jev-x")
    conns += [FakeResponse(200, OK)]
    c = Client(api_key="k")
    c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
    assert json.loads(FakeConn.created[0].requests[0]["body"])["model"] == "jev-x"


def test_redact_strips_bearer_and_sk_tokens():
    obj = {"a": "Authorization: Bearer abc.def-123", "b": ["sk-ant-api03-XYZXYZXYZXYZXYZXYZ", {"c": "fine"}],
           "Authorization": "Bearer qqq", "d": "prefix Bearer tok_123 suffix"}
    out = redact(obj)
    text = json.dumps(out)
    assert "abc.def-123" not in text and "XYZXYZ" not in text and "qqq" not in text and "tok_123" not in text
    assert out["b"][1] == {"c": "fine"}


def test_request_log_has_no_headers(conns):
    conns += [FakeResponse(200, OK)]
    c = client()
    c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
    assert c.log and "headers" not in c.log[-1] and "test-key-not-real" not in json.dumps(c.log)
    assert c.log[-1]["body"]["questions"]["q"]["instructions"] == "x"
