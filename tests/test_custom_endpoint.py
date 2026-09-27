"""Custom SystemOne endpoints: routing, authentication, preflight and privacy (offline)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jev_browse import config, doctor, textgen, typesafe
from tests.test_typesafe import OK, FakeConn, FakeResponse


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_JEV_BASE_URL", "https://example.com:8443/inference/")
    monkeypatch.setenv("JEV_BROWSE_JEV_MODEL", "kev-latest")
    monkeypatch.setenv("TYPESAFE_API_KEY", "hosted-test-secret")
    FakeConn.created = []
    script = [FakeResponse(200, OK)]
    monkeypatch.setattr(typesafe.http.client, "HTTPSConnection",
                        lambda host, timeout=None: FakeConn(host, timeout, script))
    return script


def ask(client):
    return client.ask("sample state", {"q": {"type": "noul", "instructions": "sample question"}})


def test_custom_endpoint_without_auth(endpoint, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_JEV_AUTH", "none")
    assert config.require_key() is None
    c = typesafe.Client()
    ask(c)
    conn = FakeConn.created[0]
    request = conn.requests[0]
    assert conn.host == "example.com:8443"
    assert request["path"] == "/inference/v1/systemone"
    assert "Authorization" not in request["headers"]
    assert json.loads(request["body"])["model"] == "kev-latest"
    assert "example.com" not in repr(c) + json.dumps(c.log)


def test_custom_endpoint_never_implicitly_uses_typesafe_key(endpoint):
    with pytest.raises(typesafe.MissingKey, match="JEV_BROWSE_API_KEY"):
        typesafe.Client()
    assert config.require_key() is not None
    assert not FakeConn.created


@pytest.mark.parametrize("explicit_name", [False, True])
def test_custom_bearer_key(endpoint, monkeypatch, explicit_name):
    name = "KEV_API_KEY" if explicit_name else "JEV_BROWSE_API_KEY"
    if explicit_name:
        monkeypatch.setenv("JEV_BROWSE_JEV_API_KEY_ENV", name)
    monkeypatch.setenv(name, "custom-test-secret")
    assert config.require_key() is None
    ask(typesafe.Client())
    assert FakeConn.created[0].requests[0]["headers"]["Authorization"] == "Bearer custom-test-secret"


def test_default_endpoint_preserves_typesafe_auth(endpoint, monkeypatch):
    monkeypatch.delenv("JEV_BROWSE_JEV_BASE_URL")
    ask(typesafe.Client())
    conn = FakeConn.created[0]
    assert conn.host == "api.typesafe.ai"
    assert conn.requests[0]["path"] == "/v1/systemone"
    assert conn.requests[0]["headers"]["Authorization"] == "Bearer hosted-test-secret"


def test_http_local_endpoint(endpoint, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_JEV_BASE_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("JEV_BROWSE_JEV_AUTH", "none")
    monkeypatch.setattr(typesafe.http.client, "HTTPConnection",
                        lambda host, timeout=None: FakeConn(host, timeout, endpoint))
    ask(typesafe.Client())
    assert FakeConn.created[0].host == "127.0.0.1:8080"
    assert FakeConn.created[0].requests[0]["path"] == "/v1/systemone"


@pytest.mark.parametrize("url", ["", "example.com", "ftp://example.com", "https://user:secret@example.com",
                                "https://example.com?token=secret", "https://example.com#secret",
                                "https://example.com:bad", "https://example.com:99999", "https://[broken",
                                "https://example.com/\nsecret", "https://example.com/a b"])
def test_invalid_urls_fail_closed_without_disclosing_url(endpoint, monkeypatch, url):
    monkeypatch.setenv("JEV_BROWSE_JEV_BASE_URL", url)
    monkeypatch.setenv("JEV_BROWSE_JEV_AUTH", "none")
    with pytest.raises(ValueError, match="jev.base_url") as exc:
        typesafe.Client()
    assert "example.com" not in str(exc.value) and "secret" not in str(exc.value)
    assert config.require_key() is not None
    check = doctor.check_key(offline=True)
    assert check.status == doctor.FAIL and "example.com" not in check.detail
    assert not FakeConn.created


def test_bad_auth_does_not_fall_back(endpoint, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_JEV_AUTH", "typo")
    with pytest.raises(ValueError, match="jev.auth"):
        typesafe.Client()
    assert config.require_key() is not None


def test_none_ignores_even_explicit_key(endpoint, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_JEV_AUTH", "none")
    ask(typesafe.Client(api_key="explicit-test-secret"))
    assert "Authorization" not in FakeConn.created[0].requests[0]["headers"]


def test_doctor_checks_unauthenticated_endpoint_live(endpoint, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_JEV_AUTH", "none")
    assert doctor.check_key(offline=True).status == doctor.OK
    assert not FakeConn.created
    check = doctor.check_key(offline=False)
    assert check.status == doctor.OK and len(FakeConn.created) == 1
    assert "example.com" not in check.detail


@pytest.mark.parametrize("status", [302, 401, 403, 422, 500])
def test_custom_server_errors_do_not_leak_keys_or_urls(endpoint, monkeypatch, status):
    monkeypatch.setenv("JEV_BROWSE_API_KEY", "custom-test-secret")
    endpoint[:] = [FakeResponse(status, {"detail": "custom-test-secret https://example.com:8443/inference/"})]
    check = doctor.check_key(offline=False)
    assert check.status == doctor.FAIL
    assert "custom-test-secret" not in check.detail and "example.com" not in check.detail
    assert len(FakeConn.created[0].requests) == 1


def test_custom_key_is_not_passed_to_text_cli(endpoint, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_JEV_API_KEY_ENV", "KEV_API_KEY")
    assert "KEV_API_KEY" not in textgen.scrubbed_env({"KEV_API_KEY": "custom-test-secret", "PATH": "/bin"})


def test_config_file_and_env_precedence_and_privacy(tmp_path, monkeypatch):
    path = tmp_path / "custom.toml"
    path.write_text('[jev]\nbase_url = "https://example.com/prefix"\nauth = "none"\nmodel = "kev-latest"\n')
    monkeypatch.setenv("JEV_BROWSE_CONFIG", str(path))
    assert config.get("jev.base_url") == "https://example.com/prefix"
    assert config.require_key() is None
    assert next(r for r in config.active() if r["key"] == "jev.base_url")["value"] == "<set>"
    monkeypatch.setenv("JEV_BROWSE_JEV_BASE_URL", "http://127.0.0.1:8080")
    assert config.get("jev.base_url") == "http://127.0.0.1:8080"


@pytest.mark.parametrize("auth", ["none", "bearer"])
def test_systemone_roundtrip_on_local_http_server(monkeypatch, auth):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers.get("Authorization"), payload))
            response = {"model": "kev-latest", "answers": {
                "team": {"type": "choice", "choice": "shipping", "confidence": 0.4005,
                         "probabilities": {"returns": 0.2327, "billing": 0.1669, "shipping": 0.6003}},
                "urgent": {"type": "noul", "noul": 0.3711}},
                "usage": {"input_tokens": 107, "output_tokens": 85}, "latency_ms": 354.0}
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("JEV_BROWSE_JEV_BASE_URL", f"http://127.0.0.1:{server.server_port}/proxy/")
    monkeypatch.setenv("JEV_BROWSE_JEV_MODEL", "kev-latest")
    monkeypatch.setenv("JEV_BROWSE_JEV_AUTH", auth)
    monkeypatch.setenv("JEV_BROWSE_API_KEY", "local-test-secret")
    client = typesafe.Client()
    questions = {"team": {"type": "choice", "instructions": "Which team?", "criteria": {
        "returns": "Exchange", "billing": "Payment", "shipping": "Delivery"}},
        "urgent": {"type": "noul", "instructions": "Urgent?"}}
    try:
        answer = client.ask("Sample delivery issue", questions)
        assert answer.model == "kev-latest" and answer.usage["input_tokens"] == 107
        typesafe.validate_choice(answer.answers["team"], {"returns", "billing", "shipping"})
        assert typesafe.validate_noul(answer.answers["urgent"]) == 0.3711
        path, authorization, payload = received[0]
        assert path == "/proxy/v1/systemone"
        assert authorization == ("Bearer local-test-secret" if auth == "bearer" else None)
        assert payload == {"state": "Sample delivery issue", "model": "kev-latest", "questions": questions}
    finally:
        client._drop()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_doctor_unexpected_exception_is_private(endpoint, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_JEV_AUTH", "none")

    def broken_client():
        raise RuntimeError("private server at https://example.com/inference with custom-test-secret")

    check = doctor.check_key(False, client_factory=broken_client)
    assert check.status == doctor.FAIL and "RuntimeError" in check.detail
    assert "example.com" not in check.detail and "custom-test-secret" not in check.detail
