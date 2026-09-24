"""Regression: jev-browse and the benchmark close only tabs they provably created (never by diff, blankness,
'current', or 'newest'). A user's pre-existing blank tab, and a tab the user opens mid-attempt, must survive."""

import re
import time
from pathlib import Path

import pytest

from bench import run_bench
from jev_browse import harness_api, run, tab
from jev_browse.tab import OwnedTab, Registry
from tests.fakes import FakeCDP, choice_answer

ROOT = Path(__file__).resolve().parents[1]


def page_for(url="https://example.test/"):
    return {"url": url, "href": url, "title": "T", "text": "done", "doc": "1.5", "w": 1000, "h": 800,
            "actions": [{"id": "wait", "kind": "wait", "label": "Wait"}], "fields": [], "regions": {},
            "surfaces": {}, "marker": ["m"], "page_key": ["k"], "guards": {}, "scroll": {"y": 0},
            "file_activated": False}


@pytest.fixture
def chrome(tmp_path, monkeypatch):
    f = FakeCDP()
    f.socket_dir = tmp_path
    f.tmp_dir = tmp_path
    f.daemon_name = "jevbench"
    # the user's own tabs: a blank one and a New Tab Page, both pre-existing
    f.targets["USER_BLANK"] = {"targetId": "USER_BLANK", "type": "page", "url": "about:blank", "title": ""}
    f.targets["USER_NTP"] = {"targetId": "USER_NTP", "type": "page", "url": "chrome://newtab/", "title": "New Tab"}
    f.current = "USER_BLANK"

    def evaluate(params, sid):
        expr = params["expression"]
        if expr == "document.readyState":
            return {"result": {"value": "complete"}}
        if "s?.marker" in expr:
            return {"result": {"value": ["m"]}}
        if "__jevFast ||=" in expr:
            return {"result": {"value": page_for()}}
        return {"result": {"value": True}}
    f.handlers["Runtime.evaluate"] = evaluate
    harness_api.install_fake(f)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    yield f
    harness_api.install_fake(None)
    tab._open_tabs.clear()


class DoneJev:
    def ask(self, state, questions, *, deadline=None):
        from jev_browse.typesafe import Answer
        return Answer(answers={"operation": choice_answer(questions["operation"]["criteria"], "DONE")}, model="m")


def closed(f):
    return [p["targetId"] for m, _, p in f.calls if m == "Target.closeTarget"]


def test_fast_run_creates_its_own_target_and_closes_only_it(chrome):
    r = run.fast_run("https://example.test/", "x", keep_open=False, _client=DoneJev(),
                     _backend=run.make_backend("none"), _out=lambda *a, **k: None)
    assert r.status == "claimed_done"
    creates = [p for m, _, p in chrome.calls if m == "Target.createTarget"]
    assert creates == [{"url": "about:blank", "background": True}]
    assert closed(chrome) == [r.target_id] and r.target_id not in {"USER_BLANK", "USER_NTP"}
    assert {"USER_BLANK", "USER_NTP"} <= set(chrome.targets)


def test_jev_close_all_owned_force_never_touches_user_tabs(chrome):
    from jev_browse.interactive import jev_close
    t = OwnedTab.create("https://example.test/")
    assert jev_close(all_owned=True, force=True) == 1
    assert closed(chrome) == [t.target_id] and {"USER_BLANK", "USER_NTP"} <= set(chrome.targets)


def test_jev_adopt_refuses_a_blank_or_new_tab_page_it_did_not_create(chrome):
    reg = Registry(chrome.socket_dir, "jevbench")
    for tid in ("USER_BLANK", "USER_NTP"):
        with pytest.raises(tab.NotOwned, match="blank|new-tab|not created"):
            OwnedTab.adopt(tid, registry=reg, created=Registry(chrome.socket_dir, "jevbench", "created"))
        assert not reg.has(tid)


def test_benchmark_cleanup_spares_preexisting_and_mid_attempt_user_tabs(chrome, tmp_path):
    t0 = time.time()
    prep = run_bench.new_owned_tab(cdp=harness_api.cdp)  # the orchestrator's own fresh tab, via raw CDP
    ours = OwnedTab.create("https://example.test/")       # a jev-browse tab created during the attempt
    # the user opens a new tab mid-attempt, and an arm (with the recorder off) creates one we cannot prove is ours
    chrome.targets["USER_MID"] = {"targetId": "USER_MID", "type": "page", "url": "chrome://newtab/", "title": ""}
    chrome.targets["UNPROVEN"] = {"targetId": "UNPROVEN", "type": "page", "url": "https://example.test/x",
                                  "title": ""}
    ids = run_bench.closable_targets(prep, t0, registry=Registry(chrome.socket_dir, "jevbench"),
                                     created=Registry(chrome.socket_dir, "jevbench", "created"),
                                     live=[t["targetId"] for t in chrome.targets.values()])
    assert set(ids) == {prep, ours.target_id}
    run_bench.close_owned(ids, cdp=harness_api.cdp)
    assert set(closed(chrome)) == {prep, ours.target_id}
    assert {"USER_BLANK", "USER_NTP", "USER_MID", "UNPROVEN"} <= set(chrome.targets)


def test_no_diff_or_blankness_based_close_anywhere():
    """Every close site is ownership-checked; nothing calls harness new_tab/close_tab to create or close tabs."""
    for path in [*(ROOT / "jev_browse").glob("*.py"), *(ROOT / "bench").glob("*.py")]:
        code = re.sub(r'""".*?"""', "", path.read_text(), flags=re.S)
        assert "close_tab(" not in code, path
        if path.name != "harness.py":  # harness.py only wraps new_tab with the provenance recorder
            assert not re.search(r"(^|[=(,.:]\s*)new_tab\(", code, re.M), path
    bench = (ROOT / "bench" / "run_bench.py").read_text()
    assert "close_targets(" not in bench and "closeTarget" in bench.split("def close_owned")[1].split("\ndef ")[0]
    closes = [m.start() for m in re.finditer("Target.closeTarget", (ROOT / "jev_browse" / "tab.py").read_text())]
    assert len(closes) == 1  # OwnedTab.close only, reached from jev_close / keep_open=False on registered targets
