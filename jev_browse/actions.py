"""Element table, per-operation target heads, value heads, commit gate, two-stage regions, and budgeting.

`action_space` is ported from browser-use/jev-ultrafast jev_ultrafast/model.py (MIT). See NOTICE.
The 254-options-per-Choice limit is applied here, not in the snapshot.
"""

import json
from dataclasses import dataclass, field

from . import config, questions
from .candidates import candidates as goal_candidates
from .results import HandBack, Reason
from .textnorm import any_whole_word, fold, words
from .typesafe import validate_choice, validate_noul

OPERATIONS = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
CONTROL_IDS = {"scroll_down": "SCROLL_DOWN", "scroll_up": "SCROLL_UP", "wait": "WAIT"}
REDACTED = "<redacted>"


# ------------------------------------------------------------------------------------------------ fields
def field_keys(fields):
    """Stable, unique keys: label | name/id | placeholder, + region heading on collision, + #n if still colliding."""
    base = [(f.get("label") or f.get("name_attr") or f.get("id_attr") or f.get("placeholder") or "field").strip()[:80]
            for f in fields]
    counts = {}
    for b in base:
        counts[fold(b)] = counts.get(fold(b), 0) + 1
    keyed = [f"{b} / {f['heading'][:40]}" if counts[fold(b)] > 1 and f.get("heading") else b
             for f, b in zip(fields, base, strict=True)]
    groups = {}
    for i, k in enumerate(keyed):
        groups.setdefault(fold(k), []).append(i)
    out = list(keyed)
    for idxs in groups.values():
        if len(idxs) > 1:
            for n, i in enumerate(idxs, 1):
                out[i] = f"{keyed[i]} #{n}"
    return out


def annotate_fields(page):
    """Adds `key` to every snapshot field (in place) and returns the editable, visible, non-sensitive ones."""
    fields = page.get("fields") or []
    for f, k in zip(fields, field_keys(fields), strict=True):
        f["key"] = k
    return [f for f in fields if f.get("visible") and not f.get("sensitive") and not f.get("readonly")]


def personal_state(field_, requested):
    """What Jev may see of a personal-data field: never the value."""
    value = field_.get("value") or ""
    if not value.strip():
        return "empty"
    if requested is None:
        return "filled"
    return ("filled (matches requested value)" if fold(value) == fold(requested)
            else "filled (differs from requested value)")


def matches_requested(field_, requested):
    if requested is None or not (field_.get("value") or "").strip():
        return None
    return fold(field_["value"]) == fold(requested)


def match_values(values, page):
    """Map caller `values` to field nodes. Keys are a field label (folded) or its stable `key`; indices never.

    A list/tuple of strings is not keyed: its entries become caller candidates for every field.
    Returns (by_node: dict[int, str], caller_candidates: list[str], HandBack | None).
    """
    if not values:
        return {}, [], None
    if isinstance(values, (list, tuple)):
        return {}, [str(v) for v in values if str(v).strip()], None
    visible = annotate_fields(page)
    by_node = {}
    for key, value in values.items():
        k = fold(key)
        by_key = [f for f in visible if fold(f["key"]) == k]
        if len(by_key) == 1:
            by_node[by_key[0]["node"]] = str(value)
            continue
        by_label = [f for f in visible if fold(f.get("label")) == k]
        if len(by_label) > 1:
            return {}, [], HandBack(Reason.text_value_unavailable, "ambiguous key", {
                "fields": [{"key": f["key"], "label": f.get("label"), "role": f.get("role"), "candidates_tried": []}
                           for f in by_label], "surfaces": page.get("surfaces", {})})
        if len(by_label) == 1:
            by_node[by_label[0]["node"]] = str(value)
    return by_node, [], None


# ------------------------------------------------------------------------------------------------ action space
def _sanitise_value(action, page_fields, requested):
    if action.get("sensitive") or action.get("handback_only"):
        return REDACTED
    if action.get("personal"):
        f = page_fields.get(action["node"], {"value": action.get("value", "")})
        return personal_state(f, requested.get(action["node"]))
    return action.get("value", "")


def action_space(actions, *, label_limit=120, page_fields=None, requested=None):
    """One index per observed element; each operation has its own valid target choices (ported)."""
    page_fields = page_fields or {}
    requested = requested or {}
    elements, indices, targets, controls = [], {}, {}, {}
    for action in actions:
        kind = action["kind"]
        if kind not in OPERATIONS:
            controls[CONTROL_IDS.get(action["id"], action["id"].upper())] = action
            continue
        if action.get("offscreen"):
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "checked", "selected", "expanded") if k in action}
            element["value"] = _sanitise_value(action, page_fields, requested)
            element.update(index=index, label=action["label"].split(" → ")[0][:label_limit], operations=[])
            if action.get("handback_only"):
                element["note"] = "sensitive field: this tool never types into it"
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = OPERATIONS[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"][:label_limit],
                                       "value": action["value"]})
        group[target] = action
    return elements, targets, controls


# ------------------------------------------------------------------------------------------------ commit gate
@dataclass
class Gate:
    gated: bool
    kind: str | None = None  # verb | structural
    verb: str | None = None
    signal: str | None = None


def gate_for(action):
    """Is this CLICK/SELECT target a commit? Whole-word folded verbs, or structural signals."""
    label = action.get("label", "").split(" → ")[-1] if action.get("kind") == "select" else action.get("label", "")
    verb = any_whole_word(config.commit_verbs(), label)
    if verb:
        return Gate(True, "verb", verb=verb)
    if action.get("submit") and (action.get("form_sensitive") or action.get("form_personal")):
        return Gate(True, "structural", signal="submit in a form with sensitive or personal data")
    if action.get("in_dialog") and fold(label) in config.generic_confirmations():
        return Gate(True, "structural", signal="generic confirmation in a dialog")
    if action.get("unnamed") and action.get("role") in {"button", "link"}:
        return Gate(True, "structural", signal="unlabelled icon-only button")
    return Gate(False)


def authorise(gate, commit_ok, op_confidence, target_confidence, confirmed=False):
    if not gate.gated:
        return "ok"
    if (confirmed or (commit_ok is not None and commit_ok >= config.COMMIT_OK)) and \
            op_confidence >= config.COMMIT_CONFIDENCE and (target_confidence or 0) >= config.COMMIT_CONFIDENCE:
        return "ok"
    return "confirm_required"


def commit_question(goal, action, context=""):
    gate = gate_for(action)
    label = action.get("label") or "(unlabelled icon button)"
    if gate.kind == "structural":
        return {"type": "noul", "instructions": {"goal": goal, "dialog": (context or "")[:400], "button": label,
                                                 "question": questions.COMMIT_STRUCTURAL},
                "criteria": questions.COMMIT_STRUCTURAL_CRITERIA}
    return {"type": "noul", "instructions": {"goal": goal, "button": label, "question": questions.COMMIT_VERB},
            "criteria": questions.COMMIT_VERB_CRITERIA}


# ------------------------------------------------------------------------------------------------ regions
@dataclass
class Region:
    id: str
    heading: str
    first_labels: list
    action_ids: list = field(default_factory=list)


def regions(page, action_ids=None):
    """Group actions by region, then split any region with > 254 by preceding heading, then chunks of ≤ 254."""
    info = page.get("regions") or {}
    groups = {}
    for a in page["actions"]:
        if a["kind"] not in OPERATIONS or a.get("offscreen"):
            continue
        if action_ids is not None and a["id"] not in action_ids:
            continue
        groups.setdefault(a.get("region", "r0"), []).append(a)
    out = []
    for rid, acts in groups.items():
        meta = info.get(rid, {})
        heading = meta.get("heading", "")
        if len(acts) <= config.MAX_OPTIONS:
            out.append(Region(rid, heading, [a["label"][:60] for a in acts[:3]], [a["id"] for a in acts]))
            continue
        by_heading = {}
        for a in acts:
            by_heading.setdefault(a.get("heading") or "", []).append(a)
        n = 0
        for sub_heading, sub in by_heading.items():
            for start in range(0, len(sub), config.MAX_OPTIONS):
                chunk = sub[start:start + config.MAX_OPTIONS]
                n += 1
                out.append(Region(f"{rid}.{n}", sub_heading or heading,
                                  [a["label"][:60] for a in chunk[:3]], [a["id"] for a in chunk]))
    return out


def _region_criteria(regs):
    return {r.id: {"heading": r.heading, "first_labels": r.first_labels, "size": len(r.action_ids)} for r in regs}


# ------------------------------------------------------------------------------------------------ request
@dataclass
class Request:
    body: dict
    targets: dict
    controls: dict
    value_fields: dict  # head index -> {"node", "label", "key", "candidates", "personal"}
    stage: str = "single"  # single | stage1 | stage2
    regions: dict = field(default_factory=dict)  # op -> {region id -> [action ids]}
    elements: list = field(default_factory=list)
    operations: dict = field(default_factory=dict)


def _state(page, goal, history, elements, text_limit):
    recent = []
    for h in history[-10:]:
        text = h.get("text")
        if h.get("personal") or h.get("sensitive"):
            text = REDACTED if text else text
        recent.append({"action": h.get("action"), "kind": h.get("kind"), "text": text,
                       "page_changed": h.get("page_changed")})
    return {"page": {"url": page.get("url"), "title": page.get("title"), "text": (page.get("text") or "")[:text_limit]},
            "elements": elements, "recent_actions": recent}


def build_request(page, goal, history, *, values=None, caller_candidates=(), value_heads=None,
                  candidates_enabled=None, text_limit=6000, label_limit=120, cand_limit=None, today=None,
                  exclude_nodes=()):
    """One request per decision: operation, per-operation targets (≤ 254), and value heads."""
    value_heads = config.value_heads() if value_heads is None else value_heads
    candidates_enabled = config.value_candidates_enabled() if candidates_enabled is None else candidates_enabled
    cand_limit = config.MAX_VALUE_CANDIDATES if cand_limit is None else cand_limit
    requested = dict(values or {})
    page_fields = {f["node"]: f for f in page.get("fields") or []}
    annotate_fields(page)
    if select_too_large(page):
        raise TooManyOptions("a native <select> has more than 254 options")
    elements, targets, controls = action_space(page["actions"], label_limit=label_limit, page_fields=page_fields,
                                               requested=requested)
    operations = {k: questions.OPERATION_LABELS[k] for k in targets}
    for key, action in controls.items():
        operations[key] = questions.OPERATION_LABELS.get(key, action.get("label", key))
    operations["DONE"] = questions.OPERATION_LABELS["DONE"]
    operations["BLOCKED"] = questions.OPERATION_LABELS["BLOCKED"]
    qs = {"operation": {"type": "choice", "criteria": operations,
                        "instructions": {"goal": goal, "rules": questions.NEXT_ACTION}}}
    req = Request(body={}, targets=targets, controls=controls, value_fields={}, elements=elements,
                  operations=operations)
    for operation, cands in targets.items():
        if len(cands) > config.MAX_OPTIONS:
            regs = regions(page, action_ids={a["id"] for a in cands.values()})
            if len(regs) > config.MAX_OPTIONS:
                raise TooManyOptions(f"{operation}: {len(regs)} regions")
            req.stage = "stage1"
            req.regions[operation] = {r.id: r.action_ids for r in regs}
            qs[operation.lower() + "_region"] = {
                "type": "choice", "criteria": _region_criteria(regs),
                "instructions": {"goal": goal, "operation": operation, "rules": [questions.NEXT_ACTION,
                                                                                 questions.REGION]}}
            continue
        qs[operation.lower() + "_target"] = _target_head(goal, operation, cands, label_limit, page_fields, requested)
    # value heads: visible, editable, non-sensitive fields with no caller value, in document order
    fill = targets.get("TYPE_TEXT", {})
    head_i = 0
    for index, action in fill.items():
        if head_i >= value_heads:
            break
        if action.get("sensitive") or action.get("handback_only") or action["node"] in requested \
                or action["node"] in exclude_nodes:
            continue
        head_i += 1
        f = page_fields.get(action["node"], {})
        label = action["label"][:label_limit]
        cands = []
        if candidates_enabled:
            cands = list(caller_candidates) + goal_candidates(goal, {**f, "label": label}, limit=cand_limit,
                                                              today=today)
            cands = [c for c in dict.fromkeys(cands) if fold(c) != "none"][:cand_limit]
        premise = f"[{index}] {label}"
        if cands:
            qs[f"value_{index}"] = {"type": "choice", "criteria": {**{c: None for c in cands},
                                                                  "none": questions.VALUE_NONE},
                                    "instructions": {"goal": goal, "field": premise,
                                                     "question": questions.VALUE_CHOICE}}
        qs[f"value_in_goal_{index}"] = {"type": "noul", "instructions": {"goal": goal, "field": premise,
                                                                         "question": questions.VALUE_IN_GOAL}}
        req.value_fields[index] = {"node": action["node"], "label": label, "key": f.get("key"),
                                   "candidates": cands, "personal": bool(action.get("personal"))}
    req.body = {"state": _state(page, goal, history, elements, text_limit), "questions": qs}
    return req


def _target_head(goal, operation, cands, label_limit, page_fields, requested):
    criteria = {}
    for index, a in cands.items():
        entry = {"element": f"[{index}] {a['label'][:label_limit]}",
                 "current_value": a.get("current_value", _sanitise_value(a, page_fields, requested))}
        entry.update({k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a})
        if a.get("handback_only"):
            entry["note"] = "sensitive field: this tool never types into it"
        criteria[index] = entry
    return {"type": "choice", "criteria": criteria,
            "instructions": {"goal": goal, "operation": operation,
                             "rules": [questions.NEXT_ACTION, questions.TARGET]}}


class TooManyOptions(ValueError):
    pass


def build_stage2(request, page, goal, history, stage1_answers, *, label_limit=120):
    """Only the chosen operation's target head, restricted to the chosen region."""
    op = validate_choice(stage1_answers.get("operation", {}), request.operations)["choice"]
    rq = request.regions.get(op)
    if not rq:
        return None, op
    region = validate_choice(stage1_answers.get(op.lower() + "_region", {}), rq)["choice"]
    ids = set(rq[region])
    cands = {i: a for i, a in request.targets[op].items() if a["id"] in ids}
    page_fields = {f["node"]: f for f in page.get("fields") or []}
    head = _target_head(goal, op, cands, label_limit, page_fields, {})
    body = {"state": request.body["state"], "questions": {op.lower() + "_target": head}}
    return Request(body=body, targets={op: cands}, controls=request.controls, value_fields={}, stage="stage2",
                   elements=request.elements, operations=request.operations), op


# ------------------------------------------------------------------------------------------------ decision
@dataclass
class Decision:
    operation: str
    action_id: str | None
    action: dict | None
    target: str | None
    confidence: float
    target_confidence: float | None
    probabilities: dict
    value: str | None = None
    value_source: str | None = None  # candidate | miss | none
    miss_fields: list = field(default_factory=list)  # [(index, node, label, key)]
    gated: bool = False
    gate: Gate | None = None
    heads: dict = field(default_factory=dict)
    top3: list = field(default_factory=list)
    value_index: str | None = None


def value_heads_result(request, answers):
    """Per value head: (choice, confidence, in_goal, value_or_None). Unused heads cannot cause an action."""
    out = {}
    for index, vf in request.value_fields.items():
        in_goal = None
        ans = answers.get(f"value_in_goal_{index}")
        if ans is not None:
            try:
                in_goal = validate_noul(ans)
            except ValueError:
                in_goal = None
        choice = conf = None
        value = None
        vans = answers.get(f"value_{index}")
        if vans is not None and vf["candidates"]:
            try:
                vans = validate_choice(vans, set(vf["candidates"]) | {"none"})
                choice, conf = vans["choice"], vans["confidence"]
            except ValueError:
                choice = conf = None
        if choice not in (None, "none") and (conf or 0) >= config.VALUE_CONFIDENCE and \
                (in_goal or 0) >= config.VALUE_IN_GOAL:
            value = choice
        out[index] = {"choice": choice, "confidence": conf, "in_goal": in_goal, "value": value}
    return out


def read_decision(request, answers, *, op_override=None):
    op_answer = validate_choice(answers.get("operation", {}), request.operations)
    operation = op_override or op_answer["choice"]
    target = target_answer = action = None
    probabilities = {}
    if operation in request.targets:
        head = answers.get(operation.lower() + "_target", {})
        target_answer = validate_choice(head, request.targets[operation])
        target = target_answer["choice"]
        action = request.targets[operation][target]
        probabilities = {a["id"]: target_answer["probabilities"][i] for i, a in request.targets[operation].items()}
    elif operation in request.controls:
        action = request.controls[operation]
        probabilities = {action["id"]: op_answer["probabilities"][operation]}
    else:
        probabilities = {operation: op_answer["probabilities"][operation]}
    heads = value_heads_result(request, answers)
    miss = []
    for index, h in heads.items():
        vf = request.value_fields[index]
        if (h["in_goal"] or 0) >= config.VALUE_IN_GOAL and h["value"] is None and not vf["personal"]:
            miss.append((index, vf["node"], vf["label"], vf["key"]))
    d = Decision(operation=operation, action_id=action["id"] if action else None, action=action, target=target,
                 confidence=op_answer["confidence"],
                 target_confidence=target_answer["confidence"] if target_answer else None,
                 probabilities=probabilities, miss_fields=miss, heads=heads,
                 top3=sorted(probabilities.items(), key=lambda kv: -kv[1])[:3])
    if operation == "TYPE_TEXT" and target in request.value_fields:
        h = heads[target]
        d.value_index = target
        if h["value"] is not None:
            d.value, d.value_source = h["value"], "candidate"
        elif (h["in_goal"] or 0) >= config.VALUE_IN_GOAL:
            d.value_source = "miss"
        else:
            d.value_source = "none"
    if operation in {"CLICK", "SELECT"} and action is not None:
        d.gate = gate_for(action)
        d.gated = d.gate.gated
    return d


# ------------------------------------------------------------------------------------------------ budgeting
def estimate_tokens(body):
    """(longest, total): state + longest question, state + all questions; len(json)/3.5."""
    state = len(json.dumps(body["state"], ensure_ascii=False)) / 3.5
    sizes = [len(json.dumps(q, ensure_ascii=False)) / 3.5 for q in body["questions"].values()] or [0]
    return int(state + max(sizes)), int(state + sum(sizes))


def fits(body):
    longest, total = estimate_tokens(body)
    return longest <= config.LONGEST_BUDGET and total <= config.TOTAL_BUDGET


TRIM_STEPS = [
    {"text_limit": 3000}, {"text_limit": 1500}, {"label_limit": 60}, {"cand_limit": 15},
    {"value_heads": 2}, {"value_heads": 0},
]


def fit_budget(page, goal, history, **kw):
    """Build the request, trimming text → labels → candidates → heads until both limits fit."""
    opts = dict(kw)
    try:
        req = build_request(page, goal, history, **opts)
    except TooManyOptions as exc:
        return HandBack(Reason.too_many_options, str(exc), {"surfaces": page.get("surfaces", {})})
    trimmed = []
    for step in TRIM_STEPS:
        if fits(req.body):
            break
        opts.update(step)
        trimmed.append(step)
        req = build_request(page, goal, history, **opts)
    if not fits(req.body):
        longest, total = estimate_tokens(req.body)
        return HandBack(Reason.state_too_large, f"~{longest} / ~{total} tokens after trimming",
                        {"surfaces": page.get("surfaces", {})})
    req.trimmed = trimmed
    return req


def select_too_large(page):
    """A native <select> whose non-selected options exceed 254."""
    return any(a.get("option_count", 0) > config.MAX_OPTIONS for a in page["actions"] if a["kind"] == "select")


def upload_by_label(action):
    return bool(action.get("upload_trigger")) or bool(
        any_whole_word(["upload", "attach", "anexar", "enviar arquivo", "carregar"], action.get("label", "")))


__all__ = ["action_space", "build_request", "build_stage2", "read_decision", "fit_budget", "regions", "gate_for",
           "authorise", "commit_question", "match_values", "field_keys", "annotate_fields", "estimate_tokens",
           "words"]
