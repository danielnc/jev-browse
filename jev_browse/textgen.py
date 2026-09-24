"""Text backends (claude, codex, ollama, openai, none; docs/backends.md): batched per page, speculated after a miss,
validated as strict JSON, and gated by grounding before anything is typed. Subscription CLIs never get API keys.

Nothing outlives the call: subprocesses start in their own process group with piped stdio and are killed by
group on every exit path. Background threads only run the CLI; validation and every TypeSafe call happen on
the calling thread (the keep-alive HTTPSConnection is not thread-safe).
"""

import atexit
import datetime as dt
import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

from . import config, harness_api, questions
from .candidates import field_date_format, is_date_field, parse_dates
from .textnorm import fold, words

MAX_VALUE_LEN = 500
EXCERPT_CHARS = 2000
MCP_EMPTY = '{"mcpServers":{}}'
KEEP_ENV = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
DROP_ENV = {"TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
DROP_PREFIXES = ("CLAUDE_", "CLAUDECODE")
OPENAI_ENV = ("OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "AZURE_OPENAI_API_KEY")


@dataclass
class BatchResult:
    values: dict
    latency_ms: int = 0
    tokens: dict = field(default_factory=dict)
    model: str = ""
    backend: str = ""
    cost_usd: float | None = None
    error: str | None = None
    error_kind: str | None = None  # unreachable | invalid output | http error | ...
    fallback: dict | None = None   # {"from": <primary backend>, "why": <error_kind>} when another backend answered
    canary: dict | None = None     # the process's known-answer check of a local/OpenAI-compatible backend


def scrubbed_env(env):
    """Prefix-based scrub with a keep-list: API keys and every CLAUDE_*/CLAUDECODE* var removed,
    except the subscription credentials CLAUDE_CODE_OAUTH_TOKEN and CLAUDE_CONFIG_DIR."""
    out = {}
    for k, v in env.items():
        if k in KEEP_ENV:
            out[k] = v
        elif k in DROP_ENV or k.startswith(DROP_PREFIXES) or k in OPENAI_ENV:
            continue
        else:
            out[k] = v
    return out


def private_cwd():
    d = harness_api.tmp_dir() / "jev-browse-cli"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return str(d)


# ---- process bookkeeping: nothing outlives the call -----------------------------------------------------------
_procs = set()
_procs_lock = threading.Lock()


def kill_all():
    with _procs_lock:
        procs = list(_procs)
        _procs.clear()
    for p in procs:
        _kill(p)


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


atexit.register(kill_all)

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*\n?(.*?)\n?\s*```\s*$", re.S)


def parse_values(text, keys):
    """Strip a Markdown fence, json.loads, and require exactly {"values": {<known key>: str|null}} (≤ 500 chars)."""
    if not isinstance(text, str):
        raise ValueError("no text")
    m = _FENCE.match(text)
    body = m.group(1) if m else text.strip()
    data = json.loads(body)
    if not isinstance(data, dict) or set(data) != {"values"} or not isinstance(data["values"], dict):
        raise ValueError("expected exactly {\"values\": {...}}")
    known = set(keys)
    out = {}
    for k, v in data["values"].items():
        if k not in known:
            raise ValueError(f"unknown key {k!r}")
        if v is not None and (not isinstance(v, str) or len(v) > MAX_VALUE_LEN):
            raise ValueError(f"bad value for {k!r}")
        out[k] = v.strip() if isinstance(v, str) and v.strip() else None
    return out


def build_prompt(goal, fields, page_excerpt):
    return json.dumps({
        "goal": goal,
        "fields": [{"key": f["key"], "label": f.get("label", ""), "placeholder": f.get("placeholder", "")}
                   for f in fields],
        "page_excerpt": (page_excerpt or "")[:EXCERPT_CHARS],
    }, ensure_ascii=False)


def local_keys(fields):
    """Short positional keys (f1, f2, ...) for local models, which rewrite long keys such as 'Search Wikipedia #1'."""
    return [f"f{i + 1}" for i in range(len(fields))]


def local_schema(fields):
    """An Ollama structured-output schema: exactly the local keys, each a string or null."""
    keys = local_keys(fields)
    return {"type": "object", "required": ["values"], "additionalProperties": False,
            "properties": {"values": {"type": "object", "required": keys, "additionalProperties": False,
                                      "properties": {k: {"type": ["string", "null"]} for k in keys}}}}


def build_local_prompt(goal, fields, page_excerpt):
    """The same JSON prompt as the Claude backend, but with short positional keys (f1, ...): local models rewrite
    long keys such as 'Search Wikipedia #1'. With Ollama's schema-constrained output (local_schema) the answer
    cannot echo the input. A plain-text variant was tried and made qwen3 copy goal spans instead of answering."""
    return json.dumps({
        "goal": goal,
        "fields": [{"key": k, "label": f.get("label", ""), "placeholder": f.get("placeholder", "")}
                   for k, f in zip(local_keys(fields), fields, strict=True)],
        "page_excerpt": (page_excerpt or "")[:EXCERPT_CHARS],
    }, ensure_ascii=False)


# ---- backends -------------------------------------------------------------------------------------------------
class NullBackend:
    """Privacy mode: nothing is ever sent to an LLM provider; misses hand back to the caller."""

    name = "none"
    model = ""

    def batch(self, goal, fields, page_excerpt, deadline):
        return BatchResult(values={}, backend="none", error="text backend disabled (text_backend='none')")


class ClaudeSubscriptionBackend:
    """`claude -p` on the subscription. mode="pool" (the default; measured faster) keeps one pre-started
    stream-json process ready; each request uses a fresh process and a replacement is started at once.
    claude.pool = false (JEV_BROWSE_CLAUDE_POOL=0) selects cold one-shot calls only."""

    name = "claude"

    def __init__(self, model=None, *, popen=subprocess.Popen, mode=None):
        self.model = model or config.text_model()
        self._popen = popen
        if mode is None:
            mode = "pool" if config.get("claude.pool") else "cold"
        self.mode = mode
        self._warm = None
        self._warm_lock = threading.Lock()

    def _base_argv(self):
        return ["claude", "-p", "--model", self.model, "--system-prompt", questions.TEXT_BATCH,
                "--strict-mcp-config", "--mcp-config", MCP_EMPTY, "--setting-sources", "",
                "--disable-slash-commands", "--tools", "", "--no-session-persistence", "--effort", "low"]

    def argv(self):
        return self._base_argv() + ["--max-turns", "1", "--output-format", "json"]

    def stream_argv(self):
        return self._base_argv() + ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]

    def _spawn(self, argv):
        proc = self._popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           cwd=private_cwd(), env=scrubbed_env(os.environ), start_new_session=True)
        with _procs_lock:
            _procs.add(proc)
        return proc

    def prewarm(self):
        """Start one stream-json process (no page data is sent until a request). No-op in cold mode."""
        if self.mode != "pool":
            return None
        with self._warm_lock:
            if self._warm is not None and self._warm.poll() is None:
                return self._warm
            try:
                self._warm = self._spawn(self.stream_argv())
            except OSError:
                self._warm = None
            return self._warm

    def _take_warm(self):
        with self._warm_lock:
            proc, self._warm = self._warm, None
        if proc is not None and proc.poll() is None:
            return proc
        return None

    def _stream_once(self, proc, prompt, keys, deadline):
        remaining = None if deadline is None else deadline - time.monotonic()
        line = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n"
        try:
            try:
                out, _err = proc.communicate(input=line, timeout=remaining)
            except subprocess.TimeoutExpired:
                _kill(proc)
                return None, {}, None, "text backend exceeded the deadline"
        finally:
            with _procs_lock:
                _procs.discard(proc)
        result = None
        for raw in (out or "").splitlines():
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event.get("type") == "result":
                result = event
        if result is None:
            return None, {}, None, f"claude stream ended without a result (exit {proc.returncode})"
        return self._parse_result(result, keys)

    def _parse_result(self, result, keys):
        usage = result.get("usage") or {}
        tokens = {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                  "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                  "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0)}
        cost = result.get("total_cost_usd")
        if result.get("is_error"):
            return None, tokens, cost, f"claude CLI error: {str(result.get('result') or result.get('subtype'))[:200]}"
        try:
            return parse_values(result.get("result"), keys), tokens, cost, None
        except (ValueError, TypeError) as exc:
            return None, tokens, cost, f"invalid JSON from the text backend: {exc}"

    def _once(self, prompt, keys, deadline):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return None, {}, None, "deadline reached before the text backend call"
        try:
            proc = self._popen(self.argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, cwd=private_cwd(), env=scrubbed_env(os.environ), start_new_session=True)
        except FileNotFoundError:
            return None, {}, None, "claude CLI not found on PATH"
        except OSError as exc:
            return None, {}, None, f"claude CLI failed to start: {exc}"
        with _procs_lock:
            _procs.add(proc)
        try:
            try:
                out, _err = proc.communicate(input=prompt, timeout=remaining)
            except subprocess.TimeoutExpired:
                _kill(proc)
                return None, {}, None, "text backend exceeded the deadline"
        finally:
            with _procs_lock:
                _procs.discard(proc)
        try:
            result = json.loads(out)
        except (ValueError, TypeError):
            return None, {}, None, f"claude CLI returned no JSON result (exit {proc.returncode})"
        return self._parse_result(result, keys)

    def batch(self, goal, fields, page_excerpt, deadline):
        started = time.perf_counter()
        keys = [f["key"] for f in fields]
        prompt = build_prompt(goal, fields, page_excerpt)
        total = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        cost_sum = 0.0
        error = None
        for attempt in range(2):  # retry once in a fresh process, then unavailable
            warm = self._take_warm() if attempt == 0 else None
            if warm is not None:
                self.prewarm()  # refill for the next request
                values, tokens, cost, error = self._stream_once(warm, prompt, keys, deadline)
            else:
                values, tokens, cost, error = self._once(prompt, keys, deadline)
            for k in total:
                total[k] += tokens.get(k, 0) or 0
            cost_sum += cost or 0.0
            if values is not None:
                return BatchResult(values=values, latency_ms=round((time.perf_counter() - started) * 1000),
                                   tokens=total, model=self.model, backend=self.name, cost_usd=cost_sum)
            if error and ("not found" in error or "deadline" in error or "failed to start" in error):
                break
        return BatchResult(values={}, latency_ms=round((time.perf_counter() - started) * 1000), tokens=total,
                           model=self.model, backend=self.name, cost_usd=cost_sum, error=error)


class CodexSubscriptionBackend:
    """`codex exec` on the ChatGPT subscription, cold one-shot (UNMEASURED: built with mocked tests while the author's
    Codex plan was rate-limited; the only live number is a 3.7-5.9 s cold start). Read-only sandbox, ephemeral
    session, no user config (so no MCP servers or hooks), a private empty working directory, the page text on stdin
    only, and schema-constrained output with short keys. OPENAI_API_KEY and friends are removed from its environment
    so it can only use the stored subscription login."""

    name = "codex"

    def __init__(self, model=None, *, popen=subprocess.Popen, reasoning_effort=None):
        self.model = model or config.get("codex.model")
        self.effort = reasoning_effort or config.get("codex.reasoning_effort")
        self._popen = popen

    def __repr__(self):
        return f"CodexSubscriptionBackend(model={self.model!r})"

    def prewarm(self):
        return None

    def argv(self, cwd, schema_path, out_path):
        return ["codex", "exec", "-m", self.model, "--sandbox", "read-only", "--ephemeral", "--ignore-user-config",
                "--ignore-rules", "--skip-git-repo-check", "--color", "never", "--json",
                "-c", f'model_reasoning_effort="{self.effort}"', "-C", cwd,
                "--output-schema", schema_path, "-o", out_path, "-"]

    @staticmethod
    def _usage(stdout):
        tokens = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        for raw in (stdout or "").splitlines():
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            usage = event.get("usage") if isinstance(event, dict) and event.get("type") == "turn.completed" else None
            if isinstance(usage, dict):
                tokens["input_tokens"] += int(usage.get("input_tokens") or 0)
                tokens["cache_read_input_tokens"] += int(usage.get("cached_input_tokens") or 0)
                tokens["output_tokens"] += int(usage.get("output_tokens") or 0)
        return tokens

    def _once(self, goal, fields, page_excerpt, deadline):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return None, {}, "deadline reached before the text backend call"
        cwd = private_cwd()
        tag = f"{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns()}"
        schema_path, out_path = os.path.join(cwd, f"codex-{tag}.schema.json"), os.path.join(cwd, f"codex-{tag}.out")
        try:
            fd = os.open(schema_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(local_schema(fields), f)
            prompt = questions.TEXT_BATCH_LOCAL + "\n\n" + build_local_prompt(goal, fields, page_excerpt)
            try:
                proc = self._popen(self.argv(cwd, schema_path, out_path), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, cwd=cwd, env=scrubbed_env(os.environ),
                                   start_new_session=True)
            except FileNotFoundError:
                return None, {}, "codex CLI not found on PATH"
            except OSError as exc:
                return None, {}, f"codex CLI failed to start: {exc}"
            with _procs_lock:
                _procs.add(proc)
            try:
                try:
                    out, _err = proc.communicate(input=prompt, timeout=remaining)
                except subprocess.TimeoutExpired:
                    _kill(proc)
                    return None, {}, "text backend exceeded the deadline"
            finally:
                with _procs_lock:
                    _procs.discard(proc)
            tokens = self._usage(out)
            try:
                with open(out_path) as f:
                    text = f.read()
            except OSError:
                return None, tokens, f"codex returned no answer (exit {proc.returncode})"
            try:
                short = parse_values(text, local_keys(fields))
            except ValueError as exc:
                return None, tokens, f"invalid output from the codex backend: {exc}"
            return {f["key"]: short.get(k) for k, f in zip(local_keys(fields), fields, strict=True)}, tokens, None
        finally:
            for path in (schema_path, out_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def batch(self, goal, fields, page_excerpt, deadline):
        started = time.perf_counter()
        total = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        error = None
        for _attempt in range(2):  # retry once in a fresh process
            values, tokens, error = self._once(goal, fields, page_excerpt, deadline)
            for k in total:
                total[k] += tokens.get(k, 0) or 0
            if values is not None:
                return BatchResult(values=values, latency_ms=round((time.perf_counter() - started) * 1000),
                                   tokens=total, model=self.model, backend=self.name)
            if "not found" in error or "deadline" in error or "failed to start" in error:
                break
        return BatchResult(values={}, latency_ms=round((time.perf_counter() - started) * 1000), tokens=total,
                           model=self.model, backend=self.name, error=error)


class _Unreachable(Exception):
    pass


def _post_json(base_url, path, body, headers, deadline, connect_timeout):
    """POST JSON with a short connect timeout. Errors never carry the host (it is private configuration)."""
    import http.client
    from urllib.parse import urlparse

    u = urlparse(base_url)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.hostname, u.port, timeout=connect_timeout)
    try:
        try:
            conn.connect()
        except OSError:
            raise _Unreachable() from None
        remaining = 60.0 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        conn.sock.settimeout(min(60.0, remaining))
        full_path = (u.path.rstrip("/") + path) or path
        conn.request("POST", full_path, body=json.dumps(body), headers={"Content-Type": "application/json", **headers})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


class _HTTPBackend:
    """Shared batch logic for HTTP text backends: one retry on invalid output; nothing typed without validation."""

    name = "http"
    _connect_timeout = 1.0

    def _once(self, goal, fields, page_excerpt, deadline):
        raise NotImplementedError

    def prewarm(self):
        return None

    def batch(self, goal, fields, page_excerpt, deadline):
        started = time.perf_counter()
        keys = [f["key"] for f in fields]
        tokens = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        error, kind = None, None
        for _attempt in range(2):
            try:
                status, raw = self._once(goal, fields, page_excerpt, deadline)
            except _Unreachable:
                error, kind = f"{self.name} backend unreachable", "unreachable"
                break
            except (TimeoutError, OSError):
                error, kind = f"{self.name} backend timed out", "timeout"
                break
            if status != 200:
                error, kind = f"{self.name} backend returned HTTP {status}", "http error"
                break
            try:
                text, usage = self._content(json.loads(raw))
                for k, v in usage.items():
                    tokens[k] += v
                try:  # short positional keys (f1, ...), mapped back to the real field keys
                    short = parse_values(text, local_keys(fields))
                    values = {f["key"]: short.get(k) for k, f in zip(local_keys(fields), fields, strict=True)}
                except ValueError:  # a model that answered with the real keys is accepted too
                    values = parse_values(text, keys)
                return BatchResult(values=values, latency_ms=round((time.perf_counter() - started) * 1000),
                                   tokens=tokens, model=self.model, backend=self.name, cost_usd=self._cost(tokens))
            except (ValueError, TypeError, KeyError, AttributeError):
                error, kind = f"invalid output from the {self.name} backend", "invalid output"
        return BatchResult(values={}, latency_ms=round((time.perf_counter() - started) * 1000), tokens=tokens,
                           model=self.model, backend=self.name, cost_usd=self._cost(tokens), error=error,
                           error_kind=kind)

    def _cost(self, tokens):
        return None


class OllamaBackend(_HTTPBackend):
    """A local model on an Ollama server (native /api/chat). The URL is private configuration
    (JEV_BROWSE_OLLAMA_URL, normally in the harness agent-workspace .env) and never leaves this process."""

    name = "ollama"

    def __init__(self, base_url, model, num_gpu=None, *, keep_alive="30m", think="auto"):
        self._base_url = base_url
        self.model = model
        self.num_gpu = num_gpu  # None = Ollama decides; 0 = CPU only
        self.keep_alive = keep_alive
        self.think = think      # auto | on | off | low | medium | high

    def __repr__(self):
        return f"OllamaBackend(model={self.model!r}, num_gpu={self.num_gpu!r})"

    def request_body(self, goal, fields, page_excerpt):
        body = {"model": self.model, "stream": False, "format": local_schema(fields), "keep_alive": self.keep_alive,
                "options": {"temperature": 0, "num_predict": 256},
                "messages": [{"role": "system", "content": questions.TEXT_BATCH_LOCAL},
                             {"role": "user", "content": build_local_prompt(goal, fields, page_excerpt)}]}
        if self.num_gpu is not None:
            body["options"]["num_gpu"] = self.num_gpu
        if self.think == "auto":
            family = self.model.split(":")[0].lower()
            if family.startswith("qwen3"):
                body["think"] = False
            elif family.startswith("gpt-oss"):
                body["think"] = "low"
        else:
            body["think"] = {"on": True, "off": False}.get(self.think, self.think)
        return body

    def _once(self, goal, fields, page_excerpt, deadline):
        return _post_json(self._base_url, "/api/chat", self.request_body(goal, fields, page_excerpt), {}, deadline,
                          self._connect_timeout)

    def _content(self, data):
        usage = {"input_tokens": int(data.get("prompt_eval_count") or 0),
                 "output_tokens": int(data.get("eval_count") or 0)}
        return data["message"]["content"], usage

    def _cost(self, tokens):
        return 0.0  # local inference: no per-token list price


class OpenAICompatBackend(_HTTPBackend):
    """An OpenAI-compatible chat-completions endpoint (OpenRouter, Groq, a local server). Off by default: it needs
    JEV_BROWSE_OPENAI_BASE_URL, JEV_BROWSE_OPENAI_API_KEY, and JEV_BROWSE_OPENAI_MODEL."""

    name = "openai"

    def __init__(self, base_url, api_key, model):
        self._base_url = base_url
        self._key = api_key
        self.model = model

    def __repr__(self):
        return f"OpenAICompatBackend(model={self.model!r})"

    def _once(self, goal, fields, page_excerpt, deadline):
        body = {"model": self.model, "temperature": 0, "max_tokens": 256, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": questions.TEXT_BATCH_LOCAL},
                             {"role": "user", "content": build_local_prompt(goal, fields, page_excerpt)}]}
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        return _post_json(self._base_url, "/chat/completions", body, headers, deadline, self._connect_timeout)

    def _content(self, data):
        u = data.get("usage") or {}
        usage = {"input_tokens": int(u.get("prompt_tokens") or 0), "output_tokens": int(u.get("completion_tokens") or 0)}
        return data["choices"][0]["message"]["content"], usage


# Known-answer canary: a backend that answers valid JSON can still be wrong (for example a misconfigured server).
# A local/OpenAI-compatible backend must get these right, or it is marked unhealthy and the
# Claude backend answers instead. Every browser-harness script is a new process, so the verdict is also kept in a
# small file under the harness tmp dir (healthy for 10 min, unhealthy for 2 min) and reused by later processes.
CANARIES = [
    ("Find hotels in Lima", "Destination", "Lima"),
    ("Open the Wikipedia article about the capital city of France", "Search Wikipedia", "Paris"),
    ("Find one-way flights from Zurich to Dakar", "Where to?", "Dakar"),
]
CANARIES_BY_GOAL = {goal: answer for goal, _label, answer in CANARIES}
CANARY_TIMEOUT = 20.0
_canary_cache = {}
_canary_lock = threading.Lock()
_canary_threads = {}


CANARY_CACHE_FILE = "jev-browse-canary.json"


def _canary_key(primary):
    """What can change a backend's answers: kind, server, model, and the options that affect decoding."""
    return (primary.name, getattr(primary, "_base_url", ""), primary.model, getattr(primary, "num_gpu", None),
            getattr(primary, "think", None))


def canary_cache_key(primary):
    """A hash of backend kind, base URL, model, options, and the canary set itself: the file never holds the host,
    the model name, or any credential."""
    raw = json.dumps([*_canary_key(primary), CANARIES, questions.TEXT_BATCH_LOCAL])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _canary_cache_path():
    return harness_api.tmp_dir() / CANARY_CACHE_FILE


def _read_canary_entries():
    """{key: entry} from the cache file; anything unreadable, malformed, or writable by others counts as empty."""
    try:
        path = _canary_cache_path()
        if path.stat().st_mode & 0o077:
            return {}
        entries = json.loads(path.read_text())["entries"]
        return entries if isinstance(entries, dict) else {}
    except Exception:
        return {}


def _fresh(entry, now):
    if not isinstance(entry, dict) or not isinstance(entry.get("healthy"), bool):
        return False
    at = entry.get("checked_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return False
    ttl = config.get("canary.ttl_healthy_s") if entry["healthy"] else config.get("canary.ttl_unhealthy_s")
    return 0 <= now - at < ttl


def load_canary_verdict(primary):
    entry = _read_canary_entries().get(canary_cache_key(primary))
    if not _fresh(entry, time.time()):
        return None
    ms = entry.get("latency_ms")
    return {"ok": entry["healthy"], "why": None if entry["healthy"] else (entry.get("why") or "canary failed"),
            "wrong": [], "ms": ms if isinstance(ms, int) and not isinstance(ms, bool) else None, "cached": True}


def save_canary_verdict(primary, verdict):
    """Atomic (temp file + rename, mode 600); expired entries are dropped. Failure to write is not an error."""
    now = time.time()
    entries = {k: v for k, v in _read_canary_entries().items() if _fresh(v, now)}
    entries[canary_cache_key(primary)] = {"healthy": bool(verdict["ok"]), "why": verdict.get("why"),
                                          "checked_at": now, "latency_ms": verdict.get("ms")}
    tmp = None
    try:
        path = _canary_cache_path()
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"entries": entries}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def run_canary(primary):
    """Ask every canary in parallel; {"ok", "why", "wrong", "ms"}. Nothing about the page is sent."""
    started = time.perf_counter()
    results = {}

    def one(goal, label, answer):
        r = primary.batch(goal, [{"key": label, "label": label, "placeholder": ""}], "",
                          time.monotonic() + CANARY_TIMEOUT)
        results[goal] = (r.error_kind, r.values.get(label))

    threads = [threading.Thread(target=one, args=c, daemon=True) for c in CANARIES]
    for t in threads:
        t.start()
    for t in threads:
        t.join(CANARY_TIMEOUT + 2)
    wrong = [g for g, _label, a in CANARIES if results.get(g, ("timeout", None))[1] is None
             or fold(results[g][1]) != fold(a)]
    kinds = {results.get(g, ("timeout", None))[0] for g in wrong}
    why = "unreachable" if kinds and kinds <= {"unreachable", "timeout"} else "canary failed"
    return {"ok": not wrong, "why": None if not wrong else why, "wrong": wrong,
            "ms": round((time.perf_counter() - started) * 1000)}


def canary_health(primary, wait=True):
    key = _canary_key(primary)
    with _canary_lock:
        if key in _canary_cache:
            return _canary_cache[key]
        enabled = bool(CANARIES) and config.get("canary.enabled")
        cached = load_canary_verdict(primary) if enabled else None
        if cached is not None:
            _canary_cache[key] = cached
            return cached
        thread = _canary_threads.get(key)
        if thread is None:
            def work():
                if enabled:
                    verdict = run_canary(primary)
                    save_canary_verdict(primary, verdict)
                else:
                    verdict = {"ok": True, "why": None, "wrong": [], "ms": 0}
                with _canary_lock:
                    _canary_cache[key] = verdict
            thread = threading.Thread(target=work, daemon=True, name="jev-browse-canary")
            _canary_threads[key] = thread
            thread.start()
    if not wait:
        return None
    thread.join(CANARY_TIMEOUT + 5)
    with _canary_lock:
        _canary_threads.pop(key, None)
        return _canary_cache.get(key, {"ok": False, "why": "unreachable", "wrong": [], "ms": None})


class FallbackBackend:
    """Use `primary` once its known-answer canary passed; if the canary failed, or the primary is unreachable, times
    out, errors, or returns invalid output twice, ask `fallback` (the Claude subscription backend) and record the
    fallback on the result."""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.name = primary.name
        self.model = primary.model

    def prewarm(self):
        # Start the canary in the background (no page data exists yet). The local model stays loaded via keep_alive;
        # never spawn the fallback CLI speculatively.
        canary_health(self.primary, wait=False)

    def _fallback(self, goal, fields, page_excerpt, deadline, why, spent_ms=0):
        if isinstance(self.fallback, NullBackend):  # text.fallback = none: hand the miss back, send nothing
            return BatchResult(values={}, latency_ms=spent_ms or 0, model=self.primary.model, backend=self.primary.name,
                               error=f"{self.primary.name} backend {why}; no fallback (text.fallback = none)",
                               error_kind=why)
        fb = self.fallback.batch(goal, fields, page_excerpt, deadline)
        fb.fallback = {"from": self.primary.name, "why": why}
        fb.latency_ms = (fb.latency_ms or 0) + (spent_ms or 0)
        return fb

    def batch(self, goal, fields, page_excerpt, deadline):
        health = canary_health(self.primary)
        if not health["ok"]:
            fb = self._fallback(goal, fields, page_excerpt, deadline, health["why"] or "canary failed")
            fb.canary = health
            return fb
        r = self.primary.batch(goal, fields, page_excerpt, deadline)
        r.canary = health
        if not r.error:
            return r
        fb = self._fallback(goal, fields, page_excerpt, deadline, r.error_kind or "error", r.latency_ms)
        fb.canary = health
        return fb


def _cli_backend(name):
    if name == "claude":
        return ClaudeSubscriptionBackend()
    if name == "codex":
        return CodexSubscriptionBackend()
    return NullBackend()


def make_backend(name=None):
    """The configured text backend (text.backend; see docs/configuration.md). Local and OpenAI-compatible backends
    are wrapped with the canary and the text.fallback backend."""
    name = config.text_backend_name(name)
    if name in ("claude", "codex", "none"):
        return _cli_backend(name)
    if name == "ollama":
        url = config.get("ollama.url")
        if not url:
            raise ValueError("text backend 'ollama' needs ollama.url (JEV_BROWSE_OLLAMA_URL)")
        primary = OllamaBackend(url, config.get("ollama.model"), num_gpu=config.get("ollama.num_gpu"),
                                keep_alive=config.get("ollama.keep_alive"), think=config.get("ollama.think"))
    elif name == "openai":
        base, model = config.get("openai.base_url"), config.get("openai.model")
        if not base or not model:
            raise ValueError("text backend 'openai' needs openai.base_url and openai.model "
                             "(JEV_BROWSE_OPENAI_BASE_URL, JEV_BROWSE_OPENAI_MODEL)")
        primary = OpenAICompatBackend(base, os.environ.get(config.get("openai.api_key_env")), model)
    else:
        raise ValueError(f"unknown text_backend {name!r}; use 'claude', 'codex', 'ollama', 'openai', or 'none'")
    return FallbackBackend(primary, _cli_backend(config.text_fallback_name()))


# ---- grounding --------------------------------------------------------------------------------------
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_DATEISH = re.compile(r"^\s*\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}\s*$")
_NATIONAL_ID = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b|\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b|\b\d{3}-\d{2}-\d{4}\b")


def _luhn(digits):
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def pii_shaped(value):
    v = value.strip()
    if _EMAIL.search(v):
        return "email"
    digits = re.sub(r"\D", "", v)
    if 13 <= len(digits) <= 19 and re.fullmatch(r"[\d\s\-]+", v) and _luhn(digits):
        return "card"
    if _NATIONAL_ID.search(v):
        return "national_id"
    if len(digits) >= 10 and re.fullmatch(r"\+?[\d\s\-().]+", v) and not _DATEISH.match(v):
        return "phone"
    return None


def is_personal(field_):
    return bool(field_.get("personal")) or config.is_personal_field(field_)


def grounded(value, goal, field_, client=None, deadline=None):
    """True when an LLM value may be typed into `field_`."""
    if not value or is_personal(field_) or field_.get("sensitive"):
        return False
    shape = pii_shaped(value)
    if shape:
        return value.strip() in goal
    goal_words = set(words(goal))
    value_words = words(value)
    if value_words and all(w in goal_words for w in value_words):
        return True
    if client is None:
        return False
    from .typesafe import validate_noul

    q = {"type": "noul", "instructions": {"goal": goal, "value": value, "field": field_.get("label", ""),
                                          "question": questions.GROUNDING}}
    try:
        answer = client.ask({"field": field_.get("label", "")}, {"grounded": q}, deadline=deadline)
        return validate_noul(answer.answers.get("grounded", {})) >= config.GROUNDING_NOUL
    except Exception:
        return False


def normalise_date_value(value, field_, today=None):
    """For a date-typed field, re-parse the LLM's date and render it in the observed format; ambiguous → None."""
    if not is_date_field(field_):
        return value
    renderer, known = field_date_format(field_)
    m = re.fullmatch(r"\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\s*", value)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= 12 and b <= 12 and not known:
            return None  # day/month order ambiguous and the field format is unknown
    dates = parse_dates(value, today or dt.date.today())
    if not dates and m and renderer:
        return value.strip()
    if not dates:
        return None
    d = dates[0]
    return renderer(d) if renderer else d.isoformat()


def form_signature(page):
    fields = [(f.get("label", ""), f.get("role", ""), f.get("placeholder", ""))
              for f in page.get("fields") or [] if f.get("visible")]
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()[:16]


# ---- the service used by fast_run -------------------------------------------------------------------------------
class _Batch:
    def __init__(self, fields, speculative):
        self.fields = fields
        self.keys = {f["key"] for f in fields}
        self.speculative = speculative
        self.started = time.monotonic()
        self.finished = None
        self.done = threading.Event()
        self.result = None
        self.used = False


class TextService:
    def __init__(self, backend, typesafe_client=None, speculate_text="after_miss"):
        if speculate_text not in {"after_miss", "eager", "off"}:
            raise ValueError("speculate_text must be 'after_miss', 'eager', or 'off'")
        self.backend = backend
        self.client = typesafe_client
        self.mode = speculate_text
        self.cache = {}  # (goal, signature) -> {key: value|None}
        self.batches = {}  # (goal, signature) -> _Batch
        self.stats = {"llm_calls": 0, "llm_ms_total": 0, "llm_ms_overlapped": 0, "llm_speculative_unused": 0,
                      "llm_input_tokens": 0, "llm_output_tokens": 0, "llm_cache_read_tokens": 0,
                      "llm_cache_write_tokens": 0, "llm_cost_usd": 0.0}
        self.calls = []  # per-call records for the trace

    # --- triggers ---
    def _eligible(self, page, nodes=None):
        out = []
        for f in page.get("fields") or []:
            if not f.get("visible") or f.get("sensitive") or f.get("readonly") or is_personal(f):
                continue
            if nodes is not None and f["node"] not in nodes:
                continue
            if f.get("key") is None:
                continue
            out.append(f)
        return out

    def on_decision(self, goal, page, decision, *, deadline=None):
        """After-miss trigger: batch every non-personal missed field on this snapshot in the background."""
        if self.mode != "after_miss" or isinstance(self.backend, NullBackend) or not decision.miss_fields:
            return None
        nodes = {m[1] for m in decision.miss_fields}
        return self._start(goal, page, self._eligible(page, nodes), speculative=True, deadline=deadline)

    def on_observe(self, goal, page, *, deadline=None):
        """Eager trigger (opt-in): any page with empty editable fields."""
        if self.mode != "eager" or isinstance(self.backend, NullBackend):
            return None
        empty = [f for f in self._eligible(page) if not (f.get("value") or "").strip()]
        return self._start(goal, page, empty, speculative=True, deadline=deadline) if empty else None

    def _start(self, goal, page, fields, *, speculative, deadline):
        if not fields:
            return None
        key = (goal, form_signature(page))
        if key in self.cache or key in self.batches:
            return self.batches.get(key)
        batch = _Batch(fields, speculative)
        self.batches[key] = batch
        excerpt = (page.get("text") or "")[:EXCERPT_CHARS]

        def run():
            try:
                batch.result = self.backend.batch(goal, fields, excerpt, deadline)
            except Exception as exc:  # never raise from the thread
                batch.result = BatchResult(values={}, backend=getattr(self.backend, "name", ""), error=str(exc))
            batch.finished = time.monotonic()
            batch.done.set()

        threading.Thread(target=run, daemon=True, name="jev-browse-text").start()
        return batch

    def _account(self, batch):
        r = batch.result
        self.stats["llm_calls"] += 1
        self.stats["llm_ms_total"] += r.latency_ms
        self.stats["llm_input_tokens"] += (r.tokens or {}).get("input_tokens", 0)
        self.stats["llm_output_tokens"] += (r.tokens or {}).get("output_tokens", 0)
        self.stats["llm_cache_read_tokens"] += (r.tokens or {}).get("cache_read_input_tokens", 0)
        self.stats["llm_cache_write_tokens"] += (r.tokens or {}).get("cache_creation_input_tokens", 0)
        self.stats["llm_cost_usd"] = round(self.stats["llm_cost_usd"] + (r.cost_usd or 0.0), 6)
        self.calls.append({"backend": r.backend, "model": r.model, "latency_ms": r.latency_ms, "tokens": r.tokens,
                           "cost_usd": r.cost_usd, "speculative": batch.speculative, "error": r.error,
                           "fallback": r.fallback})

    # --- resolution on the main thread ---
    def value_for(self, goal, page, field_, deadline):
        """(value | None, source, meta). Source ∈ {llm, llm_cache, none}. Grounding and dates validated here."""
        meta = {"llm_ms": 0}
        if is_personal(field_) or field_.get("sensitive"):
            return None, "none", {**meta, "why": "personal or sensitive field never uses the LLM"}
        if isinstance(self.backend, NullBackend):
            return None, "none", {**meta, "why": "text_backend='none'"}
        key = (goal, form_signature(page))
        source = "llm"
        if key in self.cache and field_["key"] in self.cache[key]:
            raw = self.cache[key][field_["key"]]
            source = "llm_cache"
        else:
            batch = self.batches.get(key)
            if batch is None or field_["key"] not in batch.keys:
                fields = self._eligible(page)
                if field_["key"] not in {f["key"] for f in fields}:
                    fields.append(field_)
                batch = self._start(goal, page, fields, speculative=False, deadline=deadline) \
                    if key not in self.batches else None
                if batch is None:  # a batch for this form exists but lacks this field: run one just for it
                    batch = _Batch([field_], False)
                    batch.result = self.backend.batch(goal, [field_], (page.get("text") or "")[:EXCERPT_CHARS],
                                                      deadline)
                    batch.finished = time.monotonic()
                    batch.done.set()
            asked = time.monotonic()
            timeout = None if deadline is None else max(0.0, deadline - asked)
            if not batch.done.wait(timeout):
                return None, "none", {**meta, "why": "text backend did not answer before the deadline"}
            if not batch.used:
                batch.used = True
                self._account(batch)
                if batch.speculative:
                    self.stats["llm_ms_overlapped"] += max(0, round((min(asked, batch.finished) - batch.started)
                                                                    * 1000))
            meta["llm_ms"] = batch.result.latency_ms
            meta["llm"] = {"backend": batch.result.backend, "model": batch.result.model,
                           "fallback": batch.result.fallback}
            if batch.result.canary is not None:
                meta["llm"]["canary"] = {k: batch.result.canary.get(k) for k in ("ok", "why", "ms", "cached")}
            if batch.result.error:
                self.cache.setdefault(key, {})
                return None, "none", {**meta, "why": batch.result.error}
            self.cache.setdefault(key, {}).update(batch.result.values)
            raw = batch.result.values.get(field_["key"])
        if raw is None:
            return None, "none", {**meta, "why": "backend returned null"}
        value = normalise_date_value(raw, field_)
        if value is None:
            return None, "none", {**meta, "why": "ambiguous or unparseable date"}
        if not grounded(value, goal, field_, self.client, deadline):
            return None, "none", {**meta, "why": "failed grounding"}
        return value, source, meta

    def close(self):
        for batch in self.batches.values():
            if not batch.used:
                if batch.done.is_set() and batch.result is not None:
                    self._account(batch)
                    self.stats["llm_speculative_unused"] += 1
                batch.used = True
        kill_all()


def fold_equal(a, b):
    return fold(a) == fold(b)
