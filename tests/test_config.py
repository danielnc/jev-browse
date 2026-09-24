"""One config surface: environment (incl. the harness agent-workspace .env) > config file > built-in default."""

import pytest

from jev_browse import config


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in config.all_env_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JEV_BROWSE_CONFIG", str(tmp_path / "config.toml"))
    config.reset_cache()
    yield tmp_path
    config.reset_cache()


def write(tmp_path, text):
    (tmp_path / "config.toml").write_text(text)
    config.reset_cache()


def test_every_setting_has_an_env_name_a_toml_key_and_docs():
    for key, s in config.SETTINGS.items():
        assert s.env.startswith("JEV_BROWSE_") and s.doc and "." in key
        assert key == s.key


def test_defaults_without_env_or_file():
    assert config.get("jev.model") == "jev-latest"
    assert config.get("run.max_actions") == 30 and config.get("run.timeout_s") == 90
    assert config.get("ollama.model") == "qwen3:30b-a3b" and config.get("ollama.url") is None
    assert config.get("canary.enabled") is True and config.get("canary.ttl_healthy_s") == 600
    assert config.get("safety.allowed_hosts") is None


def test_config_file_then_env_wins(clean, monkeypatch):
    write(clean, '[ollama]\nurl = "http://127.0.0.1:11434"\nnum_gpu = 0\n[run]\ntimeout_s = 120\n'
                 '[safety]\nallowed_hosts = ["example.com", "*.example.org"]\n')
    assert config.get("ollama.url") == "http://127.0.0.1:11434" and config.get("ollama.num_gpu") == 0
    assert config.get("run.timeout_s") == 120
    assert config.allowed_hosts() == ["example.com", "*.example.org"]
    assert config.source("run.timeout_s") == "file"
    monkeypatch.setenv("JEV_BROWSE_TIMEOUT_S", "45")
    assert config.get("run.timeout_s") == 45 and config.source("run.timeout_s") == "env"


def test_legacy_env_names_still_work(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_TEXT_MODEL", "sonnet")
    assert config.get("claude.model") == "sonnet"
    monkeypatch.setenv("JEV_BROWSE_MODEL", "jev-alias")
    assert config.get("jev.model") == "jev-alias"


@pytest.mark.parametrize("raw,expected", [("0", False), ("off", False), ("no", False), ("false", False),
                                          ("1", True), ("on", True), ("YES", True), ("true", True)])
def test_bool_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("JEV_BROWSE_CANARY", raw)
    assert config.get("canary.enabled") is expected


def test_invalid_values_fall_back_to_the_default_and_are_reported(clean, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_MAX_ACTIONS", "lots")
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "gpt")
    assert config.get("run.max_actions") == 30
    problems = dict(config.problems())
    assert "run.max_actions" in problems and "text.backend" in problems


def test_a_broken_config_file_is_ignored_and_reported(clean):
    write(clean, "this is = not [valid toml")
    assert config.get("run.timeout_s") == 90
    assert any(k == "config file" for k, _ in config.problems())


def test_unknown_keys_in_the_file_are_reported(clean):
    write(clean, "[ollama]\nurll = 'x'\n")
    assert ("ollama.urll", "unknown setting") in config.problems()


def test_secrets_are_never_read_from_the_config_file(clean):
    write(clean, '[openai]\napi_key = "sk-should-not-be-here"\n')
    assert ("openai.api_key", "unknown setting") in config.problems()


def test_text_backend_auto_is_claude_when_the_cli_exists_else_none(monkeypatch):
    monkeypatch.setattr(config, "_which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    assert config.text_backend_name() == "claude"
    monkeypatch.setattr(config, "_which", lambda name: None)
    assert config.text_backend_name() == "none"
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "ollama")
    assert config.text_backend_name() == "ollama" and config.text_backend_name("none") == "none"


def test_text_fallback_auto(monkeypatch):
    monkeypatch.setattr(config, "_which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    assert config.text_fallback_name() == "claude"
    monkeypatch.setattr(config, "_which", lambda name: None)
    assert config.text_fallback_name() == "none"
    monkeypatch.setenv("JEV_BROWSE_TEXT_FALLBACK", "none")
    monkeypatch.setattr(config, "_which", lambda name: "/usr/bin/claude")
    assert config.text_fallback_name() == "none"


def test_run_budgets_come_from_config(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_MAX_ACTIONS", "12")
    monkeypatch.setenv("JEV_BROWSE_MAX_REQUESTS", "20")
    monkeypatch.setenv("JEV_BROWSE_TIMEOUT_S", "40")
    assert (config.get("run.max_actions"), config.get("run.max_requests"), config.get("run.timeout_s")) == (12, 20, 40)


def test_config_path_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_BROWSE_CONFIG")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.config_path() == tmp_path / "xdg" / "jev-browse" / "config.toml"


def test_active_listing_masks_nothing_secret_and_names_sources(clean, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_OLLAMA_URL", "http://192.0.2.5:11434")
    rows = {r["key"]: r for r in config.active()}
    assert rows["ollama.url"]["source"] == "env" and rows["ollama.url"]["value"] == "<set>"
    assert rows["jev.model"]["value"] == "jev-latest" and rows["jev.model"]["source"] == "default"


def test_example_config_parses_and_uses_only_known_keys():
    from pathlib import Path

    text = (Path(config.__file__).resolve().parents[1] / "docs" / "config.example.toml").read_text()
    import tomllib

    data = tomllib.loads(text)
    assert not [k for k in config.flatten(data) if k not in config.SETTINGS]


def test_example_file_is_the_generated_one():
    from pathlib import Path

    path = Path(config.__file__).resolve().parents[1] / "docs" / "config.example.toml"
    assert path.read_text() == config.example_toml(), "regenerate: python3 -m jev_browse config --example"


def test_configuration_doc_has_the_generated_table():
    from pathlib import Path

    doc = (Path(config.__file__).resolve().parents[1] / "docs" / "configuration.md").read_text()
    assert config.markdown_table() in doc, "regenerate: python3 -m jev_browse config --markdown"
    for name in config.ENV_ONLY:
        assert name in doc
