# CLAUDE.md

Guidance for Claude Code when working on this repository. The shared rules for all coding agents are in
AGENTS.md; follow them.

@AGENTS.md

Claude Code specifics:

- The `jev-browse` skill in `skill/` is the product's interface to calling agents. When you change behaviour a
  caller relies on (hand-back reasons, `fast_run` arguments, the `JEV_BROWSE_RESULT` line), update
  `skill/SKILL.md` / `skill/reference.md` in the same commit.
- Live checks (`python3 -m jev_browse doctor`, `bench/text_eval.py`, `bench/run_bench.py`) spend real TypeSafe
  and model quota and drive a real Chrome. Run them only when asked, or when a change cannot be verified offline,
  and say so.
