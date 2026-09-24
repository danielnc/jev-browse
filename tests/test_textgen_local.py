"""Local (Ollama) and OpenAI-compatible text backends, with fallback to the Claude subscription backend."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jev_browse import harness_api, textgen
from jev_browse.textgen import (
    BatchResult,
    FallbackBackend,
    OllamaBackend,
    OpenAICompatBackend,
    TextService,
    make_backend,
)
from tests.fakes import FakeCDP


class Stub:
    """A tiny HTTP server that records requests and replies with scripted bodies."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                status, reply = stub.replies.pop(0) if len(stub.replies) > 1 else stub.replies[0]
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


def ollama_reply(content, prompt=61, out=10):
    return 200, {"model": "qwen3:30b-a3b", "message": {"role": "assistant", "content": content}, "done": True,
                 "prompt_eval_count": prompt, "eval_count": out, "total_duration": 400_000_000}


def fld(key, **kw):
    return {"key": key, "label": key, "node": 1, "visible": True, "sensitive": False, "personal": False, "value": "",
            **kw}


class ClaudeStub:
    name = "claude"
    model = "haiku"

    def __init__(self, values=None):
        self.calls = 0
        self.values = values or {}

    def batch(self, goal, fields, excerpt, deadline):
        self.calls += 1
        return BatchResult(values={f["key"]: self.values.get(f["key"]) for f in fields}, latency_ms=3000,
                           tokens={"input_tokens": 530, "output_tokens": 12}, model="haiku", backend="claude",
                           cost_usd=0.0007)

    def prewarm(self):
        self.prewarmed = True


@pytest.fixture(autouse=True)
def no_canary_by_default(monkeypatch):
    """Stub servers are not models: the canary is off unless a test turns it on (fresh_canary)."""
    monkeypatch.setattr(textgen, "CANARIES", [])
    textgen._canary_cache.clear()
    yield
    textgen._canary_cache.clear()


@pytest.fixture(autouse=True)
def harness(tmp_path, monkeypatch):
    f = FakeCDP()
    f.tmp_dir = tmp_path
    harness_api.install_fake(f)
    for k in ("JEV_BROWSE_TEXT_BACKEND", "JEV_BROWSE_OLLAMA_URL", "JEV_BROWSE_OLLAMA_MODEL",
              "JEV_BROWSE_OPENAI_BASE_URL", "JEV_BROWSE_OPENAI_API_KEY", "JEV_BROWSE_OPENAI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield
    harness_api.install_fake(None)


def test_ollama_request_shape_and_parse():
    stub = Stub([ollama_reply('{"values": {"Search": "Paris"}}')])
    try:
        b = OllamaBackend(stub.url, "qwen3:30b-a3b")
        r = b.batch("Open the article about the capital of France", [fld("Search")], "Welcome", time.monotonic() + 10)
        assert r.values == {"Search": "Paris"} and r.backend == "ollama" and r.model == "qwen3:30b-a3b"
        assert r.tokens == {"input_tokens": 61, "output_tokens": 10, "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0}
        assert r.cost_usd == 0.0
        req = stub.requests[0]
        assert req["path"] == "/api/chat"
        body = req["body"]
        assert body["model"] == "qwen3:30b-a3b" and body["stream"] is False
        assert body["format"]["properties"]["values"]["required"] == ["f1"]
        assert body["options"]["temperature"] == 0 and body["think"] is False and body["keep_alive"] == "30m"
        assert body["messages"][0]["role"] == "system" and "untrusted" in body["messages"][0]["content"]
        assert "capital of France" in body["messages"][1]["content"]
    finally:
        stub.close()


@pytest.mark.parametrize("model,think", [("qwen3:30b-a3b", False), ("gpt-oss:20b", "low"), ("gemma3:4b", None)])
def test_think_setting_per_model_family(model, think):
    body = OllamaBackend("http://127.0.0.1:1", model).request_body("g", [fld("x")], "")
    if think is None:
        assert "think" not in body
    else:
        assert body["think"] == think


def unused_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_unreachable_falls_back_to_claude_quickly_and_records_it():
    claude = ClaudeStub({"Search": "Paris"})
    b = FallbackBackend(OllamaBackend(f"http://127.0.0.1:{unused_port()}", "qwen3:30b-a3b"), claude)
    started = time.monotonic()
    r = b.batch("g", [fld("Search")], "", time.monotonic() + 10)
    assert time.monotonic() - started < 2.0
    assert r.values == {"Search": "Paris"} and claude.calls == 1
    assert r.fallback == {"from": "ollama", "why": "unreachable"} and r.backend == "claude"


def test_invalid_json_twice_falls_back():
    stub = Stub([ollama_reply("Thinking: Paris"), ollama_reply("not json either")])
    claude = ClaudeStub({"Search": "Paris"})
    try:
        r = FallbackBackend(OllamaBackend(stub.url, "qwen3:30b-a3b"), claude).batch("g", [fld("Search")], "", None)
        assert len(stub.requests) == 2 and claude.calls == 1
        assert r.values == {"Search": "Paris"} and r.fallback["why"] == "invalid output"
    finally:
        stub.close()


def test_invalid_once_then_valid_stays_local():
    stub = Stub([ollama_reply("nope"), ollama_reply('{"values": {"Search": "Paris"}}')])
    claude = ClaudeStub()
    try:
        r = FallbackBackend(OllamaBackend(stub.url, "m"), claude).batch("g", [fld("Search")], "", None)
        assert r.values == {"Search": "Paris"} and claude.calls == 0 and r.fallback is None
    finally:
        stub.close()


def test_null_values_are_a_valid_local_answer_not_a_fallback():
    stub = Stub([ollama_reply('{"values": {"Promo code": null}}')])
    claude = ClaudeStub()
    try:
        r = FallbackBackend(OllamaBackend(stub.url, "m"), claude).batch("g", [fld("Promo code")], "", None)
        assert r.values == {"Promo code": None} and claude.calls == 0
    finally:
        stub.close()


def test_errors_never_carry_the_host():
    b = OllamaBackend("http://secret-box.lan:11434", "m")
    b._connect_timeout = 0.05
    r = b.batch("g", [fld("x")], "", time.monotonic() + 2)
    assert r.error and "secret-box" not in r.error and "11434" not in r.error


def test_openai_compat_request_shape_key_and_fallback(monkeypatch):
    stub = Stub([(200, {"choices": [{"message": {"content": '{"values": {"Search": "Paris"}}'}}],
                        "usage": {"prompt_tokens": 70, "completion_tokens": 9}})])
    try:
        b = OpenAICompatBackend(stub.url + "/v1", "test-key-not-real", "some/model")
        r = b.batch("g", [fld("Search")], "", None)
        req = stub.requests[0]
        assert req["path"] == "/v1/chat/completions" and req["headers"]["Authorization"] == "Bearer test-key-not-real"
        assert req["body"]["response_format"] == {"type": "json_object"} and req["body"]["temperature"] == 0
        assert r.values == {"Search": "Paris"} and r.backend == "openai" and r.tokens["input_tokens"] == 70
        assert "test-key-not-real" not in json.dumps(b.__dict__.get("_last_error") or "")
    finally:
        stub.close()


def test_make_backend_selection_by_env(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "ollama")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_URL", "http://127.0.0.1:1")
    b = make_backend(None)
    assert isinstance(b, FallbackBackend) and isinstance(b.primary, OllamaBackend)
    assert b.primary.model == "qwen3:30b-a3b" and b.fallback.name == "claude"
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_MODEL", "gpt-oss:20b")
    assert make_backend(None).primary.model == "gpt-oss:20b"
    monkeypatch.delenv("JEV_BROWSE_OLLAMA_URL")
    with pytest.raises(ValueError, match="JEV_BROWSE_OLLAMA_URL"):
        make_backend("ollama")
    monkeypatch.setenv("JEV_BROWSE_OPENAI_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("JEV_BROWSE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("JEV_BROWSE_OPENAI_MODEL", "m")
    assert isinstance(make_backend("openai").primary, OpenAICompatBackend)
    monkeypatch.delenv("JEV_BROWSE_TEXT_BACKEND")
    assert isinstance(make_backend(None), textgen.ClaudeSubscriptionBackend)  # the default is unchanged


def test_fallback_backend_does_not_prewarm_claude():
    claude = ClaudeStub()
    FallbackBackend(OllamaBackend("http://127.0.0.1:1", "m"), claude).prewarm()
    assert not getattr(claude, "prewarmed", False)


def test_personal_and_sensitive_fields_never_reach_the_local_backend():
    stub = Stub([ollama_reply('{"values": {"Where to?": "Paris"}}')])
    try:
        svc = TextService(FallbackBackend(OllamaBackend(stub.url, "m"), ClaudeStub()), None)
        page = {"fields": [fld("Where to?"), fld("Email", personal=True), fld("Card", sensitive=True)], "text": ""}
        for f in page["fields"][1:]:
            assert svc.value_for("g", page, f, None)[0] is None
        svc.value_for("Fly to Paris", page, page["fields"][0], None)
        sent = json.dumps(stub.requests)
        assert "Email" not in sent and "Card" not in sent and "Where to?" in sent
    finally:
        stub.close()


def test_trace_records_backend_model_and_fallback():
    claude = ClaudeStub({"Search": "Paris"})
    svc = TextService(FallbackBackend(OllamaBackend(f"http://127.0.0.1:{unused_port()}", "m"), claude), None)
    page = {"fields": [fld("Search")], "text": ""}
    svc.value_for("Open the capital of France page Paris", page, page["fields"][0], None)
    call = svc.calls[-1]
    assert call["backend"] == "claude" and call["fallback"] == {"from": "ollama", "why": "unreachable"}


def test_text_eval_has_the_14_cases_and_sane_predicates():
    from bench import text_eval

    assert len(text_eval.CASES) == 14
    by = {name: ok for name, _, _, ok in text_eval.CASES}
    assert by["world: capital"]("Paris") and not by["world: capital"]("France")
    assert by["origin"]("Zürich") and by["origin"]("zurich")
    assert by["email -> null"](None) and not by["email -> null"]("me@x.test")
    assert by["search query"]("requests python library")


def test_tmp_dir_resolves_without_the_harness(monkeypatch, tmp_path):
    harness_api.install_fake(None)
    monkeypatch.setenv("BH_TMP_DIR", str(tmp_path))
    assert harness_api.tmp_dir() == tmp_path or harness_api.tmp_dir().exists()


def test_text_eval_summary_never_prints_a_leaked_personal_value():
    from bench import text_eval

    rows = [{"case": "email -> null", "rep": 0, "passed": False, "s": 1.0, "value": "owner@real.test"}]
    out = text_eval.summarise("x", rows)
    assert "owner@real.test" not in json.dumps(out) and "redacted" in out["misses"][0]


def test_local_prompt_uses_short_keys_and_a_strict_schema():
    from jev_browse.textgen import build_local_prompt, local_schema

    fields = [{"key": "Search Wikipedia #1", "label": "Search Wikipedia", "placeholder": "Search"}]
    data = json.loads(build_local_prompt("Open the capital of France", fields, "page text"))
    assert data["fields"] == [{"key": "f1", "label": "Search Wikipedia", "placeholder": "Search"}]
    assert data["goal"] == "Open the capital of France" and data["page_excerpt"] == "page text"
    schema = local_schema(fields)
    assert schema["additionalProperties"] is False and schema["properties"]["values"]["required"] == ["f1"]
    body = OllamaBackend("http://127.0.0.1:1", "qwen3:30b-a3b").request_body("g", fields, "page")
    assert body["options"]["num_predict"] <= 300 and body["format"] == schema


def test_echoed_input_is_invalid_and_retried_then_falls_back():
    echo = json.dumps({"goal": "g", "fields": [{"key": "Search"}], "page_excerpt": "..."})
    stub = Stub([ollama_reply(echo), ollama_reply(echo)])
    claude = ClaudeStub({"Search": "Paris"})
    try:
        r = FallbackBackend(OllamaBackend(stub.url, "m"), claude).batch("g", [fld("Search")], "", None)
        assert r.fallback == {"from": "ollama", "why": "invalid output"} and r.values == {"Search": "Paris"}
    finally:
        stub.close()


def test_short_keys_map_back_to_real_field_keys():
    stub = Stub([ollama_reply('{"values": {"f1": "Paris", "f2": null}}')])
    try:
        fields = [fld("Search Wikipedia #1"), fld("Promo code")]
        r = OllamaBackend(stub.url, "m").batch("g", fields, "", None)
        assert r.values == {"Search Wikipedia #1": "Paris", "Promo code": None}
    finally:
        stub.close()


def test_local_backends_use_the_local_system_prompt():
    from jev_browse import questions

    body = OllamaBackend("http://127.0.0.1:1", "qwen3:30b-a3b").request_body("g", [fld("Search")], "")
    assert body["messages"][0]["content"] == questions.TEXT_BATCH_LOCAL
    assert "personal information" in questions.TEXT_BATCH_LOCAL and "untrusted" in questions.TEXT_BATCH_LOCAL


# ---- num_gpu and the known-answer canary ------------------------------------------------------------------------
def test_num_gpu_passed_only_when_set(monkeypatch):
    assert "num_gpu" not in OllamaBackend("http://127.0.0.1:1", "m").request_body("g", [fld("x")], "")["options"]
    assert OllamaBackend("http://127.0.0.1:1", "m", num_gpu=0).request_body("g", [fld("x")], "")["options"][
        "num_gpu"] == 0
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "ollama")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_NUM_GPU", "0")
    assert make_backend(None).primary.num_gpu == 0


class Scripted:
    """A primary backend double: answers canaries from `canary`, real requests from `values`."""

    name = "ollama"
    model = "m"

    def __init__(self, canary, values=None):
        self.canary = canary
        self.values = values or {}
        self.real_calls = 0
        self.canary_calls = 0

    def batch(self, goal, fields, excerpt, deadline):
        key = fields[0]["key"]
        if goal in textgen.CANARIES_BY_GOAL:
            self.canary_calls += 1
            return BatchResult(values={key: self.canary.get(goal)}, backend="ollama", model="m")
        self.real_calls += 1
        return BatchResult(values={f["key"]: self.values.get(f["key"]) for f in fields}, backend="ollama", model="m")


REAL_CANARIES = [
    ("Find hotels in Lima", "Destination", "Lima"),
    ("Open the Wikipedia article about the capital city of France", "Search Wikipedia", "Paris"),
    ("Find one-way flights from Zurich to Dakar", "Where to?", "Dakar"),
]


@pytest.fixture
def fresh_canary(monkeypatch):
    monkeypatch.setattr(textgen, "CANARIES", REAL_CANARIES)
    textgen._canary_cache.clear()
    yield
    textgen._canary_cache.clear()


def right_answers():
    return {goal: answer for goal, _label, answer in REAL_CANARIES}


def test_canary_pass_keeps_the_local_backend(fresh_canary):
    primary = Scripted(right_answers(), {"Search": "Paris"})
    claude = ClaudeStub()
    b = FallbackBackend(primary, claude)
    r = b.batch("g", [fld("Search")], "", None)
    assert r.values == {"Search": "Paris"} and claude.calls == 0 and r.fallback is None
    assert r.canary["ok"] is True


def test_canary_wrong_answer_marks_backend_unhealthy_for_the_process(fresh_canary):
    answers = right_answers()
    first_goal = REAL_CANARIES[0][0]
    answers[first_goal] = "Oslo"
    primary = Scripted(answers, {"Search": "Paris"})
    claude = ClaudeStub({"Search": "Paris"})
    b = FallbackBackend(primary, claude)
    r = b.batch("g", [fld("Search")], "", None)
    assert r.backend == "claude" and r.fallback == {"from": "ollama", "why": "canary failed"}
    assert primary.real_calls == 0 and r.canary["ok"] is False and r.canary["wrong"] == [first_goal]
    # a second backend object in the same process reuses the verdict: no second canary
    primary2 = Scripted(right_answers())
    FallbackBackend(primary2, claude).batch("g", [fld("Search")], "", None)
    assert primary2.real_calls == 0


def test_canary_runs_once_per_process_and_prewarm_starts_it(fresh_canary):
    primary = Scripted(right_answers(), {"Search": "Paris"})
    b = FallbackBackend(primary, ClaudeStub())
    b.prewarm()
    b.batch("g", [fld("Search")], "", None)
    b.batch("g2", [fld("Search")], "", None)
    assert primary.real_calls == 2 and len(textgen._canary_cache) == 1


def test_canary_against_a_real_http_stub(fresh_canary):
    # the stub answers every request with f1 = "Oslo": a corrupted backend
    stub = Stub([ollama_reply('{"values": {"f1": "Oslo"}}')])
    claude = ClaudeStub({"Destination": "Lima"})
    try:
        r = FallbackBackend(OllamaBackend(stub.url, "m"), claude).batch("Find hotels in Lima", [fld("Destination")],
                                                                        "", None)
        assert r.backend == "claude" and r.fallback["why"] == "canary failed"
    finally:
        stub.close()


# ---- the canary verdict cached across processes -----------------------------------------------------------------
# Every browser-harness script is a new process, so the verdict is kept in a small file under the harness tmp dir
# (healthy for 10 min, unhealthy for 2 min). Clearing textgen._canary_cache stands in for a new process.
def new_process():
    textgen._canary_cache.clear()
    textgen._canary_threads.clear()


def cache_file():
    return harness_api.tmp_dir() / textgen.CANARY_CACHE_FILE


def entries():
    return json.loads(cache_file().read_text())["entries"]


def seed(primary, healthy, age_s, why=None):
    """Write one cache entry as an earlier process would have, `age_s` seconds ago."""
    cache_file().write_text(json.dumps({"entries": {textgen.canary_cache_key(primary): {
        "healthy": healthy, "why": why, "checked_at": time.time() - age_s, "latency_ms": 2400}}}))
    cache_file().chmod(0o600)


def test_canary_miss_runs_it_and_writes_a_private_verdict_file(fresh_canary):
    primary = Scripted(right_answers(), {"Search": "Paris"})
    r = FallbackBackend(primary, ClaudeStub()).batch("g", [fld("Search")], "", None)
    assert primary.canary_calls == 3 and r.canary["ok"] is True and not r.canary.get("cached")
    assert cache_file().stat().st_mode & 0o777 == 0o600
    (entry,) = entries().values()
    assert entry["healthy"] is True and isinstance(entry["latency_ms"], int)
    assert abs(entry["checked_at"] - time.time()) < 60
    assert [p.name for p in cache_file().parent.iterdir() if p.name.startswith(textgen.CANARY_CACHE_FILE)] == [
        textgen.CANARY_CACHE_FILE]  # atomic write: no temp file left behind


def test_canary_hit_in_a_new_process_skips_the_canary(fresh_canary):
    FallbackBackend(Scripted(right_answers()), ClaudeStub()).batch("g", [fld("Search")], "", None)
    new_process()
    primary = Scripted(right_answers(), {"Search": "Paris"})
    claude = ClaudeStub()
    r = FallbackBackend(primary, claude).batch("g", [fld("Search")], "", None)
    assert primary.canary_calls == 0 and primary.real_calls == 1 and claude.calls == 0
    assert r.canary["ok"] is True and r.canary["cached"] is True


def test_an_unhealthy_verdict_is_cached_too(fresh_canary):
    answers = right_answers()
    answers[REAL_CANARIES[0][0]] = "Oslo"
    FallbackBackend(Scripted(answers), ClaudeStub()).batch("g", [fld("Search")], "", None)
    new_process()
    primary = Scripted(right_answers())
    r = FallbackBackend(primary, ClaudeStub({"Search": "Paris"})).batch("g", [fld("Search")], "", None)
    assert primary.canary_calls == 0 and primary.real_calls == 0
    assert r.backend == "claude" and r.fallback == {"from": "ollama", "why": "canary failed"}
    assert r.canary["ok"] is False and r.canary["cached"] is True


def test_prewarm_in_a_new_process_uses_the_cached_verdict(fresh_canary):
    primary = Scripted(right_answers())
    seed(primary, True, 30)
    b = FallbackBackend(primary, ClaudeStub())
    b.prewarm()
    b.batch("g", [fld("Search")], "", None)
    assert primary.canary_calls == 0


@pytest.mark.parametrize("healthy,age_s,reruns", [
    (True, 9 * 60, False), (True, 11 * 60, True),     # healthy: 10 min
    (False, 90, False), (False, 3 * 60, True),         # unhealthy: 2 min
    (True, -3600, True),                               # checked "in the future" (clock moved): not trusted
])
def test_canary_cache_expiry(fresh_canary, healthy, age_s, reruns):
    primary = Scripted(right_answers())
    seed(primary, healthy, age_s, why=None if healthy else "canary failed")
    r = FallbackBackend(primary, ClaudeStub()).batch("g", [fld("Search")], "", None)
    assert (primary.canary_calls == 3) is reruns
    if reruns:  # the fresh verdict replaced the stale one
        (entry,) = entries().values()
        assert entry["healthy"] is True and time.time() - entry["checked_at"] < 60
        assert r.canary["ok"] is True


@pytest.mark.parametrize("content", [
    b"", b"not json", b"\x00\xff", b"[]", b'{"entries": []}', b'{"entries": {"KEY": "x"}}',
    b'{"entries": {"KEY": {"healthy": "yes", "checked_at": 1e18, "latency_ms": 1}}}',
    b'{"entries": {"KEY": {"healthy": true, "checked_at": "now", "latency_ms": 1}}}',
])
def test_a_corrupt_cache_file_is_treated_as_absent(fresh_canary, content):
    primary = Scripted(right_answers())
    cache_file().write_bytes(content.replace(b"KEY", textgen.canary_cache_key(primary).encode()))
    cache_file().chmod(0o600)
    r = FallbackBackend(primary, ClaudeStub()).batch("g", [fld("Search")], "", None)
    assert primary.canary_calls == 3 and r.canary["ok"] is True
    assert list(entries().values())[0]["healthy"] is True  # rewritten cleanly


def test_a_cache_file_others_can_write_is_ignored(fresh_canary):
    primary = Scripted(right_answers())
    seed(primary, True, 30)
    cache_file().chmod(0o666)
    FallbackBackend(primary, ClaudeStub()).batch("g", [fld("Search")], "", None)
    assert primary.canary_calls == 3
    assert cache_file().stat().st_mode & 0o777 == 0o600


def test_an_unreadable_cache_dir_still_runs_the_canary(fresh_canary, monkeypatch, tmp_path):
    monkeypatch.setattr(harness_api, "tmp_dir", lambda: tmp_path / "missing" / "nested")
    primary = Scripted(right_answers(), {"Search": "Paris"})
    r = FallbackBackend(primary, ClaudeStub()).batch("g", [fld("Search")], "", None)
    assert primary.canary_calls == 3 and r.values == {"Search": "Paris"}


def test_canary_cache_key_isolation(fresh_canary):
    base = OllamaBackend("http://127.0.0.1:1", "qwen3:30b-a3b", num_gpu=0)
    variants = [OllamaBackend("http://127.0.0.1:2", "qwen3:30b-a3b", num_gpu=0),
                OllamaBackend("http://127.0.0.1:1", "gemma3:4b", num_gpu=0),
                OllamaBackend("http://127.0.0.1:1", "qwen3:30b-a3b", num_gpu=None),
                OllamaBackend("http://127.0.0.1:1", "qwen3:30b-a3b", num_gpu=99),
                OpenAICompatBackend("http://127.0.0.1:1", None, "qwen3:30b-a3b"),
                OllamaBackend("http://127.0.0.1:1", "qwen3:30b-a3b", num_gpu=0, think="on")]
    keys = {textgen.canary_cache_key(b) for b in [base, *variants]}
    assert len(keys) == 7
    assert textgen.canary_cache_key(base) == textgen.canary_cache_key(
        OllamaBackend("http://127.0.0.1:1", "qwen3:30b-a3b", num_gpu=0))


def test_a_verdict_for_another_model_is_not_reused(fresh_canary):
    other = Scripted(right_answers())
    other.model = "gemma3:4b"
    seed(other, True, 30)
    primary = Scripted(right_answers())
    FallbackBackend(primary, ClaudeStub()).batch("g", [fld("Search")], "", None)
    assert primary.canary_calls == 3
    assert len(entries()) == 2  # the other model's fresh entry is kept


def test_changing_the_canaries_invalidates_the_cache(fresh_canary, monkeypatch):
    primary = Scripted(right_answers())
    key = textgen.canary_cache_key(primary)
    monkeypatch.setattr(textgen, "CANARIES", REAL_CANARIES[:2])
    assert textgen.canary_cache_key(primary) != key


def test_the_cache_file_holds_no_secrets_or_hosts(fresh_canary):
    b = OpenAICompatBackend("https://llm.example.internal:8443/v1", "sk-test-secret-value", "some-model")
    textgen.save_canary_verdict(b, {"ok": True, "why": None, "wrong": [], "ms": 1200})
    text = cache_file().read_text()
    for private in ("sk-test-secret-value", "example.internal", "8443", "some-model"):
        assert private not in text
    new_process()
    assert textgen.load_canary_verdict(b)["ok"] is True


def test_expired_entries_are_pruned_on_write(fresh_canary):
    stale = Scripted(right_answers())
    stale.model = "old"
    seed(stale, True, 3600)
    primary = Scripted(right_answers())
    FallbackBackend(primary, ClaudeStub()).batch("g", [fld("Search")], "", None)
    assert list(entries()) == [textgen.canary_cache_key(primary)]


def test_trace_marks_a_cached_canary(fresh_canary):
    primary = Scripted(right_answers(), {"Search": "Paris"})
    seed(primary, True, 30)
    r = FallbackBackend(primary, ClaudeStub()).batch("g", [fld("Search")], "", None)
    assert r.canary == {"ok": True, "why": None, "wrong": [], "ms": 2400, "cached": True}


def test_trace_step_records_that_the_canary_verdict_was_cached(fresh_canary):
    primary = Scripted(right_answers(), {"Search": "Paris"})
    seed(primary, True, 30)
    svc = TextService(FallbackBackend(primary, ClaudeStub()), None)
    page = {"fields": [fld("Search")], "text": ""}
    _value, _source, meta = svc.value_for("Open the capital of France page Paris", page, page["fields"][0], None)
    assert meta["llm"]["canary"] == {"ok": True, "why": None, "ms": 2400, "cached": True}


# ---- configuration: options, key indirection, fallback choice, canary switch ------------------------------------
def test_ollama_keep_alive_and_think_options(monkeypatch):
    b = OllamaBackend("http://127.0.0.1:1", "qwen3:30b-a3b", keep_alive="5m", think="on")
    body = b.request_body("g", [fld("x")], "")
    assert body["keep_alive"] == "5m" and body["think"] is True
    assert "think" not in OllamaBackend("http://127.0.0.1:1", "llama3", think="auto").request_body("g", [fld("x")], "")
    assert OllamaBackend("http://127.0.0.1:1", "llama3", think="low").request_body("g", [fld("x")], "")["think"] == "low"
    assert OllamaBackend("http://127.0.0.1:1", "qwen3:8b", think="off").request_body("g", [fld("x")], "")[
        "think"] is False
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "ollama")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_KEEP_ALIVE", "1h")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_THINK", "off")
    p = make_backend(None).primary
    assert p.keep_alive == "1h" and p.think == "off"


def test_ollama_settings_from_the_config_file(tmp_path, monkeypatch):
    from jev_browse import config

    cfg = tmp_path / "config.toml"
    cfg.write_text('[text]\nbackend = "ollama"\n[ollama]\nurl = "http://127.0.0.1:1"\nmodel = "gemma3:4b"\n'
                   'num_gpu = 0\n')
    monkeypatch.setenv("JEV_BROWSE_CONFIG", str(cfg))
    config.reset_cache()
    p = make_backend(None).primary
    assert (p.model, p.num_gpu) == ("gemma3:4b", 0)


def test_openai_key_comes_from_the_named_variable(monkeypatch):
    stub = Stub([(200, {"choices": [{"message": {"content": '{"values": {"f1": "Paris"}}'}}], "usage": {}})])
    try:
        monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "openai")
        monkeypatch.setenv("JEV_BROWSE_OPENAI_BASE_URL", stub.url)
        monkeypatch.setenv("JEV_BROWSE_OPENAI_MODEL", "some/model")
        monkeypatch.setenv("JEV_BROWSE_OPENAI_API_KEY_ENV", "OPENROUTER_API_KEY")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
        r = make_backend(None).batch("g", [fld("Search")], "", None)
        assert r.values == {"Search": "Paris"}
        assert stub.requests[0]["headers"]["Authorization"] == "Bearer or-test-key"
    finally:
        stub.close()


def test_fallback_none_hands_back_instead_of_sending_text_elsewhere(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "ollama")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_URL", f"http://127.0.0.1:{unused_port()}")
    monkeypatch.setenv("JEV_BROWSE_TEXT_FALLBACK", "none")
    r = make_backend(None).batch("g", [fld("Search")], "", None)
    assert r.values == {} and r.backend == "ollama" and r.fallback is None
    assert "unreachable" in r.error and "text.fallback" in r.error


def test_fallback_auto_without_the_claude_cli_is_none(monkeypatch):
    from jev_browse import config

    monkeypatch.setattr(config, "_which", lambda name: None)
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "ollama")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_URL", "http://127.0.0.1:1")
    assert isinstance(make_backend(None).fallback, textgen.NullBackend)


def test_canary_can_be_switched_off(fresh_canary, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_CANARY", "0")
    primary = Scripted(right_answers(), {"Search": "Paris"})
    r = FallbackBackend(primary, ClaudeStub()).batch("g", [fld("Search")], "", None)
    assert primary.canary_calls == 0 and r.values == {"Search": "Paris"} and r.canary["ok"] is True
    assert not cache_file().exists()


def test_canary_ttl_comes_from_config(fresh_canary, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_CANARY_TTL_S", "60")
    primary = Scripted(right_answers())
    seed(primary, True, 90)  # fresh under the 10-min default, stale under a 60-s TTL
    FallbackBackend(primary, ClaudeStub()).batch("g", [fld("Search")], "", None)
    assert primary.canary_calls == 3


def test_text_eval_builds_backends_from_config_and_never_labels_with_a_url(monkeypatch):
    from bench import text_eval

    monkeypatch.setenv("JEV_BROWSE_OLLAMA_URL", "http://192.0.2.3:11434")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_NUM_GPU", "0")
    (label, b), = text_eval.backends_for("ollama", [])
    assert b.model == "qwen3:30b-a3b" and b.num_gpu == 0 and "192.0.2.3" not in label
    assert [lb for lb, _ in text_eval.backends_for("ollama", ["a", "b"])] == ["ollama a num_gpu=0", "ollama b num_gpu=0"]
    monkeypatch.setenv("JEV_BROWSE_OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("JEV_BROWSE_OPENAI_API_KEY_ENV", "GROQ_API_KEY")
    monkeypatch.setenv("GROQ_API_KEY", "gk-test")
    (label, b), = text_eval.backends_for("openai", ["m1"])
    assert b._key == "gk-test" and "example.invalid" not in label
