"""`python3 -m jev_browse doctor`: check the install and configuration, and print what is active.

Checks: Python, the config file and every setting, SystemOne authentication (and a live request unless --offline),
browser-harness (installed, healthy, telemetry), the agent_helpers block and skill links, and the text backend
(CLI present, or server reachable plus the known-answer canary). Nothing secret is printed: keys are reported as
set/unset and private URLs as <set>. Exit status 1 if any check FAILs.
"""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__, config, install

OK, WARN, FAIL, INFO = "ok", "warn", "FAIL", "info"


@dataclass
class Check:
    """One doctor line: status (ok, warn, FAIL, info), the thing checked, and a detail with no secrets."""
    status: str
    name: str
    detail: str


def load_harness_env(workspace=None, environ=None):
    """Load the browser-harness agent-workspace .env the way the harness does (setdefault: the shell wins).
    Returns (path, loaded names, permission warning or None)."""
    environ = os.environ if environ is None else environ
    path = Path(workspace or install.workspace_dir()) / ".env"
    if not path.exists():
        return path, [], None
    names = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        environ.setdefault(k, v.strip().strip('"').strip("'"))
        names.append(k)
    mode = path.stat().st_mode & 0o077
    warn = f"{path} is readable by other users; run: chmod 600 {path}" if mode else None
    return path, names, warn


def _run(argv, timeout=20):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return None, "", "not found"
    except subprocess.TimeoutExpired:
        return None, "", "timed out"


def check_python():
    """FAIL unless this interpreter is Python 3.11+."""
    ok = sys.version_info >= (3, 11)
    return Check(OK if ok else FAIL, "python", f"{sys.version.split()[0]} ({sys.executable})"
                 + ("" if ok else "; jev-browse needs 3.11+"))


def check_config():
    """Where the config file is, plus a warning per invalid value or unknown key."""
    path = config.config_path()
    out = [Check(INFO, "config file", f"{path} ({'found' if path.exists() else 'not present; defaults apply'})")]
    for key, msg in config.problems():
        out.append(Check(WARN, f"config {key}", msg))
    return out


def check_key(offline, client_factory=None):
    """Validate endpoint/auth, then make one tiny request even when authentication is disabled."""
    try:
        key = config.jev_api_key()
        anonymous = config.jev_auth() == "none"
        name = "Jev endpoint" if anonymous else config.jev_api_key_env()
        if not anonymous and not key:
            return Check(FAIL, name, config.key_detail())
    except ValueError as exc:
        return Check(FAIL, "Jev endpoint", str(exc))
    note = "authentication disabled" if anonymous else "set"
    if offline:
        return Check(OK, name, f"{note} (not verified: --offline)")
    from .typesafe import Client, ServiceError

    try:
        client = (client_factory or Client)()
        q = {"type": "noul", "instructions": {"question": "Is this message a connectivity check?"}}
        started = time.perf_counter()
        client.ask({"message": "jev-browse doctor connectivity check"}, {"ok": q}, deadline=time.monotonic() + 20)
        ms = round((time.perf_counter() - started) * 1000)
        return Check(OK, name, f"{note}; SystemOne answered in {ms} ms (model {client.model})")
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ServiceError) else type(exc).__name__
        return Check(FAIL, name, f"{note}, but the SystemOne request failed: {detail}")


def check_harness(run=_run, telemetry=install.telemetry_enabled):
    """Is browser-harness on PATH and healthy, and is its telemetry on? (`run` and `telemetry` are test seams.)"""
    rc, out, err = run(["browser-harness", "--version"])
    if rc is None:
        return [Check(FAIL, "browser-harness", "not on PATH; install it: https://github.com/browser-use/browser-harness")]
    checks = [Check(OK, "browser-harness", f"installed (reports version {out or '?'})")]
    rc, out, err = run(["browser-harness", "doctor", "--json"], timeout=60)
    try:
        health = json.loads(out)
        checks.append(Check(OK if health.get("healthy") else WARN, "browser-harness health",
                            "healthy" if health.get("healthy") else f"not healthy: {out[:200]}"))
    except ValueError:
        checks.append(Check(WARN, "browser-harness health", f"`browser-harness doctor --json` gave no JSON: "
                            f"{(err or out)[:200]}"))
    enabled = telemetry()
    if enabled:
        checks.append(Check(WARN, "harness telemetry", "ENABLED: script text, stdout, and helper-call arguments "
                            "(goals, values=) go to PostHog. Opt out: browser-harness telemetry disable"))
    elif enabled is False:
        checks.append(Check(OK, "harness telemetry", "disabled"))
    return checks


def check_install(workspace=None, skill_dirs=None):
    """Is the current helpers block in the harness agent_helpers.py, and is the skill linked for an agent?"""
    out = []
    helpers = Path(workspace or install.workspace_dir()) / "agent_helpers.py"
    text = helpers.read_text() if helpers.exists() else ""
    if install.BEGIN not in text:
        out.append(Check(FAIL, "harness helpers", f"no jev-browse block in {helpers}; run: {install.CLI} install"))
    elif install.render_block() not in text:
        out.append(Check(WARN, "harness helpers", f"the block in {helpers} is from another checkout or version; "
                         f"re-run: {install.CLI} install"))
    else:
        out.append(Check(OK, "harness helpers", f"block current in {helpers}"))
    target = install.SKILL_DIR.resolve()
    linked = []
    for d in skill_dirs or install.agent_skill_dirs():
        link = Path(d) / "jev-browse"
        if link.is_symlink() and link.resolve() == target:
            linked.append(str(link))
        elif link.exists() or link.is_symlink():
            out.append(Check(WARN, "skill", f"{link} exists but does not point at this install's skill"))
    out.append(Check(OK if linked else WARN, "skill", ", ".join(linked) if linked else
                     "not linked for any agent (fine if the Claude Code plugin provides it); "
                     f"run: {install.CLI} install"))
    return out


def check_text_backend(offline, no_canary, run=_run):
    """The active text backend, its CLI, its fallback, and (for ollama/openai, unless `offline` or
    `no_canary`) a fresh known-answer canary."""
    from . import textgen

    out = []
    name = config.text_backend_name()
    src = config.source("text.backend")
    try:
        backend = textgen.make_backend(name)
    except ValueError as exc:
        return [Check(FAIL, "text backend", f"{name}: {exc}")]
    label = f"{name} (text.backend {config.get('text.backend')!r} from {src})"
    if name == "none":
        return [Check(INFO, "text backend", label + ": misses hand back to the caller; nothing goes to an LLM")]
    primary = getattr(backend, "primary", backend)
    out.append(Check(INFO, "text backend", f"{label}, model {primary.model}"))
    for cli in ([name] if name in ("claude", "codex") else []):
        rc, ver, _ = run([cli, "--version"])
        out.append(Check(OK if rc == 0 else FAIL, f"{cli} CLI", ver if rc == 0 else f"`{cli}` not found on PATH"))
    if name in ("ollama", "openai"):
        fb = backend.fallback
        fb_note = ("none: a failed or unreachable backend hands the miss back" if fb.name == "none"
                   else f"{fb.name}: page text goes there when {name} fails")
        out.append(Check(INFO, "text fallback", fb_note))
        if name == "openai" and not os.environ.get(config.get("openai.api_key_env")):
            out.append(Check(WARN, "openai key", f"${config.get('openai.api_key_env')} is not set "
                             "(fine for a local server without auth)"))
        if offline or no_canary or not config.get("canary.enabled"):
            out.append(Check(INFO, "canary", "skipped"))
        else:
            verdict = textgen.run_canary(primary)
            textgen.save_canary_verdict(primary, verdict)
            if verdict["ok"]:
                out.append(Check(OK, "canary", f"3/3 known answers correct in {verdict['ms']} ms"))
            else:
                out.append(Check(FAIL, "canary", f"{verdict['why']} ({len(verdict['wrong'])} of 3 wrong or missing, "
                                 f"{verdict['ms']} ms): the backend will not be used; see docs/backends.md"))
    return out


def summary_lines():
    """The three summary lines doctor prints first: version and location, model and budgets, hosts and canary."""
    budgets = f"{config.get('run.max_actions')} actions, {config.get('run.max_requests')} requests, " \
              f"{config.get('run.timeout_s')} s"
    hosts = config.allowed_hosts()
    return [f"jev-browse {__version__} at {install.CHECKOUT}",
            f"Jev model: {config.get('jev.model')} · budgets: {budgets} · speculate text: "
            f"{config.get('text.speculate')}",
            f"allowed hosts: {', '.join(hosts) if hosts else 'all'} · canary: "
            f"{'on' if config.get('canary.enabled') else 'off'}"]


def main(argv=None, *, out=print):
    """`jev-browse doctor [--offline] [--no-canary] [--workspace DIR]`: print every check. Returns 1 if any check
    FAILs, else 0."""
    import argparse

    ap = argparse.ArgumentParser(prog=f"{install.CLI} doctor")
    ap.add_argument("--offline", action="store_true", help="no network: skip the TypeSafe request and the canary")
    ap.add_argument("--no-canary", action="store_true", help="skip the text-backend canary")
    ap.add_argument("--workspace", help="browser-harness agent-workspace dir")
    args = ap.parse_args(argv)
    env_path, names, perm_warn = load_harness_env(args.workspace)
    config.reset_cache()
    checks = [check_python(),
              Check(INFO, "harness .env", f"{env_path}: " + (f"{len(names)} variables" if env_path.exists()
                                                            else "not present"))]
    if perm_warn:
        checks.append(Check(WARN, "harness .env", perm_warn))
    checks += check_config()
    checks.append(check_key(args.offline))
    checks += check_harness()
    checks += check_install(args.workspace)
    checks += check_text_backend(args.offline, args.no_canary)
    for line in summary_lines():
        out(line)
    out("")
    width = max(len(c.name) for c in checks)
    for c in checks:
        out(f"[{c.status:>4}] {c.name:<{width}}  {c.detail}")
    failed = [c for c in checks if c.status == FAIL]
    out("")
    out(f"{len(failed)} problem(s) to fix." if failed else "All required checks passed.")
    return 1 if failed else 0
