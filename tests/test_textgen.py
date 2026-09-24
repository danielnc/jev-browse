import json
import os
import signal
import stat
import subprocess
import time

import pytest

from jev_browse import harness_api, textgen
from jev_browse.actions import Decision
from jev_browse.textgen import (
    ClaudeSubscriptionBackend,
    NullBackend,
    TextService,
    grounded,
    make_backend,
    normalise_date_value,
    parse_values,
    scrubbed_env,
)
from tests.fakes import FakeCDP, FakeProc, FakeTypeSafe, claude_json, noul_answer


@pytest.fixture(autouse=True)
def fake_harness(tmp_path):
    f = FakeCDP()
    f.tmp_dir = tmp_path
    f.socket_dir = tmp_path
    harness_api.install_fake(f)
    FakeProc.instances = []
    textgen._procs.clear()
    yield f
    textgen._procs.clear()
    harness_api.install_fake(None)


def proc_factory(*outputs, hang=False, returncode=0):
    outputs = list(outputs)

    def popen(argv, **kw):
        text = outputs.pop(0) if outputs else ""
        return FakeProc(argv, stdout_text=text, hang=hang, returncode=returncode, **kw)
    return popen


def fld(key, label=None, **kw):
    return {"key": key, "label": label or key, "node": hash(key) % 1000, "visible": True, "sensitive": False,
            "personal": False, "value": "", "role": "textbox", "placeholder": "", **kw}


# ---- parsing -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    '```json\n{"values": {"Search": "Paris"}}\n```',
    '```\n{"values": {"Search": "Paris"}}\n```',
    '{"values": {"Search": "Paris"}}',
])
def test_parse_values_strips_json_fence(text):
    assert parse_values(text, ["Search"]) == {"Search": "Paris"}


@pytest.mark.parametrize("text", [
    '{"values": {"Search": "Paris"}, "extra": 1}',
    '{"values": {"Other": "Paris"}}',
    '{"values": {"Search": "' + "x" * 501 + '"}}',
    '{"values": {"Search": 5}}',
    'Thinking: Paris',
])
def test_parse_values_rejects_extra_keys_unknown_keys_long_values(text):
    with pytest.raises((ValueError, TypeError)):
        parse_values(text, ["Search"])


# ---- argv, env, process hygiene -----------------------------------------------------------------------------
def test_cold_argv_exact(tmp_path):
    popen = proc_factory(claude_json('{"values": {"Search": "Paris"}}'))
    b = ClaudeSubscriptionBackend(model="haiku", popen=popen)
    r = b.batch("Open the article about the capital of France", [fld("Search")], "page", time.monotonic() + 30)
    assert r.values == {"Search": "Paris"}
    p = FakeProc.instances[0]
    argv = p.argv
    assert argv[:4] == ["claude", "-p", "--model", "haiku"]
    for flag in ["--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--max-turns",
                 "--output-format"]:
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--effort") + 1] == "low" and argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--bare" not in argv
    assert "Open the article" in p.input  # the prompt goes on stdin
    assert p.kwargs["start_new_session"] is True
    assert p.kwargs["stdin"] == subprocess.PIPE and p.kwargs["stdout"] == subprocess.PIPE
    cwd = p.kwargs["cwd"]
    assert cwd == str(tmp_path / "jev-browse-cli") and cwd != os.getcwd()
    assert stat.S_IMODE(os.stat(cwd).st_mode) == 0o700


def test_cold_json_result_parsed_with_usage_and_cost():
    popen = proc_factory(claude_json('{"values": {"Search": "Paris"}}', input_tokens=550, output_tokens=12, cost=0.001))
    r = ClaudeSubscriptionBackend(popen=popen).batch("g", [fld("Search")], "", None)
    assert r.tokens["input_tokens"] == 550 and r.tokens["output_tokens"] == 12 and r.cost_usd == pytest.approx(0.001)
    popen = proc_factory(claude_json("rate limited", is_error=True), claude_json("rate limited", is_error=True))
    r = ClaudeSubscriptionBackend(popen=popen).batch("g", [fld("Search")], "", None)
    assert r.values == {} and "error" in r.error


def test_make_backend_env_default(monkeypatch):
    monkeypatch.delenv("JEV_BROWSE_TEXT_BACKEND", raising=False)
    assert isinstance(make_backend(None), ClaudeSubscriptionBackend)
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "none")
    assert isinstance(make_backend(None), NullBackend)
    with pytest.raises(ValueError, match="unknown text_backend"):
        make_backend("gpt")


def test_auto_backend_is_none_without_the_claude_cli(monkeypatch):
    from jev_browse import config

    monkeypatch.setattr(config, "_which", lambda name: None)
    assert isinstance(make_backend(None), NullBackend)


def test_scrubbed_env_prefix_and_keeps_oauth():
    env = {"TYPESAFE_API_KEY": "t", "ANTHROPIC_API_KEY": "a", "ANTHROPIC_AUTH_TOKEN": "b", "CLAUDE_EFFORT": "high",
           "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_CODE_OAUTH_TOKEN": "o", "CLAUDE_CONFIG_DIR": "/c",
           "OPENAI_API_KEY": "x", "PATH": "/bin", "HOME": "/h"}
    out = scrubbed_env(env)
    assert out == {"CLAUDE_CODE_OAUTH_TOKEN": "o", "CLAUDE_CONFIG_DIR": "/c", "PATH": "/bin", "HOME": "/h"}


def test_subprocesses_killed_by_group(monkeypatch):
    killed = []
    monkeypatch.setattr(textgen.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    r = ClaudeSubscriptionBackend(popen=proc_factory(hang=True)).batch("g", [fld("Search")], "",
                                                                       time.monotonic() + 0.05)
    assert r.values == {} and "deadline" in r.error
    assert killed == [(4242, signal.SIGKILL)]


def test_backend_hang_respects_deadline(monkeypatch):
    monkeypatch.setattr(textgen.os, "killpg", lambda pid, sig: None)
    started = time.monotonic()
    r = ClaudeSubscriptionBackend(popen=proc_factory(hang=True)).batch("g", [fld("Search")], "", started + 0.1)
    assert r.error and time.monotonic() - started < 2


def test_retry_once_on_invalid_json_then_unavailable():
    popen = proc_factory(claude_json("not json"), claude_json("still not json"))
    r = ClaudeSubscriptionBackend(popen=popen).batch("g", [fld("Search")], "", None)
    assert r.values == {} and len(FakeProc.instances) == 2
    popen = proc_factory(claude_json("nope"), claude_json('{"values": {"Search": "Paris"}}'))
    assert ClaudeSubscriptionBackend(popen=popen).batch("g", [fld("Search")], "", None).values == {"Search": "Paris"}


def test_backend_cli_missing():
    def popen(*a, **k):
        raise FileNotFoundError("claude")
    r = ClaudeSubscriptionBackend(popen=popen).batch("g", [fld("Search")], "", None)
    assert r.error == "claude CLI not found on PATH"


def test_backend_error_result():
    popen = proc_factory(claude_json("Not logged in", is_error=True), claude_json("Not logged in", is_error=True))
    r = ClaudeSubscriptionBackend(popen=popen).batch("g", [fld("Search")], "", None)
    assert "Not logged in" in r.error


def test_null_backend_never_spawns():
    svc = TextService(NullBackend(), None)
    page = {"fields": [fld("Search")], "text": ""}
    d = Decision("TYPE_TEXT", "e1", {}, "1", 1.0, 1.0, {}, miss_fields=[("1", page["fields"][0]["node"], "Search",
                                                                          "Search")])
    assert svc.on_decision("g", page, d) is None
    assert svc.value_for("g", page, page["fields"][0], None)[0] is None
    assert FakeProc.instances == []


# ---- speculation ------------------------------------------------------------------------------------------
class SlowBackend:
    name = "claude"
    model = "haiku"

    def __init__(self, values, delay=0.05):
        self.values = values
        self.delay = delay
        self.calls = []

    def batch(self, goal, fields, excerpt, deadline):
        self.calls.append([f["key"] for f in fields])
        time.sleep(self.delay)
        return textgen.BatchResult(values={f["key"]: self.values.get(f["key"]) for f in fields}, latency_ms=50,
                                   tokens={"input_tokens": 500, "output_tokens": 10}, model="haiku", backend="claude",
                                   cost_usd=0.0006)


def miss_decision(*fields):
    return Decision("CLICK", "e9", {}, "9", 1.0, 1.0, {}, miss_fields=[("1", f["node"], f["label"], f["key"])
                                                                       for f in fields])


def test_after_miss_speculation_starts_only_for_miss_fields():
    a, b = fld("Where to?"), fld("Newsletter")
    page = {"fields": [a, b], "text": "x"}
    backend = SlowBackend({"Where to?": "Paris"})
    svc = TextService(backend, None)
    batch = svc.on_decision("Fly to the capital of France", page, miss_decision(a))
    batch.done.wait(2)
    assert backend.calls == [["Where to?"]]


def test_no_speculation_for_irrelevant_empty_fields():
    page = {"fields": [fld("Newsletter")], "text": ""}
    svc = TextService(SlowBackend({}), None)
    assert svc.on_decision("Click Next", page, miss_decision()) is None
    assert svc.on_observe("Click Next", page) is None  # after_miss mode never speculates on observe


def test_eager_speculation_starts_on_observe():
    page = {"fields": [fld("Search")], "text": ""}
    backend = SlowBackend({"Search": "x"})
    svc = TextService(backend, None, speculate_text="eager")
    svc.on_observe("g", page).done.wait(2)
    assert backend.calls == [["Search"]]


def test_off_never_speculates():
    a = fld("Where to?")
    svc = TextService(SlowBackend({}), None, speculate_text="off")
    assert svc.on_decision("g", {"fields": [a], "text": ""}, miss_decision(a)) is None


def test_speculation_result_used_and_overlap_recorded():
    a = fld("Where to?")
    page = {"fields": [a], "text": ""}
    svc = TextService(SlowBackend({"Where to?": "Paris"}, delay=0.1), None)
    svc.on_decision("Fly to Paris", page, miss_decision(a))
    time.sleep(0.05)
    value, source, _ = svc.value_for("Fly to Paris", page, a, time.monotonic() + 5)
    assert value == "Paris" and source == "llm"
    assert svc.stats["llm_ms_overlapped"] >= 40 and svc.stats["llm_calls"] == 1
    assert svc.value_for("Fly to Paris", page, a, None)[1] == "llm_cache"


def test_speculation_unused_counted():
    a = fld("Where to?")
    page = {"fields": [a], "text": ""}
    svc = TextService(SlowBackend({"Where to?": "Paris"}, delay=0.0), None)
    svc.on_decision("Fly to Paris", page, miss_decision(a)).done.wait(2)
    svc.close()
    assert svc.stats["llm_speculative_unused"] == 1 and svc.stats["llm_calls"] == 1


def test_cache_hit_same_goal_and_form_signature():
    a = fld("Where to?")
    page = {"fields": [a], "text": ""}
    backend = SlowBackend({"Where to?": "Paris"}, delay=0)
    svc = TextService(backend, None)
    svc.value_for("Fly to Paris", page, a, None)
    svc.value_for("Fly to Paris", dict(page), a, None)
    assert len(backend.calls) == 1


def test_speculation_thread_never_calls_typesafe():
    import threading

    seen = []

    class Client:
        def ask(self, *a, **k):
            seen.append(threading.current_thread().name)
            from jev_browse.typesafe import Answer
            return Answer(answers={"grounded": noul_answer(0.9)}, model="m")

    a = fld("Where to?")
    page = {"fields": [a], "text": ""}
    svc = TextService(SlowBackend({"Where to?": "the capital"}), Client())
    svc.on_decision("Fly to France's first city", page, miss_decision(a))
    svc.value_for("Fly to France's first city", page, a, time.monotonic() + 5)
    assert seen and all(name == threading.main_thread().name for name in seen)


def test_personal_fields_excluded_from_batch():
    a, email = fld("Where to?"), fld("Email", personal=True)
    page = {"fields": [a, email], "text": ""}
    backend = SlowBackend({"Where to?": "Paris"}, delay=0)
    svc = TextService(backend, None)
    svc.on_decision("g", page, miss_decision(a, email)).done.wait(2)
    assert backend.calls == [["Where to?"]]
    assert svc.value_for("g", page, email, None)[0] is None


def test_subprocess_stdio_is_piped_never_inherited():
    popen = proc_factory(claude_json('{"values": {"Search": "x"}}'))
    ClaudeSubscriptionBackend(popen=popen).batch("g", [fld("Search")], "", None)
    kw = FakeProc.instances[0].kwargs
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.PIPE


def test_no_backend_child_survives_return(monkeypatch):
    killed = []
    monkeypatch.setattr(textgen.os, "killpg", lambda pid, sig: killed.append(pid))
    hang = FakeProc(["claude"], hang=True)
    with textgen._procs_lock:
        textgen._procs.add(hang)
    svc = TextService(SlowBackend({}), None)
    svc.close()
    assert killed == [4242] and not textgen._procs


# ---- grounding and dates ----------------------------------------------------------------------------------
def test_grounding_rejects_account_email_not_in_goal():
    assert not grounded("owner@example.test", "Sign up for the newsletter", {"label": "Subscribe"})


def test_grounding_accepts_token_subset_without_noul():
    assert grounded("Paris", "Fly from London to Paris", {"label": "Where to?"}, client=None)


def test_grounding_uses_noul_for_nonpersonal():
    yes = FakeTypeSafe(lambda body: {"grounded": noul_answer(0.8)})
    no = FakeTypeSafe(lambda body: {"grounded": noul_answer(0.3)})
    goal = "Open the Wikipedia article about the capital city of France"
    assert grounded("Paris", goal, {"label": "Search"}, yes)
    assert not grounded("Paris", goal, {"label": "Search"}, no)
    assert "Paris" in json.dumps(yes.requests[0])


def test_grounding_pii_shapes_must_be_verbatim():
    goal = "Call +1 415 555 0100 about card 4111 1111 1111 1111"
    assert grounded("+1 415 555 0100", goal, {"label": "Note"})
    assert not grounded("+1 415 555 0199", goal, {"label": "Note"}, FakeTypeSafe(lambda b: {"grounded": noul_answer(1)}))
    assert not grounded("4012 8888 8888 1881", goal, {"label": "Note"},
                        FakeTypeSafe(lambda b: {"grounded": noul_answer(1)}))
    assert not grounded("123.456.789-09", "Register", {"label": "Note"}, FakeTypeSafe(lambda b: {"grounded": noul_answer(1)}))


def test_date_is_not_phone_shaped():
    assert textgen.pii_shaped("2026-10-20") is None and textgen.pii_shaped("10/20/2026") is None


def test_grounding_folds_diacritics():
    assert grounded("Zurich", "Trains from Zürich to Bern", {"label": "From"})


def test_llm_date_reparsed_and_rendered_in_field_format():
    assert normalise_date_value("2026-10-20", {"label": "Departure", "placeholder": "MM/DD/YYYY"}) == "10/20/2026"
    assert normalise_date_value("02/10/2026", {"label": "Departure date"}) is None
    assert normalise_date_value("Paris", {"label": "Where to?"}) == "Paris"


# ---- pre-started process (the default pool mode) ----------------------------------------------------------------
def stream_output(result_text, is_error=False):
    return "\n".join([json.dumps({"type": "system", "subtype": "init"}),
                      json.dumps({"type": "assistant", "message": {}}),
                      json.dumps({"type": "result", "is_error": is_error, "result": result_text,
                                  "usage": {"input_tokens": 531, "output_tokens": 14}, "total_cost_usd": 0.0006})])


def test_pool_argv_and_protocol(tmp_path):
    popen = proc_factory(stream_output('{"values": {"Search": "Paris"}}'), stream_output("{}"))
    b = ClaudeSubscriptionBackend(popen=popen, mode="pool")
    warm = b.prewarm()
    argv = warm.argv
    assert argv[argv.index("--input-format") + 1] == "stream-json" and "--verbose" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--bare" not in argv
    assert warm.kwargs["cwd"] == str(tmp_path / "jev-browse-cli") and warm.kwargs["start_new_session"]
    r = b.batch("Capital of France", [fld("Search")], "", None)
    assert r.values == {"Search": "Paris"} and r.tokens["input_tokens"] == 531
    msg = json.loads(warm.input)
    assert msg["type"] == "user" and msg["message"]["role"] == "user" and warm.input.count("\n") == 1
    assert "Capital of France" in msg["message"]["content"]


def test_pool_error_result_falls_back_to_a_fresh_cold_process():
    popen = proc_factory(stream_output("rate limited", is_error=True), "",
                         claude_json('{"values": {"Search": "Paris"}}'))
    b = ClaudeSubscriptionBackend(popen=popen, mode="pool")
    b.prewarm()
    r = b.batch("g", [fld("Search")], "", None)
    assert r.values == {"Search": "Paris"}
    assert "--input-format" not in FakeProc.instances[-1].argv  # the retry was a cold one-shot


def test_each_request_uses_fresh_process():
    popen = proc_factory(stream_output('{"values": {"Search": "a"}}'), stream_output('{"values": {"Search": "b"}}'),
                         stream_output("{}"))
    b = ClaudeSubscriptionBackend(popen=popen, mode="pool")
    b.prewarm()
    first = b.batch("g", [fld("Search")], "", None).values
    second = b.batch("g", [fld("Search")], "", None).values
    assert first == {"Search": "a"} and second == {"Search": "b"}
    used = [p for p in FakeProc.instances if p.input]
    assert len(used) == 2 and used[0] is not used[1]


def test_pool_refills_in_background():
    popen = proc_factory(stream_output('{"values": {"Search": "a"}}'), stream_output("{}"))
    b = ClaudeSubscriptionBackend(popen=popen, mode="pool")
    first = b.prewarm()
    b.batch("g", [fld("Search")], "", None)
    assert b._warm is not None and b._warm is not first and b._warm.input is None


def test_pool_disabled_by_env(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_TEXT_POOL", "0")
    b = ClaudeSubscriptionBackend(popen=proc_factory())
    assert b.mode == "cold" and b.prewarm() is None and FakeProc.instances == []


def test_prewarmed_process_killed_on_close(monkeypatch):
    killed = []
    monkeypatch.setattr(textgen.os, "killpg", lambda pid, sig: killed.append(pid))
    b = ClaudeSubscriptionBackend(popen=proc_factory(stream_output("{}")), mode="pool")
    b.prewarm()
    TextService(b, None).close()
    assert killed == [4242]
