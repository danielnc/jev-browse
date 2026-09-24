# Text backends

Most `fast_run` decisions need no generated text. Jev picks buttons and links, and field values come from you
(`values=`) or from the goal ("Find flights to **London**" gives the destination). A **text backend** is asked
only after a *miss*: a field whose value the goal implies but does not contain ("the capital of France" →
`Paris`). The answer is then checked by a grounding gate before anything is typed.

Choose a backend with `text.backend` (`JEV_BROWSE_TEXT_BACKEND`), or per call with
`fast_run(..., text_backend="none")`. Settings: [configuration.md](configuration.md).

## At a glance

| Backend | Where the goal, field labels, and page excerpt go | You pay | Needs | Status |
|---|---|---|---|---|
| `claude` | Anthropic, through your Claude subscription (`claude -p`, Haiku by default) | your subscription; no API key is ever used | `claude` CLI logged in | benchmarked |
| `codex` | OpenAI, through your ChatGPT subscription (`codex exec`) | your subscription | `codex` CLI logged in | **not yet benchmarked** |
| `ollama` | your Ollama server | nothing per token | a server and a model | benchmarked; speed depends entirely on your hardware |
| `openai` | whichever OpenAI-compatible endpoint you configure (OpenRouter, Groq, Cerebras, Gemini, a local server) | the provider's per-token price | base URL, model, key | not yet benchmarked |
| `none` | nowhere | nothing | nothing | the privacy mode |

With zero config (`text.backend = "auto"`) you get `claude` if the `claude` CLI is on `PATH`, else `none`.

**Never sent to any text backend:** personal fields (name, email, phone, address, birthday) and sensitive fields
(passwords, one-time codes, card numbers, CVV, IBAN, national IDs). Personal fields appear only as
`empty`/`filled`. Sensitive fields are never read out of the page.

**Always sent to TypeSafe, whatever the backend:** visible page text, element labels, non-personal field values,
URL, and title, on every decision. That is how Jev decides. See the README's "Data egress".

## Trade-offs in one paragraph

Subscription CLIs (`claude`, `codex`) need no keys and use a strong model, but each call starts a CLI process,
which costs seconds. `claude` hides most of that by pre-starting one process per run. A local model (`ollama`)
keeps page text on your machine and costs nothing per token; its speed is whatever your hardware gives, and small
or misconfigured models can answer wrongly, which is what the canary is for. Hosted OpenAI-compatible endpoints
can be very fast and cheap, but your page text goes to that provider. `none` sends nothing and hands every miss
back to your agent. Whatever you choose, measure it on your setup with `bench/text_eval.py`
([benchmarking.md](benchmarking.md)): it takes a few minutes.

## claude (default when the CLI is present)

Runs `claude -p --model haiku` with no tools, no MCP servers, no slash commands, no session persistence, and low
effort, from a private empty directory. `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and every `CLAUDE_*` session
variable are removed from its environment (except the subscription credentials), so it can never bill the API.
By default one `claude` process is pre-started per run and nothing is sent to it until a miss (`claude.pool`), which
removes most of the CLI's start-up time from the miss.

- A model with account context can volunteer personal data it knows about you when a field asks for it. That
  is why `fast_run` never sends personal fields to any text backend, and why the grounding gate rejects values the
  goal does not support.
- Because it runs from a private directory, a shell wrapper that picks an account by directory is bypassed. The
  globally active `claude` login is used.

## codex (unmeasured)

Runs `codex exec -m <codex.model> --sandbox read-only --ephemeral --ignore-user-config --ignore-rules
--skip-git-repo-check --output-schema <schema> -o <file> -` from a private empty directory:

- The page text goes on stdin only, never on the command line.
- Output is constrained by a JSON schema with short field keys.
- Your Codex config (MCP servers, hooks, rules) is not loaded.
- `OPENAI_API_KEY` and similar variables are removed, so it uses the stored subscription login.

Each call is a cold `codex exec` process, so expect a few seconds of start-up per miss. The backend is covered by
tests with a mocked CLI but has not been benchmarked yet. Run `bench/text_eval.py --backends codex` before relying
on it, and please share the result.

## ollama

Sends the request to `POST <ollama.url>/api/chat` with a strict JSON schema of short field keys (`f1`, `f2`, …).
Local models tend to rewrite long keys or echo the input without it. It uses temperature 0 and a 256-token output
cap, `keep_alive` from `ollama.keep_alive` (default `30m`), and `think` from `ollama.think` (`auto` turns thinking
off for qwen3 and sets it low for gpt-oss). The URL is private configuration: it is never logged, never put in
traces, and shown as `<set>` by `doctor`.

**The canary.** A model can return well-formed JSON with wrong answers. So before a local or OpenAI-compatible
backend is used, it must answer three known-answer prompts correctly (Lima, Paris, Dakar; no page data is sent).
If any answer is wrong, or the server is unreachable (1 s connect timeout), errors, or returns invalid output
twice, the `text.fallback` backend answers instead. The trace records `{backend, model, fallback, canary}`.

The verdict is cached across processes in `jev-browse-canary.json` in the harness tmp dir, so each `fast_run` (a
new process) does not pay for it again:

- The file is mode 600, written atomically.
- Keys are hashed, so the file holds no URL, model name, or key.
- A healthy verdict lasts 10 min and an unhealthy one 2 min (`canary.ttl_*`).
- Turn the canary off with `canary.enabled = false` only if you have checked the backend yourself.

Why it exists: an inference stack running on an unsupported or misconfigured GPU can return fluent,
well-formed, **wrong** answers (for example a different city than the one in the goal), consistently and without
any error. If the canary fails on GPU but passes with `ollama.num_gpu = 0` (CPU only), suspect the GPU stack.
Server-side batching of concurrent requests has been seen to cause the same symptom on some setups; if answers
are wrong only under load, try the server's one-request-at-a-time setting. `jev-browse doctor` runs the canary for
you.

Speed depends on your hardware and on the server's **prompt cache**. A prompt whose beginning the server has just
seen is much faster than a new one. The canary's verdict cache also keeps the canary's own prompts from evicting
your real prompts on every run. Measure your model with `bench/text_eval.py` (use `--excerpt` for realistic prompt
sizes).

## openai (any OpenAI-compatible endpoint)

`POST <openai.base_url>/chat/completions` with `response_format: {"type": "json_object"}`, temperature 0,
256-token output cap, the same short keys, and the same canary and fallback as `ollama`. The key is read from the
environment variable that `openai.api_key_env` names (default `JEV_BROWSE_OPENAI_API_KEY`), so you can reuse
`OPENROUTER_API_KEY`, `GROQ_API_KEY`, and so on. The endpoint must accept `json_object` response format.
**Unmeasured**: run `text_eval` against your provider and model. Hosted endpoints receive the goal, field labels,
and page excerpt, like any other backend, under that provider's data policy.

## none (privacy mode)

Nothing is sent to any LLM provider. A miss hands back to you with `text_value_unavailable` and the list of
fields. Resume with `fast_run(None, <same goal>, target_id=r.target_id, values={...})`. Use it for sensitive sites.
Page text still goes to TypeSafe.

## Fallback

`text.fallback` decides who answers when an `ollama` or `openai` backend fails its canary or its request:
`auto` (the default: `claude` if the CLI is on `PATH`, else `none`), `claude`, `codex`, or `none`. With a
cloud fallback, page text goes to that provider whenever your local backend is down, which can surprise you if
you chose a local model for privacy. Set `text.fallback = "none"` in that case: misses then hand back instead.

## Adding a backend

A backend is a class with `name`, `model`, `prewarm()` (may do nothing), and `batch(goal, fields, page_excerpt,
deadline) -> BatchResult`. `values` must map every field key to a string or `None`, and `error` is set on failure
(never raise for an expected failure). Wire it into `make_backend` in `jev_browse/textgen.py`, add its settings to
the registry in `jev_browse/config.py` (then `make docs-gen`), and add tests with a fake process or a stub HTTP
server. See [CONTRIBUTING.md](../CONTRIBUTING.md).
