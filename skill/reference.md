# jev-browse reference

Read this when you use the interactive helpers, need a tab-ownership detail, or a hand-back reason is unclear.

## Decision server

TypeSafe is the default. For a compatible self-hosted server, set `jev.base_url`, `jev.model`, and `jev.auth`
in the config file (or their `JEV_BROWSE_JEV_*` environment variables). The base URL gets `/v1/systemone`
appended. `auth = "none"` needs no key and sends no Authorization header; `bearer` uses the environment variable
named by `jev.api_key_env`, defaulting to `JEV_BROWSE_API_KEY` for custom servers and `TYPESAFE_API_KEY` for the
default endpoint. Missing credentials or invalid endpoint/auth settings hand back `service_error` before
browser work. `doctor` tests the selected endpoint; private base URLs are not printed. Page content still goes
to that server even with `text_backend="none"`.

## Signatures
```python
fast_run(url, goal, *, target_id=None, values=None, confirm=(), run_id=None, max_actions=30, max_requests=60,
         timeout_s=90, keep_open=True, text_backend=None, speculate_text="after_miss", screenshot=False) -> RunResult
jev_open(url) -> {target_id, url, title} | HandBack
jev_adopt(target_id) -> {target_id, url, title} | HandBack      # only tabs new_tab() created
jev_find(description, *, k=3, scroll=True, target_id=None) -> FindResult | HandBack
jev_click(found, *, target_id=None, confirm=False) -> dict | HandBack
jev_check(condition, *, target_id=None) -> CheckResult | HandBack
jev_close(target_id=None, *, all_owned=False, force=False) -> int
```

## RunResult
`status` (`claimed_done` | `blocked` | `handed_back`), `reason`, `detail`, `target_id`, `url`, `title`,
`evidence` (`visible_text`, `fields` with sensitive/personal values `<redacted>` and `matches_requested`,
`surfaces`, `screenshot_path`), `trace` (per step: operation, label, top-3 probabilities, confidence, jev_ms,
`text_source` ∈ caller/candidate/llm/llm_cache/none, commit gate), `stats` (wall_ms, Jev calls/tokens, LLM
calls/ms/overlap/tokens/cost, actions), `data` (per reason, always `surfaces`).

## Values
- `values={"Where to?": "Paris"}`: keys are a field label (case/diacritic-folded) or the stable
  `data.fields[].key` (label | name/id | placeholder, plus region heading, plus `#n` on collision). Indices are
  never keys. A label matching two fields hands back `text_value_unavailable` (`ambiguous key`) with their keys.
- `values=["Paris"]` (a list) adds caller candidates for every field; Jev still picks the field.
- Resolution order: caller value → goal-derived candidate chosen by Jev → the text backend after a miss (by
  default the `claude` CLI if installed, else none; see docs/backends.md) → hand back. Personal fields (name,
  email, phone, address, birthday) never use the text backend; their values are never sent, only `empty` /
  `filled` / `filled (matches|differs from requested value)`. A prefill that differs from your `values=` entry
  is retyped before DONE.

## FindResult
`outcome`: `found` (element with `target_id`, `node`, `doc`, `ctx`, role, label, value, fresh `x`, `y`, rect),
`ambiguous` (top-k; `data.occluded` if covered: dismiss the overlay or scroll, retry once; else re-ask once with
a more specific description, then pick by label yourself or use the harness; never loop more than twice), or
`not_found` (top-k plus `surfaces`: a relevant frame/canvas/shadow root means use the harness; otherwise scroll
once and retry). `found.ref()` is the JSON-safe `{target_id, node, doc, label, ctx}` for `jev_click`.

## jev_click results
`{clicked: True, x, y, url_after, popup_target_id?}` · `{clicked: False, reason: stale|occluded|confirm_required}`
(no event sent; `stale` → re-run `jev_find`) · `{clicked: "unconfirmed", reason: dialog_open, data.dialog}` (the
click may have run: handle the dialog, do not click again) · a `HandBack` for ownership, host, or browser errors.

## Reasons, grouped by what you do next
| Do next | Reasons |
|---|---|
| Take over with the harness on `target_id` | blocked, too_many_options, state_too_large, sensitive_field, auth_required, upload, in_frame (cross-origin: `iframe_target()`), visual_only (`data.screenshot_path`), in_shadow_dom, dialog_open, low_confidence, no_progress, budget_exhausted |
| Resume with `values=` | text_value_unavailable |
| Confirm, then resume or `jev_click(..., confirm=True)` | confirm_required |
| Re-open / continue elsewhere | not_owned_tab, browser_error, popup_tab (`data.target_id`), host_not_allowed |
| Retry later | stale_page, service_error |

## Tabs
- jev-browse tabs are background tabs; it never makes them current and never calls `switch_tab`.
- `jev_adopt(id)` accepts only a tab `new_tab()` created while jev-browse was installed (not a reused blank
  tab, not the daemon's start page, not your own tabs).
- Each tab keeps a focus-emulation keeper session until `jev_close`. Tabs accumulate until you close them.
- A killed run leaves `jev-browse-run-<run_id>.json` in the harness tmp dir; if you lost the id, open the newest
  one whose `goal` matches.
