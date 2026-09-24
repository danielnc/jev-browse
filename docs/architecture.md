# Architecture

This page is for people evaluating or contributing to jev-browse: how the code fits together and where its
safety limits are. Setup is in the [README](../README.md), settings in [configuration.md](configuration.md), and
caller-facing usage in [`skill/SKILL.md`](../skill/SKILL.md) and [`skill/reference.md`](../skill/reference.md).

## Overview

jev-browse is a small, stdlib-only Python package that is loaded into every
[browser-harness](https://github.com/browser-use/browser-harness) script. A coding agent (Claude Code, Codex)
writes a harness script, and the script calls the helpers `fast_run`, `jev_open`, `jev_adopt`, `jev_find`,
`jev_click`, `jev_check`, and `jev_close`. They drive Chrome over CDP through the harness daemon, and only on
background tabs that jev-browse created or was explicitly handed. Each decision is one request to
[TypeSafe](https://docs.typesafe.ai) Jev, a model that answers typed questions: a *Choice* (one of a fixed set
of options, with probabilities) or a *Noul* (a single probability). Jev never writes text, selectors, or code;
it picks among options the code offers. When a field needs a value that is neither given by the caller nor
literally in the goal, an optional *text backend* generates it: a subscription CLI (`claude`, `codex`), an
Ollama server, an OpenAI-compatible endpoint, or none.

The observe/act loop, snapshot, action space, and question wording are ported from
[browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT). Ported files carry a header
naming the upstream file; see [`NOTICE`](../NOTICE).

## Where it sits, and what leaves the machine

```text
 Claude Code / Codex (the caller)
        │ writes a heredoc
        ▼
 browser-harness script process
   agent_helpers.py managed block ──► jev_browse.harness
   fast_run · jev_open · jev_adopt · jev_find · jev_click · jev_check · jev_close
        │                           │                            │
        │ CDP, explicit session     │ HTTPS POST /v1/systemone   │ subprocess or HTTP,
        │ ids, over the daemon IPC  │ (every decision)           │ only after a value miss
        ▼                           ▼                            ▼
 browser-harness daemon        TypeSafe API                text backend
        │                      api.typesafe.ai             claude CLI  → Anthropic
        ▼                                                  codex CLI   → OpenAI
 Chrome, the user's profile                                ollama      → your Ollama server
 (owned background tabs only)                              openai      → the endpoint you configure
                                                           none        → nothing is sent
```

| Recipient | What is sent | When |
|---|---|---|
| TypeSafe | Goal, URL, title, visible text (≤ 6,000 chars, less under budget), element labels, non-sensitive field values (personal fields only as `empty` / `filled` / matches-or-differs), the last 10 actions, candidate strings | Every decision, find, and check |
| Text backend | Goal, the missed fields' keys, labels, and placeholders, ≤ 2,000 chars of visible text | After a miss (or on every page with fields under `speculate_text="eager"`); never with `none` |
| browser-harness telemetry | Script text, stdout, helper-call argument `repr`s | Only if the harness's own telemetry is on |

jev-browse sends no telemetry of its own. Sensitive values (passwords, card numbers, one-time codes, and
similar) are never read out of the page, so they are never sent anywhere.

## Module map

| Module (`jev_browse/`) | Responsibility |
|---|---|
| `harness.py` | Names exported into harness scripts; the `new_tab` provenance recorder (`wrap_new_tab`). |
| `harness_api.py` | The only code that touches browser-harness (`cdp`, current tab, dialog slot, socket and tmp dirs). Lazy; replaceable by a fake. |
| `run.py` | `fast_run` and its `Runner`: the loop, budgets, hand-backs, run files, cleanup. |
| `actions.py` | Element table, target and value heads, stage-2 regions, token budgeting, the commit gate, reading decisions. |
| `snapshot.js` | One synchronous in-page read: actions, fields, text, regions, surfaces, freshness keys. |
| `tab.py` | `OwnedTab` (create, attach, adopt, observe, fresh, act, close) and the file-backed `Registry`. |
| `questions.py` | Every question sent to Jev, and the text-backend system prompts. |
| `candidates.py` | Goal-derived candidate values, including dates rendered in the field's format. |
| `textgen.py` | Text backends, `FallbackBackend` and the canary, the per-page batch service, date normalisation, grounding. |
| `typesafe.py` | Stdlib client for `POST /v1/systemone`: keep-alive, retries, deadline-aware timeouts, answer validation, `redact()`. |
| `interactive.py` | `jev_open`, `jev_adopt`, `jev_find`, `jev_click`, `jev_check`, `jev_close`. |
| `results.py` | `Reason`, `HandBack`, `Step`, `RunResult`, `FindResult`, `CheckResult`, with compact `repr`s. |
| `config.py` | Settings registry (env > file > default), thresholds and limits, verb and field patterns, host allowlist. |
| `textnorm.py` | Case and diacritic folding, whole-word matching. |
| `install.py`, `doctor.py`, `cleanup.py`, `__main__.py` | Install hooks, health checks, `make clean-traces`, and the CLI. |

## The decision loop (`fast_run`)

```text
fast_run(url, goal, values=..., confirm=..., run_id=...)
  │ print JEV_BROWSE_RUN=<path>; key and host preflight; create or attach the owned tab; prewarm the backend
  ▼
┌─► observe: snapshot.js in one Runtime.evaluate on the owned session ............... stays local
│     │ host check on the observed URL; file-input activation flag
│     ▼
│   fit_budget: one request = operation + per-operation targets + value heads, trimmed to fit
│     │ ──────────────────────────────── state + questions ──────────────────────────► TypeSafe
│     ▼
│   read_decision (+ stage 2 on dense pages, + value follow-up, + commit_ok follow-up)
│     │ after_miss: background text batch ────── goal, labels, excerpt ──────────────► text backend
│     ▼
│   DONE ─────────► claimed_done          BLOCKED ─► blocked, or a relabelled hand-back
│   TYPE_TEXT ────► value: caller → candidate → text backend (grounded) → hand back
│   CLICK/SELECT ─► upload check; commit gate (confirm= entry or commit_ok), else hand back
│     ▼
│   freshness check (page key + element guard) ── stale? observe again, nothing executed
│     ▼
│   act: CDP Input.* on the owned session; log the step; write the run file
│     │ new tab ─► popup_tab    3 unchanged pages ─► no_progress    over budget ─► budget_exhausted
└─────┘
  cleanup: kill text subprocesses, detach the per-call session, final run file, print JEV_BROWSE_RESULT
```

### Observation

`snapshot.js` is one synchronous `Runtime.evaluate` with a timeout of min(10 s, time left). While the document
navigates it retries with backoff, up to 10 times, then stops with `stale_page`. It returns:

- **actions**: visible, enabled interactive elements in the viewport (plus off-viewport ones for `jev_find`),
  each with a code-owned node id, role, label, region, heading, an unsalted row-context hash (`ctx`), and flags
  (`sensitive`, `personal`, `upload_trigger`, `in_dialog`, `unnamed`, `submit`, `form_sensitive`,
  `form_personal`). Each `<select>` option is its own action; scroll and wait are controls. Capped at
  `config.SNAPSHOT_CAP`, with `omitted_actions` reporting the rest.
- **fields**: every editable field with its classification; sensitive values arrive as `<redacted>`.
- **text** (≤ 6,000 chars of viewport text), **regions** (landmarks with headings), and **surfaces** (frames,
  shadow roots, canvas area, file inputs, password and OTP fields, share of unnamed icon buttons).
- **freshness keys** (`page_key`, per-element `guards`, `marker`). Sensitive and personal values contribute
  only a SipHash-2-4 digest keyed with a random per-process salt; digests never leave the process.

`fast_run` targets only in-viewport elements and explores long pages with SCROLL_UP / SCROLL_DOWN.

### One request per decision

`actions.build_request` sends the page (URL, title, text), the element table, and the last 10 actions
(personal and sensitive text redacted), with these questions from `questions.py`:

| Key | Type | Text | Asked |
|---|---|---|---|
| `operation` | Choice over valid operations | `NEXT_ACTION`, `OPERATION_LABELS` | Every decision |
| `<op>_target` | Choice over element indices (≤ 254) | `NEXT_ACTION` + `TARGET` | Per available operation |
| `<op>_region` | Choice over regions | `NEXT_ACTION` + `REGION` | Instead of a target head past 254 targets |
| `value_<i>` | Choice over candidates plus `none` | `VALUE_CHOICE`, `VALUE_NONE` | Per editable field, up to `values.heads` fields |
| `value_in_goal_<i>` | Noul | `VALUE_IN_GOAL` | Same fields |
| `commit_ok` | Noul with criteria | `COMMIT_VERB` or `COMMIT_STRUCTURAL` | Follow-up, only for a gated click |
| `grounded` | Noul | `GROUNDING` | Follow-up, only for a generated value not made of goal words |

`jev_find` uses `FIND_ELEMENT`, `FIND_EXISTS`, `FIND_REGION`; `jev_check` uses `CHECK_HOLDS`, `CHECK_WHERE`.

Operations are CLICK, TYPE_TEXT, SELECT, SCROLL_UP, SCROLL_DOWN, WAIT, DONE, and BLOCKED, each offered only when
valid. Code reads only the heads that apply to the chosen operation. Every answer is validated
(`validate_choice`, `validate_noul`): the choice must be offered, and the probabilities must cover exactly the
offered ids and sum to 1. An invalid answer stops the run with `service_error` before any action.

**Dense pages.** Past `config.MAX_OPTIONS` targets, stage 1 picks a region and stage 2 picks the target inside
it. Oversized regions are split by preceding heading, then into chunks. A native `<select>` or a region list
that is still too large returns `too_many_options`.

**Token budget.** `fit_budget` estimates tokens as `len(json) / 3.5` against two limits, state plus the longest
question (`config.LONGEST_BUDGET`) and state plus all questions (`config.TOTAL_BUDGET`). It trims in
`actions.TRIM_STEPS` order (text, labels, candidates, value heads), then returns `state_too_large`. A TypeSafe
400/413/422 that names a token or length limit maps to the same reason.

**Thresholds** are constants at the bottom of `config.py` (`VALUE_CONFIDENCE`, `VALUE_IN_GOAL`, `COMMIT_OK`,
`COMMIT_CONFIDENCE`, `LOW_CONFIDENCE`, `LOW_CONFIDENCE_STREAK`, `FIND_*`, `GROUNDING_NOUL`, `CHECK_HOLDS`, and
the surface-area ratios). They are starting points chosen on a small set of held-out cases, not universal
measurements. Each trace step records head probabilities so they can be recalibrated.

### Acting

Before any mutation, `OwnedTab.fresh` re-reads the page: the page key and target guard for a click or select,
the page marker otherwise. If anything changed, the decision is dropped and the loop observes again. A decision
is consumed once, and a mutation is never retried. `OwnedTab.act` re-resolves the node in the page, checks it
is connected, enabled, visible, in the viewport, and not covered (`elementFromPoint`), then dispatches: a mouse
press/release at its fresh centre (click); click, select-all, `Input.insertText` (type); an in-page value set
with `input`/`change` events (select); a wheel event at the viewport centre (scroll). It then waits two frames,
or up to 200 ms for autocomplete options. No device-metrics override is applied, so coordinates match the tab's
natural layout.

An IPC timeout may mean a native dialog. A read with no dialog pending is retried once with a longer timeout;
input is never retried. A dialog is confirmed through the daemon's dialog slot and matched to an owned tab by
URL. jev-browse never accepts or dismisses dialogs; it hands back `dialog_open` with the session id that can.

### How a run ends

- **DONE**: after a freshness check, and after retyping any personal field whose prefill differs from the
  caller's value, the status is `claimed_done`. This is Jev's claim, not a verification.
- **BLOCKED**: `blocked`, or a more specific hand-back when a relevant surface explains it.
- **Budgets** (`max_actions`, `max_requests`, `timeout_s`; defaults in `config.py`): `budget_exhausted`. The
  deadline is enforced inside socket timeouts, subprocess waits, and the wait for a speculative batch.
- **No progress**: three consecutive non-WAIT actions without a page change.
- **Low confidence**: operation confidence under `LOW_CONFIDENCE` on `LOW_CONFIDENCE_STREAK` decisions in a row.
- Any other **hand-back** (see [Safety model](#safety-model)).

When BLOCKED, no-progress, low-confidence, or a budget stop coincides with a relevant surface, the reason is
relabelled in this order: `auth_required` > `sensitive_field` > `upload` > `in_frame` > `visual_only` >
`in_shadow_dom`, with the original in `data.original`. Cleanup always runs (also from a SIGTERM handler): text
subprocesses are killed by process group, the per-call session is detached, and the run file is finalised.
The tab stays open unless the status is `claimed_done` and `keep_open=False`.

### RunResult and run files

```python
RunResult(
    status,     # "claimed_done" | "blocked" | "handed_back"
    reason,     # Reason | None
    detail,     # one human-readable line
    run_id, url, title,
    evidence,   # visible_text (≤ 3,000 chars), fields [{key, label, role, value, checked, matches_requested}],
                # surfaces, screenshot_path; sensitive and personal values are "<redacted>"
    trace,      # [Step]: operation, label, top3, confidences, jev_ms, tokens, text_source
                # (caller | candidate | llm | llm_cache | none), llm_ms, llm, commit_gate, page_changed, heads
    stats,      # wall_ms, Jev calls and tokens, LLM calls, tokens, cost, ms, overlap, unused batches, actions
    target_id,  # the owned tab, still open
    data,       # per reason; always has surfaces
)
```

Result `repr`s omit field values, evidence text, and confirm references, because harness telemetry (when on)
sends helper-call `repr`s off the machine.

`fast_run` prints `JEV_BROWSE_RUN=<path>` before any other work, `JEV_BROWSE_CONFIRM=<json>` on
`confirm_required`, and `JEV_BROWSE_RESULT={...}` (status, final URL, title, redacted field values, start of
the text) at the end unless `run.quiet` is set. The run file is `<harness tmp>/jev-browse-run-<run_id>.json`,
where the harness tmp dir is `BH_TMP_DIR`, else `<harness home>/tmp`. It is written atomically (mode 600) at
start, after each executed step, and at the end. It holds the goal, start time, pid, a `final` flag, the tab id,
the action history, any pending confirm reference, and the partial or final result, but never freshness keys.
A `run_id` owned by a live, unfinished run is refused. `fast_run(None, goal, target_id=...)` resumes: it seeds
the action history from the newest run file for that tab when the goal matches exactly, starts budgets from
zero, and refuses while another live process drives the tab. The same dir holds screenshots
(`jev-browse-shot-*`), the text backends' private cwd (`jev-browse-cli/`, mode 700), and the canary cache.

## Values

On TYPE_TEXT for field *i*, the value is resolved in this order:

1. **Caller value**: a `values={...}` entry keyed by the field's folded label or its stable `key` (label, else
   name/id, else placeholder, plus region heading and `#n` when needed to stay unique). Indices are never
   keys. A label matching two fields hands back `text_value_unavailable` (`ambiguous key`). A list
   (`values=["Paris"]`) adds candidates for every field instead.
2. **Goal candidate**: `candidates.py` derives up to `config.MAX_VALUE_CANDIDATES` strings from the goal
   (quoted spans, emails, URLs, dates, capitalised spans, 1–5-token n-grams, plus ASCII variants). Dates,
   including relative ones, are parsed in code and rendered in the field's observed format; with an unknown
   format, only order-unambiguous renderings are offered. Jev picks one in the same request as the operation.
   It is accepted when the choice is not `none` and both value heads clear their thresholds. If the chosen
   field had no heads (past the cap, or trimmed), one follow-up request asks just that field.
3. **Text backend after a miss**: a miss is a field whose `value_in_goal_<i>` says the goal implies a value but
   no candidate was accepted. Only non-personal misses reach the backend. A generated date is re-parsed and
   re-rendered (ambiguous day/month order is rejected), then the value must pass the grounding gate.
4. **Hand back**: `text_value_unavailable`, with `data.fields` listing every visible, editable, non-sensitive
   field still unresolved and the candidates tried. The caller resumes with `values=`.

**Speculation** (`text.speculate`, or `speculate_text=`): `after_miss` (default) starts one background batch
for every missed non-personal field on the snapshot as soon as a decision shows a miss, overlapping the steps
before the TYPE_TEXT; `eager` starts a batch on every page with empty eligible fields, before any miss, so more
text goes out; `off` calls the backend only when the value is needed. Unused batches are counted in
`stats.llm_speculative_unused`. The thread only runs the backend; validation, grounding, and every TypeSafe call
stay on the main thread, because the keep-alive HTTPS connection is not thread-safe. Results are cached per
`(goal, form signature)`, a hash of the visible fields' ordered (label, role, placeholder).

**Grounding gate** (`textgen.grounded`). A generated value is typed only into a field that is neither personal
nor sensitive, and only if: a value shaped like an email, phone number, Luhn-valid card number, or national ID
appears verbatim in the goal; or every word of the value is in the goal; or the `grounded` Noul clears
`config.GROUNDING_NOUL`.

**Sensitive fields** (password, file, and hidden inputs; card, one-time-code, and password autocomplete;
labels matching `config.SENSITIVE_PATTERNS` plus `safety.sensitive_patterns_extra`) are never typed into or
read. They get no value heads and are offered only as hand-back targets, so a decision that needs one returns
`auth_required` (password/OTP), `upload` (file), or `sensitive_field`. **Personal fields** (name, email, phone,
address, postal code, birthday, username; by autocomplete, input type, or label) never reach the text backend.
Jev sees only `empty`, `filled`, or `filled (matches|differs from requested value)`, and evidence, traces, and
run files show `<redacted>` plus `matches_requested`. A caller value for a personal field is still typed, and a
prefill that differs from it (for example a browser autofill) is retyped before DONE is accepted.

## Text backends

```python
batch(goal, fields, page_excerpt, deadline) -> BatchResult
# BatchResult(values={field_key: str | None}, latency_ms, tokens, model, backend, cost_usd,
#             error, error_kind, fallback, canary)
```

Backends must return `{"values": {<known key>: string or null}}` with strings ≤ 500 chars; code fences are
stripped and anything else is invalid, with one retry. Subprocesses run in their own process group, with piped
stdio, in the private `jev-browse-cli/` dir, and are killed on every exit path. `text.backend` chooses; the
default `auto` means `claude` if the CLI is on `PATH`, else `none`. Trade-offs: [backends.md](backends.md).

- **`claude`**: `claude -p` on the user's subscription with no tools, MCP servers, setting sources, or session
  persistence, and low effort. The environment drops the TypeSafe key, Anthropic and OpenAI API keys, and every
  `CLAUDE_*` variable except `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CONFIG_DIR`, so it can only use the
  subscription login. With `claude.pool` (default), `fast_run` pre-starts one stream-json process that receives
  nothing until a miss; each request uses a fresh process and a replacement starts at once. Otherwise cold.
- **`codex`**: `codex exec` cold, read-only sandbox, ephemeral session, user config ignored, schema-constrained
  output, OpenAI API keys removed. Covered by offline tests; not yet measured live.
- **`ollama`**: `/api/chat` with a JSON schema, short positional keys (`f1`, `f2`, ...), temperature 0, and
  optional `num_gpu`, `keep_alive`, `think`. The server URL is private and never appears in errors or logs.
- **`openai`**: any `/chat/completions` endpoint that accepts `response_format: {"type": "json_object"}`; the
  key comes from the environment variable named by `openai.api_key_env`.
- **`none`**: sends nothing; every miss hands back.

**Fallback and canary.** `ollama` and `openai` are wrapped in `FallbackBackend`. The primary must first pass a
known-answer canary: three small field prompts with fixed answers, asked in parallel with no page data, all of
which must match. It catches backends that return well-formed but wrong answers (for example a misconfigured GPU
path). The fallback answers instead when the canary fails, or the server is unreachable (1 s connect timeout),
times out, returns an HTTP error, or returns invalid output twice. The fallback is `text.fallback`: `auto` means
`claude` if on `PATH`, else `none`, in which case the miss hands back and nothing is sent elsewhere. The trace
records backend, model, fallback, and canary verdict.

Every harness script is a new process, so the verdict is cached in `<harness tmp>/jev-browse-canary.json`. Keys
are a truncated SHA-256 of backend kind, base URL, model, `num_gpu`, the canary set, and the local prompt, so the
file holds no host, model name, or credential. Entries are `{healthy, why, checked_at, latency_ms}`, reused for
`canary.ttl_healthy_s` or `canary.ttl_unhealthy_s` (defaults in `config.py`). Writes are atomic (mode 600); an
unreadable, malformed, or group/other-accessible file counts as empty. `fast_run` starts the canary in the
background when the run begins; `canary.enabled = false` skips it.

## Tabs and ownership

The harness daemon has one shared "current" session that can silently re-attach to another tab, possibly the
user's. So jev-browse never uses it and never calls `switch_tab`: every call carries an explicit `session_id`
on a target it owns.

- **Registry**: `jev-browse-owned-<daemon>.json` next to the daemon's IPC socket, so isolated harness instances
  never share one. Updated under `fcntl.flock` with atomic writes (mode 600), pruned against live targets. Each
  entry has an owner tag: `JEV_BROWSE_OWNER`, else the Claude Code session id, else `unknown`.
- **Creating**: `fast_run` and `jev_open` create a background target, register it at once, then navigate, so
  even a failed navigation leaves a tab `jev_close` can close. Pop-ups opened by an owned tab are registered
  automatically (`fast_run` hands back `popup_tab`).
- **Keeper session**: each owned tab keeps one long-lived session holding only focus emulation, so background
  tabs keep rendering and dropdowns survive between harness calls. It does not enable the Page domain, so a
  dialog in a forgotten tab cannot disturb other sessions. It is re-attached after a daemon restart.
- **Per-call sessions**: each call attaches its own Page-enabled session and detaches it at exit. Sessions left
  by dead processes are detached on the next attach; one holding an open dialog is kept for the caller.
- **`jev_adopt(target_id)`**: takes over a tab the caller opened with `new_tab()`. The installed block wraps
  `new_tab` with a recorder that lists target ids before the call and records the returned id in
  `jev-browse-created-<daemon>.json` only if it is new. A reused blank tab, the daemon's start page, a
  user-opened tab, or a tab from before the install is never adoptable. The recorder's lock is non-blocking:
  on any failure it records nothing, and `new_tab` behaves exactly as before. There is no implicit default.
- **`jev_close`**: `jev_close(target_id)` closes one owned tab and refuses unregistered ones;
  `all_owned=True` closes this owner's tabs, `force=True` every owner's; both skip a tab with a live run.
- **Implicit targets**: the interactive helpers use the harness's current tab only if it is the sole live
  owned tab; otherwise they return `not_owned_tab`.

## Safety model

**Commit gate** (`actions.gate_for`). A CLICK or SELECT target is gated when its label contains a commit verb as
whole words after folding (`config.COMMIT_VERBS`, English and Portuguese: delete, send, pay, book, sign out,
...), or it carries a structural signal: a submit control in a form with a sensitive or personal field, a
generic confirmation ("OK", "Continue", "Done") inside a dialog, or an unlabelled icon-only button or link. A
gated target runs only if an unused `confirm=` entry matches it or `commit_ok` clears `config.COMMIT_OK`, and
both operation and target confidence clear `config.COMMIT_CONFIDENCE`. A node-bound `confirm=` entry never
falls back to its label; a bare label authorises only when exactly one visible target carries it; each entry is
used once. Otherwise the run hands back `confirm_required` with the reference and ≤ 200 chars of redacted
context. `jev_click` applies the same gate and needs `confirm=True`. The skill forbids confirming in the same
script that received the hand-back.

**Hand-back reasons**, grouped by what the caller does next (`results.CALLER_ACTION`):

| Do next | Reasons |
|---|---|
| Take over with the harness on `target_id` | `blocked`, `too_many_options`, `state_too_large`, `sensitive_field`, `auth_required`, `upload`, `in_frame` (cross-origin: `iframe_target()`), `visual_only` (`data.screenshot_path`), `in_shadow_dom`, `dialog_open`, `low_confidence`, `no_progress`, `budget_exhausted` |
| Resume with `values=` | `text_value_unavailable` |
| Confirm, then resume or `jev_click(..., confirm=True)` | `confirm_required` |
| Re-open, or continue elsewhere | `not_owned_tab`, `browser_error`, `popup_tab` (`data.target_id`), `host_not_allowed` |
| Retry later | `stale_page`, `service_error` |

**Host allowlist.** `safety.allowed_hosts` (exact hosts or `*.suffix`; empty allows all) is checked before
navigation and adoption, on every `fast_run` observation, and before `jev_find`, `jev_click`, and `jev_check`
read or act. A rejected URL returns `host_not_allowed`.

**Other guards.** Jev can only pick indices the code offered, and the operation rules and text-backend prompts
mark page text as untrusted data. The TypeSafe key comes only from the environment, is never logged, and a
missing key hands back `service_error` before any browser work. Request logs keep bodies only, via `redact()`.

**What is not protected.**

- The commit gate is a **heuristic**. A commit control with an unusual label, in a language outside the verb
  list, or with no structural signal, can be missed, and `commit_ok` is a probability that can be wrong. An
  empty `safety.commit_verbs` turns off only the verb check; structural signals stay on.
- Sensitive and personal classification is pattern-based. A card field with an unrecognised label and no
  autocomplete hint is treated as ordinary.
- The grounding gate reduces, but does not remove, the risk of a generated value the goal does not support. For
  unclassified name-like fields ("Guest", "Recipient"), the Noul is the only barrier.
- A hostile page can steer which observed element Jev picks, and can steer a generated value. It cannot make
  jev-browse run a selector, script, or command: none come from a model.
- Everything not sensitive goes to TypeSafe, and to the text backend after a miss. Use `text_backend="none"`
  for sensitive sites, and do not use jev-browse on pages you would not send to those services.
- Frames, shadow DOM, canvas UIs, uploads, and native dialogs are detected and handed back, not handled.
- The allowlist and ownership rules cover only jev-browse's own calls, not raw harness helpers in the script.
- Run files, traces, and screenshots can contain page content and typed values; `make clean-traces` is yours
  to run.

## Configuration and install

**Settings.** Each setting resolves as: per-call argument > environment (including the harness agent-workspace
`.env`) > TOML config file > default. Secrets are environment-only. An invalid value keeps its default and is
reported by `doctor`. See [configuration.md](configuration.md).

**Install.** `python3 -m jev_browse install`:

1. Appends a marked block to the harness's `agent_helpers.py`, found as the harness finds it
   (`BH_AGENT_WORKSPACE`, then `BH_HOME` / `BROWSER_HARNESS_HOME`, then `XDG_CONFIG_HOME`, then
   `~/.config/browser-harness`). The block appends the checkout to `sys.path` (never at the front), imports the
   seven helpers, and wraps `new_tab` with the recorder, the only harness name it replaces. Its internals are
   underscore-prefixed because the harness copies public names into every script. If the import fails, it
   defines stubs that raise `jev-browse failed to import: ...`, so a broken checkout never breaks other
   scripts. With `JEV_BROWSE_DISABLE=1` the stubs raise `jev-browse disabled` and `new_tab` is untouched.
   Re-running is a no-op; `--uninstall` removes only the block.
2. Symlinks `skill/` into `~/.claude/skills` and, when Codex is installed, `${CODEX_HOME:-~/.codex}/skills`. It
   refuses to overwrite a path that is not its own link.
3. Warns if browser-harness telemetry is on.

**Doctor.** `python3 -m jev_browse doctor` checks Python, config problems, the TypeSafe key (one small live
request unless `--offline`), harness health and telemetry, the block and skill links, and the text backend (the
CLI's presence, or for a server backend a fresh canary unless `--no-canary`). Keys print only as set/unset and
private URLs as `<set>`. It exits 1 on any failed check. `python3 -m jev_browse config` lists every setting
with its value and source.

## Testing

`make check` runs `ruff`, `pytest`, and `node --check jev_browse/snapshot.js`. The unit tests are offline: no
browser, TypeSafe, CLI, or network.

- **`FakeCDP`** (`tests/fakes.py`), installed with `harness_api.install_fake`, the single harness seam. It
  records every call with its session id and reproduces the harness's failures (stale sessions, IPC timeouts,
  an unreachable daemon), so tests can assert that every page call uses an explicit session and that only
  registered tabs are closed.
- **`FakeTypeSafe`** replays scripted Jev answers and records requests, so tests check what was sent
  (redaction, question keys, budgets) as well as decisions.
- **`FakeProc`** replaces `subprocess.Popen` for the `claude` and `codex` backends: output, errors, hangs, kills.
- **Stub HTTP servers** (`ThreadingHTTPServer` on loopback) stand in for Ollama and OpenAI-compatible endpoints:
  request shapes, invalid output, unreachable servers, canary verdicts and their cache, and fallback.
- **`tests/conftest.py`** isolates each test from the developer's setup: it removes `JEV_BROWSE_*` variables,
  points `JEV_BROWSE_CONFIG` at a missing file, and makes "is this CLI on `PATH`" deterministic.

Live checks sit outside the unit tests. `scripts/check_guards.py` exercises freshness, execution, and guards in
a real browser with no model calls, including a check that a planted card number never appears in the evaluate
payload. `bench/` measures jev-browse against an agent driving browser-harness directly, with a local fixture
server (cross-origin frame, commit log), code-only verification, list-price cost accounting, and a routing
decision rule; `bench/text_eval.py` scores a text backend on fixed field cases. See
[benchmarking.md](benchmarking.md).
