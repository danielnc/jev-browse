"""`python3 -m jev_browse doctor`: every check with fakes; nothing secret is ever printed."""

import pytest

from jev_browse import doctor, harness_api, install, textgen
from tests.fakes import FakeCDP


@pytest.fixture(autouse=True)
def harness(tmp_path):
    f = FakeCDP()
    f.tmp_dir = tmp_path
    harness_api.install_fake(f)
    yield
    harness_api.install_fake(None)


def statuses(checks):
    return {c.name: c.status for c in checks}


def test_load_harness_env_sets_defaults_only_and_flags_loose_permissions(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nTYPESAFE_API_KEY='ts-abc'\nexport JEV_BROWSE_OLLAMA_URL=http://127.0.0.1:1\n"
                        "JEV_BROWSE_TEXT_BACKEND=ollama\n")
    env_file.chmod(0o644)
    environ = {"JEV_BROWSE_TEXT_BACKEND": "none"}
    path, names, warn = doctor.load_harness_env(tmp_path, environ)
    assert environ["TYPESAFE_API_KEY"] == "ts-abc" and environ["JEV_BROWSE_OLLAMA_URL"] == "http://127.0.0.1:1"
    assert environ["JEV_BROWSE_TEXT_BACKEND"] == "none"  # the shell wins, as in the harness
    assert "chmod 600" in warn
    env_file.chmod(0o600)
    assert doctor.load_harness_env(tmp_path, {})[2] is None


def test_key_missing_offline_and_live(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert doctor.check_key(offline=False).status == doctor.FAIL
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-secret-value")
    assert doctor.check_key(offline=True).status == doctor.OK

    class Good:
        model = "jev-latest"

        def ask(self, state, questions, deadline=None):
            return None

    class Bad(Good):
        def ask(self, state, questions, deadline=None):
            raise RuntimeError("TypeSafe rejected the API key (HTTP 401)")

    good = doctor.check_key(offline=False, client_factory=Good)
    assert good.status == doctor.OK and "ts-secret-value" not in good.detail
    bad = doctor.check_key(offline=False, client_factory=Bad)
    assert bad.status == doctor.FAIL and "401" in bad.detail


def test_harness_checks():
    def run(argv, timeout=20):
        if argv[1] == "--version":
            return 0, "0.1.13", ""
        return 0, '{"healthy": true}', ""

    s = statuses(doctor.check_harness(run=run, telemetry=lambda: True))
    assert s["browser-harness"] == doctor.OK and s["browser-harness health"] == doctor.OK
    assert s["harness telemetry"] == doctor.WARN
    missing = doctor.check_harness(run=lambda argv, timeout=20: (None, "", "not found"), telemetry=lambda: False)
    assert missing[0].status == doctor.FAIL


def test_install_checks(tmp_path):
    ws, skills = tmp_path / "ws", tmp_path / "skills"
    s = statuses(doctor.check_install(ws, [skills]))
    assert s["harness helpers"] == doctor.FAIL and s["skill"] == doctor.WARN
    install.install_block(ws)
    install.link_skill(skills)
    s = statuses(doctor.check_install(ws, [skills]))
    assert s == {"harness helpers": doctor.OK, "skill": doctor.OK}


def test_text_backend_none_and_missing_cli(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "none")
    assert "nothing goes to an LLM" in doctor.check_text_backend(False, False)[0].detail
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "codex")
    s = statuses(doctor.check_text_backend(False, False, run=lambda argv, timeout=20: (None, "", "not found")))
    assert s["codex CLI"] == doctor.FAIL


def test_text_backend_ollama_runs_the_canary_and_never_prints_the_url(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "ollama")
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_URL", "http://192.0.2.7:11434")
    monkeypatch.setattr(textgen, "run_canary", lambda p: {"ok": True, "why": None, "wrong": [], "ms": 1500})
    checks = doctor.check_text_backend(False, False)
    assert statuses(checks)["canary"] == doctor.OK
    assert not any("192.0.2.7" in c.detail for c in checks)
    monkeypatch.setattr(textgen, "run_canary", lambda p: {"ok": False, "why": "canary failed",
                                                          "wrong": ["g"], "ms": 900})
    assert statuses(doctor.check_text_backend(False, False))["canary"] == doctor.FAIL
    assert statuses(doctor.check_text_backend(True, False))["canary"] == doctor.INFO


def test_text_backend_misconfigured_is_a_failure(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "ollama")
    (c,) = doctor.check_text_backend(False, False)
    assert c.status == doctor.FAIL and "ollama.url" in c.detail


def test_main_exit_status_and_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-secret-value")
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "none")
    monkeypatch.setattr(doctor, "check_harness", lambda: [doctor.Check(doctor.OK, "browser-harness", "fake")])
    monkeypatch.setattr(doctor, "check_install", lambda ws: [doctor.Check(doctor.OK, "harness helpers", "fake")])
    lines = []
    assert doctor.main(["--offline", "--workspace", str(tmp_path)], out=lines.append) == 0
    text = "\n".join(lines)
    assert "ts-secret-value" not in text and "Jev model: jev-latest" in text and "All required checks passed." in text
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert doctor.main(["--offline", "--workspace", str(tmp_path)], out=lines.append) == 1


def test_module_entry_point_dispatch(capsys):
    from jev_browse.__main__ import main

    assert main(["config", "--example"]) == 0 and "[ollama]" in capsys.readouterr().out
    assert main(["nope"]) == 2


def test_checkout_launcher_runs_from_any_directory(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    launcher = Path(install.CHECKOUT) / "scripts" / "jev-browse"
    p = subprocess.run([sys.executable, str(launcher), "config", "--example"], cwd=tmp_path, capture_output=True,
                       text=True, timeout=30)
    assert p.returncode == 0 and "[text]" in p.stdout
