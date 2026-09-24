"""Typed results: the Reason enum, HandBack, Step, RunResult, FindResult, CheckResult.

Every `repr` is compact and carries no field value, evidence text, or confirm JSON: browser-harness telemetry
sends helper-argument reprs off the machine.
"""

import enum
import json
from dataclasses import asdict, dataclass, field


class Reason(str, enum.Enum):
    blocked = "blocked"
    not_owned_tab = "not_owned_tab"
    too_many_options = "too_many_options"
    state_too_large = "state_too_large"
    sensitive_field = "sensitive_field"
    auth_required = "auth_required"
    upload = "upload"
    in_frame = "in_frame"
    visual_only = "visual_only"
    in_shadow_dom = "in_shadow_dom"
    popup_tab = "popup_tab"
    confirm_required = "confirm_required"
    text_value_unavailable = "text_value_unavailable"
    dialog_open = "dialog_open"
    browser_error = "browser_error"
    low_confidence = "low_confidence"
    no_progress = "no_progress"
    budget_exhausted = "budget_exhausted"
    stale_page = "stale_page"
    host_not_allowed = "host_not_allowed"
    service_error = "service_error"

    def __str__(self):
        return self.value


# What the caller should do, grouped by action.
CALLER_ACTION = {
    "take_over": {Reason.blocked, Reason.too_many_options, Reason.state_too_large, Reason.sensitive_field,
                  Reason.auth_required, Reason.upload, Reason.in_frame, Reason.visual_only, Reason.in_shadow_dom,
                  Reason.dialog_open, Reason.low_confidence, Reason.no_progress, Reason.budget_exhausted},
    "resume_with_values": {Reason.text_value_unavailable},
    "confirm_then_resume": {Reason.confirm_required},
    "reopen": {Reason.not_owned_tab, Reason.browser_error, Reason.popup_tab, Reason.host_not_allowed},
    "retry_later": {Reason.stale_page, Reason.service_error},
}

_REPR_LIMIT = 200


def _clip(s, n=_REPR_LIMIT):
    return s if len(s) <= n else s[: n - 3] + "..."


@dataclass(repr=False)
class HandBack:
    reason: Reason
    detail: str = ""
    data: dict = field(default_factory=dict)
    status: str = "handed_back"

    def __post_init__(self):
        self.reason = Reason(self.reason)
        self.data.setdefault("surfaces", {})

    def __repr__(self):
        return _clip(f"HandBack(reason={self.reason.value}, data_keys={sorted(self.data)[:8]})")

    def to_dict(self):
        return {"status": self.status, "reason": self.reason.value, "detail": self.detail, "data": self.data}


@dataclass(repr=False)
class Step:
    operation: str
    label: str = ""
    kind: str = ""
    top3: list = field(default_factory=list)
    confidence: float | None = None
    target_confidence: float | None = None
    jev_ms: int = 0
    tokens: dict = field(default_factory=dict)
    text_source: str = "none"
    llm_ms: int = 0
    commit_gate: dict | None = None
    page_changed: bool | None = None
    url: str = ""
    executed: str = "yes"  # yes | unconfirmed
    llm: dict | None = None  # {backend, model, fallback} when a text backend answered
    heads: dict = field(default_factory=dict)  # value-head probabilities, for recalibration

    def __repr__(self):
        return _clip(f"Step(operation={self.operation}, source={self.text_source}, jev_ms={self.jev_ms})")

    def to_dict(self):
        return asdict(self)


@dataclass(repr=False)
class RunResult:
    status: str
    reason: Reason | None = None
    detail: str = ""
    run_id: str = ""
    url: str = ""
    title: str = ""
    evidence: dict = field(default_factory=dict)
    trace: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    target_id: str | None = None
    data: dict = field(default_factory=dict)

    def __repr__(self):
        reason = f", reason={self.reason.value}" if self.reason else ""
        return _clip(f"RunResult(status={self.status}{reason}, steps={len(self.trace)}, "
                     f"wall_ms={self.stats.get('wall_ms')}, target_id={self.target_id})")

    def to_dict(self):
        return {
            "status": self.status, "reason": self.reason.value if self.reason else None, "detail": self.detail,
            "run_id": self.run_id, "url": self.url, "title": self.title, "evidence": self.evidence,
            "trace": [s.to_dict() if isinstance(s, Step) else s for s in self.trace], "stats": self.stats,
            "target_id": self.target_id, "data": self.data,
        }


REF_KEYS = ("target_id", "node", "doc", "label", "ctx")


def ref_of(element):
    """The JSON-safe cross-process reference {target_id, node, doc, label, ctx}, nothing else."""
    return {k: element[k] for k in REF_KEYS}


@dataclass(repr=False)
class FindResult:
    outcome: str  # found | ambiguous | not_found
    element: dict | None = None
    candidates: list = field(default_factory=list)
    exists: float = 0.0
    confidence: float = 0.0
    surfaces: dict = field(default_factory=dict)
    target_id: str | None = None
    data: dict = field(default_factory=dict)
    status: str = "ok"

    def ref(self):
        if not self.element:
            raise ValueError(f"no element to reference (outcome={self.outcome})")
        return ref_of(self.element)

    def ref_json(self):
        return json.dumps(self.ref())

    def __getitem__(self, key):
        """A found result can be passed where an element dict is expected."""
        if not self.element:
            raise KeyError(key)
        return self.element[key]

    def __repr__(self):
        return _clip(f"FindResult(outcome={self.outcome}, exists={self.exists:.2f}, "
                     f"confidence={self.confidence:.2f}, candidates={len(self.candidates)})")


@dataclass(repr=False)
class CheckResult:
    probability: float
    holds: bool
    evidence_line: str | None = None
    lines_truncated: bool = False
    status: str = "ok"

    def __repr__(self):
        return _clip(f"CheckResult(holds={self.holds}, probability={self.probability:.2f}, "
                     f"lines_truncated={self.lines_truncated})")
