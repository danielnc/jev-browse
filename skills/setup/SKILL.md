---
name: setup
description: Install or repair jev-browse for this machine. Installs the jev-browse package, wires it into browser-harness, stores the TypeSafe key, picks a text backend, and runs doctor. Run it once after installing the plugin, and again after upgrading.
disable-model-invocation: true
---

# Set up jev-browse

This plugin already gives Claude Code the `jev-browse` skill. What it can't do by itself is put `fast_run` and the
other helpers into browser-harness. Do that here, step by step. Check each result. Stop and ask the user where
this says **Ask**. Never print, echo, or log the TypeSafe key or any other secret.

## 1. Prerequisites

```bash
python3 --version            # needs 3.11+
browser-harness --version    # any output means it is installed
uv --version                 # or: pipx --version
```

If browser-harness is missing or not connected to Chrome, send the user to its setup first
(https://github.com/browser-use/browser-harness/blob/main/install.md) and stop. If neither `uv` nor `pipx` is
installed, **ask** the user which one to install (recommend uv: https://docs.astral.sh/uv/).

## 2. Install the package

```bash
uv tool install jev-browse     # or: pipx install jev-browse
jev-browse --help
```

If it is already installed, upgrade it instead: `uv tool upgrade jev-browse` (or `pipx upgrade jev-browse`). If
`jev-browse` is not found after installing, the tool bin dir is not on PATH: run `uv tool update-shell` (or
`pipx ensurepath`) and tell the user to open a new shell. Until then, use the absolute path that
`uv tool dir --bin` prints.

## 3. Wire it into browser-harness

The plugin provides the skill for Claude Code, so don't link a second copy into `~/.claude/skills`:

```bash
if [ -d "${CODEX_HOME:-$HOME/.codex}" ]; then
  jev-browse install --agent codex     # Codex is installed too: link the skill for Codex only
else
  jev-browse install --no-skill
fi
```

This appends a marked block to the harness's `agent-workspace/agent_helpers.py`. The user's own code in that file
is left untouched. If `~/.claude/skills/jev-browse` already exists from an earlier manual install, tell the user
that Claude Code now sees the skill twice. Offer to remove the old link with
`jev-browse uninstall --agent claude`, then re-run the command above, because uninstall also removes the block.

If the installer warns that browser-harness telemetry is enabled, **ask** whether to opt out
(`browser-harness telemetry disable`). Telemetry sends script text and helper-call arguments, including goals and
`values=`, to PostHog. Recommend opting out, but it is the user's global setting: don't change it without a yes.

## 4. Key, text backend, check, and pointer

Follow steps 3 to 6 of `${CLAUDE_PLUGIN_ROOT}/install.md` exactly:

- **3.** Store the TypeSafe API key in the harness `.env` (ask for it unless `jev-browse doctor` reports it set).
- **4.** Ask which text backend to use (default: `claude` when the CLI is installed).
- **5.** Run `jev-browse doctor` until it ends with `All required checks passed.`, then run the one real task.
  A `skill ... not linked` warning is expected here: the plugin provides the skill.
- **6.** Ask whether to add the global pointer to `~/.claude/CLAUDE.md`.

Wherever install.md says `python3 -m jev_browse`, run `jev-browse` instead.

## After an upgrade

After `uv tool upgrade jev-browse`, run `jev-browse doctor`. If it says the helpers block is from another
checkout or version (for example, uv moved the tool to a new Python), re-run step 3.
