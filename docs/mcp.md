# MCP server

`jev-browse mcp` runs an [MCP](https://modelcontextprotocol.io) server on stdio, so clients that cannot write
browser-harness scripts can use jev-browse too: Claude Desktop, Cursor, Codex, Windsurf, or any other MCP client.
Claude Code and Codex users can keep calling the helpers from browser-harness scripts (the skill). Both paths run the
same code.

Every tool call runs a short generated script through the `browser-harness` CLI, exactly as an agent's heredoc
would. That means the same daemon, the same harness `.env`, the same installed helpers block, the same owned-tab
safety, config, and text backends, and the same hand-back reasons. The server itself holds no browser state.

## Requirements

Everything in the [Quickstart](../README.md#quickstart) has to work first: Chrome, browser-harness connected to it,
`python3 -m jev_browse install`, and `TYPESAFE_API_KEY` in the harness `.env`. `python3 -m jev_browse doctor` should
pass. The server also needs [uv](https://docs.astral.sh/uv/), which installs the MCP SDK (the `mcp` extra) on first
start. The browser-harness runtime itself stays standard-library only.

Before the first MCP call, run one browser-harness command in a terminal (for example `python3 -m jev_browse
doctor`) so that Chrome's "Allow remote debugging" prompt is out of the way. A tool call that waits on that prompt
times out.

## Launch command

From a clone of this repository (replace `/path/to/jev-browse`):

```bash
uv run --directory /path/to/jev-browse --extra mcp python -m jev_browse mcp
```

With a package install that includes the extra (`pip install 'jev-browse[mcp]'`), the command is `jev-browse mcp`.

GUI apps such as Claude Desktop often start with a short `PATH`. If the client cannot find `uv`, use its absolute
path (`which uv`). If the server cannot find `browser-harness`, set `JEV_BROWSE_HARNESS_COMMAND` to its absolute path
(`which browser-harness`) in the server's `env`. The server also looks in `~/.local/bin`, where `uv tool install`
puts it.

## Client configuration

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json`
on Windows. Restart Claude Desktop afterwards.

```json
{
  "mcpServers": {
    "jev-browse": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/jev-browse", "--extra", "mcp", "python", "-m", "jev_browse", "mcp"],
      "env": {
        "JEV_BROWSE_HARNESS_COMMAND": "/absolute/path/to/browser-harness"
      }
    }
  }
}
```

The `env` block is optional. You need it only when `browser-harness` is not on the app's `PATH` or in
`~/.local/bin`.

### Cursor

`~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` in a project:

```json
{
  "mcpServers": {
    "jev-browse": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/jev-browse", "--extra", "mcp", "python", "-m", "jev_browse", "mcp"]
    }
  }
}
```

### Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.jev-browse]
command = "uv"
args = ["run", "--directory", "/path/to/jev-browse", "--extra", "mcp", "python", "-m", "jev_browse", "mcp"]
env = { JEV_BROWSE_MCP_WAIT_S = "150" }
```

Codex allows 300 s per tool call by default (`tool_timeout_sec`). With the wait above, most `fast_run` calls finish
in a single call instead of returning `running`.

### Other clients

Claude Code: `claude mcp add jev-browse -- uv run --directory /path/to/jev-browse --extra mcp python -m jev_browse
mcp`. Windsurf and other clients take the same command and arguments in their own MCP config file.

These snippets follow each client's documented config format. The author has not tried every client, and MCP use
has not been benchmarked. The numbers in [benchmark.md](benchmark.md) were measured with browser-harness scripts.

## Tools

| Tool | What it does |
|---|---|
| `fast_run(goal, url?, values?, run_id?, timeout_s?, target_id?, confirm?, text_backend?)` | A multi-step website sub-task on a background tab jev-browse owns. Returns `claimed_done`, `blocked`, `handed_back` (with a typed reason), or `running`. |
| `fast_run_status(run_id)` | Wait for a `running` fast_run and return its result, or `running` again. |
| `jev_open(url)` | Open a new owned background tab. Returns its `target_id`. |
| `jev_find(description, target_id, k?, scroll?)` | Locate one element. `found` comes with a `ref` for `jev_click`. The other outcomes are `ambiguous` or `not_found`, both with candidates. |
| `jev_click(ref, confirm?)` | Click a `jev_find` ref (or a `confirm ref`). Commit controls are refused unless `confirm=true`. |
| `jev_check(condition, target_id)` | Is a condition true of the page? This helps with navigation. It does not prove a task succeeded. |
| `jev_close(target_id?, all_owned?)` | Close an owned tab, or every tab this server's runs opened. |
| `doctor(offline?)` | The `doctor` checks plus the server's own settings. |

Results are short text written for the agent to act on: the status and reason, a `next:` line saying what to do
(resume with `values`, confirm, take over, retry), `target_id` and `run_id`, the page title and URL, visible field
values (personal and sensitive values are never shown), the start of the page text, and the last steps. Setup
problems and bad arguments come back as MCP tool errors. Hand-backs do not: they are normal results.

The rules from the skill still apply. `claimed_done` is not proof: check the returned URL, fields, and text first.
Resume on the same tab with `url=null`, the same goal verbatim, and the returned `target_id`. Pass a
`confirm_required` result's `confirm ref` back (in `fast_run`'s `confirm` list, or to `jev_click` with
`confirm=true`) only when the user's request clearly covers that exact action. `jev_adopt` is not exposed, because it
needs a tab from the harness's `new_tab()`.

## Timeouts and resuming

MCP clients cancel tool calls that run too long. Clients built on the TypeScript SDK wait 60 s by default. A
`fast_run` can take longer (its own budget, `timeout_s`, defaults to 90 s), so the server waits at most
`mcp.wait_s` (45 s by default) and then answers:

```text
fast_run: running (run_id 20260925-101500-a1b2c3, 45 s so far, 6 steps, target_id 1A2B...)
next: call fast_run_status(run_id="20260925-101500-a1b2c3") to wait for the result. ...
```

The run keeps going in its own browser-harness process and still stops at its own `timeout_s`. `fast_run_status`
collects the result. This also works after the server restarts, because it reads the run file
(`jev-browse-run-<run_id>.json`) and the run's log in the harness tmp dir. While a run is in progress the server
sends MCP progress notifications (steps so far) to clients that ask for them. Starting a second run under a
`run_id` that is still running is refused.

The short tools (`jev_open`, `jev_find`, `jev_click`, `jev_check`, `jev_close`) also wait at most `mcp.wait_s`. If
one is stopped at that limit, the result says so. A `jev_click` stopped this way may or may not have clicked, so
check the page before you click again.

## Settings

| Setting | Environment | Default | Meaning |
|---|---|---|---|
| `mcp.harness_command` | `JEV_BROWSE_HARNESS_COMMAND` | `browser-harness` | The command every tool call runs through (name on `PATH` or absolute path) |
| `mcp.wait_s` | `JEV_BROWSE_MCP_WAIT_S` | `45` | How long a tool call waits before returning; keep it below the client's tool timeout |
| (environment only) | `JEV_BROWSE_OWNER` | `mcp-<random>` per server | Tab-ownership tag of this server's runs; `jev_close(all_owned=true)` closes only its tabs |

Set them in the client's `env` block, in the browser-harness `.env` (which the server loads at start, as the
harness does), or in `config.toml`. Every other setting (text backend, budgets, `safety.allowed_hosts`) works as
described in [configuration.md](configuration.md), because it is read inside the harness script.

## Privacy

- **Harness telemetry is off for MCP calls.** The server sets `BH_TELEMETRY=0` in the environment of every script it
  runs, whatever your browser-harness telemetry setting is. With harness telemetry on, the generated script text
  (your goal and `values`) and the helper-call arguments would go to PostHog, and MCP users never see those
  scripts. Your own browser-harness scripts are unaffected.
- **What leaves your machine** is the same as for scripts: page text to TypeSafe on every decision and, after a
  miss, to your text backend (see [the README](../README.md#safety-and-privacy)). Pass `text_backend: "none"` on
  sensitive sites. The MCP client's model also sees what the tools return: page text excerpts and non-personal
  field values, never sensitive or personal ones.
- **Local files.** Each `fast_run` writes its output to `jev-browse-mcp-<run_id>.log` (mode 600) next to its run file
  in the harness tmp dir. It can contain page text and typed values. `make clean-traces` removes these logs along
  with the run files.

## Troubleshooting

- Run the `doctor` tool (or `python3 -m jev_browse doctor` in a terminal). It checks the key, browser-harness, the
  helpers block, and the text backend, and it prints the harness command and wait the server uses.
- `jev-browse is not installed in browser-harness`: run `python3 -m jev_browse install` from the clone.
- `browser-harness failed. ... remote debugging ...`: follow the harness's message. Usually Chrome is waiting for
  you to allow remote debugging; see [install.md](../install.md).
- `jev-browse mcp needs the MCP SDK`: start the server with `--extra mcp` (or install `jev-browse[mcp]`).
- Every call returns `running`: raise `JEV_BROWSE_MCP_WAIT_S` if your client allows longer tool calls, or keep
  calling `fast_run_status`.
