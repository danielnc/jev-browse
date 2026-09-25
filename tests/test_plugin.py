"""The Claude Code plugin and marketplace manifests at the repo root (.claude-plugin/)."""

import json
from pathlib import Path

import jev_browse

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
MARKETPLACE = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())


def frontmatter(path):
    head = path.read_text().split("---\n", 2)[1]
    return dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)


def test_plugin_version_tracks_the_package():
    assert PLUGIN["name"] == "jev-browse" and PLUGIN["version"] == jev_browse.__version__


def test_plugin_ships_the_same_skill_the_installer_links():
    assert PLUGIN["skills"] == ["./skill"]
    assert frontmatter(ROOT / "skill" / "SKILL.md")["name"] == "jev-browse"


def test_setup_skill_is_user_invoked_and_runs_the_installer():
    setup = ROOT / "skills" / "setup" / "SKILL.md"
    meta = frontmatter(setup)
    assert meta["name"] == "setup" and meta["disable-model-invocation"] == "true"
    text = setup.read_text()
    assert "uv tool install jev-browse" in text and "jev-browse install" in text and "jev-browse doctor" in text


def test_marketplace_lists_the_plugin_at_the_repo_root():
    assert MARKETPLACE["name"] == "jev-browse"
    [entry] = MARKETPLACE["plugins"]
    assert entry["name"] == PLUGIN["name"] and entry["source"] == "./"


def test_no_bin_dir_at_the_plugin_root():
    """Claude Code puts <plugin root>/bin on the Bash PATH. A checkout launcher there would stand in for the
    package's `jev-browse` and install from the plugin cache, which moves on every plugin update."""
    assert not (ROOT / "bin").exists()
