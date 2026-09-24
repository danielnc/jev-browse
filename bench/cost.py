"""List-price-equivalent USD per attempt, split per component, cache-neutral and as billed.

Cache-neutral (the verdict's basis): every input token (uncached, cache write, cache read) at
the model's base input rate, plus output at the output rate. As billed: cache writes and reads at their rates.
"""

import json
from pathlib import Path

PRICING = json.loads((Path(__file__).with_name("pricing.json")).read_text())


class MissingRate(KeyError):
    pass


def rate(model, pricing=PRICING):
    """Longest model-id prefix match (a dated Haiku 4.5 id prices as claude-haiku-4-5)."""
    models = pricing["models"]
    m = (model or "").lower()
    for alias, key in (("opus", "claude-opus-5-5"), ("haiku", "claude-haiku-4-5"), ("sonnet", "claude-sonnet-5")):
        if m == alias:
            m = key
    best = max((k for k in models if m.startswith(k)), key=len, default=None)
    if best is None:
        raise MissingRate(f"no published rate for model {model!r} in bench/pricing.json")
    return models[best]


def usage_cost(usage, model, pricing=PRICING):
    r = rate(model, pricing)
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cw_1h = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0) or 0
    cw_5m = cw - cw_1h
    neutral = ((inp + cw + cr) * r["input"] + out * r["output"]) / 1e6
    billed = (inp * r["input"] + cw_5m * r.get("cache_write_5m", r["input"]) +
              cw_1h * r.get("cache_write_1h", r["input"]) + cr * r.get("cache_read", r["input"]) +
              out * r["output"]) / 1e6
    return {"cache_neutral": neutral, "as_billed": billed}


def claude_cost(result_event, default_model, pricing=PRICING):
    """Calling-Claude cost from a stream-json `result` event: per-model `modelUsage` when present."""
    per_model = result_event.get("modelUsage") or {}
    total = {"cache_neutral": 0.0, "as_billed": 0.0}
    usage = result_event.get("usage") or {}
    if len(per_model) == 1 and usage:  # one model: `usage` carries the 1h/5m cache-write split that modelUsage lacks
        total = usage_cost(usage, next(iter(per_model)), pricing)
    elif per_model:
        for model, u in per_model.items():
            usage = {"input_tokens": u.get("inputTokens", 0), "output_tokens": u.get("outputTokens", 0),
                     "cache_creation_input_tokens": u.get("cacheCreationInputTokens", 0),
                     "cache_read_input_tokens": u.get("cacheReadInputTokens", 0)}
            c = usage_cost(usage, model, pricing)
            total = {k: total[k] + c[k] for k in total}
    else:
        total = usage_cost(result_event.get("usage") or {}, default_model, pricing)
    total["reported_total_cost_usd"] = result_event.get("total_cost_usd")
    return total


def jev_cost(input_tokens, output_tokens=0, pricing=PRICING):
    r = pricing["models"]["jev"]
    c = (input_tokens * r["input"] + output_tokens * r["output"]) / 1e6
    return {"cache_neutral": c, "as_billed": c}


def attempt_cost(*, claude_event=None, claude_model="opus", jev_input=0, jev_output=0, llm_usage=None,
                 llm_model="haiku", pricing=PRICING):
    parts = {
        "calling_claude": claude_cost(claude_event, claude_model, pricing) if claude_event else
        {"cache_neutral": 0.0, "as_billed": 0.0},
        "jev": jev_cost(jev_input, jev_output, pricing),
        "text_backend": usage_cost(llm_usage, llm_model, pricing) if llm_usage else {"cache_neutral": 0.0,
                                                                                     "as_billed": 0.0},
    }
    return {"parts": parts,
            "cache_neutral": sum(p["cache_neutral"] for p in parts.values()),
            "as_billed": sum(p["as_billed"] for p in parts.values())}
