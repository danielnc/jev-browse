import json
import os
import re
import stat
import time
from copy import deepcopy
from pathlib import Path

import pytest

from jev_browse import harness_api, run
from jev_browse.results import Reason
from jev_browse.tab import BrowserGone, DialogSuspected, StalePage
from jev_browse.typesafe import RequestTooLarge, ServiceError
from tests.fakes import FakeCDP, choice_answer, noul_answer

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------------------------------------ fakes
def act(i, kind, label, node, **kw):
    return {"id": f"e{i}", "kind": kind, "label": label, "node": node, "role": kw.pop("role", "button"),
            "value": kw.pop("value", ""), "region": "r1", "ctx": kw.pop("ctx", f"c{node}"), **kw}


def fld(node, label, **kw):
    return {"node": node, "label": label, "role": "textbox", "visible": True, "sensitive": False, "personal": False,
            "value": "", "filled": False, "readonly": False, "heading": "", "name_attr": "", "id_attr": "",
            "placeholder": "", "region": "r1", **kw}


def mkpage(actions, fields=(), url="https://example.test/", text="page", surfaces=None, doc="1000.5", fp=None):
    p = {"url": url, "title": "T", "text": text, "doc": doc, "w": 1000, "h": 800, "scroll": {"y": 0},
         "actions": list(actions) + [{"id": "wait", "kind": "wait", "label": "Wait"}], "fields": list(fields),
         "regions": {"r1": {"heading": "", "first_labels": []}}, "surfaces": surfaces or {}, "marker": ["m"],
         "page_key": ["k"], "guards": {}, "file_activated": False}
    p["fingerprint"] = fp or json.dumps([url, text, [a["label"] for a in actions]])
    return p


class FakeTab:
    last = None

    def __init__(self, pages, target_id="T1"):
        self.pages = list(pages)
        self.target_id = target_id
        self.session = "S1"
        self.acted = []
        self.fresh_value = True
        self.act_error = None
        self.observe_error = None
        self.new_popups = []
        self.dialog = None
        self.closed = self.detached = False
        self.resolve_info = {"state": "ok", "context": ""}
        self.navigated = []
        FakeTab.last = self

    def observe(self, **kw):
        if self.observe_error:
            raise self.observe_error
        page = self.pages[0] if len(self.pages) == 1 else self.pages.pop(0)
        return deepcopy(page)

    def fresh(self, page, action=None):
        if isinstance(self.fresh_value, Exception):
            raise self.fresh_value
        return self.fresh_value

    def act(self, action, page, text=None):
        if self.act_error:
            err, self.act_error = self.act_error, None
            raise err
        self.acted.append((action["id"], action.get("label"), text))

    def popups(self):
        out, self.new_popups = self.new_popups, []
        return out

    def snapshot_popups(self):
        pass

    def dialog_open(self):
        return self.dialog

    def resolve_node(self, node):
        return self.resolve_info

    def screenshot(self, path):
        return str(path)

    def detach(self):
        self.detached = True

    def close(self):
        self.closed = True

    def navigate(self, url, deadline=None):
        self.navigated.append(url)


class Jev:
    """Scripted Jev: `plan` is a list of callables(body) -> answers, or dicts {op, target_label, heads}."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.requests = []

    def ask(self, state, questions, *, deadline=None):
        from jev_browse.typesafe import Answer

        body = {"state": state, "questions": questions}
        self.requests.append(body)
        step = self.plan.pop(0) if len(self.plan) > 1 else self.plan[0]
        if isinstance(step, Exception):
            raise step
        answers = step(body) if callable(step) else decide(body, **step)
        return Answer(answers=answers, model="jev-test", usage={"input_tokens": 1000, "output_tokens": 20},
                      latency_ms=7)


def decide(body, op, target=None, conf=0.95, heads=None, commit_ok=None):
    qs = body["questions"]
    if "commit_ok" in qs:
        return {"commit_ok": noul_answer(commit_ok if commit_ok is not None else 0.9)}
    a = {"operation": choice_answer(qs["operation"]["criteria"], op, conf)}
    for name, q in qs.items():
        if name.endswith("_target"):
            ids = list(q["criteria"])
            pick = ids[0]
            if target is not None and name == op.lower() + "_target":
                pick = next(i for i, c in q["criteria"].items() if target in c["element"])
            a[name] = choice_answer(ids, pick)
        elif name.startswith("value_in_goal_"):
            a[name] = noul_answer((heads or {}).get(name, 0.1))
        elif name.startswith("value_"):
            opts = list(q["criteria"])
            want = (heads or {}).get(name, "none")
            a[name] = choice_answer(opts, want if want in opts else "none", 0.9)
    return a


class Backend:
    name = "claude"
    model = "haiku"

    def __init__(self, values):
        self.values = values
        self.calls = 0

    def batch(self, goal, fields, excerpt, deadline):
        from jev_browse.textgen import BatchResult

        self.calls += 1
        return BatchResult(values={f["key"]: self.values.get(f["key"]) for f in fields}, latency_ms=30,
                           tokens={"input_tokens": 550, "output_tokens": 12}, model="haiku", backend="claude",
                           cost_usd=0.0007)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    f = FakeCDP()
    f.tmp_dir = tmp_path
    f.socket_dir = tmp_path
    harness_api.install_fake(f)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.delenv("JEV_BROWSE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("JEV_BROWSE_VALUE_CANDIDATES", raising=False)
    yield tmp_path
    harness_api.install_fake(None)


def use_tab(monkeypatch, pages, target_id="T1", registered=True):
    tab = FakeTab(pages, target_id)

    class Factory:
        @staticmethod
        def create(url, deadline=None, **kw):
            tab.navigated.append(url)
            return tab

        @staticmethod
        def attach(tid, **kw):
            if not registered:
                from jev_browse.tab import NotOwned
                raise NotOwned("not registered")
            return tab

    monkeypatch.setattr(run, "OwnedTab", Factory)
    return tab


def fr(url, goal, jev, **kw):
    out = []
    kw.setdefault("_backend", Backend({}))
    r = run.fast_run(url, goal, _client=jev, _out=lambda *a, **k: out.append(a[0]), **kw)
    r.printed = out
    return r


SEARCH = [act(1, "fill", "Search", 10, role="searchbox"), act(2, "click", "Go", 20)]
SEARCH_FIELDS = [fld(10, "Search")]


# ------------------------------------------------------------------------------------------------ ported loop tests
def test_loading_waits_do_not_trigger_no_progress_stop(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    jev = Jev([{"op": "WAIT"}] * 5 + [{"op": "DONE"}])
    r = fr("https://example.test/", "Find a book", jev)
    assert r.status == "claimed_done"
    assert [a[0] for a in tab.acted] == ["wait"] * 5


def test_stale_observation_preserves_executed_action(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    jev = Jev([{"op": "CLICK", "target": "Go"}])

    orig = tab.observe
    calls = {"n": 0}

    def observe(**kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise StalePage("never settles")
        return orig(**kw)
    tab.observe = observe
    r = fr("https://example.test/", "Find", jev)
    assert r.reason == Reason.stale_page
    history = run.read_run_file(run.run_path(r.run_id))["history"]
    assert history[-1]["action"] == "Go" and tab.acted == [("e2", "Go", None)]


def test_navigation_during_prediction_reobserves_without_action(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    tab.fresh_value = False
    jev = Jev([{"op": "CLICK", "target": "Go"}, {"op": "CLICK", "target": "Go"}, {"op": "DONE"}])
    seq = iter([False, False, True])
    tab.fresh = lambda page, action=None: next(seq, True)
    r = fr("https://example.test/", "Find", jev)
    assert r.status == "claimed_done" and tab.acted == []


# ------------------------------------------------------------------------------------------------ results and tab
def test_done_is_claimed_done_with_evidence_and_keeps_tab_by_default(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS, text="Results for books")])
    r = fr("https://example.test/", "Find", Jev([{"op": "DONE"}]))
    assert r.status == "claimed_done" and r.reason is None
    assert r.evidence["visible_text"] == "Results for books" and "surfaces" in r.evidence
    assert r.target_id == "T1" and tab.detached and not tab.closed


def test_keep_open_false_closes_on_claimed_done(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    fr("https://example.test/", "Find", Jev([{"op": "DONE"}]), keep_open=False)
    assert tab.closed


def test_handed_back_keeps_tab_registered_and_does_not_switch(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    r = fr("https://example.test/", "Find", Jev([{"op": "BLOCKED"}]))
    assert r.status == "blocked" and r.reason == Reason.blocked and not tab.closed


def test_fast_run_never_calls_switch_tab():
    code = (ROOT / "jev_browse/run.py").read_text()
    assert "switch_tab(" not in code and "drain_events(" not in code


# ------------------------------------------------------------------------------------------------ run file
def test_first_stdout_line_is_flushed_absolute_run_file_path(monkeypatch, env):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    r = fr("https://example.test/", "Find", Jev([{"op": "DONE"}]), run_id="abc_1")
    assert r.printed[0] == f"JEV_BROWSE_RUN={env / 'jev-browse-run-abc_1.json'}"
    assert Path(r.printed[0].split("=", 1)[1]).is_absolute()


def test_run_file_written_at_start_each_step_and_final(monkeypatch, env):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    snapshots = []
    orig = run._write_json
    monkeypatch.setattr(run, "_write_json", lambda path, data: (snapshots.append(deepcopy(data)), orig(path, data)))
    r = fr("https://example.test/", "Find", Jev([{"op": "CLICK", "target": "Go"}, {"op": "DONE"}]))
    assert snapshots[0]["final"] is False and snapshots[0]["target_id"] is None
    assert any(s["history"] and not s["final"] for s in snapshots)
    assert snapshots[-1]["final"] is True and snapshots[-1]["result"]["status"] == "claimed_done"
    path = run.run_path(r.run_id)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert tab.acted


def test_caller_supplied_run_id_names_file(monkeypatch, env):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    fr("https://example.test/", "Find", Jev([{"op": "DONE"}]), run_id="my-run")
    assert (env / "jev-browse-run-my-run.json").exists()


def test_run_id_charset_and_live_collision_refused(monkeypatch, env):
    with pytest.raises(ValueError):
        fr("https://example.test/", "Find", Jev([{"op": "DONE"}]), run_id="bad id!")
    (env / "jev-browse-run-live.json").write_text(json.dumps({"final": False, "pid": os.getpid()}))
    with pytest.raises(ValueError, match="live run"):
        fr("https://example.test/", "Find", Jev([{"op": "DONE"}]), run_id="live")


def test_run_file_never_contains_page_key_or_digests(monkeypatch, env):
    p = mkpage(SEARCH, SEARCH_FIELDS)
    p["page_key"] = ["SECRET_PAGE_KEY"]
    p["marker"] = ["SECRET_MARKER"]
    p["guards"] = {"20": ["d:SECRET_DIGEST"]}
    use_tab(monkeypatch, [p])
    r = fr("https://example.test/", "Find", Jev([{"op": "CLICK", "target": "Go"}, {"op": "DONE"}]))
    text = run.run_path(r.run_id).read_text() + json.dumps(r.to_dict())
    assert "SECRET" not in text


# ------------------------------------------------------------------------------------------------ resume
def _write_prior(env, target, goal, pid=999999, final=True, history=None):
    data = {"run_id": "prior", "goal": goal, "started_at": time.time() - 10, "pid": pid, "final": final,
            "target_id": target, "history": history or [{"action": "Go", "kind": "click", "page_changed": True}],
            "result": {}}
    (env / "jev-browse-run-prior.json").write_text(json.dumps(data))


def test_resume_seeds_history_only_when_goal_matches_and_resets_budgets(monkeypatch, env):
    _write_prior(env, "T1", "Find a book")
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    jev = Jev([{"op": "DONE"}])
    r = fr(None, "Find a book", jev, target_id="T1", max_requests=1)
    assert r.status == "claimed_done"
    assert jev.requests[0]["state"]["recent_actions"][0]["action"] == "Go"
    jev2 = Jev([{"op": "DONE"}])
    fr(None, "Another goal", jev2, target_id="T1")
    assert jev2.requests[0]["state"]["recent_actions"] == []


def test_resumed_run_file_carries_history_forward(monkeypatch, env):
    _write_prior(env, "T1", "Find a book")
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    r = fr(None, "Find a book", Jev([{"op": "CLICK", "target": "Go"}, {"op": "DONE"}]), target_id="T1")
    history = run.read_run_file(run.run_path(r.run_id))["history"]
    assert [h["action"] for h in history] == ["Go", "Go"]


def test_resume_after_budget_exhausted_can_act(monkeypatch, env):
    _write_prior(env, "T1", "Find")
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    r = fr(None, "Find", Jev([{"op": "CLICK", "target": "Go"}, {"op": "DONE"}]), target_id="T1", max_actions=1)
    assert r.status == "claimed_done" and tab.acted


def test_resume_refused_while_target_has_live_run(monkeypatch, env):
    _write_prior(env, "T1", "Find", pid=os.getppid(), final=False)
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    r = fr(None, "Find", Jev([{"op": "DONE"}]), target_id="T1")
    assert r.reason == Reason.browser_error and "run in progress" in r.detail


def test_resume_with_target_id_and_no_url(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    r = fr(None, "Find", Jev([{"op": "DONE"}]), target_id="T1")
    assert r.status == "claimed_done" and tab.navigated == []


def test_resume_refuses_unregistered_target_id(monkeypatch):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)], registered=False)
    r = fr(None, "Find", Jev([{"op": "DONE"}]), target_id="T-unknown")
    assert r.reason == Reason.not_owned_tab


# ------------------------------------------------------------------------------------------------ text values
def test_caller_value_used_before_candidates_and_llm(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    backend = Backend({"Search": "llm-value"})
    jev = Jev([{"op": "TYPE_TEXT", "target": "Search"}, {"op": "DONE"}])
    fr("https://example.test/", 'Search "quoted value"', jev, values={"Search": "caller value"}, _backend=backend)
    assert tab.acted[0] == ("e1", "Search", "caller value") and backend.calls == 0
    assert not any(k.startswith("value_") for k in jev.requests[0]["questions"])


def test_candidate_value_typed_without_llm_call(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    backend = Backend({"Search": "llm-value"})
    jev = Jev([{"op": "TYPE_TEXT", "target": "Search",
                "heads": {"value_1": "Gödel's incompleteness theorems", "value_in_goal_1": 0.9}}, {"op": "DONE"}])
    r = fr("https://example.test/", "Open the article on Gödel's incompleteness theorems", jev, _backend=backend)
    assert tab.acted[0][2] == "Gödel's incompleteness theorems" and backend.calls == 0
    assert r.trace[0].text_source == "candidate"


def test_llm_value_used_for_miss(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    backend = Backend({"Search": "Paris"})
    goal = "Open the Wikipedia article about the capital city of France"
    jev = Jev([{"op": "TYPE_TEXT", "target": "Search", "heads": {"value_in_goal_1": 0.9}},
               lambda body: {"grounded": noul_answer(0.9)}, {"op": "DONE"}])
    r = fr("https://example.test/", goal, jev, _backend=backend)
    assert tab.acted[0][2] == "Paris" and backend.calls == 1
    assert r.trace[0].text_source == "llm" and r.stats["llm_calls"] == 1 and r.stats["llm_input_tokens"] == 550


def test_a0_llm_config_reaches_llm_path(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_VALUE_CANDIDATES", "0")
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    backend = Backend({"Search": "Paris"})
    jev = Jev([{"op": "TYPE_TEXT", "target": "Search", "heads": {"value_in_goal_1": 0.9}},
               lambda body: {"grounded": noul_answer(0.9)}, {"op": "DONE"}])
    r = fr("https://example.test/", "Open the article about the capital city of France", jev, _backend=backend)
    assert "value_1" not in jev.requests[0]["questions"] and backend.calls == 1 and r.status == "claimed_done"


def test_text_value_unavailable_lists_all_unresolved_fields_beyond_head_cap(monkeypatch):
    acts = [act(i, "fill", f"Field {i}", 100 + i, role="textbox") for i in range(1, 10)]
    fields = [fld(100 + i, f"Field {i}") for i in range(1, 10)]
    use_tab(monkeypatch, [mkpage(acts, fields)])
    jev = Jev([{"op": "TYPE_TEXT", "target": "Field 1"}])
    r = fr("https://example.test/", "Fill the form", jev, text_backend="none", _backend=None)
    assert r.reason == Reason.text_value_unavailable
    assert len(r.data["fields"]) == 9 and {f["key"] for f in r.data["fields"]} >= {"Field 9"}


def test_headless_field_followup_asks_choice_and_noul(monkeypatch):
    acts = [act(i, "fill", f"Field {i}", 100 + i, role="textbox") for i in range(1, 9)]
    fields = [fld(100 + i, f"Field {i}") for i in range(1, 9)]
    tab = use_tab(monkeypatch, [mkpage(acts, fields)])
    jev = Jev([{"op": "TYPE_TEXT", "target": "Field 8"},
               lambda body: {"value_in_goal_8": noul_answer(0.9), "value_8": choice_answer(
                   list(body["questions"]["value_8"]["criteria"]), "Lisbon", 0.9)},
               {"op": "DONE"}])
    fr("https://example.test/", "Put Lisbon in field 8", jev)
    assert set(jev.requests[1]["questions"]) == {"value_in_goal_8", "value_8"}
    assert tab.acted[0][2] == "Lisbon"


def test_sensitive_field_choice_hands_back_without_typing(monkeypatch):
    acts = [act(1, "fill", "Card number", 30, role="textbox", sensitive=True, handback_only=True),
            act(2, "fill", "Password", 31, role="textbox", handback_only=True, input_type="password")]
    tab = use_tab(monkeypatch, [mkpage(acts, [fld(30, "Card number", sensitive=True)])])
    r = fr("https://example.test/", "Pay", Jev([{"op": "TYPE_TEXT", "target": "Card number"}]))
    assert r.reason == Reason.sensitive_field and tab.acted == []
    r = fr("https://example.test/", "Log in", Jev([{"op": "TYPE_TEXT", "target": "Password"}]))
    assert r.reason == Reason.auth_required


@pytest.mark.parametrize("label,expected", [("Password", Reason.auth_required), ("Upload file", Reason.upload),
                                            ("IBAN", Reason.sensitive_field)])
def test_hand_back_only_index_maps_by_class(monkeypatch, label, expected):
    extra = {"input_type": "password"} if label == "Password" else {"input_type": "file"} if label == "Upload file" else {}
    acts = [act(1, "fill", label, 30, role="textbox", handback_only=True, sensitive=True, **extra)]
    use_tab(monkeypatch, [mkpage(acts)])
    assert fr("https://example.test/", "x", Jev([{"op": "TYPE_TEXT", "target": label}])).reason == expected


def test_personal_field_three_state_and_differs_blocks_done(monkeypatch):
    acts = [act(1, "fill", "Email", 40, role="textbox", personal=True, value="owner@real.test")]
    fields = [fld(40, "Email", personal=True, value="owner@real.test", filled=True)]
    tab = use_tab(monkeypatch, [mkpage(acts, fields)])
    jev = Jev([{"op": "DONE"}])
    r = fr("https://example.test/", "Sign up", jev, values={"Email": "me@wanted.test"})
    assert "owner@real.test" not in json.dumps(jev.requests[0])
    assert "filled (differs from requested value)" in json.dumps(jev.requests[0])
    assert tab.acted and tab.acted[0][2] == "me@wanted.test"
    ev = next(f for f in r.evidence["fields"] if f["label"] == "Email")
    assert ev["value"] == "<redacted>" and ev["matches_requested"] is False


def test_values_duplicate_label_hands_back(monkeypatch):
    fields = [fld(1, "Name", heading="A"), fld(2, "Name", heading="B")]
    use_tab(monkeypatch, [mkpage([act(1, "fill", "Name", 1, role="textbox")], fields)])
    r = fr("https://example.test/", "x", Jev([{"op": "DONE"}]), values={"Name": "Ana"})
    assert r.reason == Reason.text_value_unavailable and r.detail == "ambiguous key"


# ------------------------------------------------------------------------------------------------ commit gate
SEND = [act(1, "click", "Send to a friend", 50), act(2, "click", "View Casa Flora", 51)]


def test_commit_gate_hands_back_confirm_required_and_executes_nothing(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEND)])
    jev = Jev([{"op": "CLICK", "target": "Send to a friend"}, {"op": "CLICK", "commit_ok": 0.03}])
    r = fr("https://example.test/", "Open Casa Flora", jev)
    assert r.reason == Reason.confirm_required and tab.acted == []
    assert {"target_id", "node", "doc", "label", "ctx"} <= set(r.data) and r.data["commit_ok"] == 0.03
    printed = next(p for p in r.printed if p.startswith("JEV_BROWSE_CONFIRM="))
    ref = json.loads(printed.split("=", 1)[1])
    assert set(ref) == {"target_id", "node", "doc", "label", "ctx"}
    assert r.trace[0].commit_gate["verdict"] == "confirm_required"


def test_confirm_required_data_has_target_and_node(monkeypatch):
    use_tab(monkeypatch, [mkpage(SEND)])
    r = fr("https://example.test/", "x", Jev([{"op": "CLICK", "target": "Send to a friend"},
                                               {"op": "CLICK", "commit_ok": 0.1}]))
    assert r.data["target_id"] == "T1" and r.data["node"] == 50 and r.data["doc"].startswith("1000.5|")


def test_confirm_required_context_redacted_shown_in_detail_not_in_ref_or_repr(monkeypatch):
    acts = [act(1, "click", "OK", 60, in_dialog=True)]
    fields = [fld(61, "Email", personal=True, value="owner@real.test")]
    tab = use_tab(monkeypatch, [mkpage(acts, fields)])
    tab.resolve_info = {"state": "ok", "context": "Confirm subscription for owner@real.test"}
    r = fr("https://example.test/", "Read the page", Jev([{"op": "CLICK", "target": "OK"},
                                                           {"op": "CLICK", "commit_ok": 0.1}]))
    assert r.reason == Reason.confirm_required
    assert r.data["context"] == "Confirm subscription for <redacted>" and "Confirm subscription" in r.detail
    ref = json.loads(next(p for p in r.printed if p.startswith("JEV_BROWSE_CONFIRM=")).split("=", 1)[1])
    assert "context" not in ref and "Confirm" not in repr(r)


def test_commit_gate_authorised_executes_and_traces(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEND)])
    jev = Jev([{"op": "CLICK", "target": "Send to a friend"}, {"op": "CLICK", "commit_ok": 0.95}, {"op": "DONE"}])
    r = fr("https://example.test/", "Send Casa Flora to a friend", jev)
    assert r.status == "claimed_done" and tab.acted[0][1] == "Send to a friend"
    assert r.trace[0].commit_gate["verdict"] == "ok" and r.trace[0].commit_gate["commit_ok"] == 0.95


def test_confirm_required_prints_confirm_json_and_accepts_it_back(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEND)])
    r = fr("https://example.test/", "x", Jev([{"op": "CLICK", "target": "Send to a friend"},
                                               {"op": "CLICK", "commit_ok": 0.1}]))
    confirm_json = next(p for p in r.printed if p.startswith("JEV_BROWSE_CONFIRM=")).split("=", 1)[1]
    jev = Jev([{"op": "CLICK", "target": "Send to a friend"}, {"op": "DONE"}])
    r2 = fr(None, "x", jev, target_id="T1", confirm=[confirm_json])
    assert r2.status == "claimed_done" and tab.acted[0][1] == "Send to a friend"
    assert not any("commit_ok" in q["questions"] for q in jev.requests)
    assert r2.trace[0].commit_gate["confirmed_by"] == "caller"


def test_confirm_entries_single_use_and_node_bound(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEND)])
    ref = {"target_id": "T1", "node": 50, "doc": "1000.5|https://example.test/", "label": "Send to a friend",
           "ctx": "c50"}
    jev = Jev([{"op": "CLICK", "target": "Send to a friend"}, {"op": "CLICK", "target": "Send to a friend"},
               {"op": "CLICK", "commit_ok": 0.1}])
    r = fr(None, "x", jev, target_id="T1", confirm=[ref])
    assert len(tab.acted) == 1 and r.reason == Reason.confirm_required


def test_stale_node_bound_confirm_never_falls_back(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEND, doc="2000.1")])
    ref = {"target_id": "T1", "node": 50, "doc": "1000.5|https://example.test/", "label": "Send to a friend",
           "ctx": "c50"}
    r = fr(None, "x", Jev([{"op": "CLICK", "target": "Send to a friend"}]), target_id="T1", confirm=[ref])
    assert r.reason == Reason.confirm_required and tab.acted == [] and r.data["doc"].startswith("2000.1")


def test_recycled_row_ctx_mismatch_is_stale(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage([act(1, "click", "Delete", 70, ctx="row-B")])])
    ref = {"target_id": "T1", "node": 70, "doc": "1000.5|u", "label": "Delete", "ctx": "row-A"}
    r = fr(None, "x", Jev([{"op": "CLICK", "target": "Delete"}]), target_id="T1", confirm=[ref])
    assert r.reason == Reason.confirm_required and tab.acted == []


def test_bare_label_confirm_only_if_unique_at_execution(monkeypatch):
    two = [act(1, "click", "Delete", 70, ctx="a"), act(2, "click", "Delete", 71, ctx="b")]
    tab = use_tab(monkeypatch, [mkpage(two)])
    r = fr(None, "x", Jev([{"op": "CLICK", "target": "Delete"}]), target_id="T1", confirm=["Delete"])
    assert r.reason == Reason.confirm_required and len(r.data["candidates"]) == 2 and tab.acted == []
    tab2 = use_tab(monkeypatch, [mkpage([act(1, "click", "Delete", 70)])])
    r = fr(None, "x", Jev([{"op": "CLICK", "target": "Delete"}, {"op": "DONE"}]), target_id="T1", confirm=["delete"])
    assert r.status == "claimed_done" and tab2.acted


# ------------------------------------------------------------------------------------------------ budgets
def test_budget_actions_requests_deadline(monkeypatch):
    pages = [mkpage(SEARCH, SEARCH_FIELDS, text=f"p{i}") for i in range(20)]
    use_tab(monkeypatch, pages)
    r = fr("https://example.test/", "x", Jev([{"op": "CLICK", "target": "Go"}]), max_actions=2)
    assert r.reason == Reason.budget_exhausted and "action budget" in r.detail
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS, text=f"q{i}") for i in range(20)])
    r = fr("https://example.test/", "x", Jev([{"op": "WAIT"}]), max_requests=3)
    assert r.reason == Reason.budget_exhausted and "request budget" in r.detail and r.stats["jev_calls"] == 3
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    r = fr("https://example.test/", "x", Jev([{"op": "WAIT"}]), timeout_s=0.05)
    assert r.reason == Reason.budget_exhausted


def test_deadline_enforced_inside_llm_wait(monkeypatch):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])

    class Slow(Backend):
        def batch(self, goal, fields, excerpt, deadline):
            time.sleep(0.5)
            return super().batch(goal, fields, excerpt, deadline)

    jev = Jev([{"op": "TYPE_TEXT", "target": "Search", "heads": {"value_in_goal_1": 0.9}}])
    started = time.monotonic()
    r = fr("https://example.test/", "Open the capital of France", jev, _backend=Slow({"Search": "Paris"}),
           timeout_s=0.2, speculate_text="off")
    assert time.monotonic() - started < 0.45
    assert r.reason in {Reason.text_value_unavailable, Reason.budget_exhausted}


def test_low_confidence_three_in_a_row_hands_back(monkeypatch):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS, text=f"t{i}") for i in range(10)])
    r = fr("https://example.test/", "x", Jev([{"op": "WAIT", "conf": 0.2}]))
    assert r.reason == Reason.low_confidence


def test_no_progress_three_unchanged_actions(monkeypatch):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    r = fr("https://example.test/", "x", Jev([{"op": "CLICK", "target": "Go"}]))
    assert r.reason == Reason.no_progress


# ------------------------------------------------------------------------------------------------ surfaces
FRAME = {"frames": [{"origin": "http://127.0.0.1:8766", "area_ratio": 0.3, "visible": True}]}
AUTH = {"password_fields": 1}
CANVAS = {"canvas_area_ratio": 0.6}
SHADOW = {"shadow_hosts": [{"area_ratio": 0.5, "interactive": True}]}


@pytest.mark.parametrize("surfaces,expected", [
    ({}, Reason.blocked),
    (FRAME, Reason.in_frame),
    ({**FRAME, **AUTH}, Reason.auth_required),
    ({**FRAME, **CANVAS}, Reason.in_frame),
    ({**CANVAS, **SHADOW}, Reason.visual_only),
    (SHADOW, Reason.in_shadow_dom),
    ({"frames": [{"origin": "x", "area_ratio": 0.05, "visible": True}]}, Reason.blocked),
])
def test_surface_precedence(monkeypatch, surfaces, expected):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS, surfaces=surfaces)])
    r = fr("https://example.test/", "x", Jev([{"op": "BLOCKED"}]))
    assert r.reason == expected
    assert "surfaces" in r.data and "frames" in r.data["surfaces"] or surfaces == {} or "surfaces" in r.data
    if expected != Reason.blocked:
        assert r.data["original"] == "blocked" and r.status == "handed_back"


def test_upload_trigger_hands_back_before_click(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage([act(1, "click", "Attach receipt", 80, upload_trigger=True)])])
    r = fr("https://example.test/", "x", Jev([{"op": "CLICK", "target": "Attach"}]))
    assert r.reason == Reason.upload and tab.acted == []


def test_file_input_activation_flag_hands_back_upload(monkeypatch):
    p = mkpage(SEARCH, SEARCH_FIELDS)
    p["file_activated"] = True
    use_tab(monkeypatch, [p])
    assert fr("https://example.test/", "x", Jev([{"op": "DONE"}])).reason == Reason.upload


def test_popup_after_click_registers_and_hands_back(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    tab.new_popups = ["P9"]
    r = fr("https://example.test/", "x", Jev([{"op": "CLICK", "target": "Go"}]))
    assert r.reason == Reason.popup_tab and r.data["target_id"] == "P9"


# ------------------------------------------------------------------------------------------------ failures
def test_browser_gone_hands_back_browser_error(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    tab.act_error = BrowserGone("Session with given id not found")
    r = fr("https://example.test/", "x", Jev([{"op": "CLICK", "target": "Go"}]))
    assert r.reason == Reason.browser_error


def test_observe_retries_exceed_limit_hands_back_stale_page(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    tab.observe_error = StalePage("never settles")
    assert fr("https://example.test/", "x", Jev([{"op": "DONE"}])).reason == Reason.stale_page


def test_dialog_during_act_logged_executed_unconfirmed_and_hands_back_dialog_open(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    tab.act_error = DialogSuspected("Input.dispatchMouseEvent", during_act=True)
    tab.dialog = {"type": "confirm", "message": "Sure?", "url": "https://example.test/", "session_id": "S1"}
    r = fr("https://example.test/", "x", Jev([{"op": "CLICK", "target": "Go"}]))
    assert r.reason == Reason.dialog_open and r.data["dialog"]["session_id"] == "S1"
    assert r.data["executed"] == "unconfirmed" and r.trace[-1].executed == "unconfirmed"
    history = run.read_run_file(run.run_path(r.run_id))["history"]
    assert history[-1]["executed"] == "unconfirmed"


def test_dialog_open_keeps_owned_session_and_returns_its_id(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    tab.act_error = DialogSuspected("Input.dispatchMouseEvent", during_act=True)
    tab.dialog = {"type": "alert", "message": "Hi", "url": "https://example.test/", "session_id": "S1"}
    r = fr("https://example.test/", "x", Jev([{"op": "CLICK", "target": "Go"}]))
    assert r.data["dialog"]["session_id"] == "S1"


def test_timeout_without_dialog_is_browser_error_unresponsive(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    tab.act_error = DialogSuspected("Input.dispatchMouseEvent", during_act=True)
    tab.dialog = None
    r = fr("https://example.test/", "x", Jev([{"op": "CLICK", "target": "Go"}]))
    assert r.reason == Reason.browser_error and r.detail == "page unresponsive"


def test_host_not_allowed_before_any_snapshot_send(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_ALLOWED_HOSTS", "example.org")
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    jev = Jev([{"op": "DONE"}])
    r = fr("https://example.test/", "x", jev)
    assert r.reason == Reason.host_not_allowed and jev.requests == [] and tab.navigated == []


def test_host_not_allowed_after_navigation(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_ALLOWED_HOSTS", "example.test")
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS, url="https://elsewhere.test/")])
    jev = Jev([{"op": "DONE"}])
    r = fr("https://example.test/", "x", jev)
    assert r.reason == Reason.host_not_allowed and jev.requests == []


def test_service_error_executes_nothing(monkeypatch):
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    r = fr("https://example.test/", "x", Jev([ServiceError("HTTP 500")]))
    assert r.reason == Reason.service_error and tab.acted == []


def test_422_hands_back_state_too_large(monkeypatch):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    assert fr("https://example.test/", "x", Jev([RequestTooLarge("max_tokens_exceeded")])).reason == \
        Reason.state_too_large


def test_require_key_preflight_no_tab(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    tab = use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    out = []
    r = run.fast_run("https://example.test/", "x", _out=lambda *a, **k: out.append(a[0]))
    assert r.reason == Reason.service_error and "TYPESAFE_API_KEY not set" in r.detail and tab.navigated == []


# ------------------------------------------------------------------------------------------------ trace/stats
def test_trace_contains_llm_latency_text_source_and_commit_gate(monkeypatch):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    jev = Jev([{"op": "TYPE_TEXT", "target": "Search", "heads": {"value_in_goal_1": 0.9}},
               lambda body: {"grounded": noul_answer(0.9)}, {"op": "CLICK", "target": "Go"}, {"op": "DONE"}])
    r = fr("https://example.test/", "Search the capital of France", jev, _backend=Backend({"Search": "Paris"}),
           speculate_text="off")
    t = r.trace[0]
    assert t.text_source == "llm" and t.llm_ms == 30 and t.jev_ms == 7 and t.top3
    assert r.trace[1].commit_gate == {"gated": False}
    assert r.stats["llm_cost_usd"] == pytest.approx(0.0007) and r.stats["jev_input_tokens"] >= 3000


def test_stats_record_llm_tokens_and_cost(monkeypatch):
    test_trace_contains_llm_latency_text_source_and_commit_gate(monkeypatch)


def test_trace_contains_no_authorization(monkeypatch):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    r = fr("https://example.test/", "x", Jev([{"op": "DONE"}]))
    text = run.run_path(r.run_id).read_text()
    assert "Authorization" not in text and "Bearer" not in text and "test" != os.environ.get("NOPE")


def test_sensitive_values_redacted_in_evidence_and_trace(monkeypatch):
    acts = [act(1, "fill", "Card number", 30, role="textbox", sensitive=True, handback_only=True, value="<redacted>"),
            act(2, "fill", "Email", 31, role="textbox", personal=True, value="p@x.test")]
    fields = [fld(30, "Card number", sensitive=True, value="<redacted>"),
              fld(31, "Email", personal=True, value="p@x.test", filled=True)]
    use_tab(monkeypatch, [mkpage(acts, fields)])
    r = fr("https://example.test/", "x", Jev([{"op": "DONE"}]))
    text = json.dumps(r.to_dict()) + run.run_path(r.run_id).read_text()
    assert "p@x.test" not in text


def test_personal_field_miss_never_reaches_llm(monkeypatch):
    acts = [act(1, "fill", "Email", 31, role="textbox", personal=True)]
    use_tab(monkeypatch, [mkpage(acts, [fld(31, "Email", personal=True)])])
    backend = Backend({"Email": "owner@real.test"})
    r = fr("https://example.test/", "Sign up for the newsletter",
           Jev([{"op": "TYPE_TEXT", "target": "Email", "heads": {"value_in_goal_1": 0.9}}]), _backend=backend)
    assert r.reason == Reason.text_value_unavailable and backend.calls == 0


def test_sigterm_finalises_run_file_and_kills_group(monkeypatch):
    killed = []
    monkeypatch.setattr(run, "_cleanup", lambda runner, result: killed.append(result))
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])

    def boom(body):
        import signal as s
        os.kill(os.getpid(), s.SIGTERM)
        time.sleep(0.1)
        return decide(body, "DONE")
    with pytest.raises(SystemExit):
        fr("https://example.test/", "x", Jev([boom]))
    assert killed and killed[0].reason == Reason.budget_exhausted and "SIGTERM" in killed[0].detail


def test_result_repr_is_compact(monkeypatch):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS, text="secret page text")])
    r = fr("https://example.test/", "x", Jev([{"op": "DONE"}]))
    assert "secret" not in repr(r) and re.match(r"RunResult\(status=claimed_done", repr(r))


# ---- verify-ready summary (cuts the caller's separate verification turn) ------------------------------------------
def summary_line(r):
    line = next(p for p in r.printed if p.startswith("JEV_BROWSE_RESULT="))
    return json.loads(line.split("=", 1)[1])


def test_prints_compact_verify_ready_summary_with_redaction(monkeypatch):
    monkeypatch.delenv("JEV_BROWSE_QUIET", raising=False)
    acts = [act(1, "fill", "Search", 10, role="searchbox"), act(2, "fill", "Email", 11, role="textbox", personal=True),
            act(3, "fill", "Card number", 12, role="textbox", sensitive=True, handback_only=True)]
    fields = [fld(10, "Search", value="Gödel", filled=True), fld(11, "Email", personal=True, value="p@x.test", filled=True),
              fld(12, "Card number", sensitive=True, value="<redacted>")]
    use_tab(monkeypatch, [mkpage(acts, fields, url="https://example.test/done", text="Result page " * 50)])
    r = fr("https://example.test/", "x", Jev([{"op": "DONE"}]))
    s = summary_line(r)
    assert s["status"] == "claimed_done" and s["url"] == "https://example.test/done" and s["target_id"] == "T1"
    assert {"label": "Search", "value": "Gödel"} in s["fields"]
    assert "p@x.test" not in json.dumps(s) and len(s["text_head"]) <= 300
    assert r.printed[0].startswith("JEV_BROWSE_RUN=") and r.printed[-1].startswith("JEV_BROWSE_RESULT=")


def test_summary_on_hand_back_and_quiet_env(monkeypatch):
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    s = summary_line(fr("https://example.test/", "x", Jev([{"op": "BLOCKED"}])))
    assert s["status"] == "blocked" and s["reason"] == "blocked"
    monkeypatch.setenv("JEV_BROWSE_QUIET", "1")
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    r = fr("https://example.test/", "x", Jev([{"op": "DONE"}]))
    assert not any(p.startswith("JEV_BROWSE_RESULT=") for p in r.printed)


def test_trace_records_text_backend_and_fallback(monkeypatch):
    from jev_browse.textgen import BatchResult

    class Fell(Backend):
        def batch(self, goal, fields, excerpt, deadline):
            r = super().batch(goal, fields, excerpt, deadline)
            r.fallback = {"from": "ollama", "why": "unreachable"}
            return r
    use_tab(monkeypatch, [mkpage(SEARCH, SEARCH_FIELDS)])
    jev = Jev([{"op": "TYPE_TEXT", "target": "Search", "heads": {"value_in_goal_1": 0.9}},
               lambda body: {"grounded": noul_answer(0.9)}, {"op": "DONE"}])
    r = fr("https://example.test/", "Open the capital city of France", jev, _backend=Fell({"Search": "Paris"}),
           speculate_text="off")
    assert r.trace[0].llm == {"backend": "claude", "model": "haiku", "fallback": {"from": "ollama", "why": "unreachable"}}
    assert BatchResult(values={}).fallback is None
