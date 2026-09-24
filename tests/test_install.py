import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from jev_browse import harness, install

ROOT = Path(__file__).resolve().parents[1]


def test_workspace_resolution_mirrors_harness():
    assert install.workspace_dir({"BH_AGENT_WORKSPACE": "/w", "BH_HOME": "/h"}) == Path("/w")
    assert install.workspace_dir({"BH_HOME": "/h", "XDG_CONFIG_HOME": "/x"}) == Path("/h/agent-workspace")
    assert install.workspace_dir({"BROWSER_HARNESS_HOME": "/b"}) == Path("/b/agent-workspace")
    assert install.workspace_dir({"XDG_CONFIG_HOME": "/x", "HOME": "/u"}) == Path("/x/browser-harness/agent-workspace")
    assert install.workspace_dir({"HOME": "/u"}) == Path("/u/.config/browser-harness/agent-workspace")


def run_main(args, tmp_path, telemetry=False):
    out = []

    def fake_run(argv, **kw):
        return types.SimpleNamespace(stdout=json.dumps({"enabled": telemetry}), returncode=0)
    rc = install.main(["--workspace", str(tmp_path / "ws"), "--skills-dir", str(tmp_path / "skills"), *args],
                      run=fake_run, out=out.append)
    return rc, "\n".join(out)


def test_install_writes_block_and_symlink(tmp_path):
    run_main([], tmp_path)
    helpers = (tmp_path / "ws" / "agent_helpers.py").read_text()
    assert install.BEGIN in helpers and install.END in helpers and repr(str(install.CHECKOUT)) in helpers
    link = tmp_path / "skills" / "jev-browse"
    assert link.is_symlink() and (link / "SKILL.md").exists()


def test_install_idempotent(tmp_path):
    run_main([], tmp_path)
    first = (tmp_path / "ws" / "agent_helpers.py").read_text()
    _, out = run_main([], tmp_path)
    assert (tmp_path / "ws" / "agent_helpers.py").read_text() == first
    assert "already current" in out and "already linked" in out


def test_install_warns_when_telemetry_enabled(tmp_path):
    _, out = run_main([], tmp_path, telemetry=True)
    assert "telemetry is ENABLED" in out and "browser-harness telemetry disable" in out and "PostHog" in out


def test_install_silent_when_telemetry_disabled(tmp_path):
    _, out = run_main([], tmp_path, telemetry=False)
    assert "telemetry" not in out.lower()


def test_install_refuses_conflicting_skill_path(tmp_path):
    (tmp_path / "skills" / "jev-browse").mkdir(parents=True)
    (tmp_path / "skills" / "jev-browse" / "SKILL.md").write_text("someone else's")
    with pytest.raises(SystemExit, match="refusing"):
        run_main([], tmp_path)
    assert (tmp_path / "skills" / "jev-browse" / "SKILL.md").read_text() == "someone else's"


def test_install_preserves_user_helpers(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "agent_helpers.py").write_text("def my_helper():\n    return 42\n")
    run_main([], tmp_path)
    text = (ws / "agent_helpers.py").read_text()
    assert text.startswith("def my_helper():\n    return 42\n") and install.BEGIN in text


def test_uninstall_removes_only_block(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    user = "def my_helper():\n    return 42\n"
    (ws / "agent_helpers.py").write_text(user)
    run_main([], tmp_path)
    run_main(["--uninstall"], tmp_path)
    assert (ws / "agent_helpers.py").read_text() == user
    run_main(["--uninstall"], tmp_path)
    assert (ws / "agent_helpers.py").read_text() == user


def test_unlink_skill_removes_only_own_link(tmp_path):
    run_main([], tmp_path)
    run_main(["--unlink-skill"], tmp_path)
    assert not (tmp_path / "skills" / "jev-browse").exists()
    other = tmp_path / "elsewhere"
    other.mkdir()
    (tmp_path / "skills" / "jev-browse").symlink_to(other)
    with pytest.raises(SystemExit, match="refusing"):
        run_main(["--unlink-skill"], tmp_path)


def exec_block(block, extra_modules=None, env=None, monkeypatch=None):
    ns = {"__name__": "agent_helpers_test"}
    if env and monkeypatch:
        for k, v in env.items():
            monkeypatch.setenv(k, v)
    exec(compile(block, "agent_helpers.py", "exec"), ns)
    return ns


@pytest.fixture
def fake_harness_module(monkeypatch):
    calls = []

    def new_tab(url="about:blank"):
        calls.append(url)
        return "T-new"
    helpers = types.SimpleNamespace(new_tab=new_tab)
    pkg = types.ModuleType("browser_harness")
    pkg.helpers = helpers
    monkeypatch.setitem(sys.modules, "browser_harness", pkg)
    monkeypatch.setitem(sys.modules, "browser_harness.helpers", helpers)
    return helpers, calls


def test_block_import_failure_defines_stubs(monkeypatch, fake_harness_module):
    helpers, _ = fake_harness_module
    monkeypatch.delenv("JEV_BROWSE_DISABLE", raising=False)
    block = install.render_block("/definitely/not/here").replace(
        "from jev_browse.harness import fast_run", "from jev_browse_missing.harness import fast_run")
    ns = exec_block(block)
    for name in harness.PUBLIC:
        assert callable(ns[name])
    with pytest.raises(RuntimeError, match="jev-browse failed to import"):
        ns["jev_find"]("x")
    assert "new_tab" not in ns


def test_block_disable_env_defines_disabled_stubs(monkeypatch, fake_harness_module):
    monkeypatch.setenv("JEV_BROWSE_DISABLE", "1")
    ns = exec_block(install.render_block())
    with pytest.raises(RuntimeError, match="jev-browse disabled"):
        ns["fast_run"]("u", "g")
    assert "new_tab" not in ns


def test_block_exports_only_public_api(monkeypatch, fake_harness_module):
    monkeypatch.delenv("JEV_BROWSE_DISABLE", raising=False)
    ns = exec_block(install.render_block())
    public = {k for k in ns if not k.startswith("_")}
    assert public == set(harness.PUBLIC) | {"new_tab"}


def test_block_appends_sys_path(monkeypatch, fake_harness_module):
    monkeypatch.delenv("JEV_BROWSE_DISABLE", raising=False)
    fake = "/tmp/jev-browse-fake-checkout"
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != fake])
    exec_block(install.render_block(fake))
    assert sys.path[-1] == fake and sys.path[0] != fake


def test_block_has_no_hardcoded_home():
    template = install.BLOCK_TEMPLATE
    assert "__JB_PATH__" in template and "/Users/" not in template and "/home/" not in template
    for path in [ROOT / "jev_browse/install.py", ROOT / "jev_browse/harness.py"]:
        assert "/Users/" not in path.read_text()


def test_new_tab_recorder_is_transparent(monkeypatch):
    recorded = []
    monkeypatch.setattr("jev_browse.tab.live_targets", lambda: [{"targetId": "OLD"}])

    class Reg:
        def record(self, tid):
            recorded.append(tid)
    monkeypatch.setattr("jev_browse.tab.default_registry", lambda kind="owned": Reg())

    def original(url="about:blank"):
        return "T-new"
    wrapped = harness.wrap_new_tab(original)
    assert wrapped("https://x/") == "T-new" and recorded == ["T-new"]

    def boom(url="about:blank"):
        raise KeyError("original failure")
    with pytest.raises(KeyError, match="original failure"):
        harness.wrap_new_tab(boom)("x")

    class Busy:
        def record(self, tid):
            raise BlockingIOError("busy")
    monkeypatch.setattr("jev_browse.tab.default_registry", lambda kind="owned": Busy())
    assert wrapped("https://x/") == "T-new"
    monkeypatch.setattr("jev_browse.tab.live_targets", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert wrapped("https://x/") == "T-new"
    assert harness.wrap_new_tab(wrapped).__jev_browse_original__ is original  # never double-wrapped


def test_new_tab_recorder_skips_reused_tab(monkeypatch):
    recorded = []
    monkeypatch.setattr("jev_browse.tab.live_targets", lambda: [{"targetId": "BLANK"}])

    class Reg:
        def record(self, tid):
            recorded.append(tid)
    monkeypatch.setattr("jev_browse.tab.default_registry", lambda kind="owned": Reg())
    assert harness.wrap_new_tab(lambda url="about:blank": "BLANK")("https://x/") == "BLANK"
    assert recorded == []


def test_skill_files_exist_and_core_rules_verbatim():
    core = (ROOT / "skill/SKILL.md").read_text()
    assert core.startswith("---\nname: jev-browse\n")
    assert len(core.splitlines()) <= 70
    for rule in ["Never handle a `confirm_required` in the same script that received it",
                 "never with `jev_check`", "resume", 'text_backend="none"']:
        assert rule in core, rule
    assert (ROOT / "skill/reference.md").exists()


def test_installer_runs_as_module(tmp_path):
    out = subprocess.run([sys.executable, "-m", "jev_browse.install", "--workspace", str(tmp_path / "w"),
                          "--skills-dir", str(tmp_path / "s"), "--no-skill"], capture_output=True, text=True,
                         cwd=ROOT, timeout=60)
    assert out.returncode == 0 and "agent_helpers block written" in out.stdout
