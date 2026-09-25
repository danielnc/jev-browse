"""Packaging metadata: the wheel carries the runtime files, the skill, and the `jev-browse` command."""

import tomllib
from pathlib import Path

import jev_browse

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_version_comes_from_the_package():
    project = PYPROJECT["project"]
    assert "version" not in project and "version" in project["dynamic"]
    assert PYPROJECT["tool"]["hatch"]["version"]["path"] == "jev_browse/__init__.py"
    assert jev_browse.__version__.count(".") == 2


def test_console_script_is_the_module_cli():
    assert PYPROJECT["project"]["scripts"] == {"jev-browse": "jev_browse.__main__:main"}


def test_runtime_has_no_dependencies():
    assert PYPROJECT["project"]["dependencies"] == []


def test_wheel_bundles_the_skill_inside_the_package():
    wheel = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["jev_browse"]
    assert wheel["force-include"] == {"skill": "jev_browse/skill"}
    assert (ROOT / "jev_browse" / "snapshot.js").exists()  # package data: shipped with the package dir


def test_project_urls_point_at_the_public_repo():
    urls = PYPROJECT["project"]["urls"]
    assert all(u.startswith("https://github.com/danielnc/jev-browse") for u in urls.values())
