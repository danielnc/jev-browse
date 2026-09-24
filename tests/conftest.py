"""Isolate every test from the developer's own configuration: no real config file, no JEV_BROWSE_* variables from
the shell, and a deterministic answer to "is this CLI on PATH" (yes, unless a test says otherwise)."""

import os

import pytest

from jev_browse import config


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path_factory):
    for name in list(os.environ):
        if name.startswith("JEV_BROWSE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("JEV_BROWSE_CONFIG", str(tmp_path_factory.mktemp("cfg") / "absent.toml"))
    monkeypatch.setattr(config, "_which", lambda name: f"/usr/bin/{name}")
    config.reset_cache()
    yield
    config.reset_cache()
