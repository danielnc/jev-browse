"""Install jev-browse into browser-harness and your coding agents (Claude Code, Codex).

jev-browse install [--uninstall] [--unlink-skill] [--workspace PATH] [--agent auto|claude|codex|all]
                             [--skills-dir PATH] [--no-skill]

1. Appends a marked block to the harness's agent_helpers.py (user code untouched; idempotent).
2. Symlinks <skills dir>/jev-browse -> the skill (jev_browse/skill in a package install, <checkout>/skill in a
   checkout) for each agent (~/.claude/skills, ${CODEX_HOME:-~/.codex}/skills;
   refuses a conflicting path; never overwrites).
3. Warns if browser-harness telemetry is enabled (what it sends, and the opt-out command).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
# The directory that holds jev_browse/: a git checkout, or site-packages for a package install. The harness block
# puts it on sys.path.
CHECKOUT = PACKAGE.parent


def find_skill_dir(package=PACKAGE):
    """A wheel bundles the skill inside the package (jev_browse/skill); a checkout keeps it at <checkout>/skill."""
    bundled = Path(package) / "skill"
    return bundled if (bundled / "SKILL.md").exists() else Path(package).parent / "skill"


SKILL_DIR = find_skill_dir()
# How to run this install's CLI, for the hints we print.
CLI = "jev-browse" if SKILL_DIR.parent == PACKAGE else "python3 -m jev_browse"
BEGIN = "# >>> jev-browse (managed by `python3 -m jev_browse.install`; remove with --uninstall) >>>"
END = "# <<< jev-browse <<<"

BLOCK_TEMPLATE = BEGIN + '''
import os as _jb_os
import sys as _jb_sys

_JB_PATH = __JB_PATH__
_JB_NAMES = ("fast_run", "jev_open", "jev_adopt", "jev_find", "jev_click", "jev_check", "jev_close")


def _jb_stub(_name, _msg):
    def _stub(*_args, **_kwargs):
        raise RuntimeError(_msg)
    _stub.__name__ = _name
    return _stub


if _jb_os.environ.get("JEV_BROWSE_DISABLE", "").strip().lower() in {"1", "true", "yes", "on"}:
    fast_run, jev_open, jev_adopt, jev_find, jev_click, jev_check, jev_close = (
        _jb_stub(_n, "jev-browse disabled (JEV_BROWSE_DISABLE=1)") for _n in _JB_NAMES)
else:
    try:
        if _JB_PATH not in _jb_sys.path:
            _jb_sys.path.append(_JB_PATH)
        from jev_browse.harness import fast_run, jev_open, jev_adopt, jev_find, jev_click, jev_check, jev_close
        from jev_browse.harness import wrap_new_tab as _jb_wrap_new_tab
        from browser_harness import helpers as _jb_helpers
        new_tab = _jb_wrap_new_tab(_jb_helpers.new_tab)
    except Exception as _jb_exc:
        _jb_msg = f"jev-browse failed to import: {_jb_exc!r}"
        fast_run, jev_open, jev_adopt, jev_find, jev_click, jev_check, jev_close = (
            _jb_stub(_n, _jb_msg) for _n in _JB_NAMES)
''' + END + "\n"


def render_block(checkout=CHECKOUT):
    return BLOCK_TEMPLATE.replace("__JB_PATH__", repr(str(checkout)))


def workspace_dir(env=None):
    """Mirror the harness: BH_AGENT_WORKSPACE > BH_HOME/BROWSER_HARNESS_HOME > XDG_CONFIG_HOME > ~/.config."""
    env = os.environ if env is None else env
    if env.get("BH_AGENT_WORKSPACE"):
        return Path(env["BH_AGENT_WORKSPACE"]).expanduser()
    home = env.get("BH_HOME") or env.get("BROWSER_HARNESS_HOME")
    if home:
        base = Path(home).expanduser()
    elif env.get("XDG_CONFIG_HOME"):
        base = Path(env["XDG_CONFIG_HOME"]).expanduser() / "browser-harness"
    else:
        base = Path(env.get("HOME", str(Path.home()))) / ".config" / "browser-harness"
    return base / "agent-workspace"


def strip_block(text):
    """Remove the managed block and the one blank separator line install_block added before it."""
    if BEGIN not in text:
        return text
    head, rest = text.split(BEGIN, 1)
    tail = rest.split(END, 1)[1] if END in rest else ""
    tail = tail[1:] if tail.startswith("\n") else tail
    head = head[:-1] if head.endswith("\n\n") else head
    return head + tail


def install_block(workspace, checkout=CHECKOUT):
    path = Path(workspace) / "agent_helpers.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    new = strip_block(text)
    if new and not new.endswith("\n"):
        new += "\n"
    new += ("\n" if new else "") + render_block(checkout)
    if new == text:
        return path, False
    path.write_text(new)
    return path, True


def uninstall_block(workspace):
    path = Path(workspace) / "agent_helpers.py"
    if not path.exists():
        return path, False
    text = path.read_text()
    new = strip_block(text)
    if new == text:
        return path, False
    path.write_text(new)
    return path, True


def skills_dir(env=None):
    """Claude Code's personal skills dir."""
    env = os.environ if env is None else env
    return Path(env.get("HOME", str(Path.home()))) / ".claude" / "skills"


def codex_skills_dir(env=None):
    env = os.environ if env is None else env
    home = env.get("CODEX_HOME") or str(Path(env.get("HOME", str(Path.home()))) / ".codex")
    return Path(home).expanduser() / "skills"


def agent_skill_dirs(agent="auto", env=None):
    """Skill dirs to link into. auto = every agent whose home exists (~/.claude, ~/.codex), else Claude Code."""
    dirs = {"claude": skills_dir(env), "codex": codex_skills_dir(env)}
    if agent == "all":
        return list(dirs.values())
    if agent in dirs:
        return [dirs[agent]]
    found = [d for d in dirs.values() if d.parent.exists()]
    return found or [dirs["claude"]]


def link_skill(skills, skill=SKILL_DIR):
    target = Path(skill)
    link = Path(skills) / "jev-browse"
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return link, False
        raise SystemExit(f"refusing: {link} is a symlink to {os.readlink(link)}, not this install's skill {target}")
    if link.exists():
        raise SystemExit(f"refusing: {link} already exists and is not this install's symlink; remove it yourself")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    return link, True


def unlink_skill(skills, skill=SKILL_DIR):
    link = Path(skills) / "jev-browse"
    if not link.exists() and not link.is_symlink():
        return link, False
    if not link.is_symlink() or link.resolve() != Path(skill).resolve():
        raise SystemExit(f"refusing: {link} does not point at this install's skill")
    link.unlink()
    return link, True


TELEMETRY_WARNING = """
WARNING: browser-harness telemetry is ENABLED. It sends each harness script's text and stdout (up to 20k chars
each) and the repr of every helper call's arguments to PostHog. With jev-browse that includes goals, values=,
and anything your scripts print. Opt out once with:

    browser-harness telemetry disable

(or set BH_TELEMETRY=0 per process). jev-browse does not change this global setting for you.
"""


def telemetry_enabled(run=subprocess.run):
    try:
        out = run(["browser-harness", "telemetry", "status"], capture_output=True, text=True, timeout=20)
        data = json.loads(out.stdout)
        return bool(data.get("enabled"))
    except Exception:
        return None


def main(argv=None, *, run=subprocess.run, out=print):
    ap = argparse.ArgumentParser(prog=f"{CLI} install")
    ap.add_argument("--uninstall", action="store_true", help="remove only the agent_helpers block")
    ap.add_argument("--unlink-skill", action="store_true", help="remove the skill symlink if it points here")
    ap.add_argument("--workspace", help="browser-harness agent-workspace dir (default: as the harness resolves it)")
    ap.add_argument("--skills-dir", help="link the skill into this dir only")
    ap.add_argument("--agent", choices=("auto", "claude", "codex", "all"), default="auto",
                    help="which agents get the skill link (auto: every agent installed here)")
    ap.add_argument("--no-skill", action="store_true", help="install only the harness block")
    args = ap.parse_args(argv)
    workspace = Path(args.workspace).expanduser() if args.workspace else workspace_dir()
    dirs = [Path(args.skills_dir).expanduser()] if args.skills_dir else agent_skill_dirs(args.agent)
    if args.unlink_skill:
        for skills in dirs:
            link, changed = unlink_skill(skills)
            out(f"skill link {'removed' if changed else 'not present'}: {link}")
        return 0
    if args.uninstall:
        path, changed = uninstall_block(workspace)
        out(f"agent_helpers block {'removed from' if changed else 'not present in'} {path}")
        return 0
    path, changed = install_block(workspace)
    out(f"agent_helpers block {'written to' if changed else 'already current in'} {path}")
    if not args.no_skill:
        for skills in dirs:
            link, changed = link_skill(skills)
            out(f"skill {'linked' if changed else 'already linked'}: {link} -> {SKILL_DIR}")
    enabled = telemetry_enabled(run)
    if enabled:
        out(TELEMETRY_WARNING)
    elif enabled is None:
        out("note: could not read `browser-harness telemetry status`; see README 'Data egress'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
