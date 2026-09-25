"""The MCP server's bridge to browser-harness (stdlib only, so it is testable without the MCP SDK).

Every tool call runs a generated script through the `browser-harness` CLI, exactly as an agent's heredoc would:
same daemon bootstrap, harness `.env`, agent_helpers block (so `JEV_BROWSE_DISABLE`, the owned-tab registry, the
config, and the text backends all behave as they do for scripts). The script calls one jev-browse helper from the
harness globals and prints one `JEV_BROWSE_MCP={json}` line, which becomes concise text for the calling agent.

fast_run runs detached, with its output in `jev-browse-mcp-<run_id>.log` next to its run file. A run that outlives
`mcp.wait_s` returns its run_id; `fast_run_status` collects the result later, from this process's handle or, after
a server restart, from the run file.
"""

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, install
from . import run as runmod
from .results import CALLER_ACTION, Reason

MARK = "JEV_BROWSE_MCP="
CALLS = ("fast_run", "jev_open", "jev_find", "jev_click", "jev_check", "jev_close")
TEXT_HEAD = 600
MAX_FIELDS = 12
MAX_STEPS = 5

SCRIPT = '''import json as _jbm_json
_jbm_req = _jbm_json.loads(__JBM_PAYLOAD__)


def _jbm_dump(r):
    import dataclasses
    kind = type(r).__name__
    if hasattr(r, "to_dict"):
        return {"kind": kind, **r.to_dict()}
    if dataclasses.is_dataclass(r) and not isinstance(r, type):
        return {"kind": kind, **dataclasses.asdict(r)}
    return {"kind": kind, "value": r}


if _jbm_req["call"] not in globals():
    _jbm_out = {"kind": "error", "error": "not_installed",
                "detail": "the jev-browse helpers are not loaded in browser-harness"}
else:
    try:
        _jbm_out = _jbm_dump(globals()[_jbm_req["call"]](*_jbm_req["args"], **_jbm_req["kwargs"]))
    except Exception as _jbm_exc:
        _jbm_out = {"kind": "error", "error": type(_jbm_exc).__name__, "detail": str(_jbm_exc)[:500]}
print("''' + MARK + '''" + _jbm_json.dumps(_jbm_out, default=str, ensure_ascii=False), flush=True)
'''


class BridgeError(Exception):
    """A problem the calling agent must see as a tool error (bad arguments, harness missing, run unknown)."""


# ------------------------------------------------------------------------------------------------ the harness
def script(call, args=(), kwargs=None):
    """The heredoc for one helper call. Arguments travel as a JSON string literal, never as code."""
    if call not in CALLS:
        raise ValueError(f"unknown call {call!r}")
    payload = json.dumps({"call": call, "args": list(args), "kwargs": kwargs or {}}, ensure_ascii=False)
    return SCRIPT.replace("__JBM_PAYLOAD__", repr(payload))


def harness_argv():
    """The configured harness command as argv; `browser-harness` also resolves from ~/.local/bin (uv tool's bin),
    which GUI-launched MCP clients often leave off PATH."""
    raw = config.get("mcp.harness_command") or "browser-harness"
    argv = shlex.split(raw)
    found = shutil.which(argv[0])
    if not found and argv[0] == "browser-harness":
        candidate = Path.home() / ".local" / "bin" / "browser-harness"
        found = str(candidate) if candidate.exists() else None
    if not found:
        raise BridgeError(f"`{argv[0]}` not found. Install browser-harness "
                          "(https://github.com/browser-use/browser-harness), or set JEV_BROWSE_HARNESS_COMMAND to "
                          "its absolute path in the MCP server's env.")
    return [found, *argv[1:]]


_OWNER = None


def owner():
    """The tab-ownership tag of this MCP server: JEV_BROWSE_OWNER if set, else one `mcp-<random>` per process."""
    global _OWNER
    if _OWNER is None:
        _OWNER = os.environ.get("JEV_BROWSE_OWNER") or f"mcp-{secrets.token_hex(4)}"
    return _OWNER


def child_env():
    """The environment of every spawned script. Harness telemetry is forced off: it would send the generated
    script (goal, values) and helper arguments to PostHog, and MCP users never see those scripts."""
    env = dict(os.environ)
    env["JEV_BROWSE_OWNER"] = owner()
    env["BH_TELEMETRY"] = "0"
    return env


def parse(output):
    """The last JEV_BROWSE_MCP line of a script's output, or None."""
    for line in reversed((output or "").splitlines()):
        if line.startswith(MARK):
            try:
                return json.loads(line[len(MARK):])
            except ValueError:
                return None
    return None


def _tail(text, n=800):
    lines = [ln for ln in (text or "").splitlines() if ln.strip() and not ln.startswith(("JEV_BROWSE_", MARK))]
    return "\n".join(lines)[-n:]


def call(name, *args, timeout=None, **kwargs):
    """Run one short helper call (jev_open/find/click/check/close) and return its payload dict."""
    timeout = timeout or config.get("mcp.wait_s")
    argv = harness_argv()
    try:
        p = subprocess.run(argv, input=script(name, args, kwargs), capture_output=True, text=True, timeout=timeout,
                           env=child_env())
    except subprocess.TimeoutExpired:
        return {"kind": "error", "error": "timeout",
                "detail": f"{name} did not finish within {timeout} s and was stopped; it may or may not have acted"}
    payload = parse(p.stdout)
    if payload is None:
        return {"kind": "error", "error": "harness", "detail": f"browser-harness exited {p.returncode}: "
                + (_tail(p.stderr) or _tail(p.stdout) or "no output")}
    return payload


# ------------------------------------------------------------------------------------------------ fast_run
@dataclass
class _Handle:
    proc: subprocess.Popen
    log: Path
    started: float = field(default_factory=time.monotonic)
    started_wall: float = field(default_factory=time.time)


_RUNS = {}


def log_path(run_id):
    return runmod.run_dir() / f"jev-browse-mcp-{run_id}.log"


def new_run_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _running(run_id):
    handle = _RUNS.get(run_id)
    if handle is not None:
        return handle.proc.poll() is None
    data = runmod.read_run_file(runmod.run_path(run_id))
    return bool(data and not data.get("final") and runmod._pid_alive(data.get("pid")))


def start_fast_run(url, goal, **kwargs):
    """Validate, then launch fast_run in its own browser-harness process. Returns the run_id."""
    if not goal or not str(goal).strip():
        raise BridgeError("fast_run needs a goal")
    if url is None and not kwargs.get("target_id"):
        raise BridgeError("pass a url, or url=null with the target_id of a jev-browse tab to resume")
    run_id = kwargs.pop("run_id", None) or new_run_id()
    if not runmod.RUN_ID.match(str(run_id)):
        raise BridgeError("run_id must match [A-Za-z0-9_-]{1,64}")
    if _running(run_id):
        raise BridgeError(f"run_id {run_id!r} is still running; call fast_run_status(run_id={run_id!r})")
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    argv = harness_argv()
    path = log_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as log:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True,
                                env=child_env(), start_new_session=True)
    proc.stdin.write(script("fast_run", (url, goal), {**kwargs, "run_id": run_id}))
    proc.stdin.close()
    _RUNS[run_id] = _Handle(proc, path)
    return run_id


def reap():
    """Collect finished children so none linger as zombies."""
    for handle in list(_RUNS.values()):
        handle.proc.poll()


def state(run_id):
    """{"done": bool, "payload": dict | None, "run": run-file dict | None, "elapsed_s": float | None}."""
    reap()
    if not runmod.RUN_ID.match(str(run_id or "")):
        raise BridgeError("run_id must match [A-Za-z0-9_-]{1,64}")
    handle = _RUNS.get(run_id)
    data = runmod.read_run_file(runmod.run_path(run_id))
    if handle and data and data.get("started_at", 0) < handle.started_wall - 1:
        data = None  # an earlier run under a reused run_id, not this one
    path = log_path(run_id)
    elapsed = round(time.monotonic() - handle.started, 1) if handle else \
        (round(time.time() - data["started_at"], 1) if data and data.get("started_at") else None)
    if handle is None and data is None and not path.exists():
        raise BridgeError(f"no fast_run with run_id {run_id!r} (unknown to this server and no run file)")
    if _running(run_id):
        return {"done": False, "payload": None, "run": data, "elapsed_s": elapsed}
    output = path.read_text(errors="replace") if path.exists() else ""
    payload = parse(output)
    if payload is None and data and data.get("final") and data.get("result"):
        payload = {"kind": "RunResult", **data["result"]}
    if payload is None:
        code = handle.proc.returncode if handle else None
        detail = _tail(output) or "no output"
        payload = {"kind": "error", "error": "harness",
                   "detail": (f"browser-harness exited {code}: " if code is not None else "the run stopped: ")
                   + detail}
    return {"done": True, "payload": payload, "run": data, "elapsed_s": elapsed}


# ------------------------------------------------------------------------------------------------ formatting
def _one_line(text, n):
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _ref(d):
    return {k: d.get(k) for k in ("target_id", "node", "doc", "label", "ctx")}


def _json(obj):
    return json.dumps(obj, ensure_ascii=False)


def _surface_note(surfaces):
    s = surfaces or {}
    notes = []
    if any(f.get("relevant") for f in s.get("frames") or []):
        notes.append("a large iframe")
    if any(h.get("relevant") for h in s.get("shadow_hosts") or []):
        notes.append("an interactive shadow DOM")
    if s.get("canvas_relevant"):
        notes.append("canvas or unlabelled icons")
    if s.get("auth_relevant"):
        notes.append("a password or one-time-code field")
    return ", ".join(notes)


TAKE_OVER = ("jev-browse stops here by design. Continue on this tab with another browser tool if you have one, "
             "try jev_find/jev_click step by step, or tell the user what is needed. The tab stays open.")
ADVICE = {
    Reason.text_value_unavailable: "Resume with values for the fields below (ask the user if you do not know "
                                   "them): fast_run(url=null, goal=<the same goal, verbatim>, target_id=<target_id>, "
                                   "values={\"<field key or label>\": \"...\"}).",
    Reason.confirm_required: "A commit click (send, pay, delete, book, confirm...) needs authorisation. Do NOT "
                             "confirm unless the user's request clearly covers this exact action; ask them if it "
                             "does not. To confirm, resume with fast_run(url=null, goal=<the same goal, verbatim>, "
                             "target_id=<target_id>, confirm=[<confirm ref>]) or call jev_click(ref=<confirm ref>, "
                             "confirm=true).",
    Reason.popup_tab: "The click opened a new tab, now owned by jev-browse: continue there with "
                      "fast_run(url=null, goal=..., target_id=<the popup target_id>) or the jev_* tools.",
    Reason.host_not_allowed: "The host is outside safety.allowed_hosts (JEV_BROWSE_ALLOWED_HOSTS); jev-browse will "
                             "not open or act on it.",
    Reason.not_owned_tab: "That tab is not a jev-browse tab (or is gone). Open one with jev_open(url) or "
                          "fast_run(url, goal).",
    Reason.browser_error: "The browser or tab failed. Re-open with jev_open(url) or fast_run(url, goal); if it "
                          "repeats, run the doctor tool.",
    Reason.dialog_open: "A native dialog (alert/confirm/prompt) is open on the tab; jev-browse does not answer "
                        "dialogs. Handle it with another browser tool or ask the user.",
    Reason.stale_page: "The page kept changing. Retry the same call in a moment.",
    Reason.service_error: "TypeSafe (or the key) failed. Retry later; if it repeats, run the doctor tool.",
    Reason.low_confidence: TAKE_OVER + " A more specific goal can help: fast_run(url=null, goal=<same goal>, "
                                       "target_id=<target_id>) resumes where it stopped.",
    Reason.no_progress: TAKE_OVER,
    Reason.budget_exhausted: "The run's action, request, or time budget ran out. Resume where it stopped with "
                             "fast_run(url=null, goal=<the same goal, verbatim>, target_id=<target_id>) (a larger "
                             "timeout_s if it was the deadline), or take over.",
}


def advice(reason):
    reason = Reason(reason)
    if reason in ADVICE:
        return ADVICE[reason]
    if reason in CALLER_ACTION["take_over"]:
        return TAKE_OVER
    return "See the reason above."


def _fields(fields):
    out = []
    for f in (fields or [])[:MAX_FIELDS]:
        label = _one_line(f.get("label") or f.get("key") or "", 60)
        key = f.get("key")
        value = f.get("value")
        shown = "(checked)" if f.get("checked") is True else _json(_one_line(value, 80)) if value else "(empty)"
        if f.get("matches_requested") is not None:
            shown += " (matches requested)" if f["matches_requested"] else " (differs from requested)"
        out.append(f"  - {label}" + (f" [key: {key}]" if key and key != label else "") + f": {shown}")
    if len(fields or []) > MAX_FIELDS:
        out.append(f"  - … {len(fields) - MAX_FIELDS} more")
    return out


def format_run(d):
    """A RunResult dict as the text an agent acts on."""
    status, reason = d.get("status"), d.get("reason")
    tid = d.get("target_id")
    data = d.get("data") or {}
    ev = d.get("evidence") or {}
    head = f"fast_run: {status}" + (f" ({reason})" if reason else "")
    lines = [head]
    if d.get("detail"):
        lines.append(f"detail: {_one_line(d['detail'], 300)}")
    if status == "claimed_done":
        lines.append("next: claimed_done is not proof. Check the URL, fields, and text below against the goal "
                     "before you report success (jev_check is a navigation aid, not a verifier). Close the tab "
                     "with jev_close(target_id) when you are finished with it.")
    elif reason:
        lines.append("next: " + advice(reason).replace("<target_id>", _json(tid) if tid else "<target_id>"))
    lines.append(f"run_id: {d.get('run_id')} · target_id: {tid}")
    if reason == Reason.confirm_required.value:
        lines.append(f"confirm ref: {_json(_ref(data))}")
        if data.get("context"):
            lines.append(f"confirm context: {_one_line(data['context'], 200)}")
    if reason == Reason.text_value_unavailable.value and data.get("fields"):
        lines.append("fields needing values:")
        for f in data["fields"][:MAX_FIELDS]:
            lines.append(f"  - {_one_line(f.get('label'), 60)} [key: {f.get('key')}]"
                         + (f" (tried: {', '.join(map(str, f['candidates_tried'][:5]))})"
                            if f.get("candidates_tried") else ""))
    if reason == Reason.popup_tab.value and data.get("target_id") and data.get("target_id") != tid:
        lines.append(f"popup target_id: {data['target_id']}")
    if reason == Reason.dialog_open.value and data.get("dialog"):
        dialog = data["dialog"]
        lines.append(f"dialog: {_one_line(dialog.get('type'), 20)} {_json(_one_line(dialog.get('message'), 160))}")
    if d.get("url") or d.get("title"):
        lines.append(f"page: {_one_line(d.get('title'), 120)} — {d.get('url')}")
    note = _surface_note(ev.get("surfaces"))
    if note:
        lines.append(f"page has: {note}")
    if ev.get("fields"):
        lines.append("fields (visible; personal and sensitive values are never shown):")
        lines += _fields(ev["fields"])
    if ev.get("visible_text"):
        lines.append(f"text: {_one_line(ev['visible_text'], TEXT_HEAD)}")
    if ev.get("screenshot_path"):
        lines.append(f"screenshot: {ev['screenshot_path']}")
    trace = d.get("trace") or []
    stats = d.get("stats") or {}
    if trace or stats:
        steps = [f"{s.get('operation')} {_json(_one_line(s.get('label'), 50))}" if s.get("label") else
                 str(s.get("operation")) for s in trace[-MAX_STEPS:]]
        bits = [f"{len(trace)} decisions"]
        if stats.get("wall_ms") is not None:
            bits.append(f"{stats['wall_ms'] / 1000:.1f} s")
        if stats.get("jev_calls") is not None:
            bits.append(f"{stats['jev_calls']} Jev requests")
        if stats.get("llm_calls"):
            bits.append(f"{stats['llm_calls']} text-backend calls")
        lines.append("steps: " + " · ".join(bits) + (f" (last: {'; '.join(steps)})" if steps else ""))
    return "\n".join(lines)


def format_running(run_id, st):
    data = st.get("run") or {}
    partial = data.get("result") or {}
    steps = len(partial.get("trace") or [])
    elapsed = st.get("elapsed_s")
    bits = [f"run_id {run_id}"] + ([f"{elapsed:.0f} s so far"] if elapsed is not None else []) + [f"{steps} steps"]
    if data.get("target_id"):
        bits.append(f"target_id {data['target_id']}")
    return (f"fast_run: running ({', '.join(bits)})\n"
            f"next: call fast_run_status(run_id={_json(run_id)}) to wait for the result. Do not start the same "
            "goal again; the run continues on its own and stops at its timeout_s.")


def format_error(d):
    err, detail = d.get("error"), d.get("detail") or ""
    if err == "not_installed":
        return ("jev-browse is not installed in browser-harness (its helpers are not loaded). Run "
                f"`{install.CLI} install`, then the doctor tool.")
    if err == "timeout":
        return f"timed out: {detail}. Increase JEV_BROWSE_MCP_WAIT_S if your MCP client allows longer tool calls."
    if err == "harness":
        return f"browser-harness failed. {detail}"
    return f"jev-browse error ({err}): {detail}"


def format_handback(d, call):
    reason = d.get("reason")
    data = d.get("data") or {}
    lines = [f"{call}: handed_back ({reason})"]
    if d.get("detail"):
        lines.append(f"detail: {_one_line(d['detail'], 300)}")
    tid = data.get("target_id")
    lines.append("next: " + advice(reason).replace("<target_id>", _json(tid) if tid else "<target_id>"))
    if tid:
        lines.append(f"target_id: {tid}")
    note = _surface_note(data.get("surfaces"))
    if note:
        lines.append(f"page has: {note}")
    return "\n".join(lines)


def _candidate_lines(cands):
    out = []
    for c in cands or []:
        p = c.get("probability")
        out.append(f"  - {_json(_one_line(c.get('label'), 80))} ({c.get('role') or 'element'}"
                   + (f", p={p:.2f}" if isinstance(p, (int, float)) else "") + f") ref: {_json(_ref(c))}")
    return out


def format_find(d):
    outcome = d.get("outcome")
    tid = d.get("target_id")
    if outcome == "found":
        el = d.get("element") or {}
        return "\n".join([
            f"jev_find: found {_json(_one_line(el.get('label'), 80))} ({el.get('role') or 'element'}) on {tid} "
            f"(confidence {d.get('confidence', 0):.2f})",
            f"ref: {_json(_ref(el))}",
            "next: jev_click(ref=<ref above>) clicks it; commit buttons (send, pay, delete...) need confirm=true, "
            "which you pass only when the user's request clearly covers that action."])
    lines = [f"jev_find: {outcome} on {tid} (exists {d.get('exists', 0):.2f}, confidence {d.get('confidence', 0):.2f})"]
    if outcome == "ambiguous":
        if (d.get("data") or {}).get("occluded"):
            lines.append("next: the element is covered by an overlay. Dismiss it (or scroll) and retry once.")
        else:
            lines.append("next: re-ask once with a more specific description, or pick one of these by label and "
                         "pass its ref to jev_click. Do not loop more than twice.")
    else:
        note = _surface_note(d.get("surfaces"))
        lines.append("next: " + (f"the page has {note}, which jev-browse cannot see into; use another tool."
                                 if note else "scroll or change the page state and retry once, or describe it "
                                 "differently."))
    if d.get("candidates"):
        lines.append("candidates:")
        lines += _candidate_lines(d["candidates"])
    return "\n".join(lines)


def format_click(v):
    clicked = v.get("clicked")
    if clicked is True:
        s = f"jev_click: clicked at ({v.get('x')}, {v.get('y')})"
        if v.get("url_after"):
            s += f"; url after: {v['url_after']}"
        if v.get("popup_target_id"):
            s += f"\npopup target_id: {v['popup_target_id']} (a new tab jev-browse now owns)"
        return s
    reason = v.get("reason")
    data = v.get("data") or {}
    if clicked == "unconfirmed":
        dialog = data.get("dialog") or {}
        return ("jev_click: unconfirmed (dialog_open): the click may have run and a native dialog is now open "
                f"({_one_line(dialog.get('type'), 20)} {_json(_one_line(dialog.get('message'), 160))}). Do not click "
                "again; handle the dialog with another browser tool or ask the user.")
    if reason == "confirm_required":
        return "\n".join([
            "jev_click: not clicked (confirm_required): this is a commit control"
            + (f" ({data.get('gate')})" if data.get("gate") else "") + ".",
            f"confirm ref: {_json(_ref(data))}",
            *([f"confirm context: {_one_line(data['context'], 200)}"] if data.get("context") else []),
            "next: " + ADVICE[Reason.confirm_required]])
    if reason == "occluded":
        return "jev_click: not clicked (occluded): something covers the element. Dismiss it or scroll, then jev_find again."
    return "jev_click: not clicked (stale): the page changed since jev_find. Run jev_find again for a fresh ref."


def format_check(d):
    holds = "yes" if d.get("holds") else "no"
    p = d.get("probability", 0)
    s = f"jev_check: holds={holds} (probability {p:.2f}"
    s += "; 0.3–0.7 means unsure)" if 0.3 <= p <= 0.7 else ")"
    if d.get("evidence_line"):
        s += f"\nevidence line: {_json(_one_line(d['evidence_line'], 200))}"
    if d.get("lines_truncated"):
        s += "\n(the page was long; only its first lines were checked)"
    return s + "\nnote: a navigation aid, not proof that a task succeeded."


def format_open(v):
    return (f"jev_open: opened target_id {v.get('target_id')}: {_one_line(v.get('title'), 120)} — {v.get('url')}\n"
            "next: pass this target_id to fast_run(url=null, ...), jev_find, jev_check, and jev_click; close it with "
            "jev_close(target_id) when finished.")


def render(call_name, payload):
    """(text, is_error) for any payload a script printed."""
    kind = payload.get("kind")
    if kind == "error":
        return format_error(payload), True
    if kind == "HandBack":
        return format_handback(payload, call_name), False
    if kind == "RunResult":
        return format_run(payload), False
    if kind == "FindResult":
        return format_find(payload), False
    if kind == "CheckResult":
        return format_check(payload), False
    value = payload.get("value")
    if call_name == "jev_open" and isinstance(value, dict):
        return format_open(value), False
    if call_name == "jev_click" and isinstance(value, dict):
        return format_click(value), False
    if call_name == "jev_close":
        return f"jev_close: closed {value} tab(s)", False
    return f"{call_name}: {_json(value)}", False
