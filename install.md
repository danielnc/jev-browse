---
name: jev-browse-install
description: Install jev-browse into browser-harness and a coding agent (Claude Code or Codex), configure it, and verify it with doctor.
---

# Installing jev-browse

Written for a coding agent to follow step by step (a person can too). Do each step, check its result, and stop to
ask the user where this says **Ask**. Never print, echo, or log the TypeSafe key or any other secret. Never edit
files other than the ones named here.

## 0. Prerequisites

Check each. If one is missing, fix it or tell the user what is needed, then continue.

```bash
python3 --version            # needs 3.11+
browser-harness --version    # any output means it is installed
uv --version                 # or: pipx --version (either installs the jev-browse command)
```

- **browser-harness missing or not connected to Chrome:** follow its own setup first
  (https://github.com/browser-use/browser-harness/blob/main/install.md). It is ready when this prints page info:
  `browser-harness <<'PY'` / `print(page_info())` / `PY`.
- **Chrome** must be running with remote debugging allowed (browser-harness's install covers this).
- **Neither uv nor pipx:** **Ask** the user which to install (recommend uv: https://docs.astral.sh/uv/), or use
  the git checkout in step 1.

## 1. Get jev-browse

Install the package as a tool. It has no runtime dependencies (standard library only):

```bash
uv tool install jev-browse           # or: pipx install jev-browse
jev-browse --help
```

If it is already installed, upgrade it instead: `uv tool upgrade jev-browse` (or `pipx upgrade jev-browse`). If
`jev-browse` is not found afterwards, run `uv tool update-shell` (or `pipx ensurepath`) and open a new shell.

**From source instead** (to hack on it, or without uv/pipx): clone it to a stable location, since the harness loads
jev-browse from the checkout, then run every `jev-browse <command>` below as `python3 -m jev_browse <command>` from
the checkout, or as `~/jev-browse/scripts/jev-browse <command>` from anywhere:

```bash
git clone https://github.com/danielnc/jev-browse ~/jev-browse     # or, if it exists: git -C ~/jev-browse pull --ff-only
```

## 2. Install the helpers and the skill

```bash
jev-browse install
```

This does two things:

1. It appends a marked block to the harness's `agent-workspace/agent_helpers.py`, so every browser-harness script
   gets `fast_run`, `jev_open`, `jev_adopt`, `jev_find`, `jev_click`, `jev_check`, `jev_close`. The user's own code
   in that file is left untouched, and `jev-browse uninstall` removes only the block. The block also
   wraps the harness's `new_tab()` with a transparent recorder, so `jev_adopt` can take over tabs the agent opened.
   If jev-browse ever fails to import, the helpers become stubs that raise a clear error, and every other harness
   script keeps working.
2. It symlinks the skill (`skill/`) as `jev-browse` into each installed agent's skills dir: `~/.claude/skills/`
   and/or `${CODEX_HOME:-~/.codex}/skills/`. Use `--agent claude|codex|all` to choose. The installer refuses to
   overwrite a different existing `jev-browse` skill; if it refuses, tell the user and stop. If the user installed
   the jev-browse **Claude Code plugin**, it already provides the skill: use `jev-browse install --no-skill`, or
   `--agent codex` when Codex is installed too.

It also warns if browser-harness telemetry is enabled. **Ask** the user whether to opt out
(`browser-harness telemetry disable`). Telemetry sends script text and helper-call arguments, including goals and
`values=`, to PostHog. Default to recommending opt-out, but it is their global setting: don't change it without a
yes.

## 3. The TypeSafe API key

If the user runs a compatible custom SystemOne server, configure `jev.base_url`, `jev.model`, and authentication
as described in [configuration.md](docs/configuration.md#custom-systemone-servers). Ask whether it requires a
bearer token. With `jev.auth = "none"`, skip the API-key step; otherwise store only the selected server's key
in the harness `.env`. Do not request a TypeSafe key for a custom server or forward one to it implicitly.

**Ask** the user for their TypeSafe API key (from https://console.typesafe.ai/keys) unless `doctor` already reports it set. Store
it in the harness agent-workspace `.env`, which the harness loads into every script. Replace an existing line
rather than adding a duplicate. Keep the file private:

```bash
ENV=~/.config/browser-harness/agent-workspace/.env      # or $BH_AGENT_WORKSPACE/.env if that is set
grep -q '^TYPESAFE_API_KEY=' "$ENV" 2>/dev/null && echo "already set: ask before replacing"
# write it without echoing the value to the terminal or your transcript, e.g. from the user's clipboard or a prompt:
printf 'TYPESAFE_API_KEY=%s\n' "$KEY" >> "$ENV" && chmod 600 "$ENV"
```

Every Jev decision is a metered TypeSafe request (a few tenths of a cent per task at the time of writing). Tell
the user this once.

## 4. Choose a text backend

**Ask** the user which text backend to use for values that the goal implies but does not state. Explain the
choices in one line each (details: `docs/backends.md`):

- `claude`: their Claude subscription via the `claude` CLI. This is the default when the CLI is installed.
- `codex`: their ChatGPT subscription via the `codex` CLI (unmeasured so far).
- `ollama`: a local model on an Ollama server. Needs the URL and a model.
- `openai`: any OpenAI-compatible endpoint (OpenRouter, Groq, …). Needs the base URL, model, and a key.
- `none`: nothing goes to an LLM provider; misses come back to the agent.

If the answer is the default, write nothing. Otherwise write the choice to the harness `.env` (for URLs and keys)
or to `~/.config/jev-browse/config.toml` (for everything else). For `ollama` or `openai`, also **ask** whether a failure may
fall back to the `claude` CLI, which sends the page text to Anthropic. That is the default (`text.fallback =
"auto"`) when the CLI is installed. If not, set `JEV_BROWSE_TEXT_FALLBACK=none`. Example for Ollama:

```bash
printf 'JEV_BROWSE_TEXT_BACKEND=ollama\nJEV_BROWSE_OLLAMA_URL=%s\n' "http://127.0.0.1:11434" >> "$ENV"
mkdir -p ~/.config/jev-browse
jev-browse config --example > ~/.config/jev-browse/config.toml   # then uncomment what you change
```

## 5. Verify

```bash
jev-browse doctor
```

Fix every `FAIL` line (each one says how), then run it again until it ends with `All required checks passed.` It
sends one tiny request to the configured SystemOne server (even with authentication disabled), and for
`ollama`/`openai` it runs the three-prompt canary. A failed canary means
the backend answers wrongly (for example a misconfigured GPU): try `ollama.num_gpu = 0`, another model, or another
backend. Show the user the summary lines at the top of the output.

Then run one real task:

```bash
browser-harness <<'PY'
r = fast_run("https://en.wikipedia.org/wiki/Main_Page", "Open the Wikipedia article about the Eiffel Tower",
             run_id="install-check")
print(r.status, r.reason, r.url)
jev_close(r.target_id)
PY
```

Expect `claimed_done None https://en.wikipedia.org/wiki/Eiffel_Tower`. A `handed_back` result prints its reason.
Use `docs/architecture.md` and `skill/reference.md` to interpret it.

## 6. The global pointer (optional, recommended)

**Ask** the user whether to add the jev-browse pointer to their global agent instructions (`~/.claude/CLAUDE.md`
for Claude Code, `~/.codex/AGENTS.md` for Codex). Without it, agents rarely use jev-browse unprompted. The text is
in `docs/global-pointer.md`: append it verbatim, once, near any browser-harness instructions. Don't add it twice.
Check for the heading `Browser tasks: jev-browse fast path` first.

## Uninstall

```bash
jev-browse uninstall                # removes the agent_helpers block and the skill links; nothing else
uv tool uninstall jev-browse        # or: pipx uninstall jev-browse (or delete the checkout)
```

Then remove the `TYPESAFE_API_KEY` / `JEV_BROWSE_*` lines from the harness `.env` and the
pointer from the agent instructions if the user wants.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `fast_run` is not defined in a harness script | `jev-browse doctor` → re-run `install`; check `BH_AGENT_WORKSPACE` if the harness uses a custom workspace |
| `RuntimeError: jev-browse failed to import: …` | The package was reinstalled under another Python, or the checkout moved: re-run `jev-browse install` |
| `service_error` / TypeSafe 401 | The key is wrong or missing in the harness `.env` |
| `text_value_unavailable` on every miss | The text backend is `none`, or it failed (see the trace's `llm` entry); run `doctor` |
| The skill never triggers | Add the global pointer (step 6) |
