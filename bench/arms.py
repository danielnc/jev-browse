"""Benchmark arms: which tasks each arm runs, how a Claude arm is launched, and the
stream-json audit (turns, usage, forbidden names, jev_used, caller_values, skill loaded)."""

import json
import re

CLAUDE_ARMS = {"A", "A-int", "B", "A-llm", "A-none", "A-unprompted", "A-fast", "A-llm-ollama", "A-fast-ollama"}
A_FAMILY = {"A", "A-int", "A-llm", "A-none", "A-unprompted", "A-fast", "A-llm-ollama", "A-fast-ollama"}
SCRIPT_ARMS = {"A0", "A0-llm", "A0-llm-ollama", "A0-ollama"}
ALL_PUBLIC = ("wiki", "flights", "hotel", "llm", "iframe")

# One mapping, so a new arm cannot inherit the full matrix by omission.
ARM_TASKS = {
    "A": ALL_PUBLIC + ("private",),
    "B": ALL_PUBLIC + ("private",),
    "A0": ALL_PUBLIC + ("private",),
    "A-int": ("hotel",),
    "A0-llm": ("llm",),
    "A-llm": ("llm",),
    "A-none": ("llm",),
    "A-unprompted": ALL_PUBLIC,
    "A-fast": ALL_PUBLIC,  # addendum: the global-context fast path, no skill-load instruction
    "A-fast-ollama": ALL_PUBLIC,  # addendum: same, with the local text backend
    "A0-ollama": ALL_PUBLIC,  # addendum: script only, with the local text backend
    "A-llm-ollama": ("llm",),  # addendum: the local text backend
    "A0-llm-ollama": ("llm",),
}

ARM_ENV = {
    "B": {"JEV_BROWSE_DISABLE": "1"},
    "A-fast": {"JEV_BROWSE_TEXT_BACKEND": "claude"},
    "A-fast-ollama": {"JEV_BROWSE_TEXT_BACKEND": "ollama"},
    "A0-ollama": {"JEV_BROWSE_TEXT_BACKEND": "ollama"},
    "A0-llm": {"JEV_BROWSE_VALUE_CANDIDATES": "0"},
    "A-llm": {"JEV_BROWSE_VALUE_CANDIDATES": "0"},
    "A-none": {"JEV_BROWSE_VALUE_CANDIDATES": "0", "JEV_BROWSE_TEXT_BACKEND": "none"},
    "A-llm-ollama": {"JEV_BROWSE_VALUE_CANDIDATES": "0", "JEV_BROWSE_TEXT_BACKEND": "ollama"},
    "A0-llm-ollama": {"JEV_BROWSE_VALUE_CANDIDATES": "0", "JEV_BROWSE_TEXT_BACKEND": "ollama"},
}
PRIVATE_ENV = {"JEV_BROWSE_TEXT_BACKEND": "none"}  # every arm, so no private page text reaches a text provider

TOOL_INSTRUCTION = {
    "A": "Use the jev-browse skill (fast_run and its helpers) for this.",
    "A-llm": "Use the jev-browse skill (fast_run and its helpers) for this.",
    "A-none": "Use the jev-browse skill (fast_run and its helpers) for this.",
    "A-int": "Use the jev-browse interactive helpers (jev_open, jev_find, jev_click, jev_check) for this; do not "
             "use fast_run.",
    "B": "Drive the browser with the browser-harness helpers directly.",
    "A-unprompted": "",
    "A-fast": "Use jev-browse's fast_run for this.",
    "A-fast-ollama": "Use jev-browse's fast_run for this.",
    "A-llm-ollama": "Use the jev-browse skill (fast_run and its helpers) for this.",
}

FORBIDDEN = {
    "B": re.compile(r"\b(fast_run|jev_open|jev_adopt|jev_find|jev_click|jev_check|jev_close)\s*\("),
    "A-int": re.compile(r"\bfast_run\s*\("),
}
JEV_NAME = re.compile(r"\b(fast_run|jev_open|jev_adopt|jev_find|jev_click|jev_check|jev_close)\s*\(")


def env_for(arm, task_key):
    env = dict(ARM_ENV.get(arm, {}))
    if task_key == "private":
        env.update(PRIVATE_ENV)
    return env


def prompt(arm, task):
    base = (f"Use browser-harness (the `browser-harness` command with a Python heredoc on stdin) to do this in "
            f"the browser: {task.goal} Start from {task.url} in a new tab. The harness daemon is already running. "
            "Leave the tab with the final page open when you finish (it is checked afterwards). "
            "When you are done, reply with one line saying what you did and the final URL.")
    extra = TOOL_INSTRUCTION.get(arm, "")
    return base + (" " + extra if extra else "")


def claude_argv(model="opus"):
    return ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", model,
            "--tools", "Bash,Skill", "--allowedTools", "Bash(browser-harness:*)", "Bash(browser-harness *)", "Skill"]


def audit(stream_text, arm):
    """Parse stream-json lines into the attempt's audit record."""
    commands, tools_listed, skill_loaded, result = [], None, False, None
    for line in stream_text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            tools_listed = sorted(ev.get("tools") or [])
            skills = ev.get("skills") or ev.get("slash_commands") or []
            ev_skills = [s if isinstance(s, str) else s.get("name", "") for s in skills]
            tools_listed = {"tools": tools_listed, "skills_listed": "jev-browse" in " ".join(ev_skills)}
        elif ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") != "tool_use":
                    continue
                if block.get("name") == "Bash":
                    commands.append((block.get("input") or {}).get("command", ""))
                elif block.get("name") == "Skill":
                    if "jev-browse" in json.dumps(block.get("input") or {}):
                        skill_loaded = True
        elif ev.get("type") == "result":
            result = ev
    joined = "\n".join(commands)
    forbidden = FORBIDDEN.get(arm)
    first_fast_run = next((c for c in commands if re.search(r"\bfast_run\s*\(", c)), "")
    gate_violation = any("confirm_required" in c and re.search(r"confirm\s*=\s*(True|\[)", c) for c in commands)
    return {
        "bash_commands": len(commands),
        "forbidden_used": bool(forbidden and forbidden.search(joined)),
        "jev_used": bool(JEV_NAME.search(joined)),
        "caller_values": "values=" in first_fast_run,
        "skill_loaded": skill_loaded,
        "init": tools_listed,
        "gate_violation": gate_violation,
        "update_or_reload": bool(re.search(r"browser-harness\s+--(update|reload)", joined)),
        "result": {k: result.get(k) for k in ("num_turns", "usage", "modelUsage", "total_cost_usd", "is_error",
                                              "duration_ms", "result")} if result else None,
    }
