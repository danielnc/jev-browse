"""A stand-in for the `browser-harness` CLI in MCP round-trip tests: it reads a script on stdin and execs it with
fake jev-browse helpers in its globals, as the real harness does with the agent_helpers block. The helpers return
real result objects and fast_run writes a real run file, so the bridge is exercised end to end without Chrome.

FAKE_HARNESS_MODE: done | slow | confirm | values | raise | not_installed | harness_error.
FAKE_HARNESS_CALLS: a file that receives one JSON line per helper call (name, args, kwargs, owner).
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jev_browse import run as runmod  # noqa: E402
from jev_browse.results import CheckResult, FindResult, HandBack, Reason, RunResult, Step  # noqa: E402

MODE = os.environ.get("FAKE_HARNESS_MODE", "done")
URL = "https://example.com/form"


def record(name, args, kwargs):
    path = os.environ.get("FAKE_HARNESS_CALLS")
    if path:
        with open(path, "a") as f:
            f.write(json.dumps({"call": name, "args": list(args), "kwargs": kwargs,
                                "owner": os.environ.get("JEV_BROWSE_OWNER")}) + "\n")


def evidence():
    return {"visible_text": "Thanks! Your message was sent.   We reply within a day.",
            "fields": [{"key": "Topic", "label": "Topic", "role": "textbox", "value": "Billing", "checked": None,
                        "matches_requested": None},
                       {"key": "Email", "label": "Email", "role": "textbox", "value": "<redacted>", "checked": None,
                        "matches_requested": True}],
            "surfaces": {}, "screenshot_path": None}


def fast_run(url, goal, *, run_id, **kwargs):
    record("fast_run", (url, goal), {"run_id": run_id, **kwargs})
    print(f"JEV_BROWSE_RUN={runmod.run_path(run_id)}", flush=True)
    target = kwargs.get("target_id") or "T1"
    started = time.time()
    trace = [Step("TYPE_TEXT", label="Topic", text_source="caller"), Step("CLICK", label="Next")]

    def write(result, final):
        runmod._write_json(runmod.run_path(run_id), {
            "run_id": run_id, "goal": goal, "started_at": started, "pid": os.getpid(), "final": final,
            "target_id": target, "history": [], "confirm": None, "result": result.to_dict()})

    write(RunResult(status="running", run_id=run_id, target_id=target, trace=trace[:1]), False)
    if MODE == "slow":
        time.sleep(float(os.environ.get("FAKE_HARNESS_SLEEP", "2")))
    if MODE == "raise":
        raise ValueError("run_id 'x' belongs to a live run (pid 1)")
    common = {"run_id": run_id, "url": URL, "title": "Contact", "target_id": target, "trace": trace,
              "stats": {"wall_ms": 4200, "jev_calls": 3, "actions": 2, "llm_calls": 0}}
    if MODE == "confirm":
        ref = {"target_id": target, "node": 12, "doc": f"1000.5|{URL}", "label": "Send", "ctx": "c12"}
        result = RunResult(status="handed_back", reason=Reason.confirm_required,
                           detail="the goal does not clearly authorise this commit click: 'Send'",
                           evidence={**evidence(), "visible_text": "Contact us"},
                           data={**ref, "commit_ok": 0.2, "context": "Send message", "surfaces": {}}, **common)
    elif MODE == "values":
        result = RunResult(status="handed_back", reason=Reason.text_value_unavailable,
                           detail="the goal gives no value for 'Order number'", evidence=evidence(),
                           data={"fields": [{"key": "Order number", "label": "Order number", "role": "textbox",
                                             "candidates_tried": ["Billing"]}], "surfaces": {}}, **common)
    else:
        result = RunResult(status="claimed_done", detail="Jev chose DONE; verify in the tab", evidence=evidence(),
                           data={"surfaces": {}}, **common)
    write(result, True)
    return result


def jev_open(url):
    record("jev_open", (url,), {})
    return {"target_id": "T9", "url": url, "title": "Example Domain"}


def jev_find(description, *, k=3, scroll=True, target_id=None):
    record("jev_find", (description,), {"k": k, "scroll": scroll, "target_id": target_id})
    el = {"target_id": target_id, "node": 7, "doc": f"1000.5|{URL}", "label": "Search", "ctx": "c7",
          "role": "button", "value": "", "x": 10, "y": 20, "rect": None, "offscreen": False}
    return FindResult("found", element=el, candidates=[{**el, "probability": 0.97}], exists=0.99, confidence=0.97,
                      target_id=target_id)


def jev_click(found, *, target_id=None, confirm=False):
    record("jev_click", (found,), {"target_id": target_id, "confirm": confirm})
    if isinstance(found, str):
        found = json.loads(found)
    if found.get("label") == "Send" and not confirm:
        return {"clicked": False, "reason": "confirm_required", "data": {**found, "context": "Send", "gate": "verb"}}
    return {"clicked": True, "x": 10, "y": 20, "url_after": URL + "?q=1"}


def jev_check(condition, *, target_id=None):
    record("jev_check", (condition,), {"target_id": target_id})
    if target_id == "gone":
        return HandBack(Reason.not_owned_tab, "target gone is not owned by jev-browse", {"target_id": "gone"})
    return CheckResult(probability=0.91, holds=True, evidence_line="Results for Lisbon")


def jev_close(target_id=None, *, all_owned=False, force=False):
    record("jev_close", (target_id,), {"all_owned": all_owned})
    if target_id not in (None, "T1", "T9"):
        raise ValueError(f"target {target_id} is not registered with jev-browse; refusing to close it")
    return 1


def main():
    code = sys.stdin.read()
    if MODE == "harness_error":
        print("browser-harness: Chrome remote debugging is not enabled", file=sys.stderr)
        sys.exit(1)
    scope = {"__name__": "__main__"}
    if MODE != "not_installed":
        scope.update(fast_run=fast_run, jev_open=jev_open, jev_find=jev_find, jev_click=jev_click,
                     jev_check=jev_check, jev_close=jev_close)
    exec(code, scope)


if __name__ == "__main__":
    main()
