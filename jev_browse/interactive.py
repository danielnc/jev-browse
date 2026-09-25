"""Interactive helpers: jev_open, jev_adopt, jev_find, jev_click, jev_check, jev_close.

Every read and click goes through an explicit per-call session on a registered target. None of these calls
`switch_tab`, and none reads through the daemon's shared current session.
"""

import contextlib
import functools
import json
import time

from . import actions as A
from . import config, harness_api, questions
from .results import CheckResult, FindResult, HandBack, Reason
from .run import newest_run_for, redact_context
from .tab import BrowserGone, DialogSuspected, NotOwned, OwnedTab, Registry, StalePage, live_targets
from .textnorm import fold
from .typesafe import Client, RequestTooLarge, ServiceError, validate_choice, validate_noul

_client = None


def _jev():
    global _client
    if _client is None:
        _client = Client()
    return _client


def _hb(reason, detail="", **data):
    return HandBack(reason, detail, data)


# ------------------------------------------------------------------------------------------------ targets
def _registry():
    return Registry(harness_api.socket_dir(), harness_api.daemon_name())


def _resolve(target_id):
    """An attached OwnedTab for an explicit or implicit (sole, current, registered) target, or a HandBack."""
    reg = _registry()
    try:
        owned = reg.live(live_targets())
    except BrowserGone as exc:
        return _hb(Reason.browser_error, str(exc))
    if target_id is None:
        try:
            current = harness_api.current_tab().get("targetId")
        except Exception:
            current = None
        if len(owned) > 1:
            return _hb(Reason.not_owned_tab, "pass target_id")
        if current not in owned:
            return _hb(Reason.not_owned_tab, "no owned tab; jev_open(url) first, or pass target_id")
        target_id = current
    if target_id not in owned:
        return _hb(Reason.not_owned_tab, f"target {target_id} is not owned by jev-browse; jev_open(url) or "
                   "jev_adopt(<new_tab id>)", target_id=target_id)
    try:
        return OwnedTab.attach(target_id, registry=reg)
    except NotOwned as exc:
        return _hb(Reason.not_owned_tab, str(exc), target_id=target_id)
    except BrowserGone as exc:
        return _hb(Reason.browser_error, str(exc), target_id=target_id)


def _host_ok(tab):
    try:
        url = tab.target_url()
    except BrowserGone as exc:
        return _hb(Reason.browser_error, str(exc), target_id=tab.target_id)
    if not config.host_allowed(url):
        return _hb(Reason.host_not_allowed, f"host not allowed: {url}", target_id=tab.target_id)
    return None


def _observe(tab, **kw):
    """Snapshot on the owned session, then verify its href against Target.getTargetInfo before any send."""
    for _ in range(2):
        page = tab.observe(**kw)
        if page.get("href") == tab.target_url():
            A.annotate_fields(page)
            return page
        time.sleep(0.05)
    raise StalePage("target URL changed during observation")


def _guard(fn):
    """Map browser/service failures to HandBacks; always detach this call's per-call session."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        holder = []
        try:
            return fn(*args, _tab_holder=holder, **kwargs)
        except _Early as early:
            return early.value
        except DialogSuspected:
            tab = holder[0] if holder else None
            dialog = None
            with contextlib.suppress(Exception):
                dialog = tab.dialog_open() if tab else None
            if dialog and dialog.get("session_id"):
                return _hb(Reason.dialog_open, "a native dialog is open; handle it with the harness", dialog=dialog,
                           target_id=tab.target_id)
            return _hb(Reason.browser_error, "page unresponsive", suspected_dialog=dialog)
        except StalePage as exc:
            return _hb(Reason.stale_page, str(exc))
        except BrowserGone as exc:
            return _hb(Reason.browser_error, str(exc), target_id=exc.target_id)
        except RequestTooLarge as exc:
            return _hb(Reason.state_too_large, str(exc))
        except ServiceError as exc:
            return _hb(Reason.service_error, str(exc))
        except ValueError as exc:
            if "Invalid TypeSafe" not in str(exc):
                raise
            return _hb(Reason.service_error, str(exc))
        finally:
            for t in holder:
                with contextlib.suppress(Exception):
                    t.detach()
    return wrapper


class _Early(Exception):
    def __init__(self, value):
        self.value = value


def _need(value):
    if isinstance(value, HandBack):
        raise _Early(value)
    return value


def _preflight():
    hb = config.require_key()
    if hb:
        raise _Early(hb)


# ------------------------------------------------------------------------------------------------ open / adopt / close
@_guard
def jev_open(url, *, _tab_holder=None):
    """Open `url` in a new background tab jev-browse owns. Returns {target_id, url, title} (never current)."""
    _preflight()
    if not config.host_allowed(url):
        return _hb(Reason.host_not_allowed, f"host not allowed: {url}")
    try:
        tab = OwnedTab.create(url)
    except BrowserGone as exc:
        return _hb(Reason.browser_error, str(exc), target_id=exc.target_id)
    _tab_holder.append(tab)
    info = _call_info(tab.target_id)
    return {"target_id": tab.target_id, "url": info.get("url", url), "title": info.get("title", "")}


def _call_info(target_id):
    from .tab import _call

    return _call("Target.getTargetInfo", targetId=target_id).get("targetInfo", {})


@_guard
def jev_adopt(target_id, *, _tab_holder=None):
    """Take over a tab you created with the harness's new_tab(): name its id explicitly. Returns
    {target_id, url, title}, or a HandBack (not_owned_tab for any tab new_tab() did not create)."""
    _preflight()
    if not target_id:
        return _hb(Reason.not_owned_tab, "pass the target id new_tab() returned")
    try:
        info = _call_info(target_id)
    except BrowserGone as exc:
        return _hb(Reason.browser_error, str(exc), target_id=target_id)
    if not config.host_allowed(info.get("url")):
        return _hb(Reason.host_not_allowed, f"host not allowed: {info.get('url')}", target_id=target_id)
    try:
        tab = OwnedTab.adopt(target_id, registry=_registry(),
                             created=Registry(harness_api.socket_dir(), harness_api.daemon_name(), "created"))
    except NotOwned as exc:
        return _hb(Reason.not_owned_tab, str(exc), target_id=target_id)
    _tab_holder.append(tab)
    return {"target_id": target_id, "url": info.get("url", ""), "title": info.get("title", "")}


def jev_close(target_id=None, *, all_owned=False, force=False):
    """Close an owned tab by id (returns 1), or with all_owned=True this owner's tabs (force=True: every owner's),
    skipping any target with a live, unfinished fast_run. Refuses unregistered targets."""
    reg = _registry()
    try:
        entries = reg.live(live_targets())
    except BrowserGone:
        entries = reg.entries()
    if target_id is not None:
        if target_id not in entries:
            raise ValueError(f"target {target_id} is not registered with jev-browse; refusing to close it")
        OwnedTab(target_id, reg).close()
        return 1
    if not all_owned:
        raise ValueError("pass target_id, or all_owned=True for end-of-session cleanup")
    owner = config.owner_tag()
    closed = 0
    for tid, entry in list(entries.items()):
        if not force and entry.get("owner") != owner:
            continue
        run = newest_run_for(tid)
        if run and not run.get("final") and _alive(run.get("pid")):
            continue
        with contextlib.suppress(Exception):
            OwnedTab(tid, reg).close()
            closed += 1
    return closed


def _alive(pid):
    import os

    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


# ------------------------------------------------------------------------------------------------ find
def _candidates(page):
    """One candidate per element (select options collapse to their select), scroll/wait controls excluded."""
    out, seen = [], set()
    for a in page["actions"]:
        if a["kind"] not in A.OPERATIONS or a["node"] in seen:
            continue
        seen.add(a["node"])
        label = a["label"].split(" → ")[0]
        value = "<redacted>" if a.get("sensitive") or a.get("handback_only") else \
            ("<personal data>" if a.get("personal") else a.get("current_value", a.get("value", "")))
        out.append({**a, "label": label, "value": value})
    return out


def _element(page, tab, c, resolved=None):
    r = resolved or {}
    rect = r.get("rect") or c.get("rect")
    return {"target_id": tab.target_id, "node": c["node"], "doc": f"{page.get('doc', '')}|{page.get('url', '')}",
            "label": c["label"], "ctx": c.get("ctx"), "role": c.get("role"), "value": c.get("value"),
            "x": r.get("x", (rect or {}).get("x", 0) + (rect or {}).get("w", 0) / 2),
            "y": r.get("y", (rect or {}).get("y", 0) + (rect or {}).get("h", 0) / 2), "rect": rect,
            "offscreen": bool(c.get("offscreen")) and not resolved}


def _find_state(page, cands, label_limit=120):
    return {"page": {"url": page.get("url"), "title": page.get("title"), "text": (page.get("text") or "")[:3000]},
            "elements": [{"index": str(i + 1), "label": c["label"][:label_limit], "role": c.get("role"),
                          "value": c.get("value"), "offscreen": bool(c.get("offscreen"))} for i, c in enumerate(cands)]}


@_guard
def jev_find(description, *, k=3, scroll=True, target_id=None, _tab_holder=None):
    """Find the element matching `description` on an owned tab. Returns FindResult (found/ambiguous/not_found)
    or a HandBack. Pass json.dumps(found.ref()) to jev_click in a later heredoc."""
    _preflight()
    tab = _need(_resolve(target_id))
    _tab_holder.append(tab)
    _need(_host_ok(tab))
    page = _observe(tab, offscreen=True)
    cands = _candidates(page)
    surfaces = page.get("surfaces", {})
    if not cands:
        return FindResult("not_found", exists=0.0, surfaces=surfaces, target_id=tab.target_id)
    client = _jev()
    pool = cands
    exists = None
    if len(cands) > config.MAX_OPTIONS:
        regs = A.regions({**page, "actions": [c for c in page["actions"] if c["node"] in {x["node"] for x in cands}]})
        regs = [r for r in regs if r.action_ids]
        if len(regs) > config.MAX_OPTIONS:
            return _hb(Reason.too_many_options, f"{len(regs)} regions", target_id=tab.target_id, surfaces=surfaces)
        crit = {r.id: {"heading": r.heading, "first_labels": r.first_labels} for r in regs}
        ans = client.ask({"page": {"url": page.get("url"), "title": page.get("title")}},
                         {"region": {"type": "choice", "criteria": crit,
                                     "instructions": {"description": description, "question": questions.FIND_REGION}},
                          "exists": {"type": "noul", "instructions": {"description": description,
                                                                      "question": questions.FIND_EXISTS}}})
        exists = validate_noul(ans.answers.get("exists", {}))
        region = validate_choice(ans.answers.get("region", {}), crit)["choice"]
        ids = set(next(r.action_ids for r in regs if r.id == region))
        pool = [c for c in cands if c["id"] in ids]
    index_ids = [str(i + 1) for i in range(len(pool))]
    qs = {"element": {"type": "choice", "criteria": {i: f"[{i}] {c['label'][:120]}" for i, c in zip(index_ids, pool,
                                                                                                    strict=True)},
                      "instructions": {"description": description, "question": questions.FIND_ELEMENT}}}
    if exists is None:
        qs["exists"] = {"type": "noul", "instructions": {"description": description, "question": questions.FIND_EXISTS}}
    ans = client.ask(_find_state(page, pool), qs)
    if exists is None:
        exists = validate_noul(ans.answers.get("exists", {}))
    el = validate_choice(ans.answers.get("element", {}), index_ids)
    ranked = sorted(zip(index_ids, pool, strict=True), key=lambda ic: -el["probabilities"][ic[0]])
    top = [_element(page, tab, c) | {"probability": round(el["probabilities"][i], 4)} for i, c in ranked[:k]]
    chosen = pool[int(el["choice"]) - 1]
    if exists < config.FIND_EXISTS:
        return FindResult("not_found", candidates=top, exists=exists, confidence=el["confidence"], surfaces=surfaces,
                          target_id=tab.target_id)
    if el["confidence"] < config.FIND_CONFIDENCE:
        return FindResult("ambiguous", candidates=top, exists=exists, confidence=el["confidence"],
                          surfaces=surfaces, target_id=tab.target_id)
    resolved = tab.resolve_node(chosen["node"], scroll=scroll)
    if resolved.get("state") == "occluded":
        return FindResult("ambiguous", candidates=top, exists=exists, confidence=el["confidence"], surfaces=surfaces,
                          target_id=tab.target_id, data={"occluded": True})
    if resolved.get("state") != "ok":
        if chosen.get("offscreen") and not scroll:
            element = _element(page, tab, chosen)
            return FindResult("found", element=element, candidates=top, exists=exists, confidence=el["confidence"],
                              surfaces=surfaces, target_id=tab.target_id)
        return _hb(Reason.stale_page, "the chosen element changed before it could be located", target_id=tab.target_id)
    element = _element(page, tab, chosen, resolved)
    return FindResult("found", element=element, candidates=top, exists=exists, confidence=el["confidence"],
                      surfaces=surfaces, target_id=tab.target_id)


# ------------------------------------------------------------------------------------------------ click
def _as_ref(found):
    if isinstance(found, FindResult):
        return found.ref()
    if isinstance(found, str):
        found = json.loads(found)
    if isinstance(found, HandBack):
        found = found.data
    if not isinstance(found, dict):
        raise ValueError("jev_click needs a FindResult, its ref() dict, or a confirm_required data dict/JSON")
    if not found.get("doc") or "node" not in found:
        raise ValueError("refusing a reference without `doc`: re-run jev_find and pass found.ref()")
    return {k: found.get(k) for k in ("target_id", "node", "doc", "label", "ctx")}


@_guard
def jev_click(found, *, target_id=None, confirm=False, _tab_holder=None):
    """Click a jev_find result (or a confirm_required reference) on its owned tab via an explicit session.
    Returns {clicked: True|False|"unconfirmed", reason?, x?, y?, url_after?, popup_target_id?} or a HandBack."""
    _preflight()
    ref = _as_ref(found)
    tab = _need(_resolve(ref.get("target_id") or target_id))
    _tab_holder.append(tab)
    _need(_host_ok(tab))
    info = tab.resolve_node(ref["node"])
    if info.get("state") != "ok":
        if info.get("state") == "occluded":
            return {"clicked": False, "reason": "occluded"}
        return {"clicked": False, "reason": "stale"}
    if str(info.get("time_origin")) != str(ref["doc"]).split("|")[0] or fold(info.get("label")) != fold(ref["label"]) \
            or info.get("ctx") != ref.get("ctx"):
        return {"clicked": False, "reason": "stale"}
    if info.get("upload_trigger"):
        return _hb(Reason.upload, "this opens a file chooser; use the harness upload_file()", target_id=tab.target_id)
    gate = A.gate_for({"kind": "click", **info})
    if gate.gated and confirm is not True:
        return {"clicked": False, "reason": "confirm_required",
                "data": {**ref, "context": redact_context(info.get("context", ""), None), "gate": gate.kind}}
    tab.snapshot_popups()
    x, y = info["x"], info["y"]
    try:
        tab.click_at(x, y)
    except DialogSuspected:
        dialog = tab.dialog_open()
        if dialog and dialog.get("session_id"):
            return {"clicked": "unconfirmed", "reason": "dialog_open", "data": {"dialog": dialog}}
        return _hb(Reason.browser_error, "page unresponsive", suspected_dialog=dialog, target_id=tab.target_id)
    time.sleep(0.25)
    result = {"clicked": True, "x": x, "y": y}
    with contextlib.suppress(BrowserGone):
        result["url_after"] = tab.target_url()
    with contextlib.suppress(BrowserGone):
        popups = tab.popups()
        if popups:
            result["popup_target_id"] = popups[0]
    return result


# ------------------------------------------------------------------------------------------------ check
@_guard
def jev_check(condition, *, target_id=None, _tab_holder=None):
    """Is `condition` true of the owned page? A navigation aid, never a verifier of claimed_done. Returns a
    CheckResult (probability, holds, evidence_line, lines_truncated) or a HandBack."""
    _preflight()
    tab = _need(_resolve(target_id))
    _tab_holder.append(tab)
    _need(_host_ok(tab))
    page = _observe(tab, lines=True)
    lines = page.get("lines") or []
    truncated = len(lines) > config.MAX_CHECK_LINES
    lines = lines[:config.MAX_CHECK_LINES]
    ids = {f"L{i + 1}": line[:300] for i, line in enumerate(lines)}
    fields = [{"label": f.get("label"), "value": f.get("value")} for f in page.get("fields") or []
              if f.get("visible") and not f.get("sensitive") and not f.get("personal")]
    state = {"url": page.get("url"), "title": page.get("title"), "lines": ids, "fields": fields}
    ans = _jev().ask(state, {
        "holds": {"type": "noul", "instructions": {"condition": condition, "question": questions.CHECK_HOLDS}},
        "where": {"type": "choice", "criteria": {**{k: None for k in ids}, "none": "No line shows it."},
                  "instructions": {"condition": condition, "question": questions.CHECK_WHERE}},
    })
    p = validate_noul(ans.answers.get("holds", {}))
    where = validate_choice(ans.answers.get("where", {}), set(ids) | {"none"})["choice"]
    return CheckResult(probability=p, holds=p >= config.CHECK_HOLDS, evidence_line=ids.get(where),
                       lines_truncated=truncated)
