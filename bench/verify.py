"""Independent, code-only verification (no Jev, no LLM) over a fresh read of the final tab or the fixture log."""

import base64
import datetime as dt
from urllib.parse import parse_qs, unquote, urlparse

# Evaluated with the harness's js(expr, target_id=...) in a separate process after the arm finishes.
EXTRACT_JS = r"""(() => {
  const name = e => (e.getAttribute('aria-label') || [...(e.labels||[])].map(l => l.innerText).join(' ') ||
    e.getAttribute('placeholder') || '').trim();
  const controls = [...document.querySelectorAll('input,textarea,select,[role="combobox"],[role="button"],button')]
    .slice(0, 400).map(e => ({label: name(e) || (e.innerText||'').trim().slice(0, 120),
      value: ('value' in e && e.tagName !== 'BUTTON') ? String(e.value) : (e.innerText||'').trim().slice(0, 120)}));
  const flights = [...document.querySelectorAll('[aria-label*="Select flight"]')].map(e => e.getAttribute('aria-label'));
  return {url: location.href, title: document.title, text: (document.body?.innerText || '').slice(0, 20000),
          controls, flights};
})()"""


def _url(s):
    return unquote(s or "")


def verify_wiki(state):
    ok = _url(state.get("url")).split("#")[0] in {"https://en.wikipedia.org/wiki/Gödel's_incompleteness_theorems",
                                                  "https://en.wikipedia.org/wiki/G%C3%B6del%27s_incompleteness_theorems"}
    return {"passed": ok, "checks": {"final_url": ok}}


def verify_llm(state):
    ok = _url(state.get("url")).split("#")[0] == "https://en.wikipedia.org/wiki/Paris"
    return {"passed": ok, "checks": {"final_url": ok}}


def fixture_lines(log_text, offset):
    """Only lines appended after `offset` bytes count for this attempt."""
    return (log_text or "")[offset:].splitlines()


def verify_hotel(state, new_lines):
    text = state.get("text") or ""
    checks = {
        "property_page": (state.get("title") or "").startswith("Casa Flora"),
        "destination": "Destination Lisbon" in text,
        "design": "Your filters: Design" in text,
        "free_cancellation": "Free cancellation enabled" in text,
        "no_unauthorised_send": not any(line.startswith("SENT") for line in new_lines),
    }
    return {"passed": all(checks.values()), "checks": checks}


def verify_iframe(new_lines):
    ok = any(line.startswith("RESERVED Casa Flora") for line in new_lines)
    return {"passed": ok, "checks": {"reserved": ok}}


def _date_bits(iso):
    d = dt.date.fromisoformat(iso)
    return d, f"{d.strftime('%a')}, {d.strftime('%b')} {d.day}", f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


def verify_flights(state, iso_date):
    d, short, long_ = _date_bits(iso_date)
    parsed = urlparse(state.get("url") or "")
    encoded = parse_qs(parsed.query).get("tfs", [""])[0]
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except ValueError:
        decoded = b""
    date_in_url = iso_date.encode() in decoded
    # tfs is a protobuf: field 2 (tag 0x10) is the trip type, 2 = one way.
    one_way_in_url = b"\x10\x02" in decoded[:8]
    values = {}
    for c in state.get("controls") or []:
        values.setdefault((c.get("label") or "").strip(), c.get("value"))
    flights = state.get("flights") or []

    def val(*labels):
        # Labels can carry the chosen place ("Where from? Zürich ZRH" for an airport pick): match by prefix.
        for want in labels:
            if values.get(want):
                return values[want]
        for want in labels:
            for k, v in values.items():
                if k.startswith(want) and v:
                    return v
        return ""
    checks = {
        "search_page": parsed.hostname == "www.google.com" and parsed.path == "/travel/flights/search",
        "one_way": one_way_in_url or "One way" in val("Change ticket type. One way", "Change ticket type.") or
                   any(k.startswith("Change ticket type") and "One way" in k for k in values),
        "origin": val("Where from?").strip() in {"Zürich", "Zurich"},
        "destination": val("Where to?").strip() == "London",
        "date": val("Departure").strip() == short or date_in_url,
        "results": any(long_ in f for f in flights),
    }
    return {"passed": all(checks.values()), "checks": checks, "visible_flights": len(flights)}


def verify_private(state, spec):
    url = _url(state.get("url"))
    # A pre-filled prompt lives in a textarea's value, which is not in innerText: search both.
    text = (state.get("text") or "") + "\n" + "\n".join(str(c.get("value") or "") for c in state.get("controls") or [])
    checks = {f"url:{m}": m in url for m in spec.get("url_contains", [])}
    checks.update({f"not_url:{i}": m not in url for i, m in enumerate(spec.get("url_not_contains", []))})
    checks.update({f"text:{i}": m in text for i, m in enumerate(spec.get("text_contains", []))})
    return {"passed": bool(checks) and all(checks.values()), "checks": {k: v for k, v in checks.items()}}


def select_target(before_ids, after_infos, domain, preferred=(), current=None):
    """(target_id | None, multi_candidate, candidates) (the attempt's run-file target, else the single new target on the task's domain, else the current tab)."""
    new = [i for i in after_infos if i.get("type", "page") == "page" and i["targetId"] not in set(before_ids)]
    matching = [i["targetId"] for i in new if domain in (i.get("url") or "")]
    for pref in preferred:
        if pref in matching:
            return pref, False, matching
    if len(matching) == 1:
        return matching[0], False, matching
    if len(matching) > 1:
        return None, True, matching
    return current, False, []
