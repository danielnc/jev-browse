"""The MCP server: tool schemas, result text, and round trips through a fake browser-harness CLI
(tests/fake_harness.py) with an in-process MCP client. No Chrome, TypeSafe, or network."""

import json
import os
import shlex
import stat
import sys
from pathlib import Path

import anyio
import pytest

from jev_browse import config, mcp_bridge
from jev_browse import run as runmod
from jev_browse.results import Reason, RunResult

mcp = pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

from jev_browse.mcp_server import SERVER  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_harness.py"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Point the bridge at the fake harness, with its own tmp dir and a call log."""
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("BH_TMP_DIR", str(tmp_path / "bh-tmp"))
    monkeypatch.setenv("JEV_BROWSE_HARNESS_COMMAND", f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))}")
    monkeypatch.setenv("JEV_BROWSE_MCP_WAIT_S", "10")
    monkeypatch.setenv("FAKE_HARNESS_CALLS", str(calls))
    monkeypatch.setenv("FAKE_HARNESS_MODE", "done")
    monkeypatch.delenv("BH_TELEMETRY", raising=False)
    monkeypatch.setattr(mcp_bridge, "_OWNER", None)
    monkeypatch.setattr(mcp_bridge, "_RUNS", {})
    config.reset_cache()

    def received():
        return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

    yield received
    for handle in mcp_bridge._RUNS.values():
        handle.proc.wait(timeout=10)


def call(name, args):
    async def go():
        async with Client(SERVER) as client:
            return await client.call_tool(name, args)

    result = anyio.run(go)
    return result.content[0].text, result.is_error


# ------------------------------------------------------------------------------------------------ schemas
def schemas():
    async def go():
        return {t.name: t for t in await SERVER.list_tools()}

    return anyio.run(go)


def test_tool_names_and_descriptions():
    tools = schemas()
    assert set(tools) == {"fast_run", "fast_run_status", "jev_open", "jev_find", "jev_click", "jev_check",
                          "jev_close", "doctor"}
    for tool in tools.values():
        assert tool.description and len(tool.description) > 40
        assert tool.output_schema is None  # plain text results, no duplicated structured copy


def test_fast_run_schema():
    s = schemas()["fast_run"].input_schema
    assert s["required"] == ["goal"]
    props = s["properties"]
    assert set(props) == {"goal", "url", "values", "run_id", "timeout_s", "target_id", "confirm", "text_backend"}
    assert "ctx" not in props
    assert {"type": "null"} in props["url"]["anyOf"]
    kinds = {a.get("type") for a in props["values"]["anyOf"]}
    assert kinds == {"object", "array", "null"}
    backends = next(a for a in props["text_backend"]["anyOf"] if a.get("enum"))["enum"]
    assert set(backends) == {"claude", "codex", "ollama", "openai", "none"}
    assert props["timeout_s"]["anyOf"][0]["minimum"] == 1


def test_interactive_schemas():
    tools = schemas()
    assert tools["fast_run_status"].input_schema["required"] == ["run_id"]
    assert tools["jev_open"].input_schema["required"] == ["url"]
    assert tools["jev_find"].input_schema["required"] == ["description", "target_id"]
    assert tools["jev_click"].input_schema["required"] == ["ref"]
    assert tools["jev_click"].input_schema["properties"]["confirm"]["default"] is False
    assert tools["jev_check"].input_schema["required"] == ["condition", "target_id"]
    assert "required" not in tools["jev_close"].input_schema
    assert "required" not in tools["doctor"].input_schema


# ------------------------------------------------------------------------------------------------ the script
def test_script_carries_arguments_as_data_not_code():
    goal = 'x"); import os; os.system("echo pwned'
    code = mcp_bridge.script("fast_run", ("https://example.com", goal), {"values": {"a": "b'c"}})
    seen = {}

    def fast_run(url, g, **kw):
        seen.update(url=url, goal=g, **kw)
        return RunResult(status="claimed_done", run_id="r")

    printed = []
    exec(code, {"fast_run": fast_run, "print": lambda s, **k: printed.append(s)})
    assert seen == {"url": "https://example.com", "goal": goal, "values": {"a": "b'c"}}
    payload = mcp_bridge.parse(printed[-1])
    assert payload["kind"] == "RunResult" and payload["status"] == "claimed_done"


def test_script_reports_missing_helpers_and_exceptions():
    printed = []
    exec(mcp_bridge.script("jev_open", ("https://example.com",)), {"print": lambda s, **k: printed.append(s)})
    assert mcp_bridge.parse(printed[-1])["error"] == "not_installed"

    def boom(url):
        raise RuntimeError("jev-browse disabled (JEV_BROWSE_DISABLE=1)")

    exec(mcp_bridge.script("jev_open", ("https://example.com",)),
         {"jev_open": boom, "print": lambda s, **k: printed.append(s)})
    out = mcp_bridge.parse(printed[-1])
    assert out == {"kind": "error", "error": "RuntimeError", "detail": "jev-browse disabled (JEV_BROWSE_DISABLE=1)"}


def test_unknown_call_refused():
    with pytest.raises(ValueError):
        mcp_bridge.script("switch_tab", ())


# ------------------------------------------------------------------------------------------------ round trips
def test_fast_run_round_trip_claimed_done(harness):
    text, is_error = call("fast_run", {"url": "https://example.com/form", "goal": "Send a billing question",
                                       "values": {"Topic": "Billing"}, "run_id": "mcp-rt-1", "timeout_s": 30,
                                       "text_backend": "none"})
    assert not is_error
    assert text.startswith("fast_run: claimed_done")
    assert "not proof" in text and "run_id: mcp-rt-1 · target_id: T1" in text
    assert '"Billing"' in text and "<redacted>" in text and "(matches requested)" in text
    assert "text: Thanks! Your message was sent. We reply within a day." in text
    assert "2 decisions · 4.2 s · 3 Jev requests" in text
    [c] = harness()
    assert c["call"] == "fast_run"
    assert c["args"] == ["https://example.com/form", "Send a billing question"]
    assert c["kwargs"] == {"values": {"Topic": "Billing"}, "timeout_s": 30, "text_backend": "none",
                           "run_id": "mcp-rt-1"}
    assert c["owner"].startswith("mcp-")


def test_spawned_scripts_run_with_harness_telemetry_off(harness, monkeypatch):
    monkeypatch.setenv("BH_TELEMETRY", "1")
    env = mcp_bridge.child_env()
    assert env["BH_TELEMETRY"] == "0"
    assert os.environ["BH_TELEMETRY"] == "1"  # the server's own environment is untouched


def test_numeric_values_reach_fast_run_as_text(harness):
    call("fast_run", {"url": "https://example.com/form", "goal": "Book for 2", "values": {"Guests": 2}})
    assert harness()[-1]["kwargs"]["values"] == {"Guests": "2"}


def test_reused_run_id_never_returns_the_previous_runs_result(harness, monkeypatch):
    call("fast_run", {"url": "https://example.com/form", "goal": "g", "run_id": "mcp-rt-reuse"})
    data = json.loads(runmod.run_path("mcp-rt-reuse").read_text())
    data["started_at"] -= 3600  # the earlier run's file is still there
    runmod._write_json(runmod.run_path("mcp-rt-reuse"), data)
    monkeypatch.setenv("FAKE_HARNESS_MODE", "harness_error")
    text, is_error = call("fast_run", {"url": "https://example.com/form", "goal": "g", "run_id": "mcp-rt-reuse"})
    assert is_error and "remote debugging is not enabled" in text


def test_fast_run_log_is_private(harness):
    call("fast_run", {"url": "https://example.com/form", "goal": "g", "run_id": "mcp-rt-log"})
    log = mcp_bridge.log_path("mcp-rt-log")
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert "JEV_BROWSE_RUN=" in log.read_text()


def test_confirm_required_round_trip_then_confirmed_resume(harness, monkeypatch):
    monkeypatch.setenv("FAKE_HARNESS_MODE", "confirm")
    text, is_error = call("fast_run", {"url": "https://example.com/form", "goal": "Draft a message",
                                       "run_id": "mcp-rt-2"})
    assert not is_error
    assert text.startswith("fast_run: handed_back (confirm_required)")
    assert "Do NOT confirm unless the user's request clearly covers" in text
    ref_line = next(line for line in text.splitlines() if line.startswith("confirm ref: "))
    ref = json.loads(ref_line.removeprefix("confirm ref: "))
    assert ref == {"target_id": "T1", "node": 12, "doc": "1000.5|https://example.com/form", "label": "Send",
                   "ctx": "c12"}
    assert 'target_id="T1"' in text  # the next: line names the tab to resume on

    monkeypatch.setenv("FAKE_HARNESS_MODE", "done")
    text, _ = call("fast_run", {"url": None, "goal": "Draft a message", "target_id": "T1", "confirm": [ref],
                                "run_id": "mcp-rt-3"})
    assert text.startswith("fast_run: claimed_done")
    resumed = harness()[-1]
    assert resumed["args"] == [None, "Draft a message"]
    assert resumed["kwargs"]["confirm"] == [ref] and resumed["kwargs"]["target_id"] == "T1"


def test_text_value_unavailable_lists_fields_to_resume_with(harness, monkeypatch):
    monkeypatch.setenv("FAKE_HARNESS_MODE", "values")
    text, _ = call("fast_run", {"url": "https://example.com/form", "goal": "Track my order"})
    assert "(text_value_unavailable)" in text
    assert "fields needing values:\n  - Order number [key: Order number] (tried: Billing)" in text
    assert "values={" in text


def test_long_run_returns_running_run_id_then_status_collects_it(harness, monkeypatch):
    monkeypatch.setenv("FAKE_HARNESS_MODE", "slow")
    monkeypatch.setenv("FAKE_HARNESS_SLEEP", "3")
    monkeypatch.setenv("JEV_BROWSE_MCP_WAIT_S", "1")
    config.reset_cache()
    text, is_error = call("fast_run", {"url": "https://example.com/form", "goal": "g", "run_id": "mcp-rt-slow"})
    assert not is_error
    assert text.startswith("fast_run: running (run_id mcp-rt-slow")
    assert 'fast_run_status(run_id="mcp-rt-slow")' in text

    text, is_error = call("fast_run", {"url": "https://example.com/form", "goal": "g", "run_id": "mcp-rt-slow"})
    assert is_error and "still running" in text  # never a second run (or a truncated log) under a live run_id

    monkeypatch.setenv("JEV_BROWSE_MCP_WAIT_S", "15")
    config.reset_cache()
    text, _ = call("fast_run_status", {"run_id": "mcp-rt-slow"})
    assert text.startswith("fast_run: claimed_done")


def test_status_after_a_server_restart_reads_the_run_file(harness, monkeypatch):
    call("fast_run", {"url": "https://example.com/form", "goal": "g", "run_id": "mcp-rt-restart"})
    monkeypatch.setattr(mcp_bridge, "_RUNS", {})
    mcp_bridge.log_path("mcp-rt-restart").unlink()
    text, _ = call("fast_run_status", {"run_id": "mcp-rt-restart"})
    assert text.startswith("fast_run: claimed_done") and "run_id: mcp-rt-restart" in text


def test_status_of_unknown_run_is_a_tool_error(harness):
    text, is_error = call("fast_run_status", {"run_id": "never-started"})
    assert is_error and "no fast_run with run_id" in text


def test_argument_errors_are_tool_errors_before_any_spawn(harness):
    text, is_error = call("fast_run", {"goal": "g"})
    assert is_error and "pass a url" in text
    text, is_error = call("fast_run", {"url": "https://example.com", "goal": "g", "run_id": "bad id!"})
    assert is_error and "run_id must match" in text
    assert harness() == []


def test_helper_exception_becomes_tool_error(harness, monkeypatch):
    monkeypatch.setenv("FAKE_HARNESS_MODE", "raise")
    text, is_error = call("fast_run", {"url": "https://example.com", "goal": "g"})
    assert is_error and "jev-browse error (ValueError): run_id 'x' belongs to a live run" in text


def test_not_installed_and_harness_failures_are_tool_errors(harness, monkeypatch):
    monkeypatch.setenv("FAKE_HARNESS_MODE", "not_installed")
    text, is_error = call("jev_open", {"url": "https://example.com"})
    assert is_error and "python3 -m jev_browse install" in text
    monkeypatch.setenv("FAKE_HARNESS_MODE", "harness_error")
    text, is_error = call("fast_run", {"url": "https://example.com", "goal": "g"})
    assert is_error and "remote debugging is not enabled" in text


def test_missing_harness_command_is_a_tool_error(harness, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_HARNESS_COMMAND", "definitely-not-a-browser-harness")
    config.reset_cache()
    text, is_error = call("jev_open", {"url": "https://example.com"})
    assert is_error and "JEV_BROWSE_HARNESS_COMMAND" in text


def test_interactive_round_trip(harness):
    text, _ = call("jev_open", {"url": "https://example.com"})
    assert text.startswith("jev_open: opened target_id T9: Example Domain — https://example.com")

    text, _ = call("jev_find", {"description": "the Search button", "target_id": "T9"})
    assert text.startswith('jev_find: found "Search" (button) on T9')
    ref = json.loads(next(ln for ln in text.splitlines() if ln.startswith("ref: ")).removeprefix("ref: "))
    assert ref == {"target_id": "T9", "node": 7, "doc": "1000.5|https://example.com/form", "label": "Search",
                   "ctx": "c7"}

    text, _ = call("jev_click", {"ref": ref})
    assert text == "jev_click: clicked at (10, 20); url after: https://example.com/form?q=1"
    text, _ = call("jev_click", {"ref": json.dumps({**ref, "label": "Send"})})
    assert text.startswith("jev_click: not clicked (confirm_required)") and "confirm ref: " in text

    text, _ = call("jev_check", {"condition": "results are shown", "target_id": "T9"})
    assert text.startswith("jev_check: holds=yes (probability 0.91)") and "Results for Lisbon" in text
    text, _ = call("jev_check", {"condition": "x", "target_id": "gone"})
    assert text.startswith("jev_check: handed_back (not_owned_tab)") and "jev_open(url)" in text

    text, _ = call("jev_close", {"target_id": "T9"})
    assert text == "jev_close: closed 1 tab(s)"
    text, is_error = call("jev_close", {"target_id": "not-ours"})
    assert is_error and "refusing to close it" in text
    text, is_error = call("jev_close", {})
    assert is_error and "pass target_id" in text

    sent = harness()
    assert [c["call"] for c in sent] == ["jev_open", "jev_find", "jev_click", "jev_click", "jev_check",
                                         "jev_check", "jev_close", "jev_close"]
    assert sent[1]["kwargs"] == {"k": 3, "scroll": True, "target_id": "T9"}
    assert sent[2]["args"] == [ref] and sent[2]["kwargs"]["confirm"] is False
    assert len({c["owner"] for c in sent}) == 1  # one tab owner per server process


def test_doctor_tool_offline(harness, monkeypatch, tmp_path):
    from jev_browse import doctor

    monkeypatch.setenv("BH_AGENT_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setattr(doctor, "check_harness", lambda: [doctor.Check(doctor.OK, "browser-harness", "fake")])
    monkeypatch.setattr(doctor, "check_text_backend", lambda *a: [doctor.Check(doctor.INFO, "text backend", "fake")])
    text, is_error = call("doctor", {"offline": True})
    assert not is_error
    assert "TYPESAFE_API_KEY" in text and "MCP server" in text and "mcp harness command" in text
    assert "wait 10 s per call" in text


# ------------------------------------------------------------------------------------------------ text
def run_dict(**kw):
    base = {"status": "handed_back", "run_id": "r1", "target_id": "T1", "url": "https://example.com/",
            "title": "Example", "evidence": {}, "trace": [], "stats": {}, "data": {}, "detail": ""}
    return {**base, **kw}


@pytest.mark.parametrize("reason", list(Reason))
def test_every_reason_gets_a_next_line(reason):
    text = mcp_bridge.format_run(run_dict(reason=reason.value))
    nxt = next(line for line in text.splitlines() if line.startswith("next: "))
    assert len(nxt) > 30 and "See the reason above" not in nxt


def test_surfaces_and_popup_and_dialog_are_named():
    text = mcp_bridge.format_run(run_dict(reason="in_frame", evidence={"surfaces": {
        "frames": [{"relevant": True}], "canvas_relevant": False}}))
    assert "page has: a large iframe" in text
    text = mcp_bridge.format_run(run_dict(reason="popup_tab", data={"target_id": "T7"}))
    assert "popup target_id: T7" in text
    text = mcp_bridge.format_run(run_dict(reason="dialog_open", data={"dialog": {"type": "confirm",
                                                                                 "message": "Leave?"}}))
    assert 'dialog: confirm "Leave?"' in text


def test_running_text_counts_steps_from_the_run_file():
    st = {"run": {"target_id": "T3", "result": {"trace": [{}, {}, {}]}}, "elapsed_s": 12.4}
    text = mcp_bridge.format_running("r9", st)
    assert text.startswith("fast_run: running (run_id r9, 12 s so far, 3 steps, target_id T3)")


def test_run_files_and_logs_share_the_harness_tmp_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BH_TMP_DIR", str(tmp_path))
    assert mcp_bridge.log_path("abc").parent == runmod.run_path("abc").parent == tmp_path


def test_mcp_subcommand_explains_the_missing_extra(monkeypatch, capsys):
    from jev_browse import __main__

    for name in [n for n in sys.modules if n == "mcp" or n.startswith("mcp.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, "jev_browse.mcp_server")
    assert __main__.main(["mcp"]) == 1
    assert "--extra mcp" in capsys.readouterr().err


def test_stdio_entry_point_round_trip(harness, tmp_path):
    """`python -m jev_browse mcp` over real stdio: the handshake, tools/list, and one tool call through the fake
    harness. Any stray print to stdout would corrupt the JSON-RPC stream and fail this."""
    from mcp import StdioServerParameters

    env = {k: v for k, v in os.environ.items() if k.startswith(("JEV_BROWSE_", "BH_", "FAKE_HARNESS_"))}
    env.update(BH_AGENT_WORKSPACE=str(tmp_path / "ws"), PYTHONPATH=str(ROOT))
    params = StdioServerParameters(command=sys.executable, args=["-m", "jev_browse", "mcp"], env=env, cwd=str(ROOT))

    async def go():
        async with Client(params) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            result = await client.call_tool("jev_open", {"url": "https://example.com"})
            return names, result

    names, result = anyio.run(go)
    assert "fast_run" in names and "doctor" in names
    assert not result.is_error and result.content[0].text.startswith("jev_open: opened target_id T9")
    [sent] = harness()
    assert sent["owner"].startswith("mcp-")
