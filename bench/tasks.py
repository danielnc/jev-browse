"""Benchmark tasks. Tasks 1–3 vote; 4 (llm) and 5 (iframe) are reported as costs; `private` is loaded
from the gitignored .local/bench_private.json and skipped if absent."""

import calendar
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from bench.fixture.ports import P1

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Task:
    key: str
    url: str
    goal: str
    domain: str
    votes: bool = False
    public: bool = True
    meta: dict = field(default_factory=dict)


def flights_date(today=None):
    return (today or dt.date.today()) + dt.timedelta(days=26)


def long_date(d):
    return f"{calendar.month_name[d.month]} {d.day}, {d.year}"


def load_tasks(today=None, private_path=ROOT / ".local" / "bench_private.json"):
    fd = flights_date(today)
    tasks = {
        "wiki": Task("wiki", "https://en.wikipedia.org/wiki/Main_Page",
                     "Starting from the Wikipedia Main Page, open the Wikipedia article on Gödel's incompleteness "
                     "theorems.", "wikipedia.org", votes=True),
        "flights": Task("flights", "https://www.google.com/travel/flights?hl=en",
                        f"Find one-way flights from Zurich to London on {long_date(fd)}, for one adult in economy. "
                        "Stop when matching flight options are visible. Do not select or book a flight.",
                        "google.com", votes=True, meta={"date": fd.isoformat()}),
        "hotel": Task("hotel", f"http://localhost:{P1}/",
                      "Search stays in Lisbon for October 20 to October 23, set the Design category and the Free "
                      "cancellation filter, then open Casa Flora.", f"localhost:{P1}", votes=True),
        "llm": Task("llm", "https://en.wikipedia.org/wiki/Main_Page",
                    "Open the Wikipedia article about the capital city of France.", "wikipedia.org"),
        "iframe": Task("iframe", f"http://localhost:{P1}/hotel-iframe.html",
                       "Search stays in Lisbon, set the Design category and the Free cancellation filter, open "
                       "Casa Flora, and reserve it.", f"localhost:{P1}"),
    }
    if Path(private_path).exists():
        spec = json.loads(Path(private_path).read_text())
        tasks["private"] = Task("private", spec["url"], spec["goal"], spec["domain"], public=False,
                                meta={"verify": spec.get("verify", {})})
    return tasks
