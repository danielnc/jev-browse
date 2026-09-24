import base64
import datetime as dt
import json

import pytest

from bench import arms, cost, decide, verify
from bench.run_bench import private_safe
from bench.tasks import load_tasks

TODAY = dt.date(2026, 9, 24)


# ---- verification predicates -----------------------------------------------------------------------------
def test_wiki_and_llm_predicates():
    assert verify.verify_wiki({"url": "https://en.wikipedia.org/wiki/G%C3%B6del%27s_incompleteness_theorems"})["passed"]
    assert verify.verify_wiki({"url": "https://en.wikipedia.org/wiki/Gödel's_incompleteness_theorems#Background"})["passed"]
    assert not verify.verify_wiki({"url": "https://en.wikipedia.org/wiki/Kurt_Gödel"})["passed"]
    assert verify.verify_llm({"url": "https://en.wikipedia.org/wiki/Paris"})["passed"]
    assert not verify.verify_llm({"url": "https://en.wikipedia.org/wiki/France"})["passed"]


HOTEL_OK = {"title": "Casa Flora · Forma",
            "text": "Casa Flora\nYour filters: Design · Free cancellation enabled · Destination Lisbon · Dates Oct 20"}


def test_hotel_predicate_positive_and_negative():
    assert verify.verify_hotel(HOTEL_OK, [])["passed"]
    assert not verify.verify_hotel({**HOTEL_OK, "title": "The Glasshouse · Forma"}, [])["passed"]
    assert not verify.verify_hotel({**HOTEL_OK, "text": HOTEL_OK["text"].replace("enabled", "off")}, [])["passed"]
    assert not verify.verify_hotel(HOTEL_OK, ["SENT 2026-09-24T10:00:00"])["passed"]


def test_iframe_predicate():
    assert verify.verify_iframe(["RESERVED Casa Flora 2026-09-24T10:00:00"])["passed"]
    assert not verify.verify_iframe(["RESERVED Serra Lodge 2026-09-24T10:00:00"])["passed"]


def test_fixture_log_scoped_to_attempt():
    log = "RESERVED Casa Flora t1\nSENT t2\n"
    offset = len(log)
    log += "RESERVED Serra Lodge t3\n"
    lines = verify.fixture_lines(log, offset)
    assert lines == ["RESERVED Serra Lodge t3"]
    assert not verify.verify_iframe(lines)["passed"]  # an earlier attempt's line neither passes...
    assert verify.verify_hotel(HOTEL_OK, verify.fixture_lines(log, offset))["passed"]  # ...nor fails this one


def flights_state(date="2026-10-20", one_way=True, **over):
    head = b"\x08\x1c\x10\x02" if one_way else b"\x08\x1c\x10\x01"
    tfs = base64.urlsafe_b64encode(head + f"x{date}y".encode()).decode().rstrip("=")
    state = {"url": f"https://www.google.com/travel/flights/search?tfs={tfs}",
             "controls": [{"label": "Change ticket type. One way", "value": "One way"},
                          {"label": "Where from?", "value": "Zürich"}, {"label": "Where to?", "value": "London"},
                          {"label": "Departure", "value": "Tue, Oct 20"}],
             "flights": ["Nonstop flight on Tuesday, October 20. Select flight", "Select flight"]}
    state.update(over)
    return state


@pytest.mark.parametrize("change", [None, "Where from?", "Where to?", "Departure", "results", "page"])
def test_flight_verification_rejects_wrong_trip(change):
    s = flights_state()
    if change in {"Where from?", "Where to?"}:
        next(c for c in s["controls"] if c["label"] == change)["value"] = "Paris"
    elif change == "Departure":
        s["url"] = "https://www.google.com/travel/flights/search?tfs=AAAA"
        next(c for c in s["controls"] if c["label"] == "Departure")["value"] = "Wed, Oct 21"
    elif change == "results":
        s["flights"] = ["Nonstop flight on Wednesday, October 21. Select flight"]
    elif change == "page":
        s["url"] = "https://www.google.com/travel/flights"
    assert verify.verify_flights(s, "2026-10-20")["passed"] is (change is None)


def test_target_selection_rule():
    before = ["OLD"]
    after = [{"targetId": "OLD", "url": "https://en.wikipedia.org/"}, {"targetId": "N1", "url": "https://en.wikipedia.org/x"},
             {"targetId": "N2", "url": "https://en.wikipedia.org/y"}, {"targetId": "N3", "url": "https://other/"}]
    assert verify.select_target(before, after, "wikipedia.org", preferred=["N2"]) == ("N2", False, ["N1", "N2"])
    assert verify.select_target(before, after, "wikipedia.org") == (None, True, ["N1", "N2"])
    assert verify.select_target(before, after[:2], "wikipedia.org") == ("N1", False, ["N1"])
    assert verify.select_target(before, after[:1], "wikipedia.org", current="CUR") == ("CUR", False, [])


# ---- cost ---------------------------------------------------------------------------------------------------
def test_cost_components_and_bases():
    ev = {"modelUsage": {"claude-opus-5-5": {"inputTokens": 1000, "outputTokens": 100, "cacheReadInputTokens": 100000,
                                             "cacheCreationInputTokens": 10000}},
          "total_cost_usd": 0.1}
    c = cost.attempt_cost(claude_event=ev, jev_input=1_000_000, llm_usage={"input_tokens": 1000, "output_tokens": 100},
                          llm_model="haiku")
    opus = c["parts"]["calling_claude"]
    assert opus["cache_neutral"] == pytest.approx((1000 + 100000 + 10000) * 4 / 1e6 + 100 * 20 / 1e6)
    assert opus["as_billed"] == pytest.approx(1000 * 4 / 1e6 + 10000 * 5 / 1e6 + 100000 * 0.2 / 1e6 + 100 * 20 / 1e6)
    assert opus["reported_total_cost_usd"] == 0.1
    assert c["parts"]["jev"]["cache_neutral"] == pytest.approx(0.042)
    assert c["parts"]["text_backend"]["cache_neutral"] == pytest.approx(1000 * 1 / 1e6 + 100 * 5 / 1e6)
    assert c["cache_neutral"] == pytest.approx(sum(p["cache_neutral"] for p in c["parts"].values()))


def test_cost_usage_fallback_and_model_prefix():
    ev = {"usage": {"input_tokens": 10, "output_tokens": 1}}
    assert cost.claude_cost(ev, "opus")["cache_neutral"] == pytest.approx((10 * 4 + 1 * 20) / 1e6)
    assert cost.rate("claude-haiku-4-5-20251001")["input"] == 1.0


def test_missing_rate_raises_instead_of_zero():
    with pytest.raises(cost.MissingRate):
        cost.rate("gpt-5.6-luna")


# ---- decision rule ------------------------------------------------------------------------------------------
def rows(task, arm, walls, costs, passes, **kw):
    return [{"task": task, "arm": arm, "wall_s": w, "cost": c, "passed": p, **kw}
            for w, c, p in zip(walls, costs, passes, strict=True)]


def test_same_speed_cheaper_is_a_win():
    a = rows("wiki", "A", [10, 10, 10], [0.1] * 3, [1] * 3)
    b = rows("wiki", "B", [10, 10, 10], [0.2] * 3, [1] * 3)
    assert decide.verdict(a, b)["win"] is True


def test_faster_but_dearer_loses():
    a = rows("wiki", "A", [5, 5, 5], [0.3] * 3, [1] * 3)
    b = rows("wiki", "B", [10, 10, 10], [0.2] * 3, [1] * 3)
    assert decide.verdict(a, b)["win"] is False


def test_cheaper_with_one_fewer_pass_loses():
    a = rows("wiki", "A", [5] * 3, [0.1] * 3, [1, 1, 0])
    b = rows("wiki", "B", [10] * 3, [0.2] * 3, [1, 1, 1])
    assert decide.verdict(a, b)["win"] is False


def test_band_edge():
    b = rows("wiki", "B", [10, 10, 10], [0.2] * 3, [1] * 3)  # range 0 -> band 10%
    assert decide.verdict(rows("wiki", "A", [11] * 3, [0.1] * 3, [1] * 3), b)["win"] is True
    assert decide.verdict(rows("wiki", "A", [11.01] * 3, [0.1] * 3, [1] * 3), b)["win"] is False
    wide = rows("wiki", "B", [8, 10, 14], [0.2] * 3, [1] * 3)  # (14-8)/10/2 = 30%
    assert decide.verdict(rows("wiki", "A", [13] * 3, [0.1] * 3, [1] * 3), wide)["band"] == 0.3


def test_arm_timeout_counts_and_infra_error_excluded():
    all_rows = (rows("wiki", "A", [5, 5, 900], [0.1] * 3, [1, 1, 0], timed_out=False)
                + rows("wiki", "B", [10] * 3, [0.2] * 3, [1] * 3)
                + [{"task": "wiki", "arm": "A", "wall_s": 0, "cost": 0, "passed": False, "infra_error": True}])
    out = decide.decide(all_rows)
    wiki = out["classes"]["wiki"]
    assert wiki["n_a"] == 3 and wiki["a_passes"] == 2 and wiki["win"] is False


def test_hinges_true_and_false():
    a = rows("wiki", "A", [5, 5, 5], [0.1] * 3, [1, 1, 0])
    b = rows("wiki", "B", [10] * 3, [0.2] * 3, [1] * 3)
    assert decide.hinges(a, b) is True
    a_all = rows("wiki", "A", [5] * 3, [0.1] * 3, [1] * 3)
    assert decide.hinges(a_all, b) is False


def test_llm_and_iframe_never_vote_and_opt_in_threshold():
    base = []
    for t in ("wiki", "flights", "hotel", "llm", "iframe"):
        base += rows(t, "B", [10] * 3, [0.2] * 3, [1] * 3)
    one_loss = base + rows("wiki", "A", [50] * 3, [1] * 3, [1] * 3) + rows("flights", "A", [5] * 3, [0.1] * 3, [1] * 3) \
        + rows("hotel", "A", [5] * 3, [0.1] * 3, [1] * 3) + rows("llm", "A", [50] * 3, [1] * 3, [0] * 3) \
        + rows("iframe", "A", [50] * 3, [1] * 3, [0] * 3)
    out = decide.decide(one_loss)
    assert set(out["classes"]) == {"wiki", "flights", "hotel"} and set(out["reported"]) == {"llm", "iframe"}
    assert out["losses"] == 1 and out["opt_in"] is False
    two = [r for r in one_loss if not (r["task"] == "flights" and r["arm"] == "A")] + \
        rows("flights", "A", [50] * 3, [1] * 3, [1] * 3)
    assert decide.decide(two)["opt_in"] is True


def test_routing_uses_helpers_only_if_a_int_won():
    base = rows("hotel", "B", [10] * 3, [0.2] * 3, [1] * 3) + rows("hotel", "A", [50] * 3, [1] * 3, [1] * 3)
    lost_int = base + rows("hotel", "A-int", [50] * 3, [1] * 3, [1] * 3)
    assert decide.decide(lost_int)["routing"]["hotel"] == "harness"
    won_int = base + rows("hotel", "A-int", [5] * 3, [0.1] * 3, [1] * 3)
    assert decide.decide(won_int)["routing"]["hotel"] == "interactive helpers"


# ---- arms and tasks -----------------------------------------------------------------------------------------
def test_arm_tasks_restrictions():
    assert arms.ARM_TASKS["A-int"] == ("hotel",)
    assert arms.ARM_TASKS["A0-llm"] == arms.ARM_TASKS["A-llm"] == arms.ARM_TASKS["A-none"] == ("llm",)
    assert arms.CLAUDE_ARMS >= {"A", "A-int", "B", "A-llm", "A-none"}
    assert arms.env_for("A-none", "llm") == {"JEV_BROWSE_VALUE_CANDIDATES": "0", "JEV_BROWSE_TEXT_BACKEND": "none"}
    assert arms.env_for("B", "wiki") == {"JEV_BROWSE_DISABLE": "1"}


@pytest.mark.parametrize("arm", ["A", "B", "A0", "A-int"])
def test_private_task_forces_text_backend_none_for_every_arm(arm):
    assert arms.env_for(arm, "private")["JEV_BROWSE_TEXT_BACKEND"] == "none"


def test_prompts_differ_only_in_tool_instruction():
    task = load_tasks(TODAY)["wiki"]
    a, b = arms.prompt("A", task), arms.prompt("B", task)
    base = arms.prompt("A-unprompted", task)
    assert a.startswith(base) and b.startswith(base) and a != b


def test_tasks_flights_date_and_private_skip(tmp_path):
    tasks = load_tasks(TODAY, private_path=tmp_path / "none.json")
    assert "private" not in tasks
    assert "October 20, 2026" in tasks["flights"].goal
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"url": "https://app.test/", "goal": "g", "domain": "app.test", "verify": {}}))
    assert "private" in load_tasks(TODAY, private_path=p)


def stream(*events):
    return "\n".join(json.dumps(e) for e in events)


def bash(cmd):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}


def test_audit_forbidden_jev_used_and_caller_values():
    s = stream({"type": "system", "subtype": "init", "tools": ["Bash", "Skill"]},
               {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill",
                                                               "input": {"skill": "jev-browse"}}]}},
               bash("browser-harness <<'PY'\nr = fast_run(u, g, values={'Search': 'Paris'})\nPY"),
               {"type": "result", "num_turns": 4, "usage": {"input_tokens": 5}, "total_cost_usd": 0.01})
    a = arms.audit(s, "A")
    assert a["jev_used"] and a["caller_values"] and a["skill_loaded"] and not a["forbidden_used"]
    assert a["result"]["num_turns"] == 4
    assert arms.audit(s, "B")["forbidden_used"] and arms.audit(s, "A-int")["forbidden_used"]
    violation = stream(bash("r = fast_run(u, g)\nif r.reason == 'confirm_required':\n    fast_run(None, g, confirm=[r.data])"))
    assert arms.audit(violation, "A")["gate_violation"]


def test_private_rows_carry_only_label_and_metrics():
    secret = {"url": "https://private.app.test/items/123", "goal": "archive item X", "marker": "SECRET-MARKER"}
    row = {"attempt_id": "x", "task": "private", "arm": "A", "rep": 0, "passed": True, "wall_s": 3.0, "cost": 0.1,
           "checks": {f"url:{secret['url']}": True, f"text:{secret['marker']}": True}, "reasons": [secret["goal"]],
           "detail": secret["goal"], "init": {"tools": ["Bash"]}}
    safe = private_safe(row)
    text = json.dumps(safe)
    assert all(v not in text for v in secret.values())
    assert safe["label"] == "private app flow" and safe["wall_s"] == 3.0


def test_single_model_uses_usage_cache_split():
    ev = {"modelUsage": {"claude-opus-5-5": {"inputTokens": 16, "outputTokens": 2544, "cacheReadInputTokens": 261335,
                                             "cacheCreationInputTokens": 59121}},
          "usage": {"input_tokens": 16, "output_tokens": 2544, "cache_read_input_tokens": 261335,
                    "cache_creation_input_tokens": 59121, "cache_creation": {"ephemeral_1h_input_tokens": 59121}},
          "total_cost_usd": 0.576179}
    c = cost.claude_cost(ev, "opus")
    assert c["as_billed"] == pytest.approx(0.576179, rel=0.01)  # matches Claude Code's own list-price figure
    assert c["cache_neutral"] == pytest.approx((16 + 261335 + 59121) * 4 / 1e6 + 2544 * 20 / 1e6)


def test_flights_one_way_read_from_tfs_when_no_control():
    s = flights_state()
    s["controls"] = [c for c in s["controls"] if "ticket" not in c["label"]]
    assert verify.verify_flights(s, "2026-10-20")["checks"]["one_way"]
    rt = flights_state(one_way=False)
    rt["controls"] = [c for c in rt["controls"] if "ticket" not in c["label"]]
    assert not verify.verify_flights(rt, "2026-10-20")["passed"]


def test_private_predicate_url_not_contains_and_prefilled_value():
    spec = {"url_contains": ["/c/"], "url_not_contains": ["ORIGINAL"], "text_contains": ["Forked", "Say hello."]}
    ok = {"url": "https://app.test/c/NEW", "text": "Forked from earlier", "controls": [{"label": "Message",
                                                                                         "value": "Say hello."}]}
    assert verify.verify_private(ok, spec)["passed"]
    assert not verify.verify_private({**ok, "url": "https://app.test/c/ORIGINAL"}, spec)["passed"]
    assert not verify.verify_private({**ok, "controls": []}, spec)["passed"]


def test_flights_origin_label_with_airport_suffix():
    s = flights_state()
    for c in s["controls"]:
        if c["label"] == "Where from?":
            c["label"] = "Where from? Zürich ZRH"
    assert verify.verify_flights(s, "2026-10-20")["passed"]


def test_jev_totals_records_each_text_calls_canary():
    from bench.run_bench import jev_totals

    trace = [{"llm": {"backend": "ollama", "canary": {"ok": True, "why": None, "ms": 2400, "cached": True}}},
             {"llm": {"backend": "claude", "fallback": {"from": "ollama", "why": "unreachable"}}},
             {"action": "click"}]
    tot, _reasons = jev_totals([{"result": {"status": "claimed_done", "trace": trace, "stats": {"llm_calls": 2}}}])
    assert tot["llm_backends"] == ["ollama", "claude"]
    assert tot["llm_canary"] == [{"ok": True, "cached": True, "ms": 2400}, None]


def test_summary_is_written_next_to_its_rows():
    from pathlib import Path

    from bench.run_bench import summary_path

    assert summary_path(Path("/x/rows.jsonl")) == Path("/x/summary.json")
    assert summary_path(Path("/x/ollama_rows.jsonl")) == Path("/x/ollama_rows.summary.json")


def test_report_without_results_explains_instead_of_crashing(tmp_path):
    from bench import report

    with pytest.raises(SystemExit) as exc:
        report.main([str(tmp_path / "rows.jsonl")])
    assert "run bench/run_bench.py first" in str(exc.value)
