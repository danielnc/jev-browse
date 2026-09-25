"""`jev-browse mcp`: an MCP server (stdio) so any MCP client can use jev-browse.

Needs the optional MCP SDK (`jev-browse[mcp]`); nothing that browser-harness loads imports this module. Each tool
runs through browser-harness via mcp_bridge, so owned-tab safety, config, text backends, and hand-back reasons are
exactly those of the harness helpers. Nothing here writes to stdout: that is the JSON-RPC channel.
"""

import contextlib
import time
from typing import Annotated, Any, Literal

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from . import __version__, config
from . import mcp_bridge as bridge

INSTRUCTIONS = """jev-browse runs website sub-tasks (search, fill, filter, open a result) on its own background
Chrome tab with fast typed decisions, and hands back with a typed reason when it cannot finish.
- Prefer one fast_run(url, goal, values={...}) per sub-task. Put every value you know in `values`.
- claimed_done is not proof: check the returned URL, fields, and text before reporting success.
- On a hand-back, follow the `next:` line. Resume on the same tab with url=null, the same goal verbatim, and the
  returned target_id; never re-run from the URL.
- confirm_required: never confirm in the same turn unless the user's request clearly covers that exact action.
- A `running` result means the run continues: call fast_run_status(run_id).
- Page text goes to TypeSafe (and, after a miss, the text backend). Use text_backend="none" on sensitive sites.
- Close only tabs jev-browse returned (jev_close)."""

SERVER = MCPServer("jev-browse", version=__version__, instructions=INSTRUCTIONS)

TextBackend = Literal["claude", "codex", "ollama", "openai", "none"]
Scalar = str | int | float


def _as_text(values):
    """Field values are typed as text: accept numbers from JSON clients and pass them on as strings."""
    if isinstance(values, dict):
        return {k: str(v) for k, v in values.items()}
    if isinstance(values, list):
        return [str(v) for v in values]
    return values


def _text(call_name, payload):
    text, is_error = bridge.render(call_name, payload)
    if is_error:
        raise ToolError(text)
    return text


def _guarded(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except bridge.BridgeError as exc:
        raise ToolError(str(exc)) from None


async def _progress(ctx, steps, message):
    with contextlib.suppress(Exception):  # no progress token, or no request context (direct calls)
        await ctx.report_progress(steps, None, message)


async def _wait(run_id, ctx):
    """Poll a fast_run until it finishes or mcp.wait_s passes, reporting progress as steps accrue."""
    deadline = time.monotonic() + config.get("mcp.wait_s")
    last = -1
    while True:
        st = _guarded(bridge.state, run_id)
        if st["done"]:
            return _text("fast_run", st["payload"])
        if time.monotonic() >= deadline:
            return bridge.format_running(run_id, st)
        steps = len(((st.get("run") or {}).get("result") or {}).get("trace") or [])
        if steps != last:
            last = steps
            await _progress(ctx, steps, f"fast_run {run_id}: {steps} steps")
        await anyio.sleep(0.25)


@SERVER.tool(structured_output=False)
async def fast_run(
    goal: Annotated[str, Field(description="The whole sub-task in plain words, e.g. 'Search Lisbon, set Design and "
                               "Free cancellation, open Casa Flora'. Resume with the same goal verbatim.")],
    ctx: Context,
    url: Annotated[str | None, Field(description="Start page. null to resume on target_id.")] = None,
    values: Annotated[dict[str, Scalar] | list[Scalar] | None, Field(
        description="Values you already know, keyed by field label or the field key a hand-back returned, e.g. "
                    "{\"Where to?\": \"Lisbon\"}; or a list of candidate values.")] = None,
    run_id: Annotated[str | None, Field(description="Your own id ([A-Za-z0-9_-], max 64) to find the run again. "
                                        "Default: generated.")] = None,
    timeout_s: Annotated[int | None, Field(ge=1, description="Wall-clock budget of the run in seconds (default "
                                           "run.timeout_s, 90). The tool returns after mcp.wait_s at most; a "
                                           "longer run keeps going and fast_run_status collects it.")] = None,
    target_id: Annotated[str | None, Field(description="A jev-browse tab to resume on (from a previous result or "
                                           "jev_open).")] = None,
    confirm: Annotated[list[dict[str, Any] | str] | None, Field(
        description="Commit clicks you are authorised to make: the `confirm ref` objects from a confirm_required "
                    "result. Only when the user's request clearly covers that action.")] = None,
    text_backend: Annotated[TextBackend | None, Field(
        description="Override the text backend for this run; 'none' keeps page text away from any LLM "
                    "(misses hand back).")] = None,
) -> str:
    """Run a multi-step website sub-task (search, fill, filter, open a result) on a background Chrome tab that
    jev-browse owns. Returns the outcome: claimed_done (verify it), blocked, handed_back with a typed reason and a
    `next:` instruction, or running with a run_id to pass to fast_run_status."""
    rid = _guarded(bridge.start_fast_run, url, goal, values=_as_text(values), run_id=run_id, timeout_s=timeout_s,
                   target_id=target_id, confirm=confirm, text_backend=text_backend)
    return await _wait(rid, ctx)


@SERVER.tool(structured_output=False)
async def fast_run_status(
    run_id: Annotated[str, Field(description="The run_id of a fast_run that returned `running`.")],
    ctx: Context,
) -> str:
    """Wait for a running fast_run (up to mcp.wait_s) and return its result, or `running` again."""
    return await _wait(run_id, ctx)


@SERVER.tool(structured_output=False)
def jev_open(url: Annotated[str, Field(description="The page to open.")]) -> str:
    """Open a URL in a new background tab that jev-browse owns (never your current tab). Returns its target_id
    for jev_find, jev_click, jev_check, and fast_run(url=null, target_id=...)."""
    return _text("jev_open", _guarded(bridge.call, "jev_open", url))


@SERVER.tool(structured_output=False)
def jev_find(
    description: Annotated[str, Field(description="The element in plain words, e.g. 'the Search button'.")],
    target_id: Annotated[str, Field(description="A jev-browse tab (from jev_open or fast_run).")],
    k: Annotated[int, Field(ge=1, le=10, description="How many candidates to return.")] = 3,
    scroll: Annotated[bool, Field(description="Scroll the element into view when found.")] = True,
) -> str:
    """Find one element on a jev-browse tab. Returns found (with a `ref` for jev_click), ambiguous, or not_found
    with candidates."""
    return _text("jev_find", _guarded(bridge.call, "jev_find", description, k=k, scroll=scroll,
                                      target_id=target_id))


@SERVER.tool(structured_output=False)
def jev_click(
    ref: Annotated[dict[str, Any] | str, Field(description="The `ref` from jev_find (or a `confirm ref`), as an "
                                               "object or its JSON string.")],
    confirm: Annotated[bool, Field(description="true only to click a commit control (send, pay, delete, book...) "
                                   "that the user's request clearly authorises.")] = False,
) -> str:
    """Click an element found by jev_find, on its jev-browse tab. Commit controls are refused unless confirm=true.
    Refuses stale refs: run jev_find again."""
    return _text("jev_click", _guarded(bridge.call, "jev_click", ref, confirm=confirm))


@SERVER.tool(structured_output=False)
def jev_check(
    condition: Annotated[str, Field(description="A statement about the page, e.g. 'the results list is shown'.")],
    target_id: Annotated[str, Field(description="A jev-browse tab.")],
) -> str:
    """Is a condition true of a jev-browse tab's page? A navigation aid (0.3-0.7 means unsure), never proof that
    a task succeeded."""
    return _text("jev_check", _guarded(bridge.call, "jev_check", condition, target_id=target_id))


@SERVER.tool(structured_output=False)
def jev_close(
    target_id: Annotated[str | None, Field(description="The jev-browse tab to close.")] = None,
    all_owned: Annotated[bool, Field(description="Close every tab this MCP server's runs opened (skips tabs "
                                     "with a live fast_run).")] = False,
) -> str:
    """Close a jev-browse tab by target_id, or all of this server's tabs. Never closes tabs jev-browse does not
    own."""
    if target_id is None and not all_owned:
        raise ToolError("pass target_id, or all_owned=true")
    return _text("jev_close", _guarded(bridge.call, "jev_close", target_id, all_owned=all_owned))


@SERVER.tool(structured_output=False)
def doctor(offline: Annotated[bool, Field(description="Skip the live TypeSafe request and the text-backend "
                                          "canary.")] = False) -> str:
    """Check the jev-browse install (TypeSafe key, browser-harness, the helpers block, the text backend) and
    print what is active, plus the MCP server's own settings."""
    from . import doctor as doc

    lines = []
    code = doc.main(["--offline"] if offline else [], out=lambda *a, **k: lines.append(" ".join(map(str, a))))
    try:
        harness = " ".join(bridge.harness_argv())
        harness_line = f"[  ok] mcp harness command  {harness}"
    except bridge.BridgeError as exc:
        harness_line = f"[FAIL] mcp harness command  {exc}"
        code = code or 1
    lines += ["", f"MCP server {__version__}: wait {config.get('mcp.wait_s')} s per call · tab owner "
              f"{bridge.owner()}", harness_line]
    return "\n".join(lines) + ("\n\nexit status 1: fix the FAIL lines" if code else "")


def main(argv=None):
    """Run the server on stdio until the client disconnects."""
    import argparse
    import os

    from .doctor import load_harness_env

    argparse.ArgumentParser(prog="jev-browse mcp", description="Run the jev-browse MCP server on stdio.").parse_args(argv)
    load_harness_env()  # the server's own settings (and BH_TMP_DIR) may live in the harness .env, as for scripts
    config.reset_cache()
    os.environ.setdefault("JEV_BROWSE_OWNER", bridge.owner())
    SERVER.run()
    return 0
