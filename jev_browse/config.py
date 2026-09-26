"""Settings: one registry (SETTINGS), resolved as environment > config file > built-in default.

The environment includes the browser-harness agent-workspace `.env`, which the harness loads into every script's
process. The optional config file is TOML at $JEV_BROWSE_CONFIG, else $XDG_CONFIG_HOME/jev-browse/config.toml, else
~/.config/jev-browse/config.toml. Secrets are never read from the file: API keys stay in the environment.
Also here: the fixed thresholds, commit verbs, sensitive and personal-data patterns, and the host hook.
"""

import fnmatch
import os
import re
import shutil
import textwrap
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlsplit

from .textnorm import fold

# ---- the settings registry --------------------------------------------------------------------------------------
TRUE = {"1", "true", "yes", "on"}
FALSE = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Setting:
    """One registered setting: its TOML key, environment names, type, default, and docs line."""
    key: str                 # dotted TOML key: [section] name
    env: str                 # canonical environment variable
    kind: str                # str | int | bool | choice | list
    default: object
    doc: str
    choices: tuple = ()
    aliases: tuple = ()      # older environment names, still read
    private: bool = False    # printed as <set> by `doctor` (hosts are private configuration)
    minimum: int | None = None

    @property
    def envs(self):
        return (self.env, *self.aliases)


_S = [
    # text backend
    Setting("text.backend", "JEV_BROWSE_TEXT_BACKEND", "choice", "auto",
            "Where generated field values come from after a miss. auto = claude if the `claude` CLI is on PATH, "
            "else none.", ("auto", "claude", "codex", "ollama", "openai", "none")),
    Setting("text.fallback", "JEV_BROWSE_TEXT_FALLBACK", "choice", "auto",
            "Who answers when an ollama/openai backend fails its canary, is unreachable, or returns invalid output "
            "twice. auto = claude if the CLI is on PATH, else none (the miss hands back to you).",
            ("auto", "claude", "codex", "none")),
    Setting("text.speculate", "JEV_BROWSE_SPECULATE_TEXT", "choice", "after_miss",
            "When the text backend is asked: after a miss (default), eagerly on every page with fields, or never.",
            ("after_miss", "eager", "off")),
    Setting("claude.model", "JEV_BROWSE_CLAUDE_MODEL", "str", "haiku", "Model for `claude -p`.",
            aliases=("JEV_BROWSE_TEXT_MODEL",)),
    Setting("claude.pool", "JEV_BROWSE_CLAUDE_POOL", "bool", True,
            "Keep one pre-started `claude` process per run (nothing is sent to it until a miss).",
            aliases=("JEV_BROWSE_TEXT_POOL",)),
    Setting("codex.model", "JEV_BROWSE_CODEX_MODEL", "str", "gpt-5.6-luna", "Model for `codex exec -m`."),
    Setting("codex.reasoning_effort", "JEV_BROWSE_CODEX_REASONING_EFFORT", "choice", "low",
            "Codex `model_reasoning_effort`.", ("minimal", "low", "medium", "high")),
    Setting("ollama.url", "JEV_BROWSE_OLLAMA_URL", "str", None, "Ollama server base URL (required for ollama).",
            private=True),
    Setting("ollama.model", "JEV_BROWSE_OLLAMA_MODEL", "str", "qwen3:30b-a3b", "Ollama model tag."),
    Setting("ollama.num_gpu", "JEV_BROWSE_OLLAMA_NUM_GPU", "int", None,
            "Ollama `options.num_gpu`; 0 forces CPU. Unset = the server decides.", minimum=0),
    Setting("ollama.keep_alive", "JEV_BROWSE_OLLAMA_KEEP_ALIVE", "str", "30m",
            "How long Ollama keeps the model loaded after a request."),
    Setting("ollama.think", "JEV_BROWSE_OLLAMA_THINK", "choice", "auto",
            "Ollama `think`. auto = off for qwen3, low for gpt-oss, unset otherwise.",
            ("auto", "on", "off", "low", "medium", "high")),
    Setting("openai.base_url", "JEV_BROWSE_OPENAI_BASE_URL", "str", None,
            "OpenAI-compatible base URL, e.g. https://openrouter.ai/api/v1 (required for openai).", private=True),
    Setting("openai.model", "JEV_BROWSE_OPENAI_MODEL", "str", None, "Model name at that endpoint (required)."),
    Setting("openai.api_key_env", "JEV_BROWSE_OPENAI_API_KEY_ENV", "str", "JEV_BROWSE_OPENAI_API_KEY",
            "Name of the environment variable that holds the endpoint's API key (e.g. OPENROUTER_API_KEY)."),
    Setting("canary.enabled", "JEV_BROWSE_CANARY", "bool", True,
            "Known-answer check before trusting an ollama/openai backend."),
    Setting("canary.ttl_healthy_s", "JEV_BROWSE_CANARY_TTL_S", "int", 600,
            "Seconds a passed canary is reused across processes.", minimum=0),
    Setting("canary.ttl_unhealthy_s", "JEV_BROWSE_CANARY_TTL_UNHEALTHY_S", "int", 120,
            "Seconds a failed canary is reused across processes.", minimum=0),
    # Jev and run budgets
    Setting("jev.base_url", "JEV_BROWSE_JEV_BASE_URL", "str", "https://api.typesafe.ai",
            "SystemOne server base URL; /v1/systemone is appended. Supports HTTP(S), ports and path prefixes.",
            private=True),
    Setting("jev.auth", "JEV_BROWSE_JEV_AUTH", "choice", "bearer",
            "SystemOne authentication. none omits Authorization, even when a key is available.", ("bearer", "none")),
    Setting("jev.api_key_env", "JEV_BROWSE_JEV_API_KEY_ENV", "str", None,
            "Key environment variable name. Unset = TYPESAFE_API_KEY for the default endpoint, "
            "JEV_BROWSE_API_KEY for custom endpoints. Secrets stay in the environment."),
    Setting("jev.model", "JEV_BROWSE_JEV_MODEL", "str", "jev-latest", "TypeSafe model or alias for decisions.",
            aliases=("JEV_BROWSE_MODEL",)),
    Setting("run.max_actions", "JEV_BROWSE_MAX_ACTIONS", "int", 30, "fast_run default: max page actions.",
            minimum=1),
    Setting("run.max_requests", "JEV_BROWSE_MAX_REQUESTS", "int", 60, "fast_run default: max TypeSafe requests.",
            minimum=1),
    Setting("run.timeout_s", "JEV_BROWSE_TIMEOUT_S", "int", 90, "fast_run default: wall-clock budget (s).",
            minimum=1),
    Setting("run.quiet", "JEV_BROWSE_QUIET", "bool", False, "Do not print the JEV_BROWSE_RESULT line."),
    Setting("values.candidates", "JEV_BROWSE_VALUE_CANDIDATES", "bool", True,
            "Offer goal-derived value candidates to Jev before asking the text backend."),
    Setting("values.heads", "JEV_BROWSE_VALUE_HEADS", "int", 6,
            "Maximum number of fields that get a value question in one Jev request.", minimum=0),
    # safety
    Setting("safety.allowed_hosts", "JEV_BROWSE_ALLOWED_HOSTS", "list", None,
            "Only these hosts (exact, or *.suffix). Unset or empty = all hosts."),
    Setting("safety.commit_verbs", "JEV_BROWSE_COMMIT_VERBS", "list", None,
            "Replace the built-in commit-verb list (empty disables the verb check)."),
    Setting("safety.commit_verbs_extra", "JEV_BROWSE_COMMIT_VERBS_EXTRA", "list", None,
            "Add commit verbs to the list."),
    Setting("safety.sensitive_patterns_extra", "JEV_BROWSE_SENSITIVE_PATTERNS_EXTRA", "list", None,
            "Add label patterns that mark a field sensitive (never typed, read, or sent)."),
    # MCP server (`jev-browse mcp`)
    Setting("mcp.harness_command", "JEV_BROWSE_HARNESS_COMMAND", "str", "browser-harness",
            "The browser-harness command the MCP server runs each tool call through (a name on PATH or an "
            "absolute path)."),
    Setting("mcp.wait_s", "JEV_BROWSE_MCP_WAIT_S", "int", 45,
            "Seconds an MCP tool call waits before returning. A longer fast_run then returns its run_id for "
            "fast_run_status. Keep it below your MCP client's tool timeout (often 60 s).", minimum=1),
]
SETTINGS = {s.key: s for s in _S}

# Environment-only (never read from the config file): secrets and per-process switches.
ENV_ONLY = {
    "TYPESAFE_API_KEY": "Default TypeSafe API key. Keep it in the browser-harness agent-workspace .env.",
    "JEV_BROWSE_API_KEY": "Default bearer key for a custom SystemOne server (see jev.api_key_env).",
    "JEV_BROWSE_CONFIG": "Path of the config file.",
    "JEV_BROWSE_DISABLE": "1 = the harness helpers are stubs that raise.",
    "JEV_BROWSE_OWNER": "Tab-ownership tag; set one per parallel agent (default: the Claude Code session id; "
                        "`mcp-<random>` per `jev-browse mcp` server).",
    "JEV_BROWSE_OPENAI_API_KEY": "Default variable holding the openai backend's key (see openai.api_key_env).",
}


def all_env_names():
    """Every environment variable jev-browse reads: each setting's names (with aliases) plus ENV_ONLY."""
    return [e for s in _S for e in s.envs] + list(ENV_ONLY)


def _which(name):
    return shutil.which(name)


def config_path():
    """The config file: $JEV_BROWSE_CONFIG, else $XDG_CONFIG_HOME (or ~/.config)/jev-browse/config.toml."""
    if os.environ.get("JEV_BROWSE_CONFIG"):
        return Path(os.environ["JEV_BROWSE_CONFIG"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".config") / "jev-browse" / "config.toml"


def flatten(data, prefix=""):
    """Flatten nested TOML tables into dotted keys: {"run": {"timeout_s": 90}} -> {"run.timeout_s": 90}."""
    out = {}
    for k, v in data.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


_file_cache = {"stamp": None, "values": {}, "error": None}
_file_lock = threading.Lock()


def reset_cache():
    """Forget the parsed config file, so the next read picks up changes (and a changed $JEV_BROWSE_CONFIG)."""
    with _file_lock:
        _file_cache.update(stamp=None, values={}, error=None)


def _file_values():
    """{dotted key: raw value} from the config file, re-read when it changes. Unreadable = empty + an error."""
    path = config_path()
    try:
        st = path.stat()
        stamp = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (str(path), None, None)
    with _file_lock:
        if _file_cache["stamp"] == stamp:
            return _file_cache["values"], _file_cache["error"]
        values, error = {}, None
        if stamp[1] is not None:
            try:
                import tomllib

                values = flatten(tomllib.loads(path.read_text()))
            except Exception as exc:
                error = f"{path}: {type(exc).__name__}: {exc}"[:300]
        _file_cache.update(stamp=stamp, values=values, error=error)
        return values, error


def _parse(s, raw):
    """Raw env string or TOML value -> typed value; ValueError if it does not fit."""
    if raw is None:
        return None
    if s.kind == "list":
        items = raw if isinstance(raw, list) else str(raw).split(",")
        return [str(x).strip() for x in items if str(x).strip()]
    if isinstance(raw, str):
        raw = raw.strip()
    if s.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).lower() in TRUE:
            return True
        if str(raw).lower() in FALSE:
            return False
        raise ValueError(f"expected one of {sorted(TRUE | FALSE)}")
    if s.kind == "int":
        if isinstance(raw, bool):
            raise ValueError("expected an integer")
        if raw == "":
            return None
        value = int(raw)
        if s.minimum is not None and value < s.minimum:
            raise ValueError(f"must be >= {s.minimum}")
        return value
    if s.kind == "choice":
        value = str(raw).lower()
        if value not in s.choices:
            raise ValueError(f"expected one of {', '.join(s.choices)}")
        return value
    return str(raw) if raw != "" else None


def _lookup(key):
    """(value, source, error). source is env | file | default."""
    s = SETTINGS[key]
    for name in s.envs:
        if name in os.environ:
            try:
                return _parse(s, os.environ[name]), "env", None
            except ValueError as exc:
                return s.default, "default", f"{name}={os.environ[name]!r}: {exc}"
    values, _error = _file_values()
    if key in values:
        try:
            return _parse(s, values[key]), "file", None
        except (ValueError, TypeError) as exc:
            return s.default, "default", f"{key} in {config_path()}: {exc}"
    return s.default, "default", None


def get(key):
    """The effective value of setting `key`: environment, then the config file, then the default. An invalid
    value falls back to the default (and is reported by problems())."""
    return _lookup(key)[0]


def source(key):
    """Where `key`'s effective value came from: "env", "file", or "default"."""
    return _lookup(key)[1]


def problems():
    """[(key, message)] for invalid values, unknown file keys, and an unreadable config file."""
    out = []
    values, error = _file_values()
    if error:
        out.append(("config file", error))
    out += [(k, "unknown setting") for k in values if k not in SETTINGS]
    out += [(k, err) for k in SETTINGS if (err := _lookup(k)[2])]
    return out


def active():
    """Every setting with its effective value and source; private values are shown as <set>."""
    rows = []
    for key, s in SETTINGS.items():
        value, src, _err = _lookup(key)
        shown = "<set>" if s.private and value else value
        rows.append({"key": key, "env": s.env, "value": shown, "source": src})
    return rows

# --- thresholds; starting points, recalibrated from benchmark traces ---
VALUE_CONFIDENCE = 0.6
VALUE_IN_GOAL = 0.5
COMMIT_OK = 0.7
COMMIT_CONFIDENCE = 0.6
LOW_CONFIDENCE = 0.35
LOW_CONFIDENCE_STREAK = 3
FIND_EXISTS = 0.5
FIND_CONFIDENCE = 0.6
GROUNDING_NOUL = 0.6
CHECK_HOLDS = 0.5

# --- limits ---
MAX_OPTIONS = 254
MAX_VALUE_CANDIDATES = 40
MAX_CHECK_LINES = 253
SNAPSHOT_CAP = 2000
LONGEST_BUDGET = 24_000
TOTAL_BUDGET = 56_000
FRAME_AREA = 0.15
CANVAS_AREA = 0.40
UNNAMED_ICON_RATIO = 0.30
SHADOW_AREA = 0.15

COMMIT_VERBS = [
    "delete", "remove", "discard", "send", "pay", "purchase", "buy", "order", "place order", "checkout",
    "confirm", "publish", "post", "reply", "share", "transfer", "donate", "book", "reserve", "archive",
    "unsubscribe", "cancel subscription", "sign out", "log out",
    "excluir", "apagar", "remover", "enviar", "pagar", "comprar", "confirmar", "publicar", "responder",
    "compartilhar", "transferir", "reservar", "arquivar", "sair",
]
GENERIC_CONFIRMATIONS = ["yes", "ok", "continue", "proceed", "submit", "done", "sim", "continuar", "prosseguir"]

SENSITIVE_PATTERNS = [
    "card number", "credit card", "cvv", "cvc", "csc", "security code", "ssn", "social security",
    "national id", "iban", "account number", "routing number", "cpf", "cnpj", "rg", "passport", "tax id",
    "passcode", "otp", "one-time", "one time", "pin",
]
SENSITIVE_AUTOCOMPLETE_PREFIXES = ["cc-"]
SENSITIVE_AUTOCOMPLETE = ["one-time-code", "current-password", "new-password"]

PERSONAL_AUTOCOMPLETE = ["name", "given-name", "family-name", "additional-name", "honorific-prefix", "nickname",
                         "email", "street-address", "postal-code", "username"]
PERSONAL_AUTOCOMPLETE_PREFIXES = ["tel", "address-line", "bday", "address-level"]
PERSONAL_LABELS = ["name", "first name", "last name", "full name", "given name", "family name", "surname", "email",
                   "e-mail", "phone", "telephone", "mobile", "street address", "address", "postal code", "zip",
                   "zip code", "postcode", "birthday", "date of birth", "birth date", "nome", "sobrenome",
                   "telefone", "celular", "endereco", "cep", "data de nascimento"]


def _folded(key):
    items = get(key)
    return None if items is None else [fold(x) for x in items]


def commit_verbs():
    """The active commit-verb list: safety.commit_verbs replaces (empty disables), _extra extends."""
    base = _folded("safety.commit_verbs")
    verbs = [fold(v) for v in COMMIT_VERBS] if base is None else base
    return verbs + (_folded("safety.commit_verbs_extra") or [])


def generic_confirmations():
    """The built-in generic confirmation labels ("yes", "ok", "continue", ...), case- and diacritic-folded."""
    return [fold(v) for v in GENERIC_CONFIRMATIONS]


def sensitive_patterns():
    """Folded label patterns of sensitive fields: the built-ins plus safety.sensitive_patterns_extra."""
    return [fold(p) for p in SENSITIVE_PATTERNS] + (_folded("safety.sensitive_patterns_extra") or [])


def value_heads():
    """Setting values.heads: the most fields that get a value question in one Jev request."""
    return get("values.heads")


def value_candidates_enabled():
    """Setting values.candidates: whether goal-derived value candidates are offered to Jev."""
    return get("values.candidates")


def _auto_cli(value):
    return ("claude" if _which("claude") else "none") if value == "auto" else value


def text_backend_name(explicit=None):
    """The text backend to use: `explicit` if given, else text.backend with "auto" resolved to "claude"
    (when its CLI is on PATH) or "none"."""
    return explicit or _auto_cli(get("text.backend"))


def text_fallback_name():
    """The fallback for a failing local or OpenAI-compatible backend (text.fallback, "auto" resolved)."""
    return _auto_cli(get("text.fallback"))


def text_model():
    """The model the claude text backend uses (claude.model)."""
    return get("claude.model")


def disabled():
    """True if JEV_BROWSE_DISABLE is set to a true value (the installed harness block then defines stubs that raise)."""
    return os.environ.get("JEV_BROWSE_DISABLE", "").strip().lower() in TRUE


def owner_tag():
    """This session's tab-owner tag: $JEV_BROWSE_OWNER, else the Claude Code session id, else "unknown"."""
    return os.environ.get("JEV_BROWSE_OWNER") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown"


def allowed_hosts():
    """The lower-cased safety.allowed_hosts list, or None when every host is allowed."""
    hosts = [h.lower() for h in get("safety.allowed_hosts") or []]
    return hosts or None


def host_allowed(url):
    """Allow-all by default. With JEV_BROWSE_ALLOWED_HOSTS, only exact hosts or `*.suffix` subdomains pass."""
    patterns = allowed_hosts()
    if patterns is None:
        return True
    if url in (None, "", "about:blank"):
        return True
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    for pattern in patterns:
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and fnmatch.fnmatchcase(host, pattern):
                return True
        elif host == pattern:
            return True
    return False


def jev_endpoint():
    """Validated base URL, never included in errors. Invalid endpoints must not fall back to TypeSafe."""
    value = get("jev.base_url")
    try:
        if not value or any(ord(c) <= 32 or ord(c) >= 127 for c in value) or "\\" in value:
            raise ValueError
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                or url.password is not None or "?" in value or "#" in value):
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
        return url
    except ValueError:
        raise ValueError("Invalid jev.base_url: use an HTTP(S) base URL without credentials, query or fragment.") from None


def jev_default_endpoint():
    url = jev_endpoint()
    return (url.scheme == "https" and url.hostname == "api.typesafe.ai"
            and url.port in (None, 443) and not url.path.strip("/"))


def jev_auth():
    value, _source, error = _lookup("jev.auth")
    if error or value not in {"bearer", "none"}:
        raise ValueError("Invalid jev.auth: choose bearer or none.")
    return value


def jev_api_key_env():
    name = get("jev.api_key_env")
    if name is None:
        return "TYPESAFE_API_KEY" if jev_default_endpoint() else "JEV_BROWSE_API_KEY"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Invalid jev.api_key_env: expected an environment variable name.")
    return name


def jev_api_key(explicit=None):
    """Resolve only the selected credential; custom endpoints never inherit the hosted key implicitly."""
    jev_endpoint()
    if jev_auth() == "none":
        return None
    return explicit or os.environ.get(jev_api_key_env())


def key_detail():
    """The hand-back detail for a missing key, without a key or real path."""
    return f"{jev_api_key_env()} not set (expected in <workspace>/.env)"


def require_key():
    """Validate the endpoint and its authentication before any browser work."""
    from .results import HandBack, Reason

    try:
        if jev_api_key() or jev_auth() == "none":
            return None
        detail = key_detail()
    except ValueError as exc:
        detail = str(exc)
    return HandBack(Reason.service_error, detail, {})


def _class_text(field, with_placeholder=True):
    parts = [field.get("label"), field.get("name_attr"), field.get("id_attr")]
    if with_placeholder:
        parts.append(field.get("placeholder"))
    return " ".join(p for p in parts if p).replace("_", " ").replace("-", " ")


def is_sensitive_field(field):
    """Pure-Python mirror of snapshot.js isSensitive (shared pattern table)."""
    from .textnorm import whole_word

    if field.get("input_type") in {"password", "file", "hidden"}:
        return True
    ac = fold(field.get("autocomplete", "")).split()
    if any(t.startswith("cc-") or t in SENSITIVE_AUTOCOMPLETE for t in ac):
        return True
    text = _class_text(field)
    return any(whole_word(p, text) for p in sensitive_patterns())


def is_personal_field(field):
    """Pure-Python mirror of snapshot.js isPersonal."""
    from .textnorm import whole_word

    ac = fold(field.get("autocomplete", "")).split()
    if any(t in PERSONAL_AUTOCOMPLETE or any(t.startswith(p) for p in PERSONAL_AUTOCOMPLETE_PREFIXES) for t in ac):
        return True
    if field.get("input_type") in {"email", "tel"}:
        return True
    text = _class_text(field, with_placeholder=False)
    return any(whole_word(p, text) for p in PERSONAL_LABELS)


# ---- generated documentation (docs/config.example.toml, docs/configuration.md table) ---------------------------
def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(f'"{x}"' for x in v) + "]"
    return f'"{v}"'


def example_toml():
    """Every setting, commented out at its default: uncomment what you change."""
    lines = ["# jev-browse config: ~/.config/jev-browse/config.toml (or $JEV_BROWSE_CONFIG).",
             "# Environment variables win over this file. Never put API keys here: keep them in the",
             "# browser-harness agent-workspace .env. Generated by `python3 -m jev_browse config --example`."]
    section = None
    for s in _S:
        head, name = s.key.split(".", 1)
        if head != section:
            lines += ["", f"[{head}]"]
            section = head
        text = s.doc + (f" One of: {', '.join(s.choices)}." if s.choices else "") + f" Env: {s.env}"
        lines += ["# " + line for line in textwrap.wrap(text, 98)]
        example = s.default if s.default is not None else {"ollama.url": "http://127.0.0.1:11434",
                                                         "openai.base_url": "https://openrouter.ai/api/v1",
                                                         "openai.model": "<model>", "ollama.num_gpu": 0}.get(
            s.key, [] if s.kind == "list" else "")
        lines.append(f"# {name} = {_toml_value(example)}")
    return "\n".join(lines) + "\n"


def markdown_table():
    """The settings table in docs/configuration.md, generated from the registry (`make docs-gen`)."""
    rows = ["| Key (`config.toml`) | Environment | Default | Meaning |", "|---|---|---|---|"]
    for s in _S:
        default = "unset" if s.default is None else f"`{_toml_value(s.default)}`"
        env = f"`{s.env}`" + (" (also " + ", ".join(f"`{a}`" for a in s.aliases) + ")" if s.aliases else "")
        doc = s.doc + (f" One of: {', '.join(f'`{c}`' for c in s.choices)}." if s.choices else "")
        rows.append(f"| `{s.key}` | {env} | {default} | {doc} |")
    return "\n".join(rows) + "\n"
