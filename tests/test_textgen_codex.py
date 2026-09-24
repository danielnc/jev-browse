"""CodexSubscriptionBackend: `codex exec` on the ChatGPT subscription, cold one-shot. Unmeasured on a live account
(see docs/backends.md); these tests pin the argv, privacy, and parsing contract with a Popen double."""

import json
import os
import stat
from pathlib import Path

import pytest

from jev_browse import harness_api, textgen
from jev_browse.textgen import CodexSubscriptionBackend, FallbackBackend, make_backend
from tests.fakes import FakeCDP


@pytest.fixture(autouse=True)
def harness(tmp_path):
    f = FakeCDP()
    f.tmp_dir = tmp_path
    harness_api.install_fake(f)
    textgen._procs.clear()
    yield tmp_path
    textgen._procs.clear()
    harness_api.install_fake(None)


def fld(key):
    return {"key": key, "label": key, "placeholder": "", "node": 1}


class CodexProc:
    """Writes `message` to the -o file like codex does, and prints JSONL events on stdout."""

    calls = []

    def __init__(self, message, usage=None, returncode=0, hang=False, write=True):
        self.message, self.usage, self.rc, self.hang, self.write = message, usage, returncode, hang, write

    def __call__(self, argv, **kwargs):
        spec = self
        schema = Path(argv[argv.index("--output-schema") + 1]).read_text()
        mode = stat.S_IMODE(os.stat(argv[argv.index("--output-schema") + 1]).st_mode)

        class P:
            pid = 4343
            returncode = None
            killed = False

            def communicate(self, input=None, timeout=None):
                import subprocess

                CodexProc.calls.append({"argv": argv, "kwargs": kwargs, "input": input, "schema": json.loads(schema),
                                        "schema_mode": mode})
                if spec.hang:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if spec.write:
                    Path(argv[argv.index("-o") + 1]).write_text(spec.message)
                self.returncode = spec.rc
                events = [{"type": "thread.started"}]
                if spec.usage:
                    events.append({"type": "turn.completed", "usage": spec.usage})
                return "\n".join(json.dumps(e) for e in events), ""

            def wait(self, timeout=None):
                self.returncode = -9
                return -9

            def poll(self):
                return self.returncode

            def kill(self):
                self.killed = True

        return P()


@pytest.fixture(autouse=True)
def reset_calls():
    CodexProc.calls = []


def test_argv_is_read_only_ephemeral_and_carries_no_page_text(harness, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    popen = CodexProc('{"values": {"f1": "Paris"}}', usage={"input_tokens": 700, "cached_input_tokens": 100,
                                                          "output_tokens": 9})
    r = CodexSubscriptionBackend(popen=popen).batch("Open the article about the capital of France",
                                                     [fld("Search Wikipedia")], "SECRET PAGE TEXT", None)
    assert r.values == {"Search Wikipedia": "Paris"} and r.backend == "codex" and r.model == "gpt-5.6-luna"
    assert r.tokens["input_tokens"] == 700 and r.tokens["output_tokens"] == 9
    call = CodexProc.calls[0]
    argv = call["argv"]
    assert argv[:2] == ["codex", "exec"] and argv[-1] == "-"
    for flag in ("--ephemeral", "--ignore-user-config", "--skip-git-repo-check", "--json"):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("-m") + 1] == "gpt-5.6-luna"
    assert 'model_reasoning_effort="low"' in argv
    assert not any("SECRET PAGE TEXT" in a or "capital of France" in a for a in argv)
    assert "SECRET PAGE TEXT" in call["input"] and '"f1"' in call["input"]
    assert call["schema"]["properties"]["values"]["required"] == ["f1"] and call["schema_mode"] == 0o600
    env = call["kwargs"]["env"]
    assert "OPENAI_API_KEY" not in env and "TYPESAFE_API_KEY" not in env
    assert call["kwargs"]["start_new_session"] is True
    cwd = Path(call["kwargs"]["cwd"])
    assert not list(cwd.glob("codex-*")), "schema and output files are removed after the call"


def test_model_and_effort_come_from_config(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_CODEX_MODEL", "gpt-5.6")
    monkeypatch.setenv("JEV_BROWSE_CODEX_REASONING_EFFORT", "medium")
    popen = CodexProc('{"values": {"f1": null}}')
    b = make_backend("codex")
    assert isinstance(b, CodexSubscriptionBackend) and b.model == "gpt-5.6"
    b._popen = popen
    b.batch("g", [fld("x")], "", None)
    argv = CodexProc.calls[0]["argv"]
    assert argv[argv.index("-m") + 1] == "gpt-5.6" and 'model_reasoning_effort="medium"' in argv


def test_invalid_output_is_retried_once_then_an_error():
    popen = CodexProc("not json at all")
    r = CodexSubscriptionBackend(popen=popen).batch("g", [fld("x")], "", None)
    assert r.values == {} and "invalid" in r.error and len(CodexProc.calls) == 2


def test_no_output_file_is_an_error_not_a_crash():
    r = CodexSubscriptionBackend(popen=CodexProc("", returncode=1, write=False)).batch("g", [fld("x")], "", None)
    assert r.values == {} and "no answer" in r.error


def test_missing_cli_is_reported():
    def popen(argv, **kw):
        raise FileNotFoundError("codex")

    r = CodexSubscriptionBackend(popen=popen).batch("g", [fld("x")], "", None)
    assert r.error == "codex CLI not found on PATH"


def test_deadline_kills_the_process():
    import time

    r = CodexSubscriptionBackend(popen=CodexProc("", hang=True)).batch("g", [fld("x")], "", time.monotonic() + 0.5)
    assert r.values == {} and "deadline" in r.error


def test_codex_can_be_the_fallback_for_a_local_backend(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "ollama")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("JEV_BROWSE_TEXT_FALLBACK", "codex")
    b = make_backend(None)
    assert isinstance(b, FallbackBackend) and isinstance(b.fallback, CodexSubscriptionBackend)
