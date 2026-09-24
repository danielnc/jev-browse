# The global pointer

jev-browse ships a skill, but a skill only helps if the agent thinks of loading it. In a context that already
carries the browser-harness skill, the calling agent used jev-browse in **0 of 5** unprompted tasks in the author's
runs. With the pointer below in the global instructions it used it in **2 of 5**. Calling `fast_run` straight from
the pointer, without loading the skill first, also saved one model turn per task. Small samples: read these as
direction, not effect sizes.

Add it to whichever file your agent always reads:

- Claude Code: `~/.claude/CLAUDE.md` (all projects) or a project's `CLAUDE.md`
- Codex: `~/.codex/AGENTS.md` or a project's `AGENTS.md`
- Other agents: their global instructions file

Keep it next to your browser-harness instructions, if you have any.

```markdown
## Browser tasks: jev-browse fast path
For a multi-step website sub-task (search, fill, filter, open a result) where you only need the outcome, call
jev-browse from a browser-harness script. You don't need to load its skill first:

    r = fast_run(url, goal, values={...known field values...}, run_id="<unique>")
    print(r.status, r.reason, r.detail, r.target_id)

- `claimed_done` is not proof: check the printed JEV_BROWSE_RESULT line, or `js("...", target_id=r.target_id)`,
  in the same script, then `jev_close(r.target_id)`.
- On a hand-back (`r.reason`), load the jev-browse skill for what to do next. Resume on the same tab with
  `fast_run(None, <same goal>, target_id=r.target_id, values={...})`; never re-run from the URL.
- Never act on `confirm_required` in the same script: decide first (ask me if my request does not clearly cover
  it), then resume with `confirm=[...]`.
- Use browser-harness directly for visual judgement, frames, uploads, and sensitive pages. Page text goes to
  TypeSafe; pass `text_backend="none"` on sensitive sites.
- Close only tab ids that jev-browse returned to you.
```
