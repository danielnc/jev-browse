import json
import multiprocessing
import os
import re
import stat
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from jev_browse import config, harness_api, tab
from jev_browse.tab import BrowserGone, DialogSuspected, NotOwned, OwnedTab, Registry, StalePage, fingerprint
from tests.fakes import FakeCDP

ROOT = Path(__file__).resolve().parents[1]
PAGE_METHODS = ("Page.", "Runtime.", "Input.", "DOM.", "Emulation.")


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "w": 1000, "h": 800,
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
        "marker": ["m"], "page_key": ["k"], "guards": {"10": ["g10"], "20": ["g20"]},
    }
    state["fingerprint"] = fingerprint(state)
    return state


@pytest.fixture
def fake(tmp_path):
    f = FakeCDP()
    f.socket_dir = tmp_path
    f.tmp_dir = tmp_path
    f.daemon_name = "test"
    harness_api.install_fake(f)
    yield f
    harness_api.install_fake(None)
    tab._open_tabs.clear()


def registry(fake):
    return Registry(fake.socket_dir, fake.daemon_name)


def owned(fake, tid="T9"):
    fake.targets[tid] = {"targetId": tid, "type": "page", "url": "https://example.test/", "title": "", "openerId": None}
    reg = registry(fake)
    reg.add(tid, owner="me", sessions={})
    return OwnedTab.attach(tid, registry=reg)


def evaluate_returning(value):
    return lambda params, sid: {"result": {"value": value}}


# ---- ported from browser-use/jev-ultrafast tests/test_agent.py (MIT). See NOTICE. ---------------------------
def test_observation_is_one_atomic_browser_read(fake):
    t = owned(fake)
    p = page()
    fake.calls.clear()
    fake.handlers["Runtime.evaluate"] = evaluate_returning(deepcopy(p))
    actual = t.observe()
    assert actual["actions"] == p["actions"]
    assert fake.methods() == ["Runtime.evaluate"]


def test_executor_rejects_a_stale_page_before_browser_input(fake):
    t = owned(fake)
    t.fresh = lambda *a, **k: False
    fake.calls.clear()
    with pytest.raises(StalePage):
        t.act(page()["actions"][0], page(), "book")
    assert not any(m.startswith("Input.") for m in fake.methods())


@pytest.mark.parametrize("response", [{"exceptionDetails": {"text": "Execution context destroyed"}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(fake, response):
    t = owned(fake)
    t.fresh = lambda *a, **k: True
    fake.handlers["Runtime.evaluate"] = response
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        t.act({"id": "e1", "kind": "select", "node": 1, "value": "Design"}, page())


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


# ---- jev-browse ------------------------------------------------------------------------------------------
def test_every_page_call_uses_explicit_session(fake):
    t = owned(fake)
    fake.handlers["Runtime.evaluate"] = evaluate_returning(page())
    t.observe()
    t.fresh(page())
    t.fresh(page(), page()["actions"][2])
    fake.handlers["Runtime.evaluate"] = evaluate_returning({"x": 5, "y": 5})
    t.fresh = lambda *a, **k: True
    t.act(page()["actions"][0], page(), "hello")
    t.act({"id": "scroll_down", "kind": "scroll", "delta": 500}, page())
    for method, sid, _ in fake.calls:
        if method.startswith(PAGE_METHODS):
            assert sid is not None, method


def test_attach_refuses_unregistered_target(fake):
    with pytest.raises(NotOwned):
        OwnedTab.attach("T-unknown", registry=registry(fake))
    assert "Target.attachToTarget" not in fake.methods()


def test_keeper_holds_focus_emulation_only(fake):
    fake.targets["T1"] = {"targetId": "T1", "type": "page", "url": "about:blank", "title": ""}
    reg = registry(fake)
    fake.handlers["Runtime.evaluate"] = evaluate_returning("complete")
    fake.handlers["Target.createTarget"] = {"targetId": "T1"}
    t = OwnedTab.create("https://example.test/", registry=reg)
    keeper = reg.get("T1")["keeper"]
    keeper_calls = [m for m, sid, _ in fake.calls if sid == keeper]
    assert keeper_calls == ["Emulation.setFocusEmulationEnabled"]
    assert ("Page.enable", t.session) in [(m, s) for m, s, _ in fake.calls]
    assert "Emulation.setDeviceMetricsOverride" not in fake.methods()


def test_attach_recreates_dead_keeper(fake):
    t = owned(fake)
    reg = t.registry
    old = reg.get("T9")["keeper"]
    fake.dead_sessions.add(old)
    fake.calls.clear()
    t2 = OwnedTab.attach("T9", registry=reg)
    new = reg.get("T9")["keeper"]
    assert new != old and t2.keeper == new
    assert [m for m, sid, _ in fake.calls if sid == new] == ["Emulation.setFocusEmulationEnabled"]
    # an auto-registered pop-up with no keeper gets one too
    fake.targets["P1"] = {"targetId": "P1", "type": "page", "url": "https://x/", "title": "", "openerId": "T9"}
    reg.add("P1", owner="me", sessions={})
    OwnedTab.attach("P1", registry=reg)
    assert reg.get("P1")["keeper"]


def test_leftover_per_call_session_of_dead_pid_detached_on_next_attach(fake):
    t = owned(fake)
    reg = t.registry
    reg.update(lambda targets: targets["T9"]["sessions"].update({"S-dead": {"pid": 999999, "dialog": False}}))
    fake.calls.clear()
    OwnedTab.attach("T9", registry=reg)
    assert ("Target.detachFromTarget", None, {"sessionId": "S-dead"}) in fake.calls
    assert "S-dead" not in reg.get("T9")["sessions"]


def test_kept_dialog_session_not_detached_until_dialog_clears(fake):
    t = owned(fake)
    reg = t.registry
    reg.update(lambda targets: targets["T9"]["sessions"].update({"S-dlg": {"pid": 999999, "dialog": True}}))
    fake.dialog = {"type": "confirm", "message": "Sure?", "url": "https://example.test/"}
    OwnedTab.attach("T9", registry=reg)
    assert "S-dlg" in reg.get("T9")["sessions"]
    fake.dialog = None
    OwnedTab.attach("T9", registry=reg)
    assert "S-dlg" not in reg.get("T9")["sessions"]


def test_dialog_session_kept_attached_for_later_process(fake):
    t = owned(fake)
    fake.dialog = {"type": "confirm", "message": "Sure?", "url": "https://example.test/"}
    info = t.dialog_open()
    assert info["session_id"] == t.session
    sid = t.session
    fake.calls.clear()
    t.detach()
    assert ("Target.detachFromTarget", None, {"sessionId": sid}) not in fake.calls
    assert t.registry.get("T9")["sessions"][sid]["dialog"] is True


def test_dialog_open_matches_target_url_only(fake):
    t = owned(fake)
    fake.dialog = {"type": "alert", "message": "hi", "url": "https://other.test/"}
    assert t.dialog_open().get("suspected") is True
    fake.targets["T8"] = {"targetId": "T8", "type": "page", "url": "https://example.test/", "title": ""}
    t.registry.add("T8", owner="me", sessions={})
    fake.dialog = {"type": "alert", "message": "hi", "url": "https://example.test/"}
    assert sorted(t.dialog_open()["ambiguous_targets"]) == ["T8", "T9"]


def test_attach_never_overrides_device_metrics(fake):
    owned(fake)
    assert not any("DeviceMetrics" in m for m in fake.methods())


def test_detach_only_own_sessions(fake):
    t = owned(fake)
    keeper = t.keeper
    sid = t.session
    fake.calls.clear()
    t.detach()
    detached = [p["sessionId"] for m, _, p in fake.calls if m == "Target.detachFromTarget"]
    assert detached == [sid] and keeper not in detached


def test_owned_tab_never_calls_switch_tab():
    for path in [ROOT / "jev_browse/tab.py", ROOT / "jev_browse/harness_api.py"]:
        code = re.sub(r'""".*?"""', "", path.read_text(), flags=re.S)
        assert "switch_tab(" not in code and "drain_events(" not in code, path


def test_navigate_uses_load_budget_not_ipc_default(fake):
    t = owned(fake)
    fake.handlers["Runtime.evaluate"] = evaluate_returning("complete")
    t.navigate("https://example.test/next")
    nav = [kw for m, _, kw in fake.calls if m == "Page.navigate"]
    assert nav
    timeouts = [c for c in fake.calls if c[0] == "Page.navigate"]
    assert timeouts  # FakeCDP records params; the timeout is passed through _call:
    seen = {}
    orig = harness_api._fake

    def spy(method, session_id=None, _response_timeout=5.0, **params):
        seen.setdefault(method, _response_timeout)
        return orig(method, session_id=session_id, _response_timeout=_response_timeout, **params)
    spy.current_tab = orig.current_tab
    spy.pending_dialog = orig.pending_dialog
    harness_api.install_fake(spy)
    try:
        t.navigate("https://example.test/again")
    finally:
        harness_api.install_fake(orig)
    assert seen["Page.navigate"] == pytest.approx(30.0)


def test_missing_session_raises_browser_gone(fake):
    t = owned(fake)
    fake.dead_sessions.add(t.session)
    with pytest.raises(BrowserGone):
        t.observe()


def test_daemon_unreachable_raises_browser_gone(fake):
    t = owned(fake)
    fake.unreachable = True
    with pytest.raises(BrowserGone, match="unreachable"):
        t.observe()


def test_ipc_timeout_on_input_raises_dialog_suspected_during_act(fake):
    t = owned(fake)
    t.fresh = lambda *a, **k: True
    fake.handlers["Runtime.evaluate"] = evaluate_returning({"x": 5, "y": 5})
    fake.timeout_methods.add("Input.dispatchMouseEvent")
    with pytest.raises(DialogSuspected) as e:
        t.act(page()["actions"][2], page())
    assert e.value.during_act


def test_registry_prunes_closed_targets(fake):
    reg = registry(fake)
    reg.add("A", owner="me")
    reg.add("B", owner="me")
    reg.live([{"targetId": "A", "type": "page"}])
    assert set(reg.entries()) == {"A"}


def _add_many(directory, prefix):
    reg = Registry(directory, "test")
    for i in range(25):
        reg.add(f"{prefix}{i}", owner=prefix)


def test_registry_concurrent_adds_both_persist(tmp_path):
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_add_many, args=(tmp_path, p)) for p in ("a", "b")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(10)
    assert len(Registry(tmp_path, "test").entries()) == 50


def test_registry_corrupt_file_is_empty(tmp_path):
    reg = Registry(tmp_path, "test")
    reg.path.write_text("{not json")
    assert reg.entries() == {}
    reg.add("X", owner="me")
    assert "X" in reg.entries()


def test_registry_file_mode_600(tmp_path):
    reg = Registry(tmp_path, "test")
    reg.add("X", owner="me")
    assert stat.S_IMODE(os.stat(reg.path).st_mode) == 0o600


def test_registry_lives_in_socket_dir_per_daemon(fake):
    fake.daemon_name = "jevbench"
    reg = tab.default_registry()
    assert reg.path == Path(fake.socket_dir) / "jev-browse-owned-jevbench.json"
    assert tab.default_registry("created").path.name == "jev-browse-created-jevbench.json"


def test_created_log_nonblocking_record_skips_when_busy(tmp_path):
    import fcntl

    created = Registry(tmp_path, "test", "created")
    created.record("N1")
    fd = os.open(created.lock_path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert created.record("N2") is False
    finally:
        os.close(fd)
    assert set(created.entries()) == {"N1"}


def test_popups_detects_opener_child(fake):
    t = owned(fake)
    t.snapshot_popups()
    fake.targets["P"] = {"targetId": "P", "type": "page", "url": "https://x/", "title": "", "openerId": "T9"}
    assert t.popups() == ["P"]
    assert t.registry.has("P")
    assert t.popups() == []


def test_adopt_requires_provenance_and_host(fake):
    reg = registry(fake)
    created = Registry(fake.socket_dir, fake.daemon_name, "created")
    fake.targets["U"] = {"targetId": "U", "type": "page", "url": "https://mail.test/", "title": ""}
    fake.calls.clear()
    with pytest.raises(NotOwned, match="not created by new_tab"):
        OwnedTab.adopt("U", registry=reg, created=created)
    assert "Target.attachToTarget" not in fake.methods()
    created.record("U")
    t = OwnedTab.adopt("U", registry=reg, created=created, owner="me")
    assert reg.get("U")["owner"] == "me" and reg.get("U")["keeper"] and t.session


def test_ctx_hash_is_unsalted_and_stable_across_salts():
    js = (ROOT / "jev_browse/snapshot.js").read_text()
    assert "cache.ctx = e => fnv(" in js
    assert "siphash" not in js.split("cache.ctx = e =>")[1].split("\n")[0]


SIPHASH_VECTOR = "a129ca6149be45e5"  # SipHash-2-4 paper vector: key 00..0f, message 00..0e


def test_snapshot_siphash_matches_reference_vector():
    js = (ROOT / "jev_browse/snapshot.js").read_text()
    body = js.split("// <siphash>")[1].split("// </siphash>")[0]
    salt = "0706050403020100" + "0f0e0d0c0b0a0908"
    script = f"const cfg={{salt:'{salt}'}};{body}\nconsole.log(siphash(String.fromCharCode(...Array.from({{length:15}},(_, i)=>i))));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert out.stdout.strip() == SIPHASH_VECTOR, out.stderr


def test_snapshot_js_parses():
    subprocess.run(["node", "--check", str(ROOT / "jev_browse/snapshot.js")], check=True, timeout=10)


@pytest.mark.parametrize("field,expected", [
    ({"label": "PIN"}, True), ({"label": "Shipping"}, False), ({"label": "Card number"}, True),
    ({"label": "CVV"}, True), ({"label": "Postal code"}, False), ({"label": "x", "autocomplete": "cc-number"}, True),
    ({"label": "Code", "autocomplete": "one-time-code"}, True), ({"label": "x", "input_type": "password"}, True),
    ({"label": "CPF"}, True), ({"label": "Opinion"}, False), ({"label": "Spinning class"}, False),
])
def test_sensitive_patterns_whole_word(field, expected):
    assert config.is_sensitive_field(field) is expected


def test_snapshot_cfg_carries_the_python_pattern_table():
    cfg = json.loads(tab.snapshot_expression().rsplit(")(", 1)[1][:-1])
    assert cfg["sensitive"] == config.sensitive_patterns()
    assert len(cfg["salt"]) == 32


def test_observe_backs_off_through_a_navigation(fake, monkeypatch):
    t = owned(fake)
    slept = []
    monkeypatch.setattr(tab.time, "sleep", slept.append)
    answers = iter([{"result": {"value": None}}] * 6 + [{"result": {"value": page()}}])
    fake.handlers["Runtime.evaluate"] = lambda params, sid: next(answers)
    assert t.observe()["url"] == "https://example.test/"
    assert slept == [0.05, 0.1, 0.2, 0.4, 0.8, 1.0]


def test_observe_gives_up_after_ten_attempts(fake, monkeypatch):
    t = owned(fake)
    monkeypatch.setattr(tab.time, "sleep", lambda s: None)
    fake.handlers["Runtime.evaluate"] = {"result": {"value": None}}
    with pytest.raises(StalePage):
        t.observe()
    assert fake.methods().count("Runtime.evaluate") == 10


def test_read_timeout_without_dialog_is_retried_once_with_longer_timeout(fake):
    t = owned(fake)
    fake.handlers["Runtime.evaluate"] = evaluate_returning(page())
    fake.timeout_once.add("Runtime.evaluate")
    assert t.observe()["url"] == "https://example.test/"
    assert fake.methods().count("Runtime.evaluate") == 2


def test_read_timeout_with_pending_dialog_is_not_retried(fake):
    t = owned(fake)
    fake.dialog = {"type": "alert", "message": "hi", "url": "https://example.test/"}
    fake.timeout_once.add("Runtime.evaluate")
    fake.calls.clear()
    with pytest.raises(DialogSuspected):
        t.observe()
    assert fake.methods().count("Runtime.evaluate") == 1


def test_input_timeout_is_never_retried(fake):
    t = owned(fake)
    t.fresh = lambda *a, **k: True
    fake.handlers["Runtime.evaluate"] = evaluate_returning({"x": 5, "y": 5})
    fake.timeout_once.add("Input.dispatchMouseEvent")
    fake.calls.clear()
    with pytest.raises(DialogSuspected):
        t.act(page()["actions"][2], page())
    assert fake.methods().count("Input.dispatchMouseEvent") == 1


def test_create_survives_one_slow_setup_call(fake):
    fake.targets["T1"] = {"targetId": "T1", "type": "page", "url": "about:blank", "title": ""}
    fake.handlers["Target.createTarget"] = {"targetId": "T1"}
    fake.handlers["Runtime.evaluate"] = evaluate_returning("complete")
    fake.timeout_once.add("Page.enable")
    t = OwnedTab.create("https://example.test/", registry=registry(fake))
    assert t.target_id == "T1"
