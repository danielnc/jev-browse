---
name: jev-browse
description: Use for any browser-harness task that is a multi-step form or navigation sub-task on a website (search, filter, fill, open a result), or a "find/click the X" step on a page, when you only need the outcome. Runs the sub-task on its own background tab with fast typed decisions (TypeSafe Jev) and hands back with a typed reason when it cannot finish.
---

# jev-browse

Helpers inside every `browser-harness` script: `fast_run`, `jev_open`, `jev_adopt`, `jev_find`, `jev_click`,
`jev_check`, `jev_close`. Page text goes to TypeSafe (and, only after a miss, to the text backend). Details:
`reference.md` next to this file.

## Which tool
- **`fast_run(url, goal, values={...}, run_id=...)`**: a well-specified multi-step sub-task where you only
  need the outcome. Put every value you already know in `values=` (keys: field label or `data.fields[].key`).
- **`jev_open` + `jev_find` / `jev_click` / `jev_check`**: you drive step by step and want a cheap locate,
  click, or navigation check instead of the AX tree or a screenshot. Start with `jev_open(url)` and pass its
  `target_id` to every call. Tab already open from `new_tab()`? `jev_adopt(<that id>)` instead.
- **The harness directly**: visual judgement, frames, uploads, keyboard-heavy widgets, anything sensitive, or
  when a hand-back says so.

Benchmarked routing (docs/benchmark.md, the author's runs): use `fast_run` for link navigation,
multi-field search forms with date/autocomplete widgets, and filter-and-open on listing sites (there 1.6–3.0×
faster and 2.1–5.3× cheaper than driving the harness).
A miss (value not in the goal) or a hand-back (e.g. an embedded payment widget) still came out ahead once resumed.

## Running fast_run
```python
r = fast_run("https://example.com", "Search Lisbon, set Design and Free cancellation, open Casa Flora",
             run_id="20260924-1015-a1b2")
print(r.status, r.reason, r.detail, r.target_id)
```
- Give the Bash call a timeout of at least `timeout_s + 60` s (default 150000 ms), and pass your own `run_id=`.
  If it is killed anyway, read `~/.config/browser-harness/tmp/jev-browse-run-<run_id>.json`.
- `claimed_done` is not proof. Verify in the kept-open tab with `js("...", target_id=r.target_id)` (URL, DOM
  text, field values) or from `r.evidence`, never with `jev_check`. Then `jev_close(r.target_id)`.
- `fast_run` prints `JEV_BROWSE_RESULT={...}` (status, final URL, title, visible field values, start of the page
  text). Check it, plus any `js(...)` you need, in the same script: no separate verification turn.

## After a hand-back (the tab stays open)
- Resume instead of re-running: `fast_run(None, <same goal verbatim>, target_id=r.target_id, values={...},
  confirm=[...])`. Do not re-run from the URL.
- `text_value_unavailable`: resume with `values=` for `r.data["fields"]`.
- `confirm_required`: a commit click (send, delete, pay, book, a dialog "OK"...). Read `r.data["context"]`.
  Never handle a `confirm_required` in the same script that received it: decide first (ask the user if their
  intent does not clearly cover it). Then, in a new script, resume with `confirm=[<the printed
  JEV_BROWSE_CONFIRM json>]` or call `jev_click(<that json>, confirm=True)`.
- `in_frame`, `upload`, `auth_required`, `sensitive_field`, `visual_only`, `in_shadow_dom`, `blocked`,
  `no_progress`, `low_confidence`, `budget_exhausted`, `dialog_open`: take over with the harness on that tab.
- `popup_tab`: continue on `r.data["target_id"]`. `not_owned_tab` / `browser_error`: re-open with `jev_open`.
  `stale_page` / `service_error`: retry later.

## Interactive
- `found = jev_find("the Search button", target_id=tid)`; if `found.outcome == "found"`, print
  `json.dumps(found.ref())` and click it (same or later script) with `jev_click(<that json>)`.
- Click `jev_find` results only with `jev_click`, never with raw `click_at_xy`.
- `jev_check(condition, target_id=tid)` is for navigation decisions only (0.3–0.7 means unsure).
- Dialogs: handle them on `data.dialog.session_id` with `cdp("Page.handleJavaScriptDialog", ...)`.
- Raw input on an owned tab (`click_at_xy`, `type_text`, `upload_file`) goes in a heredoc that starts with
  `switch_tab(target_id)`; reads use `js(expr, target_id=...)` and never move the shared tab.

## Rules
- Only use it on pages whose content you are comfortable sending to TypeSafe and the text backend. Use
  `text_backend="none"` for sensitive sites (misses then hand back to you).
- Close tabs by explicit `target_id`; `jev_close(all_owned=True)` for end-of-session cleanup. Parallel
  subagents set their own `JEV_BROWSE_OWNER`.
