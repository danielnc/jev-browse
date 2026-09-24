"""fast_run: the autonomous sub-task runner. Loop shape ported from browser-use/jev-ultrafast
jev_ultrafast/agent.py (MIT, see NOTICE): observe → one Jev request → freshness check → execute → log → observe.
"""

import contextlib
import glob
import json
import os
import re
import secrets
import signal
import threading
import time
from pathlib import Path

from . import actions as A
from . import config, harness_api
from .candidates import candidates as goal_candidates
from .results import HandBack, Reason, RunResult, Step
from .tab import BrowserGone, DialogSuspected, NotOwned, OwnedTab, StalePage
from .textgen import TextService, is_personal, make_backend
from .textnorm import fold
from .typesafe import Client, MissingKey, RequestTooLarge, ServiceError, validate_choice, validate_noul

RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PASSWORD_LIKE = {"password"}


# ------------------------------------------------------------------------------------------------ run file
def run_dir():
    return Path(harness_api.tmp_dir())


def run_path(run_id):
    return run_dir() / f"jev-browse-run-{run_id}.json"


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, default=str)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_run_file(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def newest_run_for(target_id):
    best = None
    for p in glob.glob(str(run_dir() / "jev-browse-run-*.json")):
        data = read_run_file(p)
        if not data or data.get("target_id") != target_id:
            continue
        if best is None or data.get("started_at", 0) > best.get("started_at", 0):
            best = data
    return best


# ------------------------------------------------------------------------------------------------ confirm
def parse_confirm(confirm):
    """Entries: a confirm_required data dict / its JSON string (node-bound, preferred) or a bare label."""
    out = []
    if isinstance(confirm, (str, dict)):
        confirm = [confirm]
    for entry in confirm or ():
        if isinstance(entry, str):
            s = entry.strip()
            if s.startswith("{"):
                with contextlib.suppress(ValueError):
                    entry = json.loads(s)
        if isinstance(entry, dict) and "node" in entry:
            out.append({"node": entry["node"], "doc": str(entry.get("doc", "")), "label": entry.get("label", ""),
                        "ctx": entry.get("ctx"), "target_id": entry.get("target_id"), "used": False})
        elif isinstance(entry, str) and entry.strip():
            out.append({"label": entry.strip(), "used": False})
    return out


def _doc_origin(doc):
    return str(doc).split("|")[0]


# ------------------------------------------------------------------------------------------------ surfaces
def relevant_surfaces(surfaces, *, targeted=None):
    """Which surfaces are relevant."""
    s = surfaces or {}
    rel = []
    if s.get("password_fields") or s.get("otp_fields"):
        rel.append(Reason.auth_required)
    if targeted == "sensitive":
        rel.append(Reason.sensitive_field)
    if targeted == "upload":
        rel.append(Reason.upload)
    if any(f.get("visible") and f.get("area_ratio", 0) >= config.FRAME_AREA for f in s.get("frames") or []):
        rel.append(Reason.in_frame)
    if s.get("canvas_area_ratio", 0) >= config.CANVAS_AREA or s.get("unnamed_icon_ratio", 0) >= config.UNNAMED_ICON_RATIO:
        rel.append(Reason.visual_only)
    if any(h.get("interactive") and h.get("area_ratio", 0) >= config.SHADOW_AREA for h in s.get("shadow_hosts") or []):
        rel.append(Reason.in_shadow_dom)
    return rel


def mark_relevance(surfaces):
    s = json.loads(json.dumps(surfaces or {}))
    for f in s.get("frames") or []:
        f["relevant"] = bool(f.get("visible") and f.get("area_ratio", 0) >= config.FRAME_AREA)
    for h in s.get("shadow_hosts") or []:
        h["relevant"] = bool(h.get("interactive") and h.get("area_ratio", 0) >= config.SHADOW_AREA)
    s["canvas_relevant"] = s.get("canvas_area_ratio", 0) >= config.CANVAS_AREA or \
        s.get("unnamed_icon_ratio", 0) >= config.UNNAMED_ICON_RATIO
    s["auth_relevant"] = bool(s.get("password_fields") or s.get("otp_fields"))
    return s


def relabel(original, surfaces, targeted=None):
    rel = relevant_surfaces(surfaces, targeted=targeted)
    return (rel[0], original) if rel else (original, None)


# ------------------------------------------------------------------------------------------------ runner
class _Stop(Exception):
    def __init__(self, result):
        self.result = result


class Runner:
    def __init__(self, goal, *, run_id, values, confirm, max_actions, max_requests, timeout_s, keep_open,
                 text_backend, speculate_text, screenshot, client=None, backend=None, out=print):
        self.goal = goal
        self.run_id = run_id
        self.path = run_path(run_id)
        self.values = values
        self.confirms = parse_confirm(confirm)
        self.max_actions = max_actions
        self.max_requests = max_requests
        self.timeout_s = timeout_s
        self.keep_open = keep_open
        self.screenshot = screenshot
        self.started_wall = time.time()
        self.started = time.monotonic()
        self.deadline = self.started + timeout_s
        self.client = client
        self.backend_name = text_backend
        self.backend = backend
        self.speculate_text = speculate_text
        self.out = out
        self.tab = None
        self.svc = None
        self.history = []
        self.trace = []
        self.jev = {"jev_calls": 0, "jev_input_tokens": 0, "jev_output_tokens": 0}
        self.actions_done = 0
        self.low_streak = 0
        self.page = None
        self.by_node = {}
        self.caller_cands = []
        self.pending_confirm = None
        self.final = None

    # ---- run file ----
    def write(self, result=None, final=False):
        data = {"run_id": self.run_id, "goal": self.goal, "started_at": self.started_wall, "pid": os.getpid(),
                "final": final, "target_id": self.tab.target_id if self.tab else None,
                "history": self.history, "confirm": self.pending_confirm,
                "result": (result or self.partial()).to_dict()}
        with contextlib.suppress(OSError):
            _write_json(self.path, data)

    def partial(self):
        return RunResult(status="running", run_id=self.run_id, trace=list(self.trace), stats=self.stats(),
                         target_id=self.tab.target_id if self.tab else None)

    def stats(self):
        s = {"wall_ms": round((time.monotonic() - self.started) * 1000), **self.jev, "actions": self.actions_done}
        if self.svc:
            s.update({k: v for k, v in self.svc.stats.items()})
        else:
            s.update({"llm_calls": 0, "llm_ms_total": 0, "llm_ms_overlapped": 0, "llm_speculative_unused": 0,
                      "llm_input_tokens": 0, "llm_output_tokens": 0, "llm_cost_usd": 0.0})
        return s

    # ---- results ----
    def evidence(self, page=None, *, force_screenshot=False):
        page = page or self.page or {}
        fields = []
        for f in page.get("fields") or []:
            if not f.get("visible"):
                continue
            requested = self.by_node.get(f.get("node"))
            redact = f.get("sensitive") or f.get("personal")
            fields.append({"key": f.get("key"), "label": f.get("label"), "role": f.get("role"),
                           "value": "<redacted>" if redact else f.get("value"), "checked": f.get("checked"),
                           "matches_requested": A.matches_requested(f, requested) if f.get("personal") else None})
        ev = {"visible_text": (page.get("text") or "")[:3000], "fields": fields,
              "surfaces": mark_relevance(page.get("surfaces")), "screenshot_path": None}
        if (self.screenshot or force_screenshot) and self.tab and self.tab.session:
            with contextlib.suppress(Exception):
                path = run_dir() / f"jev-browse-shot-{self.run_id}-{len(self.trace)}.png"
                ev["screenshot_path"] = self.tab.screenshot(path)
        return ev

    def result(self, status, reason=None, detail="", data=None, page=None, force_screenshot=False):
        page = page or self.page or {}
        ev = self.evidence(page, force_screenshot=force_screenshot)
        data = dict(data or {})
        data.setdefault("surfaces", ev["surfaces"])
        if self.tab:
            data.setdefault("target_id", self.tab.target_id)
        return RunResult(status=status, reason=Reason(reason) if reason else None, detail=detail, run_id=self.run_id,
                         url=page.get("url", ""), title=page.get("title", ""), evidence=ev, trace=list(self.trace),
                         stats=self.stats(), target_id=self.tab.target_id if self.tab else None, data=data)

    def hand_back(self, reason, detail="", data=None, **kw):
        return self.result("handed_back", reason, detail, data, **kw)

    def stop(self, reason, detail="", data=None, **kw):
        raise _Stop(self.hand_back(reason, detail, data, **kw))

    # ---- Jev ----
    def ask(self, body):
        if time.monotonic() >= self.deadline:
            self.stop(Reason.budget_exhausted, "deadline reached")
        if self.jev["jev_calls"] >= self.max_requests:
            self.stop(Reason.budget_exhausted, f"request budget ({self.max_requests}) reached")
        try:
            answer = self.client.ask(body["state"], body["questions"], deadline=self.deadline)
        except RequestTooLarge as exc:
            self.stop(Reason.state_too_large, str(exc))
        except ServiceError as exc:
            if time.monotonic() >= self.deadline:
                self.stop(Reason.budget_exhausted, "deadline reached during a TypeSafe request")
            self.stop(Reason.service_error, str(exc))
        self.jev["jev_calls"] += 1
        self.jev["jev_input_tokens"] += (answer.usage or {}).get("input_tokens", 0)
        self.jev["jev_output_tokens"] += (answer.usage or {}).get("output_tokens", 0)
        return answer

    # ---- observation ----
    def observe(self):
        try:
            page = self.tab.observe(deadline=self.deadline)
        except StalePage:
            self.stop(Reason.stale_page, "the page never settled (10 observe retries)")
        except DialogSuspected:
            self._dialog_stop(executed=False)
        except BrowserGone as exc:
            self.stop(Reason.browser_error, str(exc))
        A.annotate_fields(page)
        self.page = page
        by_node, cands, hb = A.match_values(self.values, page)
        if hb:
            hb.data.setdefault("target_id", self.tab.target_id)
            raise _Stop(self.hand_back(hb.reason, hb.detail, hb.data, page=page))
        self.by_node, self.caller_cands = by_node, cands
        return page

    def _dialog_stop(self, executed):
        try:
            dialog = self.tab.dialog_open()
        except BrowserGone:
            dialog = None
        if dialog and (dialog.get("session_id") or dialog.get("ambiguous_targets")):
            self.stop(Reason.dialog_open, "a native dialog is open; handle it with the harness",
                      {"dialog": dialog, "executed": "unconfirmed" if executed else "no"})
        data = {"executed": "unconfirmed" if executed else "no"}
        if dialog:
            data["suspected_dialog"] = dialog
        self.stop(Reason.browser_error, "page unresponsive", data)

    # ---- the loop ----
    def run(self, url, target_id):
        if target_id and url is None:
            self._resume(target_id)
        elif target_id:
            self._attach(target_id)
            self._navigate(url)
        else:
            try:
                self.tab = OwnedTab.create(url, deadline=self.deadline)
            except BrowserGone as exc:
                if exc.target_id:
                    with contextlib.suppress(Exception):
                        self.tab = OwnedTab(exc.target_id)
                self.stop(Reason.browser_error, str(exc), {"target_id": exc.target_id})
            except DialogSuspected:
                self.stop(Reason.browser_error, "page unresponsive while loading")
        self.tab.snapshot_popups()
        self.write()
        self.svc = TextService(self.backend, self.client, self.speculate_text)
        page = self.observe()
        while True:
            page = self.step(page)

    def _attach(self, target_id):
        try:
            self.tab = OwnedTab.attach(target_id)
        except NotOwned as exc:
            self.stop(Reason.not_owned_tab, str(exc))
        except BrowserGone as exc:
            self.stop(Reason.browser_error, str(exc))

    def _navigate(self, url):
        try:
            self.tab.navigate(url, deadline=self.deadline)
        except (BrowserGone, DialogSuspected) as exc:
            self.stop(Reason.browser_error, str(exc))

    def _resume(self, target_id):
        prior = newest_run_for(target_id)
        if prior and not prior.get("final") and _pid_alive(prior.get("pid")) and prior.get("pid") != os.getpid():
            self.stop(Reason.browser_error, f"run in progress, pid {prior.get('pid')}")
        self._attach(target_id)
        if prior and prior.get("goal") == self.goal:
            self.history = list(prior.get("history") or [])

    def step(self, page):
        if time.monotonic() >= self.deadline:
            self.stop_budget("deadline reached")
        if not config.host_allowed(page.get("url")):
            self.stop(Reason.host_not_allowed, f"host not allowed: {page.get('url')}")
        if page.get("file_activated"):
            self.stop(Reason.upload, "a file input was activated; use the harness upload_file()")
        self.svc.on_observe(self.goal, page, deadline=self.deadline)
        req = A.fit_budget(page, self.goal, self.history, values=self.by_node, caller_candidates=self.caller_cands)
        if isinstance(req, HandBack):
            self.stop(req.reason, req.detail, req.data)
        answer = self.ask(req.body)
        answers = dict(answer.answers)
        decision_req = req
        op_override = None
        if req.stage == "stage1":
            try:
                s2, op = A.build_stage2(req, page, self.goal, self.history, answers)
            except ValueError:
                self.stop(Reason.service_error, "invalid TypeSafe response; no action executed")
            if s2 is not None:
                if not self._fresh(page):
                    return self.observe()
                a2 = self.ask(s2.body)
                answers.update(a2.answers)
                s2.value_fields = req.value_fields
                decision_req = s2
                op_override = op
        try:
            decision = A.read_decision(decision_req, answers, op_override=op_override)
        except ValueError:
            self.stop(Reason.service_error, "invalid TypeSafe response; no action executed")
        decision.value_fields = req.value_fields
        self.svc.on_decision(self.goal, page, decision, deadline=self.deadline)
        step = Step(operation=decision.operation, label=(decision.action or {}).get("label", "")[:120],
                    kind=(decision.action or {}).get("kind", ""), top3=decision.top3, confidence=decision.confidence,
                    target_confidence=decision.target_confidence, jev_ms=answer.latency_ms,
                    tokens=answer.usage, url=page.get("url", ""),
                    heads={k: {kk: vv for kk, vv in v.items() if kk != "value"} for k, v in decision.heads.items()})
        self.trace.append(step)
        # low confidence
        self.low_streak = self.low_streak + 1 if decision.confidence < config.LOW_CONFIDENCE else 0
        if self.low_streak >= config.LOW_CONFIDENCE_STREAK:
            self.stop_relabel(Reason.low_confidence, "operation confidence < 0.35 on 3 consecutive decisions")
        op = decision.operation
        if op == "DONE":
            return self._done(page, decision)
        if op == "BLOCKED":
            if not self._fresh(page):
                return self.observe()
            reason, original = relabel(Reason.blocked, page.get("surfaces"))
            status = "blocked" if reason == Reason.blocked else "handed_back"
            raise _Stop(self.result(status, reason, "Jev chose BLOCKED",
                                    {"original": original.value} if original else {},
                                    force_screenshot=reason == Reason.visual_only))
        action = decision.action
        text = None
        if op == "TYPE_TEXT":
            text = self._resolve_text(page, decision, step)
        elif op in {"CLICK", "SELECT"}:
            self._pre_click(page, decision, step)
        return self._execute(page, decision, action, text, step)

    def stop_budget(self, detail):
        self.stop_relabel(Reason.budget_exhausted, detail)

    def stop_relabel(self, reason, detail):
        r, original = relabel(reason, (self.page or {}).get("surfaces"))
        self.stop(r, detail, {"original": original.value} if original else {},
                  force_screenshot=r == Reason.visual_only)

    def _fresh(self, page, action=None):
        try:
            return self.tab.fresh(page, action)
        except StalePage:
            return False
        except DialogSuspected:
            self._dialog_stop(executed=False)
        except BrowserGone as exc:
            self.stop(Reason.browser_error, str(exc))

    # ---- DONE ----
    def _done(self, page, decision):
        if not self._fresh(page):
            return self.observe()
        for f in page.get("fields") or []:
            requested = self.by_node.get(f.get("node"))
            if f.get("personal") and f.get("visible") and A.matches_requested(f, requested) is False:
                action = next((a for a in page["actions"] if a.get("node") == f["node"] and a["kind"] == "fill"), None)
                if action is None:
                    self.stop(Reason.text_value_unavailable, "a prefilled personal field differs from the requested "
                              "value and cannot be retyped", {"fields": [self._field_entry(f, [])]})
                step = Step(operation="TYPE_TEXT", label=action["label"][:120], kind="fill", text_source="caller",
                            url=page.get("url", ""))
                self.trace.append(step)
                return self._execute(page, decision, action, requested, step)
        raise _Stop(self.result("claimed_done", None, "Jev chose DONE; verify in the tab"))

    # ---- TYPE_TEXT ----
    def _field_entry(self, f, tried):
        return {"key": f.get("key"), "label": f.get("label"), "role": f.get("role"), "candidates_tried": tried[:10]}

    def _unresolved_fields(self, page, extra_tried=None):
        out = []
        for f in page.get("fields") or []:
            if not f.get("visible") or f.get("sensitive") or f.get("readonly") or f.get("node") in self.by_node:
                continue
            if (f.get("value") or "").strip() and not f.get("personal"):
                continue
            out.append(self._field_entry(f, (extra_tried or {}).get(f.get("node"), [])))
        return out

    def _resolve_text(self, page, decision, step):
        action = decision.action
        if action.get("handback_only") or action.get("sensitive"):
            if action.get("input_type") in PASSWORD_LIKE or "one-time-code" in (action.get("autocomplete") or ""):
                self.stop(Reason.auth_required, "a password or one-time-code field; jev-browse never types into it")
            if action.get("input_type") == "file" or action.get("upload_trigger"):
                self.stop(Reason.upload, "a file input; use the harness upload_file()")
            self.stop(Reason.sensitive_field, f"sensitive field {action.get('label', '')[:60]!r}; fill it yourself")
        node = action["node"]
        field = next((f for f in page.get("fields") or [] if f.get("node") == node), {"node": node,
                                                                                         "label": action["label"]})
        if node in self.by_node:
            step.text_source = "caller"
            return self.by_node[node]
        if decision.value_index is None:  # no value heads for this field: one follow-up request
            decision = self._value_followup(page, decision, field)
        vf = getattr(decision, "value_fields", {}) or {}
        tried = {v["node"]: list(v.get("candidates") or []) for v in vf.values()}
        if decision.value is not None:
            step.text_source = "candidate"
            return decision.value
        if decision.value_source == "miss" and not is_personal(field):
            value, source, meta = self.svc.value_for(self.goal, page, field, self.deadline)
            step.llm_ms = meta.get("llm_ms", 0)
            step.llm = meta.get("llm")
            if value is not None:
                step.text_source = source
                return value
            step.text_source = "none"
            self.stop(Reason.text_value_unavailable, f"no value for {field.get('label')!r}: {meta.get('why', '')}",
                      {"fields": self._unresolved_fields(page, tried)})
        step.text_source = "none"
        self.stop(Reason.text_value_unavailable, f"the goal gives no value for {field.get('label')!r}",
                  {"fields": self._unresolved_fields(page, tried)})

    def _value_followup(self, page, decision, field):
        label = decision.action["label"][:120]
        index = decision.target
        cands = []
        if config.value_candidates_enabled():
            cands = list(self.caller_cands) + goal_candidates(self.goal, {**field, "label": label})
            cands = [c for c in dict.fromkeys(cands) if fold(c) != "none"][:config.MAX_VALUE_CANDIDATES]
        premise = f"[{index}] {label}"
        qs = {f"value_in_goal_{index}": {"type": "noul", "instructions": {"goal": self.goal, "field": premise,
                                                                          "question": A.questions.VALUE_IN_GOAL}}}
        if cands:
            qs[f"value_{index}"] = {"type": "choice", "criteria": {**{c: None for c in cands},
                                                                  "none": A.questions.VALUE_NONE},
                                    "instructions": {"goal": self.goal, "field": premise,
                                                     "question": A.questions.VALUE_CHOICE}}
        answer = self.ask({"state": {"page": {"url": page.get("url"), "title": page.get("title"),
                                              "text": (page.get("text") or "")[:3000]}}, "questions": qs})
        in_goal = choice = conf = None
        with contextlib.suppress(ValueError):
            in_goal = validate_noul(answer.answers.get(f"value_in_goal_{index}", {}))
        if cands:
            with contextlib.suppress(ValueError):
                v = validate_choice(answer.answers.get(f"value_{index}", {}), set(cands) | {"none"})
                choice, conf = v["choice"], v["confidence"]
        decision.value_index = index
        if choice not in (None, "none") and (conf or 0) >= config.VALUE_CONFIDENCE and \
                (in_goal or 0) >= config.VALUE_IN_GOAL:
            decision.value, decision.value_source = choice, "candidate"
        elif (in_goal or 0) >= config.VALUE_IN_GOAL:
            decision.value_source = "miss"
        else:
            decision.value_source = "none"
        decision.heads[index] = {"choice": choice, "confidence": conf, "in_goal": in_goal, "followup": True}
        return decision

    # ---- CLICK / SELECT: upload trigger and commit gate ----
    def _pre_click(self, page, decision, step):
        action = decision.action
        if A.upload_by_label(action):
            self.stop(Reason.upload, f"{action.get('label', '')[:60]!r} opens a file chooser; use the harness")
        if not decision.gated:
            step.commit_gate = {"gated": False}
            return
        gate = decision.gate
        info = {"gated": True, "kind": gate.kind, "verb": gate.verb, "signal": gate.signal}
        label = action.get("label", "")
        doc = f"{page.get('doc', '')}|{page.get('url', '')}"
        ref = {"target_id": self.tab.target_id, "node": action["node"], "doc": doc, "label": label,
               "ctx": action.get("ctx")}
        # caller confirm= entries: node-bound first (never falls back to its label), then bare labels
        for entry in self.confirms:
            if entry["used"] or "node" not in entry:
                continue
            if fold(entry.get("label")) != fold(label):
                continue
            if entry["node"] == action["node"] and _doc_origin(entry["doc"]) == str(page.get("doc")) and \
                    entry.get("ctx") == action.get("ctx"):
                entry["used"] = True
                info["confirmed_by"] = "caller"
                step.commit_gate = info
                return
            self._confirm_required(ref, info, step, None, detail="the confirmed element changed; confirm again")
        for entry in self.confirms:
            if entry["used"] or "node" in entry or fold(entry["label"]) != fold(label):
                continue
            same = [a for a in page["actions"] if a["kind"] == action["kind"] and fold(a.get("label")) == fold(label)
                    and not a.get("offscreen")]
            if len({a["node"] for a in same}) == 1:
                entry["used"] = True
                info["confirmed_by"] = "caller_label"
                step.commit_gate = info
                return
            info["candidates"] = [{"node": a["node"], "label": a["label"], "ctx": a.get("ctx")} for a in same][:10]
            self._confirm_required(ref, info, step, None, detail="several targets carry that label")
        context = ""
        try:
            resolved = self.tab.resolve_node(action["node"])
            context = (resolved or {}).get("context", "") or ""
        except (StalePage, BrowserGone, DialogSuspected):
            pass
        answer = self.ask({"state": {"page": {"url": page.get("url"), "title": page.get("title")}},
                           "questions": {"commit_ok": A.commit_question(self.goal, action, context)}})
        try:
            commit_ok = validate_noul(answer.answers.get("commit_ok", {}))
        except ValueError:
            self.stop(Reason.service_error, "invalid TypeSafe response; no action executed")
        info.update(commit_ok=commit_ok)
        verdict = A.authorise(gate, commit_ok, decision.confidence, decision.target_confidence)
        info["verdict"] = verdict
        step.commit_gate = info
        if verdict != "ok":
            self._confirm_required(ref, info, step, context,
                                   numbers={"commit_ok": commit_ok, "confidence": decision.confidence,
                                            "target_confidence": decision.target_confidence})

    def _confirm_required(self, ref, info, step, context, detail="the goal does not clearly authorise this "
                          "commit click", numbers=None):
        step.commit_gate = {**info, "verdict": "confirm_required"}
        safe_context = redact_context(context or "", self.page)
        self.pending_confirm = ref
        self.out(f"JEV_BROWSE_CONFIRM={json.dumps(ref)}", flush=True)
        data = {**ref, **(numbers or {}), "context": safe_context}
        if info.get("candidates"):
            data["candidates"] = info["candidates"]
        self.stop(Reason.confirm_required, f"{detail}: {ref['label'][:60]!r}"
                  + (f" — {safe_context[:120]}" if safe_context else ""), data)

    # ---- execute ----
    def _execute(self, page, decision, action, text, step):
        if action is None:
            return self.observe()
        if self.actions_done >= self.max_actions:
            self.stop_budget(f"action budget ({self.max_actions}) reached")
        if action["kind"] != "wait" and not self._fresh(page, action if action["kind"] in {"click", "select"} else None):
            self.trace[-1].executed = "no"
            return self.observe()
        try:
            self.tab.act(action, page, text=text)
        except StalePage:
            self.trace[-1].executed = "no"
            return self.observe()
        except DialogSuspected as exc:
            step.executed = "unconfirmed"
            self._log(action, text, page, None, executed="unconfirmed")
            self.write()
            self._dialog_stop(executed=exc.during_act)
        except BrowserGone as exc:
            self.stop(Reason.browser_error, str(exc))
        except RuntimeError as exc:  # an interrupted native select: never retried
            self._log(action, text, page, None, executed="unconfirmed")
            self.stop(Reason.browser_error, str(exc))
        if action["kind"] != "wait":
            self.actions_done += 1
        self._log(action, text, page, None)
        self.write()
        new = self.tab.popups() if action["kind"] in {"click", "select"} else []
        if new:
            self.stop(Reason.popup_tab, "the click opened a new tab; it is registered — continue there",
                      {"target_id": new[0], "opener": self.tab.target_id})
        nxt = self.observe()
        changed = nxt["fingerprint"] != page["fingerprint"]
        self.history[-1]["page_changed"] = changed
        self.trace[-1].page_changed = changed
        recent = self.history[-3:]
        if len(recent) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in recent):
            self.stop_relabel(Reason.no_progress, "3 consecutive actions with no page change")
        return nxt

    def _log(self, action, text, page, changed, executed="yes"):
        sensitive = bool(action.get("personal") or action.get("sensitive"))
        self.history.append({"step": len(self.history) + 1, "action": action.get("label", "")[:120],
                             "kind": action.get("kind"), "text": ("<redacted>" if sensitive and text else text),
                             "personal": sensitive, "page_changed": changed, "url": page.get("url"),
                             "executed": executed})


def redact_context(context, page):
    """Replace sensitive and personal field values in a human-readable context string with <redacted>."""
    out = context or ""
    for f in (page or {}).get("fields") or []:
        v = (f.get("value") or "").strip()
        if (f.get("sensitive") or f.get("personal")) and v and v != "<redacted>":
            out = out.replace(v, "<redacted>")
    return out[:200]


# ------------------------------------------------------------------------------------------------ entry point
def fast_run(url, goal, *, target_id=None, values=None, confirm=(), run_id=None, max_actions=None, max_requests=None,
             timeout_s=None, keep_open=True, text_backend=None, speculate_text=None, screenshot=False, _client=None,
             _backend=None, _out=print):
    """Run a well-specified browser sub-task on an owned background tab. Returns a RunResult whose status is
    claimed_done (never proof: verify in the tab), blocked, or handed_back (with a typed Reason). Budgets and the
    text settings left as None come from the config (run.*, text.*)."""
    max_actions = config.get("run.max_actions") if max_actions is None else max_actions
    max_requests = config.get("run.max_requests") if max_requests is None else max_requests
    timeout_s = config.get("run.timeout_s") if timeout_s is None else timeout_s
    speculate_text = config.get("text.speculate") if speculate_text is None else speculate_text
    if not goal or not str(goal).strip():
        raise ValueError("fast_run needs a goal")
    if url is None and not target_id:
        raise ValueError("pass a url, or url=None with target_id=<registered id> to resume")
    if run_id is None:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    if not RUN_ID.match(str(run_id)):
        raise ValueError("run_id must match [A-Za-z0-9_-]{1,64}")
    path = run_path(run_id)
    existing = read_run_file(path)
    if existing and not existing.get("final") and _pid_alive(existing.get("pid")):
        raise ValueError(f"run_id {run_id!r} belongs to a live run (pid {existing.get('pid')})")
    _out(f"JEV_BROWSE_RUN={path}", flush=True)
    runner = Runner(goal, run_id=run_id, values=values, confirm=confirm, max_actions=max_actions,
                    max_requests=max_requests, timeout_s=timeout_s, keep_open=keep_open, text_backend=text_backend,
                    speculate_text=speculate_text, screenshot=screenshot, client=_client, backend=_backend, out=_out)
    runner.write()
    hb = config.require_key() if _client is None else None
    if hb:
        result = runner.hand_back(hb.reason, hb.detail)
        runner.write(result, final=True)
        return result
    if url is not None and not config.host_allowed(url):
        result = runner.hand_back(Reason.host_not_allowed, f"host not allowed: {url}")
        runner.write(result, final=True)
        return result
    try:
        if runner.client is None:
            runner.client = Client()
    except MissingKey as exc:
        result = runner.hand_back(Reason.service_error, str(exc))
        runner.write(result, final=True)
        return result
    if runner.backend is None:
        runner.backend = make_backend(runner.backend_name)
    prewarm = getattr(runner.backend, "prewarm", None)
    if prewarm:
        with contextlib.suppress(Exception):
            prewarm()  # starts the text process before any page data exists; nothing is sent until a miss
    prev = None

    def on_sigterm(signum, frame):
        runner.final = runner.hand_back(Reason.budget_exhausted, "terminated (SIGTERM)")
        _cleanup(runner, runner.final)
        raise SystemExit(143)

    if threading.current_thread() is threading.main_thread():
        with contextlib.suppress(ValueError):
            prev = signal.signal(signal.SIGTERM, on_sigterm)
    result = None
    try:
        runner.run(url, target_id)
    except _Stop as stop:
        result = stop.result
    finally:
        if prev is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGTERM, prev)
    _cleanup(runner, result)
    if result is not None and not config.get("run.quiet"):
        _out("JEV_BROWSE_RESULT=" + json.dumps(summary(result), ensure_ascii=False), flush=True)
    return result


def summary(result, max_fields=10):
    """A compact, verify-ready line: final URL, title, visible field values (sensitive and personal redacted), and
    the start of the visible text, so the caller can check the outcome in the same script."""
    fields = [{"label": (f.get("label") or f.get("key") or "")[:60], "value": str(f.get("value") or "")[:80]}
              for f in (result.evidence or {}).get("fields") or [] if f.get("label") or f.get("key")][:max_fields]
    return {"status": result.status, "reason": result.reason.value if result.reason else None,
            "detail": (result.detail or "")[:200], "target_id": result.target_id, "url": result.url,
            "title": (result.title or "")[:120], "fields": fields,
            "text_head": ((result.evidence or {}).get("visible_text") or "")[:300]}


def _cleanup(runner, result):
    if runner.svc:
        with contextlib.suppress(Exception):
            runner.svc.close()
    else:
        from .textgen import kill_all
        kill_all()
    if result is not None:
        result.stats = runner.stats()
    if runner.tab:
        closed = False
        if result is not None and result.status == "claimed_done" and not runner.keep_open:
            with contextlib.suppress(Exception):
                runner.tab.close()
                closed = True
        if not closed:
            with contextlib.suppress(Exception):
                runner.tab.detach()
    runner.write(result, final=result is not None)
