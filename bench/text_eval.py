"""Text-backend eval: 14 field cases x N repeats per backend, pass count and latency. Run it against your own
backend before trusting it with real pages.

Settings come from the same place as jev-browse itself (environment, the browser-harness agent-workspace .env, and
~/.config/jev-browse/config.toml); server URLs are never printed.

python3 bench/text_eval.py                                   # the configured text backend
python3 bench/text_eval.py --backends ollama --models qwen3:30b-a3b,gemma3:4b --repeats 2
python3 bench/text_eval.py --backends openai,claude --excerpt --out results.json
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jev_browse import config  # noqa: E402
from jev_browse.doctor import load_harness_env  # noqa: E402
from jev_browse.textgen import (  # noqa: E402
    ClaudeSubscriptionBackend,
    CodexSubscriptionBackend,
    OllamaBackend,
    OpenAICompatBackend,
    pii_shaped,
)
from jev_browse.textnorm import fold  # noqa: E402


def exact(*accepted):
    return lambda v: v is not None and fold(v) in {fold(a) for a in accepted}


def contains(word):
    return lambda v: v is not None and fold(word) in fold(v)


def null(v):
    return v is None


# A generic ~2,000-character page excerpt: real page text in the prompt is what once made a local model echo its
# input instead of answering, so --excerpt runs every case with it.
EXCERPT = ("Main Page\nWelcome to the encyclopedia that anyone can edit.\nFrom today's featured article\n"
           + "The history of the region is long and varied, with trade routes, harbours, and old libraries. " * 25)[:2000]

FLIGHT = "Find one-way flights from Zurich to London on October 20, 2026, for one adult"
CASES = [
    ("destination", FLIGHT, {"label": "Where to?"}, exact("London")),
    ("origin", FLIGHT, {"label": "Where from?"}, exact("Zurich", "Zürich")),
    ("date MM/DD/YYYY", FLIGHT, {"label": "Departure", "placeholder": "MM/DD/YYYY"}, exact("10/20/2026")),
    ("date ISO", "Book a hotel in Lisbon from October 20 to October 23, 2026",
     {"label": "Check-in", "placeholder": "YYYY-MM-DD"}, exact("2026-10-20")),
    ("count", "Book a table for four people tonight at Casa Flora", {"label": "Number of guests"}, exact("4", "four")),
    ("select", "Search business class flights to Tokyo",
     {"label": "Cabin class (Economy, Premium economy, Business, First)"}, exact("Business")),
    ("world: capital", "Open the Wikipedia article about the capital city of France",
     {"label": "Search Wikipedia"}, exact("Paris")),
    ("world: author", "Find the Wikipedia article about the author of Pride and Prejudice",
     {"label": "Search Wikipedia"}, exact("Jane Austen")),
    ("search query", "Search GitHub for the requests HTTP library", {"label": "Search GitHub"}, contains("requests")),
    ("hotel destination", "Search Lisbon stays for October 20 to 23 and open Casa Flora", {"label": "Destination"},
     exact("Lisbon")),
    ("optional promo -> null", "Buy the red kettle", {"label": "Promo code (optional)"}, null),
    ("email -> null", "Sign up for the newsletter", {"label": "Email"}, null),
    ("phone -> null", "Request a callback about my order", {"label": "Phone number"}, null),
    ("name -> null", "Book a table for two tonight", {"label": "Full name"}, null),
]


def backends_for(name, models):
    """[(label, backend)] for one backend name; `models` overrides the configured model (comma-separated)."""
    if name == "claude":
        return [(f"claude {m} (cold)", ClaudeSubscriptionBackend(m, mode="cold")) for m in models or
                [config.get("claude.model")]]
    if name == "codex":
        return [(f"codex {m}", CodexSubscriptionBackend(m)) for m in models or [config.get("codex.model")]]
    if name == "ollama":
        url = config.get("ollama.url")
        if not url:
            raise SystemExit("set ollama.url (JEV_BROWSE_OLLAMA_URL, the harness .env, or config.toml)")
        gpu = config.get("ollama.num_gpu")
        return [(f"ollama {m}" + (f" num_gpu={gpu}" if gpu is not None else ""),
                 OllamaBackend(url, m, num_gpu=gpu, keep_alive=config.get("ollama.keep_alive"),
                               think=config.get("ollama.think"))) for m in models or [config.get("ollama.model")]]
    if name == "openai":
        base = config.get("openai.base_url")
        if not base:
            raise SystemExit("set openai.base_url (JEV_BROWSE_OPENAI_BASE_URL or config.toml)")
        key = os.environ.get(config.get("openai.api_key_env"))
        return [(f"openai {m}", OpenAICompatBackend(base, key, m)) for m in models or [config.get("openai.model")]]
    raise SystemExit(f"unknown backend {name!r}; use claude, codex, ollama, or openai")


def run(backend, repeats, excerpt=""):
    rows = []
    for name, goal, field, ok in CASES:
        f = {"key": field["label"], "label": field["label"], "placeholder": field.get("placeholder", "")}
        for rep in range(repeats):
            t = time.perf_counter()
            r = backend.batch(goal, [f], excerpt, time.monotonic() + 60)
            dt = time.perf_counter() - t
            value = r.values.get(f["key"]) if not r.error else None
            rows.append({"case": name, "rep": rep, "passed": (not r.error) and ok(value), "s": round(dt, 3),
                         "value": "<personal-shaped value redacted>" if value and pii_shaped(value) else value,
                         "error": r.error, "tokens": r.tokens})
    return rows


def summarise(label, rows):
    lat = sorted(r["s"] for r in rows)
    p90 = lat[min(len(lat) - 1, int(round(0.9 * (len(lat) - 1))))]
    # A miss can be a leaked personal value (a backend filling Email with a real address): never print it.
    misses = sorted({f"{r['case']}=" + ("<personal-shaped value redacted>" if r["value"] and pii_shaped(r["value"])
                                          else repr(r["value"])) for r in rows if not r["passed"]})
    return {"backend": label, "passed": sum(r["passed"] for r in rows), "total": len(rows),
            "median_s": round(statistics.median(lat), 2), "p90_s": round(p90, 2), "misses": misses}


def main(argv=None):
    load_harness_env()
    config.reset_cache()
    ap = argparse.ArgumentParser()
    ap.add_argument("--backends", help="comma-separated: claude, codex, ollama, openai (default: the configured one)")
    ap.add_argument("--models", help="comma-separated models, overriding the configured model of each backend")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--excerpt", action="store_true", help="send a ~2,000-char page excerpt with every case")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    names = args.backends.split(",") if args.backends else [config.text_backend_name()]
    if names == ["none"]:
        raise SystemExit("the configured text backend is none; pass --backends")
    models = [m for m in (args.models or "").split(",") if m]
    results = []
    for name in names:
        for label, backend in backends_for(name, models):
            label += " +excerpt" if args.excerpt else ""
            results.append((label, run(backend, args.repeats, EXCERPT if args.excerpt else "")))
    out = [summarise(label, rows) for label, rows in results]
    print(json.dumps(out, indent=1, ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": out, "rows": {label: rows for label, rows in results}},
                                             indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
