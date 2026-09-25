"""harness_api: the only browser-harness touchpoint. Each call goes to the harness (faked here as a stub
`browser_harness` package) or to an installed test double; tmp_dir() mirrors the harness outside it."""

import sys
import types
from pathlib import Path

import pytest

from jev_browse import harness_api


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """A stub `browser_harness` package with the private names harness_api pins, and no test double."""
    calls = []
    helpers = types.ModuleType("browser_harness.helpers")

    def cdp(method, session_id=None, _response_timeout=5.0, **params):
        calls.append((method, session_id, _response_timeout, params))
        return {"ok": method}

    helpers.cdp = cdp
    helpers.current_tab = lambda: {"targetId": "T-current"}
    helpers._send = lambda msg: {"dialog": {"type": "alert"}} if msg == {"meta": "pending_dialog"} else {}
    helpers.NAME = "bu-test"
    ipc = types.ModuleType("browser_harness._ipc")
    ipc._RUNTIME = str(tmp_path / "run")
    paths = types.ModuleType("browser_harness.paths")
    paths.tmp_dir = lambda: str(tmp_path / "harness-tmp")
    pkg = types.ModuleType("browser_harness")
    pkg.helpers, pkg._ipc, pkg.paths = helpers, ipc, paths
    for name, module in {"browser_harness": pkg, "browser_harness.helpers": helpers,
                         "browser_harness._ipc": ipc, "browser_harness.paths": paths}.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(harness_api, "_fake", None)
    return types.SimpleNamespace(calls=calls, helpers=helpers, tmp_path=tmp_path)


def test_calls_go_to_the_harness_helpers(harness):
    assert harness_api.cdp("Target.getTargets", session_id="S1", _response_timeout=2.0, filter=[]) == \
        {"ok": "Target.getTargets"}
    assert harness.calls == [("Target.getTargets", "S1", 2.0, {"filter": []})]
    assert harness_api.current_tab() == {"targetId": "T-current"}
    assert harness_api.pending_dialog() == {"type": "alert"}
    assert harness_api.daemon_name() == "bu-test"
    assert harness_api.socket_dir() == harness.tmp_path / "run"
    assert harness_api.tmp_dir() == harness.tmp_path / "harness-tmp"


def test_no_pending_dialog_is_none(harness):
    harness.helpers._send = lambda msg: {}
    assert harness_api.pending_dialog() is None


def test_an_installed_fake_replaces_the_harness(harness):
    class Fake:
        def __call__(self, method, **kw):
            return {"fake": method, **kw}

        def current_tab(self):
            return "T-fake"

        def pending_dialog(self):
            return None

    try:
        harness_api.install_fake(Fake())
        assert harness_api.cdp("Page.enable", session_id="S2") == \
            {"fake": "Page.enable", "session_id": "S2", "_response_timeout": 5.0}
        assert harness_api.current_tab() == "T-fake" and harness_api.pending_dialog() is None
        # attributes the double does not define fall back to safe defaults
        assert harness_api.socket_dir() == Path("/tmp") and harness_api.tmp_dir() == Path("/tmp")
        assert harness_api.daemon_name() == "default"
    finally:
        harness_api.install_fake(None)
    assert harness.calls == []
    assert harness_api.current_tab() == {"targetId": "T-current"}


@pytest.fixture
def outside_harness(monkeypatch, tmp_path):
    """No browser_harness importable (as in bench/text_eval.py), no harness env, HOME in tmp."""
    monkeypatch.setitem(sys.modules, "browser_harness", None)
    monkeypatch.setattr(harness_api, "_fake", None)
    for name in ("BH_TMP_DIR", "BH_HOME", "BROWSER_HARNESS_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.mark.parametrize("env, expected", [
    ({}, "home/.config/browser-harness/tmp"),
    ({"XDG_CONFIG_HOME": "{t}/xdg"}, "xdg/browser-harness/tmp"),
    ({"BROWSER_HARNESS_HOME": "{t}/bhh", "XDG_CONFIG_HOME": "{t}/xdg"}, "bhh/tmp"),
    ({"BH_HOME": "{t}/bh", "BROWSER_HARNESS_HOME": "{t}/bhh"}, "bh/tmp"),
    ({"BH_TMP_DIR": "{t}/explicit", "BH_HOME": "{t}/bh"}, "explicit"),
])
def test_tmp_dir_outside_the_harness_resolves_as_the_harness_does(monkeypatch, outside_harness, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value.format(t=outside_harness))
    assert harness_api.tmp_dir() == outside_harness / expected


def test_error_classification():
    assert harness_api.is_ipc_timeout(TimeoutError()) and not harness_api.is_ipc_timeout(OSError())
    for exc in (FileNotFoundError(), ConnectionRefusedError(), ConnectionResetError(), BrokenPipeError()):
        assert harness_api.is_unreachable(exc)
    assert not harness_api.is_unreachable(TimeoutError()) and not harness_api.is_unreachable(ValueError())
