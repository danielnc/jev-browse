import json
import os
import re
import time
from copy import deepcopy
from pathlib import Path

import pytest

from jev_browse import harness_api, interactive, tab
from jev_browse.interactive import jev_adopt, jev_check, jev_click, jev_close, jev_find, jev_open
from jev_browse.results import FindResult, HandBack, Reason
from jev_browse.tab import Registry
from tests.fakes import FakeCDP, FakeTypeSafe, choice_answer, noul_answer

ROOT = Path(__file__).resolve().parents[1]
URL = "https://example.test/"
DOC = "1000.5"


def act(i, kind, label, node, **kw):
    return {"id": f"e{i}", "kind": kind, "label": label, "node": node, "role": kw.pop("role", "button"),
            "value": kw.pop("value", ""), "region": kw.pop("region", "r1"), "ctx": kw.pop("ctx", f"c{node}"),
            "rect": {"x": 10, "y": 10, "w": 20, "h": 20}, **kw}


def mkpage(actions, url=URL, lines=None, fields=None, surfaces=None):
    return {"url": url, "href": url, "title": "T", "text": "text", "doc": DOC, "w": 1000, "h": 800,
            "actions": list(actions), "fields": fields or [], "regions": {"r1": {"heading": "Main"}},
            "surfaces": surfaces or {"frames": []}, "lines": lines or [], "marker": [], "page_key": [], "guards": {},
            "scroll": {"y": 0}, "file_activated": False}


class World:
    def __init__(self, tmp_path):
        self.cdp = FakeCDP()
        self.cdp.socket_dir = tmp_path
        self.cdp.tmp_dir = tmp_path
        self.cdp.daemon_name = "test"
        self.reg = Registry(tmp_path, "test")
        self.page = mkpage([act(1, "click", "Casa Flora result", 11), act(2, "click", "Send to a friend", 12)])
        self.resolve = {"state": "ok", "x": 50, "y": 60, "time_origin": DOC, "label": "Casa Flora result",
                        "ctx": "c11", "role": "button", "context": ""}
        self.cdp.handlers["Runtime.evaluate"] = self.evaluate

    def evaluate(self, params, sid):
        expr = params["expression"]
        if "__jevFast?.resolve" in expr:
            return {"result": {"value": deepcopy(self.resolve)}}
        if expr == "document.readyState":
            return {"result": {"value": "complete"}}
        if "__jevFast ||=" in expr:
            return {"result": {"value": deepcopy(self.page)}}
        return {"result": {"value": None}}

    def add_target(self, tid, url=URL, owner="me", register=True):
        self.cdp.targets[tid] = {"targetId": tid, "type": "page", "url": url, "title": "T", "openerId": None}
        if register:
            self.reg.add(tid, owner=owner, sessions={})


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    harness_api.install_fake(w.cdp)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setenv("JEV_BROWSE_OWNER", "me")
    monkeypatch.delenv("JEV_BROWSE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(interactive, "_client", None)
    yield w
    harness_api.install_fake(None)
    tab._open_tabs.clear()


def jev(monkeypatch, fn):
    fake = FakeTypeSafe(fn)
    monkeypatch.setattr(interactive, "_client", fake)
    return fake


def find_answers(pick_label, exists=0.9, conf=0.9):
    def fn(body):
        crit = body["questions"]["element"]["criteria"]
        idx = next(i for i, text in crit.items() if pick_label in text)
        out = {"element": choice_answer(list(crit), idx, conf)}
        if "exists" in body["questions"]:
            out["exists"] = noul_answer(exists)
        return out
    return fn


# ---- find ------------------------------------------------------------------------------------------------
def test_find_found_returns_target_node_and_coords(world, monkeypatch):
    world.add_target("T1")
    jev(monkeypatch, find_answers("Casa Flora"))
    r = jev_find("Casa Flora result", target_id="T1")
    assert isinstance(r, FindResult) and r.outcome == "found"
    assert r.element["node"] == 11 and r.element["target_id"] == "T1" and (r.element["x"], r.element["y"]) == (50, 60)
    assert r.ref() == {"target_id": "T1", "node": 11, "doc": f"{DOC}|{URL}", "label": "Casa Flora result",
                       "ctx": "c11"}


def test_find_ambiguous_returns_top_k(world, monkeypatch):
    world.add_target("T1")
    jev(monkeypatch, find_answers("Casa Flora", conf=0.55))
    r = jev_find("the result", target_id="T1", k=2)
    assert r.outcome == "ambiguous" and len(r.candidates) == 2


def test_find_not_found_uses_exists_noul_and_surfaces(world, monkeypatch):
    world.add_target("T1")
    world.page["surfaces"] = {"frames": [{"origin": "x", "area_ratio": 0.4, "visible": True}]}
    jev(monkeypatch, find_answers("Casa Flora", exists=0.2))
    r = jev_find("the Reserve button", target_id="T1")
    assert r.outcome == "not_found" and r.surfaces["frames"] and r.candidates


def test_find_offscreen_scrolls_then_rehittests(world, monkeypatch):
    world.add_target("T1")
    world.page["actions"][0]["offscreen"] = True
    jev(monkeypatch, find_answers("Casa Flora"))
    r = jev_find("Casa Flora result", target_id="T1")
    resolve_calls = [p["expression"] for m, _, p in world.cdp.calls if m == "Runtime.evaluate"
                     and "__jevFast?.resolve" in p["expression"]]
    assert resolve_calls and "resolve(11, true)" in resolve_calls[0]
    assert r.outcome == "found" and r.element["offscreen"] is False


def test_find_occluded_after_scroll_returns_ambiguous(world, monkeypatch):
    world.add_target("T1")
    world.resolve["state"] = "occluded"
    jev(monkeypatch, find_answers("Casa Flora"))
    r = jev_find("Casa Flora result", target_id="T1")
    assert r.outcome == "ambiguous" and r.data["occluded"] is True


def test_find_over_254_uses_region_path_with_exists_in_stage1(world, monkeypatch):
    world.add_target("T1")
    world.page = mkpage([act(i, "click", f"Link {i}", 100 + i, role="link") for i in range(600)])
    calls = []

    def fn(body):
        calls.append(set(body["questions"]))
        if "region" in body["questions"]:
            regs = list(body["questions"]["region"]["criteria"])
            return {"region": choice_answer(regs, regs[1]), "exists": noul_answer(0.9)}
        crit = body["questions"]["element"]["criteria"]
        return {"element": choice_answer(list(crit), list(crit)[3])}
    jev(monkeypatch, fn)
    r = jev_find("Link 300", target_id="T1")
    assert calls[0] == {"region", "exists"} and calls[1] == {"element"}
    assert r.outcome == "found"


def test_find_and_check_refuse_unowned_tab(world, monkeypatch):
    world.add_target("U1", register=False)
    fake = jev(monkeypatch, find_answers("Casa"))
    assert jev_find("x", target_id="U1").reason == Reason.not_owned_tab
    assert jev_check("x", target_id="U1").reason == Reason.not_owned_tab
    assert fake.requests == []


def test_implicit_target_refused_with_multiple_owned(world, monkeypatch):
    world.add_target("T1")
    world.add_target("T2")
    world.cdp.current = "T1"
    r = jev_find("x")
    assert r.reason == Reason.not_owned_tab and r.detail == "pass target_id"


def test_implicit_target_used_when_sole_and_current(world, monkeypatch):
    world.add_target("T1")
    world.cdp.current = "T1"
    jev(monkeypatch, find_answers("Casa"))
    assert jev_find("Casa Flora result").outcome == "found"


def test_helpers_read_via_owned_session_after_current_swap(world, monkeypatch):
    world.add_target("T1")
    world.add_target("OTHER", register=False)
    world.cdp.current = "OTHER"
    jev(monkeypatch, find_answers("Casa"))
    jev_find("Casa Flora result", target_id="T1")
    page_calls = [(m, sid) for m, sid, _ in world.cdp.calls if m.startswith(("Runtime.", "Input.", "Page."))]
    assert page_calls and all(sid and sid.endswith("-T1") for _, sid in page_calls)


def test_helpers_verify_target_url_before_send(world, monkeypatch):
    world.add_target("T1", url="https://example.test/other")
    fake = jev(monkeypatch, find_answers("Casa"))
    r = jev_find("Casa", target_id="T1")
    assert r.reason == Reason.stale_page and fake.requests == []


@pytest.mark.parametrize("call", ["find", "check", "click"])
def test_helpers_apply_host_hook(world, monkeypatch, call):
    world.add_target("T1")
    monkeypatch.setenv("JEV_BROWSE_ALLOWED_HOSTS", "allowed.test")
    fake = jev(monkeypatch, find_answers("Casa"))
    ref = {"target_id": "T1", "node": 11, "doc": f"{DOC}|{URL}", "label": "Casa Flora result", "ctx": "c11"}
    r = {"find": lambda: jev_find("x", target_id="T1"), "check": lambda: jev_check("x", target_id="T1"),
         "click": lambda: jev_click(ref)}[call]()
    assert r.reason == Reason.host_not_allowed and fake.requests == []
    assert not any(m.startswith("Input.") for m, _, _ in world.cdp.calls)


# ---- click -----------------------------------------------------------------------------------------------
REF = {"target_id": "T1", "node": 11, "doc": f"{DOC}|{URL}", "label": "Casa Flora result", "ctx": "c11"}


def inputs(world):
    return [(m, sid, p) for m, sid, p in world.cdp.calls if m.startswith("Input.")]


def test_click_uses_owned_session_after_current_swap(world):
    world.add_target("T1")
    world.cdp.current = "SOMETHING-ELSE"
    r = jev_click(REF)
    assert r["clicked"] is True and (r["x"], r["y"]) == (50, 60) and r["url_after"] == URL
    assert inputs(world) and all(sid.endswith("-T1") for _, sid, _ in inputs(world))


def test_click_accepts_ref_from_other_process(world):
    world.add_target("T1")
    ref_json = json.dumps(REF)
    old_salt = tab.SALT
    tab.SALT = "f" * 32  # a different process salt
    try:
        assert jev_click(ref_json)["clicked"] is True
    finally:
        tab.SALT = old_salt
    for field, value in [("doc", f"9999.9|{URL}"), ("label", "Other"), ("ctx", "other")]:
        stale = dict(REF, **{field: value})
        assert jev_click(stale) == {"clicked": False, "reason": "stale"}


def test_click_stale_node_returns_stale(world):
    world.add_target("T1")
    world.resolve = {"state": "stale"}
    assert jev_click(REF) == {"clicked": False, "reason": "stale"}
    assert inputs(world) == []


def test_click_occluded_returns_occluded(world):
    world.add_target("T1")
    world.resolve["state"] = "occluded"
    assert jev_click(REF) == {"clicked": False, "reason": "occluded"}
    assert inputs(world) == []


def test_click_commit_without_confirm_returns_confirm_required(world):
    world.add_target("T1")
    world.resolve.update(label="Send to a friend", ctx="c12")
    ref = dict(REF, node=12, label="Send to a friend", ctx="c12")
    r = jev_click(ref)
    assert r["clicked"] is False and r["reason"] == "confirm_required" and inputs(world) == []
    assert r["data"]["node"] == 12
    assert jev_click(r["data"], confirm=True)["clicked"] is True


def test_click_commit_verb_requires_confirm(world):
    world.add_target("T1")
    world.resolve.update(label="Delete account", ctx="c11")
    assert jev_click(dict(REF, label="Delete account"))["reason"] == "confirm_required"
    world.resolve.update(label="Done", in_dialog=True)
    assert jev_click(dict(REF, label="Done"))["reason"] == "confirm_required"


def test_click_return_shapes(world, monkeypatch):
    world.add_target("T1")
    assert set(jev_click(REF)) >= {"clicked", "x", "y", "url_after"}
    world.resolve = {"state": "stale"}
    assert jev_click(REF)["reason"] == "stale"
    world.add_target("U1", register=False)
    assert isinstance(jev_click(dict(REF, target_id="U1")), HandBack)
    with pytest.raises(ValueError, match="without `doc`"):
        jev_click({"target_id": "T1", "node": 11})


def test_click_dialog_returns_unconfirmed_not_false(world):
    world.add_target("T1")
    world.cdp.timeout_methods.add("Input.dispatchMouseEvent")
    world.cdp.dialog = {"type": "confirm", "message": "Sure?", "url": URL}
    r = jev_click(REF)
    assert r["clicked"] == "unconfirmed" and r["reason"] == "dialog_open" and r["data"]["dialog"]["session_id"]
    sid = r["data"]["dialog"]["session_id"]
    assert ("Target.detachFromTarget", None, {"sessionId": sid}) not in world.cdp.calls


def test_click_popup_registers_and_returns_popup_target_id(world, monkeypatch):
    world.add_target("T1")
    orig = world.cdp._default

    def default(method, sid, params):
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased":
            world.cdp.targets["P1"] = {"targetId": "P1", "type": "page", "url": "https://pop.test/", "title": "",
                                       "openerId": "T1"}
        return orig(method, sid, params)
    world.cdp._default = default
    monkeypatch.setattr(interactive.time, "sleep", lambda s: None)
    r = jev_click(REF)
    assert r["popup_target_id"] == "P1" and world.reg.has("P1")


def test_helpers_never_call_switch_tab():
    code = re.sub(r'""".*?"""', "", (ROOT / "jev_browse/interactive.py").read_text(), flags=re.S)
    assert "switch_tab(" not in code and "drain_events(" not in code


# ---- check -----------------------------------------------------------------------------------------------
def test_check_returns_probability_and_verbatim_line(world, monkeypatch):
    world.add_target("T1")
    world.page["lines"] = ["Casa Flora", "Free cancellation included"]
    fake = jev(monkeypatch, lambda body: {"holds": noul_answer(0.92), "where": choice_answer(
        list(body["questions"]["where"]["criteria"]), "L2")})
    r = jev_check("Free cancellation filter is applied", target_id="T1")
    assert r.holds and r.probability == 0.92 and r.evidence_line == "Free cancellation included"
    assert not r.lines_truncated and fake.requests[0]["state"]["lines"]["L1"] == "Casa Flora"


def test_check_none_line_maps_to_none(world, monkeypatch):
    world.add_target("T1")
    world.page["lines"] = ["a"]
    jev(monkeypatch, lambda body: {"holds": noul_answer(0.1), "where": choice_answer(
        list(body["questions"]["where"]["criteria"]), "none")})
    r = jev_check("x", target_id="T1")
    assert r.evidence_line is None and r.holds is False


def test_check_caps_lines_at_253_plus_none(world, monkeypatch):
    world.add_target("T1")
    world.page["lines"] = [f"line {i}" for i in range(400)]
    fake = jev(monkeypatch, lambda body: {"holds": noul_answer(0.5), "where": choice_answer(
        list(body["questions"]["where"]["criteria"]), "none")})
    r = jev_check("x", target_id="T1")
    assert len(fake.requests[0]["questions"]["where"]["criteria"]) == 254 and r.lines_truncated


def test_check_returns_handback_on_ownership_or_host_failure(world, monkeypatch):
    world.add_target("U1", register=False)
    assert isinstance(jev_check("x", target_id="U1"), HandBack)


def test_check_never_sends_personal_or_sensitive_values(world, monkeypatch):
    world.add_target("T1")
    world.page["fields"] = [{"node": 1, "label": "Email", "value": "p@x.test", "visible": True, "personal": True},
                            {"node": 2, "label": "City", "value": "Lisbon", "visible": True}]
    fake = jev(monkeypatch, lambda body: {"holds": noul_answer(0.5), "where": choice_answer(
        list(body["questions"]["where"]["criteria"]), "none")})
    jev_check("x", target_id="T1")
    assert "p@x.test" not in json.dumps(fake.requests) and "Lisbon" in json.dumps(fake.requests)


# ---- open / adopt / close --------------------------------------------------------------------------------
def test_jev_open_creates_new_target_registers_and_does_not_switch(world):
    world.cdp.handlers["Target.createTarget"] = lambda p, s: (world.add_target("N1", register=False),
                                                              {"targetId": "N1"})[1]
    r = jev_open(URL)
    assert r["target_id"] == "N1" and world.reg.has("N1")
    assert "Target.activateTarget" not in world.cdp.methods()
    creates = [p for m, _, p in world.cdp.calls if m == "Target.createTarget"]
    assert creates == [{"url": "about:blank", "background": True}]


def test_jev_open_host_not_allowed_before_create_target(world, monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_ALLOWED_HOSTS", "allowed.test")
    r = jev_open(URL)
    assert r.reason == Reason.host_not_allowed and "Target.createTarget" not in world.cdp.methods()


def test_jev_open_navigation_failure_returns_target_id(world):
    world.cdp.handlers["Target.createTarget"] = lambda p, s: (world.add_target("N2", register=False),
                                                              {"targetId": "N2"})[1]
    world.cdp.handlers["Page.navigate"] = {"errorText": "net::ERR_NAME_NOT_RESOLVED"}
    r = jev_open("https://nope.invalid/")
    assert r.reason == Reason.browser_error and r.data["target_id"] == "N2" and world.reg.has("N2")


def test_jev_adopt_requires_explicit_id_and_new_tab_provenance(world):
    world.add_target("U1", register=False)
    assert jev_adopt(None).reason == Reason.not_owned_tab
    r = jev_adopt("U1")
    assert r.reason == Reason.not_owned_tab and "not created by new_tab" in r.detail and not world.reg.has("U1")
    Registry(world.cdp.socket_dir, "test", "created").record("U1")
    assert jev_adopt("U1")["target_id"] == "U1" and world.reg.get("U1")["owner"] == "me"
    assert jev_adopt("U1")["target_id"] == "U1"  # already owned: unchanged


def test_jev_close_refuses_unregistered_and_unregisters(world):
    world.add_target("T1")
    world.add_target("U1", register=False)
    with pytest.raises(ValueError, match="not registered"):
        jev_close("U1")
    assert jev_close("T1") == 1 and not world.reg.has("T1") and "T1" not in world.cdp.targets


def test_jev_close_all_owned_only_own_owner_and_skips_live_runs(world, tmp_path):
    world.add_target("A", owner="me")
    world.add_target("B", owner="other")
    world.add_target("C", owner="me")
    (tmp_path / "jev-browse-run-live.json").write_text(json.dumps({"target_id": "C", "final": False,
                                                                    "pid": os.getpid(), "started_at": time.time()}))
    assert jev_close(all_owned=True) == 1
    assert set(world.reg.entries()) == {"B", "C"}


def test_jev_close_force_closes_all(world, tmp_path):
    world.add_target("A", owner="me")
    world.add_target("B", owner="other")
    assert jev_close(all_owned=True, force=True) == 2 and world.reg.entries() == {}


def test_jev_close_without_target_or_all_owned_refuses(world):
    with pytest.raises(ValueError):
        jev_close()


def test_preflight_without_key_makes_no_browser_call(world, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    r = jev_open(URL)
    assert r.reason == Reason.service_error and world.cdp.calls == []
