import json
import os
from pathlib import Path

from jev_browse import cleanup

ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_covers_secret_paths():
    lines = {line.strip() for line in (ROOT / ".gitignore").read_text().splitlines()}
    for required in [".env", ".env.*", "!.env.example", ".local/", "*.key", "*.pem", "traces/", "recordings/",
                     "bench/results/raw/", "*.trace.json"]:
        assert required in lines, required


def test_clean_traces_skips_registry_and_live_runs(tmp_path):
    (tmp_path / "jev-browse-owned-default.json").write_text("{}")
    (tmp_path / "jev-browse-created-default.json").write_text("{}")
    (tmp_path / "jev-browse-run-done.json").write_text(json.dumps({"final": True, "pid": os.getpid()}))
    (tmp_path / "jev-browse-run-live.json").write_text(json.dumps({"final": False, "pid": os.getpid()}))
    (tmp_path / "jev-browse-run-dead.json").write_text(json.dumps({"final": False, "pid": 999999}))
    (tmp_path / "jev-browse-trace-done.json").write_text("{}")
    (tmp_path / "jev-browse-shot-done-1.png").write_bytes(b"x")
    (tmp_path / "jev-browse-mcp-done.log").write_text("JEV_BROWSE_RESULT={}")
    (tmp_path / "daemon.log").write_text("keep")
    removed = set(cleanup.clean(tmp_path))
    assert removed == {"jev-browse-run-done.json", "jev-browse-run-dead.json", "jev-browse-trace-done.json",
                       "jev-browse-shot-done-1.png", "jev-browse-mcp-done.log"}
    assert (tmp_path / "jev-browse-owned-default.json").exists()
    assert (tmp_path / "jev-browse-run-live.json").exists()
    assert (tmp_path / "daemon.log").exists()


def test_global_pointer_snippet_is_the_same_in_readme_and_docs():
    readme = (ROOT / "README.md").read_text().split("```markdown\n", 1)[1].split("\n```", 1)[0]
    doc = (ROOT / "docs" / "global-pointer.md").read_text().split("```markdown\n", 1)[1].split("\n```", 1)[0]
    assert readme == doc and "fast_run(url, goal" in readme


def test_docs_link_to_files_that_exist():
    import re

    for md in [ROOT / "README.md", ROOT / "install.md", ROOT / "CONTRIBUTING.md", *(ROOT / "docs").glob("*.md")]:
        for target in re.findall(r"\]\(([^)#]+?)(?:#[^)]*)?\)", md.read_text()):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            assert (md.parent / target).exists(), f"{md.name}: broken link {target}"


def test_nothing_public_depends_on_the_private_design_record():
    """docs/design/ stays in the private dev repo only; the public tree must not link to or read it."""
    offenders = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT).as_posix()
        if (not path.is_file() or path.suffix not in {".md", ".py", ".js", ".toml", ".json", ".yaml"}
                or rel.startswith(("docs/design/", "."))
                or "__pycache__" in rel or rel == "tests/test_hygiene.py"):
            continue
        if "docs/design" in path.read_text(errors="ignore"):
            offenders.append(rel)
    assert not offenders
