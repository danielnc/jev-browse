"""`jev-browse <command>` / `python3 -m jev_browse <command>`: dispatch, `config`, and the `mcp` import guard."""

import importlib.abc
import runpy
import sys
import types

import pytest

from jev_browse import __main__ as cli
from jev_browse import doctor, install


@pytest.fixture
def no_harness_env(monkeypatch, tmp_path):
    """Point the harness workspace at an empty dir, so `config` never loads the developer's harness .env."""
    monkeypatch.setenv("BH_AGENT_WORKSPACE", str(tmp_path / "ws"))


def recorder(monkeypatch, module, rc=0):
    calls = []

    def fake(argv):
        calls.append(list(argv))
        return rc
    monkeypatch.setattr(module, "main", fake)
    return calls


@pytest.mark.parametrize("argv", [[], ["-h"], ["--help"], ["help"]])
def test_help_lists_every_command(argv, capsys):
    assert cli.main(argv) == 0
    listed = {line.split()[0] for line in capsys.readouterr().out.splitlines()[1:] if line.strip()}
    assert {"install", "uninstall", "doctor", "config", "mcp"} <= listed


def test_unknown_command_prints_usage_to_stderr(capsys):
    assert cli.main(["instal"]) == 2
    captured = capsys.readouterr()
    assert "unknown command 'instal'" in captured.err and "uninstall" in captured.err and not captured.out


def test_install_and_doctor_pass_their_arguments_through(monkeypatch):
    installs = recorder(monkeypatch, install)
    doctors = recorder(monkeypatch, doctor, rc=1)
    assert cli.main(["install", "--agent", "codex"]) == 0
    assert cli.main(["doctor", "--offline"]) == 1
    assert installs == [["--agent", "codex"]] and doctors == [["--offline"]]


def test_uninstall_removes_the_block_then_the_skill_links(monkeypatch):
    calls = recorder(monkeypatch, install)
    assert cli.main(["uninstall", "--agent", "claude"]) == 0
    assert calls == [["--uninstall", "--agent", "claude"], ["--unlink-skill", "--agent", "claude"]]


def test_uninstall_stops_when_removing_the_block_fails(monkeypatch):
    calls = recorder(monkeypatch, install, rc=3)
    assert cli.main(["uninstall"]) == 3
    assert calls == [["--uninstall"]]


def test_config_lists_every_setting_with_its_source(monkeypatch, capsys, no_harness_env):
    monkeypatch.setenv("JEV_BROWSE_MAX_ACTIONS", "12")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_URL", "http://127.0.0.1:11434")
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    lines = {line.split()[0]: line for line in out.splitlines()[1:]}
    assert out.startswith("config file: ")
    assert "12" in lines["run.max_actions"] and "(env; JEV_BROWSE_MAX_ACTIONS)" in lines["run.max_actions"]
    assert "(default; JEV_BROWSE_TIMEOUT_S)" in lines["run.timeout_s"]
    # a private value (a local server URL) is shown only as set
    assert "'<set>'" in lines["ollama.url"] and "127.0.0.1" not in out
    assert "problem:" not in out


def test_config_reports_an_invalid_value(monkeypatch, capsys, no_harness_env):
    monkeypatch.setenv("JEV_BROWSE_MAX_ACTIONS", "lots")
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "problem: run.max_actions: JEV_BROWSE_MAX_ACTIONS='lots'" in out


def test_config_markdown_prints_the_docs_table(capsys):
    assert cli.main(["config", "--markdown"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("|") and "`run.max_actions`" in out and "`JEV_BROWSE_MAX_ACTIONS`" in out


def test_mcp_runs_the_server_with_the_remaining_arguments(monkeypatch):
    seen = []
    fake = types.ModuleType("jev_browse.mcp_server")
    fake.main = lambda argv: seen.append(argv) or 0
    monkeypatch.setitem(sys.modules, "jev_browse.mcp_server", fake)
    assert cli.main(["mcp", "--flag"]) == 0
    assert seen == [["--flag"]]


class _MissingDependency(importlib.abc.MetaPathFinder):
    """Makes `import jev_browse.mcp_server` fail as if one of its dependencies were not installed."""

    def __init__(self, missing):
        self.missing = missing

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "jev_browse.mcp_server":
            raise ModuleNotFoundError(f"No module named {self.missing!r}", name=self.missing)
        return None


@pytest.mark.parametrize("missing", ["mcp", "mcp.server.fastmcp", "pydantic", "anyio"])
def test_mcp_without_the_extra_explains_how_to_install_it(monkeypatch, capsys, missing):
    monkeypatch.delitem(sys.modules, "jev_browse.mcp_server", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_MissingDependency(missing), *sys.meta_path])
    assert cli.main(["mcp"]) == 1
    assert capsys.readouterr().err.strip() == cli.MCP_HINT


def test_mcp_does_not_hide_an_unrelated_missing_module(monkeypatch):
    monkeypatch.delitem(sys.modules, "jev_browse.mcp_server", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_MissingDependency("yaml"), *sys.meta_path])
    with pytest.raises(ModuleNotFoundError, match="yaml"):
        cli.main(["mcp"])


def test_python_dash_m_exits_with_the_command_status(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["jev_browse", "nope"])
    monkeypatch.delitem(sys.modules, "jev_browse.__main__")  # run it fresh, as `python3 -m` does
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("jev_browse", run_name="__main__", alter_sys=True)
    assert exc.value.code == 2 and "unknown command 'nope'" in capsys.readouterr().err
