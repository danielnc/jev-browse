import re

import pytest

from jev_browse import config
from jev_browse.results import CheckResult, FindResult, HandBack, Reason, RunResult, Step
from jev_browse.textnorm import any_whole_word, fold, whole_word

SPEC_REASONS = {
    "blocked", "not_owned_tab", "too_many_options", "state_too_large", "sensitive_field", "auth_required", "upload",
    "in_frame", "visual_only", "in_shadow_dom", "popup_tab", "confirm_required", "text_value_unavailable",
    "dialog_open", "browser_error", "low_confidence", "no_progress", "budget_exhausted", "stale_page",
    "host_not_allowed", "service_error",
}


def test_reason_enum_matches_the_documented_reasons():
    """Every hand-back reason is documented where the calling agent reads it (skill/reference.md)."""
    assert {r.value for r in Reason} == SPEC_REASONS
    reference = open("skill/reference.md").read()
    assert not [r for r in SPEC_REASONS if not re.search(rf"\b{r}\b", reference)]


def test_result_reprs_omit_values_and_evidence():
    secret = "hunter2-secret-value"
    items = [
        HandBack(Reason.confirm_required, f"click {secret}", {"label": secret, "context": secret,
                                                               "confirm": {"label": secret}}),
        Step("TYPE_TEXT", label=secret, heads={"v": secret}),
        RunResult("claimed_done", evidence={"visible_text": secret, "fields": [{"value": secret}]},
                  trace=[Step("CLICK", label=secret)], stats={"wall_ms": 5}, target_id="T1",
                  data={"confirm": secret}),
        FindResult("found", element={"label": secret, "value": secret, "target_id": "T", "node": 1, "doc": "d",
                                     "ctx": "c"}, exists=0.9, confidence=0.9),
        CheckResult(0.9, True, evidence_line=secret),
    ]
    for item in items:
        r = repr(item)
        assert secret not in r, r
        assert len(r) <= 200


def test_find_result_ref_is_json_safe_and_minimal():
    el = {"target_id": "T", "node": 3, "doc": "1.5|https://x/", "label": "Go", "ctx": "abc", "value": "v", "x": 1}
    assert FindResult("found", element=el).ref() == {"target_id": "T", "node": 3, "doc": "1.5|https://x/",
                                                     "label": "Go", "ctx": "abc"}
    with pytest.raises(ValueError):
        FindResult("not_found").ref()


def test_require_key_preflight(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    hb = config.require_key()
    assert hb.reason == Reason.service_error and "TYPESAFE_API_KEY not set" in hb.detail
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    assert config.require_key() is None


def test_fold_and_whole_word():
    assert fold("  Zürich HAUPTBAHNHOF ") == "zurich hauptbahnhof"
    assert fold("Gödel’s") == "godel's"
    assert whole_word("post", "Post comment")
    assert not whole_word("post", "Postal code")
    assert whole_word("excluir", "EXCLUIR conta")
    assert not whole_word("pin", "Shipping")
    assert whole_word("place order", "Place  order now")
    assert any_whole_word(["pay", "send"], "Send to a friend") == "send"


def test_host_allowed_exact_wildcard_and_near_misses(monkeypatch):
    monkeypatch.delenv("JEV_BROWSE_ALLOWED_HOSTS", raising=False)
    assert config.host_allowed("https://anything.test/")
    monkeypatch.setenv("JEV_BROWSE_ALLOWED_HOSTS", "example.com,*.example.org")
    assert config.host_allowed("https://example.com/x")
    assert config.host_allowed("https://a.example.org/")
    assert not config.host_allowed("https://example.org.attacker.com/")
    assert not config.host_allowed("https://evil-example.org/")
    assert not config.host_allowed("https://sub.example.com/")
    assert not config.host_allowed("data:text/html,hi")
    assert config.host_allowed("about:blank")


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("JEV_BROWSE_COMMIT_VERBS", "")
    assert config.commit_verbs() == []
    monkeypatch.setenv("JEV_BROWSE_COMMIT_VERBS", "Frobnicate")
    monkeypatch.setenv("JEV_BROWSE_COMMIT_VERBS_EXTRA", "Zap")
    assert config.commit_verbs() == ["frobnicate", "zap"]
    monkeypatch.delenv("JEV_BROWSE_COMMIT_VERBS")
    assert "excluir" in config.commit_verbs() and "zap" in config.commit_verbs()
    monkeypatch.setenv("JEV_BROWSE_VALUE_HEADS", "2")
    assert config.value_heads() == 2
    monkeypatch.setenv("JEV_BROWSE_VALUE_CANDIDATES", "0")
    assert not config.value_candidates_enabled()
    monkeypatch.setenv("JEV_BROWSE_TEXT_BACKEND", "none")
    assert config.text_backend_name() == "none" and config.text_backend_name("claude") == "claude"
    monkeypatch.setenv("JEV_BROWSE_SENSITIVE_PATTERNS_EXTRA", "Member ID")
    assert "member id" in config.sensitive_patterns()
    monkeypatch.setenv("JEV_BROWSE_OWNER", "agent-7")
    assert config.owner_tag() == "agent-7"
