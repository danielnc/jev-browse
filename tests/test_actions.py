import pytest

from jev_browse import actions, config
from jev_browse.actions import (
    action_space,
    authorise,
    build_request,
    build_stage2,
    commit_question,
    estimate_tokens,
    field_keys,
    fit_budget,
    gate_for,
    match_values,
    read_decision,
    regions,
)
from jev_browse.results import HandBack, Reason
from tests.fakes import choice_answer, noul_answer


def act(i, kind, label, node, **kw):
    return {"id": f"e{i}", "kind": kind, "label": label, "node": node, "role": kw.pop("role", "button"),
            "value": kw.pop("value", ""), "region": kw.pop("region", "r1"), **kw}


def field(node, label, **kw):
    return {"node": node, "label": label, "role": "textbox", "visible": True, "sensitive": False, "personal": False,
            "value": "", "filled": False, "readonly": False, "heading": "", "name_attr": "", "id_attr": "",
            "placeholder": "", "region": "r1", **kw}


def page():
    return {
        "url": "https://example.test/", "title": "Search", "text": "Search",
        "actions": [
            act(1, "fill", "Search", 10, role="textbox"),
            act(2, "click", "Open Search", 10, role="textbox"),
            act(3, "click", "Go", 20),
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
        "fields": [field(10, "Search")],
        "regions": {"r1": {"heading": "Top", "first_labels": []}},
        "surfaces": {},
    }


def answers_for(req, op, target=None, **extra):
    a = {"operation": choice_answer(req.operations, op)}
    for name, q in req.body["questions"].items():
        if name.endswith("_target"):
            ids = list(q["criteria"])
            a[name] = choice_answer(ids, target if name == op.lower() + "_target" and target else ids[0])
    a.update(extra)
    return a


# ---- ported from browser-use/jev-ultrafast tests/test_agent.py (MIT). See NOTICE. ---------------------------
def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_all_heads_are_one_request_and_only_matching_head_executes():
    req = build_request(page(), "Find a book", [], candidates_enabled=False)
    assert {"operation", "click_target", "type_text_target"} <= set(req.body["questions"])
    a = answers_for(req, "TYPE_TEXT", "1")
    a["click_target"] = {"choice": "invented"}
    d = read_decision(req, a)
    assert d.operation == "TYPE_TEXT" and d.target == "1" and d.action_id == "e1"


def test_click_cannot_consume_a_text_target():
    req = build_request(page(), "Find a book", [], candidates_enabled=False)
    a = answers_for(req, "CLICK")
    a["click_target"] = choice_answer(["1", "2", "999"], "999")
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        read_decision(req, a)


def test_target_head_receives_control_state_and_full_next_step_rules():
    p = page()
    p["actions"].insert(0, act(0, "click", "Free cancellation", 30, role="checkbox", checked="true", selected=False))
    req = build_request(p, "Search with free cancellation", [], candidates_enabled=False)
    target = req.body["questions"]["click_target"]
    assert target["criteria"]["1"]["checked"] == "true"
    assert target["criteria"]["1"]["selected"] is False
    ops = req.body["questions"]["operation"]
    assert ops["instructions"]["rules"] in target["instructions"]["rules"]
    d = read_decision(req, answers_for(req, "CLICK", "3"))
    assert d.action_id == "e3"


# ---- value heads ------------------------------------------------------------------------------------------
def multi_field_page():
    p = page()
    p["actions"] = [
        act(1, "fill", "Where from?", 11, role="combobox"),
        act(2, "fill", "Where to?", 12, role="combobox"),
        act(3, "fill", "Card number", 13, role="textbox", sensitive=True, handback_only=True, value="<redacted>"),
        act(4, "fill", "Email", 14, role="textbox", personal=True, value="me@x.test"),
        act(5, "click", "Search", 20),
    ]
    p["fields"] = [field(11, "Where from?", value="London"), field(12, "Where to?"),
                   field(13, "Card number", sensitive=True, value="<redacted>"),
                   field(14, "Email", personal=True, value="me@x.test", filled=True)]
    return p


def test_value_heads_only_for_editable_nonsensitive_unvalued_fields_capped():
    req = build_request(multi_field_page(), "Fly from Zurich to London", [], value_heads=6)
    heads = {vf["label"] for vf in req.value_fields.values()}
    assert heads == {"Where from?", "Where to?", "Email"}
    req2 = build_request(multi_field_page(), "Fly from Zurich to London", [], value_heads=1)
    assert len(req2.value_fields) == 1


def test_field_with_caller_value_gets_no_value_head():
    req = build_request(multi_field_page(), "Fly from Zurich to London", [], values={11: "Zurich"})
    assert "Where from?" not in {vf["label"] for vf in req.value_fields.values()}


def test_zero_candidate_field_gets_only_value_in_goal_noul():
    req = build_request(multi_field_page(), "the", [], candidates_enabled=True)
    qs = req.body["questions"]
    assert "value_in_goal_1" in qs and "value_1" not in qs
    req2 = build_request(multi_field_page(), "Fly from Zurich", [], candidates_enabled=False)
    assert "value_1" not in req2.body["questions"] and "value_in_goal_1" in req2.body["questions"]


def test_value_head_premise_names_field_label():
    req = build_request(multi_field_page(), "Fly from Zurich to London", [])
    assert req.body["questions"]["value_in_goal_1"]["instructions"]["field"] == "[1] Where from?"
    assert req.body["questions"]["value_1"]["instructions"]["field"] == "[1] Where from?"


def test_prefilled_field_gets_value_head():
    req = build_request(multi_field_page(), "Fly from Zurich to London", [])
    assert any(vf["label"] == "Where from?" for vf in req.value_fields.values())  # prefilled with London


def _type_text_answers(req, target, **heads):
    a = answers_for(req, "TYPE_TEXT", target)
    for name, value in heads.items():
        a[name] = value
    return a


def test_value_consumed_only_when_type_text_on_that_field():
    req = build_request(multi_field_page(), "Fly from Zurich to London", [])
    cands = req.value_fields["1"]["candidates"]
    heads = {"value_1": choice_answer(cands + ["none"], "Zurich", 0.9), "value_in_goal_1": noul_answer(0.9)}
    d = read_decision(req, _type_text_answers(req, "2", **heads))
    assert d.value is None and d.value_index == "2"
    d2 = read_decision(req, _type_text_answers(req, "1", **heads))
    assert d2.value == "Zurich" and d2.value_source == "candidate"
    click = answers_for(req, "CLICK")
    click.update(heads)
    assert read_decision(req, click).value is None


@pytest.mark.parametrize("choice,conf,in_goal,expected", [
    ("none", 0.9, 0.9, None), ("Zurich", 0.59, 0.9, None), ("Zurich", 0.9, 0.49, None), ("Zurich", 0.6, 0.5, "Zurich"),
])
def test_value_gate_thresholds(choice, conf, in_goal, expected):
    req = build_request(multi_field_page(), "Fly from Zurich to London", [])
    cands = req.value_fields["1"]["candidates"] + ["none"]
    a = _type_text_answers(req, "1", value_1=choice_answer(cands, choice, conf), value_in_goal_1=noul_answer(in_goal))
    assert read_decision(req, a).value == expected


def test_miss_requires_value_in_goal_and_no_passing_candidate():
    req = build_request(multi_field_page(), "Click the Search button", [])
    heads = {f"value_in_goal_{i}": noul_answer(0.1) for i in req.value_fields}
    d = read_decision(req, _type_text_answers(req, "1", **heads))
    assert d.miss_fields == [] and d.value_source == "none"
    heads["value_in_goal_2"] = noul_answer(0.8)
    d = read_decision(req, _type_text_answers(req, "2", **heads))
    assert [m[2] for m in d.miss_fields] == ["Where to?"] and d.value_source == "miss"


def test_personal_field_is_never_a_miss():
    req = build_request(multi_field_page(), "Sign up for the newsletter", [])
    heads = {f"value_in_goal_{i}": noul_answer(0.9) for i in req.value_fields}
    d = read_decision(req, _type_text_answers(req, "1", **heads))
    assert "Email" not in [m[2] for m in d.miss_fields]


def test_personal_value_never_sent_three_state():
    p = multi_field_page()
    req = build_request(p, "Sign up", [])
    assert "me@x.test" not in str(req.body)
    email = next(e for e in req.elements if e["label"] == "Email")
    assert email["value"] == "filled"
    req2 = build_request(p, "Sign up", [], values={14: "you@y.test"})
    assert next(e for e in req2.elements if e["label"] == "Email")["value"] == "filled (differs from requested value)"
    req3 = build_request(p, "Sign up", [], values={14: "ME@x.test"})
    assert next(e for e in req3.elements if e["label"] == "Email")["value"] == "filled (matches requested value)"


def test_sensitive_field_is_handback_only_target_never_value():
    req = build_request(multi_field_page(), "Pay", [])
    crit = req.body["questions"]["type_text_target"]["criteria"]
    card = next(v for v in crit.values() if "Card number" in v["element"])
    assert card["current_value"] == "<redacted>" and "never types" in card["note"]


def test_values_keys_label_or_stable_key_not_index():
    p = multi_field_page()
    by_node, _, hb = match_values({"where to?": "Paris", "Where from?": "Lyon"}, p)
    assert hb is None and by_node == {12: "Paris", 11: "Lyon"}
    by_node, _, _ = match_values({"1": "x", "e1": "y"}, p)
    assert by_node == {}
    by_node, cands, _ = match_values(["Paris"], p)
    assert by_node == {} and cands == ["Paris"]


def test_values_duplicate_label_hands_back_ambiguous_key():
    p = page()
    p["fields"] = [field(1, "Name", heading="Traveller 1"), field(2, "Name", heading="Traveller 2")]
    _, _, hb = match_values({"name": "Ana"}, p)
    assert isinstance(hb, HandBack) and hb.reason == Reason.text_value_unavailable and hb.detail == "ambiguous key"
    assert {f["key"] for f in hb.data["fields"]} == {"Name / Traveller 1", "Name / Traveller 2"}
    by_node, _, hb = match_values({"Name / Traveller 2": "Ana"}, p)
    assert hb is None and by_node == {2: "Ana"}


def test_field_keys_unique_with_region_and_ordinal():
    fs = [field(1, "Name", heading="A"), field(2, "Name", heading="B"), field(3, "Name", heading="B"),
          field(4, "", name_attr="q"), field(5, "Email")]
    keys = field_keys(fs)
    assert len(set(keys)) == len(keys)
    assert keys[0] == "Name / A" and keys[1] == "Name / B #1" and keys[2] == "Name / B #2" and keys[3] == "q"


# ---- commit gate ------------------------------------------------------------------------------------------
def test_commit_verbs_whole_word_folded(monkeypatch):
    assert not gate_for({"kind": "click", "label": "Postal code"}).gated
    assert gate_for({"kind": "click", "label": "EXCLUIR conta"}).gated
    assert gate_for({"kind": "click", "label": "Send to a friend"}).verb == "send"
    assert gate_for({"kind": "click", "label": "Archive"}).gated  # the known false-positive case
    monkeypatch.setenv("JEV_BROWSE_COMMIT_VERBS", "")
    assert not gate_for({"kind": "click", "label": "Delete"}).gated
    monkeypatch.setenv("JEV_BROWSE_COMMIT_VERBS_EXTRA", "frob")
    assert gate_for({"kind": "click", "label": "Frob it"}).gated


def test_no_commit_heads_in_decision_request():
    p = page()
    p["actions"].insert(0, act(0, "click", "Delete account", 40))
    req = build_request(p, "Search", [])
    assert not any("commit" in name for name in req.body["questions"])


def test_gated_choice_triggers_one_followup_with_context():
    dialog_done = {"kind": "click", "label": "Done", "in_dialog": True, "role": "button"}
    q = commit_question("Pick Oct 20", dialog_done, "Select dates. Oct 20 selected.")
    assert q["type"] == "noul" and q["instructions"]["dialog"].startswith("Select dates")
    assert q["criteria"]["true"].startswith("The dialog's action")
    submit = {"kind": "click", "label": "Continue", "submit": True, "form_personal": True, "role": "button"}
    assert gate_for(submit).kind == "structural"
    icon = {"kind": "click", "label": "", "unnamed": True, "role": "button"}
    assert gate_for(icon).kind == "structural"
    verb = commit_question("Delete the draft", {"kind": "click", "label": "Delete"})
    assert "dialog" not in verb["instructions"] and verb["instructions"]["button"] == "Delete"


def test_generic_confirmation_matches_whole_label_only():
    assert gate_for({"kind": "click", "label": "Done", "in_dialog": True}).gated
    assert not gate_for({"kind": "click", "label": "Done. Search for one-way flights", "in_dialog": True}).gated
    assert not gate_for({"kind": "click", "label": "Submit", "in_dialog": False, "submit": True}).gated


def test_commit_gate_needs_authorisation_and_confidence():
    g = gate_for({"kind": "click", "label": "Delete"})
    assert authorise(g, 0.69, 0.9, 0.9) == "confirm_required"
    assert authorise(g, 0.9, 0.59, 0.9) == "confirm_required"
    assert authorise(g, 0.9, 0.9, 0.59) == "confirm_required"
    assert authorise(g, 0.7, 0.6, 0.6) == "ok"
    assert authorise(g, None, 0.9, 0.9, confirmed=True) == "ok"
    assert authorise(gate_for({"kind": "click", "label": "Go"}), None, 0.1, 0.1) == "ok"


def test_decision_marks_gated_target():
    p = page()
    p["actions"].insert(0, act(0, "click", "Delete account", 40))
    req = build_request(p, "Search", [], candidates_enabled=False)
    d = read_decision(req, answers_for(req, "CLICK", "1"))
    assert d.gated and d.gate.verb == "delete"


# ---- size handling ----------------------------------------------------------------------------------------
def big_page(n=600, region="r1", heading_every=None):
    acts = [act(i, "click", f"Link {i}", 1000 + i, role="link", region=region,
                heading=(f"H{i // heading_every}" if heading_every else "")) for i in range(n)]
    return {"url": "https://x/", "title": "Many", "text": "Many", "actions": acts, "fields": [],
            "regions": {region: {"heading": "Main", "first_labels": []}}, "surfaces": {}}


def test_per_head_option_cap_254_applied_here_not_in_snapshot():
    req = build_request(big_page(254), "Open Link 5", [])
    assert "click_target" in req.body["questions"] and req.stage == "single"
    req = build_request(big_page(255), "Open Link 5", [])
    assert "click_region" in req.body["questions"] and "click_target" not in req.body["questions"]


def test_600_links_in_one_main_split_into_subregions():
    regs = regions(big_page(600))
    assert len(regs) >= 3 and all(len(r.action_ids) <= 254 for r in regs)
    assert sum(len(r.action_ids) for r in regs) == 600


def test_two_stage_fast_run_shape():
    p = big_page(600)
    p["actions"].append(act(900, "fill", "Search", 5, role="textbox"))
    p["fields"] = [field(5, "Search")]
    req = build_request(p, "Search for Link 5", [])
    qs = req.body["questions"]
    assert req.stage == "stage1" and {"operation", "click_region", "type_text_target"} <= set(qs)
    assert any(k.startswith("value_in_goal_") for k in qs)
    regs = list(req.regions["CLICK"])
    stage1 = {"operation": choice_answer(req.operations, "CLICK"), "click_region": choice_answer(regs, regs[1])}
    s2, op = build_stage2(req, p, "Search for Link 5", [], stage1)
    assert op == "CLICK" and set(s2.body["questions"]) == {"click_target"}
    assert len(s2.body["questions"]["click_target"]["criteria"]) == len(req.regions["CLICK"][regs[1]])


def test_region_then_element_maps_to_observed_node():
    p = big_page(600)
    req = build_request(p, "Open Link 300", [])
    regs = req.regions["CLICK"]
    rid = next(r for r, ids in regs.items() if "e300" in ids)
    s2, _ = build_stage2(req, p, "Open Link 300", [], {"operation": choice_answer(req.operations, "CLICK"),
                                                      "click_region": choice_answer(list(regs), rid)})
    index = next(i for i, a in s2.targets["CLICK"].items() if a["id"] == "e300")
    d = read_decision(s2, {"operation": choice_answer(req.operations, "CLICK"),
                           "click_target": choice_answer(list(s2.targets["CLICK"]), index)})
    assert d.action["node"] == 1300


def test_select_with_300_options_hands_back_too_many_options():
    p = page()
    p["actions"].insert(0, act(0, "select", "Country → X", 50, role="combobox", option_count=300))
    out = fit_budget(p, "Pick X", [])
    assert isinstance(out, HandBack) and out.reason == Reason.too_many_options


def test_fit_budget_checks_longest_and_total(monkeypatch):
    req = build_request(page(), "Find", [])
    longest, total = estimate_tokens(req.body)
    assert 0 < longest <= total
    monkeypatch.setattr(config, "TOTAL_BUDGET", total - 1)
    monkeypatch.setattr(config, "LONGEST_BUDGET", 10**9)
    assert not actions.fits(req.body)


def test_fit_budget_trim_order(monkeypatch):
    p = multi_field_page()
    p["text"] = "x" * 6000
    full = build_request(p, "Fly from Zurich to London", [])
    _, total = estimate_tokens(full.body)
    monkeypatch.setattr(config, "TOTAL_BUDGET", total - 100)
    out = fit_budget(p, "Fly from Zurich to London", [])
    assert out.trimmed == [{"text_limit": 3000}]
    monkeypatch.setattr(config, "TOTAL_BUDGET", total - 1700 / 3.5 * 3)
    out = fit_budget(p, "Fly from Zurich to London", [])
    assert out.trimmed[:2] == [{"text_limit": 3000}, {"text_limit": 1500}]


def test_fit_budget_hands_back_state_too_large(monkeypatch):
    monkeypatch.setattr(config, "TOTAL_BUDGET", 10)
    out = fit_budget(page(), "Find", [])
    assert isinstance(out, HandBack) and out.reason == Reason.state_too_large


def test_marker_rechecked_after_commit_followup():
    # The loop re-checks the page marker after the follow-up; here the follow-up request is standalone.
    q = commit_question("Send it", {"kind": "click", "label": "Send"})
    assert set(q) == {"type", "instructions", "criteria"}


def test_confirm_list_authorises_label_without_noul():
    g = gate_for({"kind": "click", "label": "Send"})
    assert authorise(g, None, 0.9, 0.9, confirmed=True) == "ok"
