"""The benchmark decision rule, as pure functions over per-attempt rows.

A row: {task, arm, passed: bool, wall_s: float, cost: float (cache-neutral USD), infra_error: bool}.
"""

import statistics

VOTING = ("wiki", "flights", "hotel")
REPORTED = ("llm", "iframe")
CLASSES = {
    "wiki": "link navigation on a public site",
    "flights": "multi-field search form with date/autocomplete widgets",
    "hotel": "filter-and-open on a listing site",
    "llm": "a form whose value must be worked out from the goal (cost of a miss)",
    "iframe": "a flow whose last step may sit in an embedded widget (cost of a hand-back)",
}
MIN_BAND = 0.10


def _counted(rows, task, arm):
    return [r for r in rows if r["task"] == task and r["arm"] == arm and not r.get("infra_error")]


def noise_band(b_rows):
    walls = [r["wall_s"] for r in b_rows]
    if len(walls) < 2:
        return MIN_BAND
    med = statistics.median(walls)
    rel_range = (max(walls) - min(walls)) / med if med else 0.0
    return max(MIN_BAND, rel_range / 2)


def verdict(a_rows, b_rows):
    """Win when A is at least as fast (within the band), cheaper (cache-neutral), with no pass-rate drop."""
    if not a_rows or not b_rows:
        return {"win": None, "why": "missing attempts"}
    band = noise_band(b_rows)
    a_wall, b_wall = statistics.median(r["wall_s"] for r in a_rows), statistics.median(r["wall_s"] for r in b_rows)
    a_cost, b_cost = statistics.median(r["cost"] for r in a_rows), statistics.median(r["cost"] for r in b_rows)
    a_pass, b_pass = sum(bool(r["passed"]) for r in a_rows), sum(bool(r["passed"]) for r in b_rows)
    fast = a_wall <= b_wall * (1 + band)
    cheap = a_cost < b_cost
    no_drop = a_pass / len(a_rows) >= b_pass / len(b_rows) - 1e-9  # pass *rate*, so unequal N compares fairly
    return {"win": fast and cheap and no_drop, "fast": fast, "cheaper": cheap, "no_pass_drop": no_drop,
            "band": round(band, 3), "a_wall_median": a_wall, "b_wall_median": b_wall, "a_cost_median": a_cost,
            "b_cost_median": b_cost, "a_passes": a_pass, "b_passes": b_pass, "n_a": len(a_rows), "n_b": len(b_rows)}


def hinges(a_rows, b_rows):
    """True when excluding any one failed attempt (of A or B) would flip the verdict."""
    base = verdict(a_rows, b_rows)["win"]
    for side in ("a", "b"):
        rows = a_rows if side == "a" else b_rows
        for i, r in enumerate(rows):
            if r["passed"]:
                continue
            rest = rows[:i] + rows[i + 1:]
            v = verdict(rest, b_rows) if side == "a" else verdict(a_rows, rest)
            if v["win"] != base:
                return True
    return False


def decide(rows, a_arm="A", b_arm="B", int_arm="A-int"):
    out = {"classes": {}, "reported": {}}
    losses = 0
    for task in VOTING:
        a, b = _counted(rows, task, a_arm), _counted(rows, task, b_arm)
        v = verdict(a, b)
        v["class"] = CLASSES[task]
        v["hinges"] = hinges(a, b) if a and b else False
        out["classes"][task] = v
        if v["win"] is False:
            losses += 1
    a_int = verdict(_counted(rows, "hotel", int_arm), _counted(rows, "hotel", b_arm))
    out["a_int"] = a_int
    for task in REPORTED:
        a, b = _counted(rows, task, a_arm), _counted(rows, task, b_arm)
        if a and b:
            out["reported"][task] = {
                "class": CLASSES[task],
                "extra_wall_s": statistics.median(r["wall_s"] for r in a) - statistics.median(r["wall_s"] for r in b),
                "extra_cost": statistics.median(r["cost"] for r in a) - statistics.median(r["cost"] for r in b),
                "a_passes": sum(bool(r["passed"]) for r in a), "b_passes": sum(bool(r["passed"]) for r in b),
            }
    out["losses"] = losses
    out["opt_in"] = losses >= 2
    out["routing"] = {task: ("jev-browse" if v["win"] else
                             ("interactive helpers" if a_int.get("win") else "harness"))
                      for task, v in out["classes"].items()}
    return out
