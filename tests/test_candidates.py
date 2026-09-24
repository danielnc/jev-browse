import datetime as dt

from jev_browse.candidates import candidates, parse_dates, render_date

TODAY = dt.date(2026, 9, 24)


def test_quoted_span_first():
    c = candidates('Search for "slow travel guides" in the shop', {}, today=TODAY)
    assert c[0] == "slow travel guides"


def test_flights_goal_yields_zurich_and_london():
    c = candidates("Find one-way flights from Zurich to London on October 20, 2026", {"label": "Where from?"},
                   today=TODAY)
    assert "Zurich" in c and "London" in c
    assert "2026-10-20" in c and "October 20, 2026" in c
    assert len(c) <= 40


def test_wikipedia_goal_yields_lowercase_query():
    c = candidates("Open the Wikipedia article on Gödel's incompleteness theorems", {"label": "Search Wikipedia"},
                   today=TODAY)
    assert "Gödel's incompleteness theorems" in c


def test_capital_of_france_ngram():
    c = candidates("Open the Wikipedia article about the capital city of France", {}, today=TODAY)
    assert "capital city of France" in c


def test_date_rendered_in_placeholder_format():
    d = dt.date(2026, 10, 20)
    assert render_date(d, {"placeholder": "MM/DD/YYYY"})[0] == "10/20/2026"
    assert render_date(d, {"placeholder": "dd.mm.yyyy"})[0] == "20.10.2026"
    assert render_date(d, {"input_type": "date"})[0] == "2026-10-20"


def test_date_clones_current_value_shape():
    d = dt.date(2026, 10, 20)
    assert render_date(d, {"value": "Sun, Sep 20"})[0] == "Tue, Oct 20"
    assert render_date(d, {"value": "Sep 20, 2026"})[0] == "Oct 20, 2026"
    c = candidates("Fly to London on October 20, 2026", {"label": "Departure", "value": "Sun, Sep 20"}, today=TODAY)
    assert "Tue, Oct 20" in c and c.index("Tue, Oct 20") < c.index("London")


def test_relative_date_tomorrow():
    assert parse_dates("Book a table for tomorrow", TODAY) == [dt.date(2026, 9, 25)]
    assert dt.date(2026, 10, 2) in parse_dates("Leave next Friday", TODAY)
    assert dt.date(2026, 10, 15) in parse_dates("in 3 weeks please", TODAY)
    assert dt.date(2026, 10, 20) in parse_dates("Oct 20", TODAY)  # no year: next occurrence
    assert dt.date(2027, 9, 1) in parse_dates("September 1", TODAY)


def test_unknown_format_offers_only_unambiguous_dates():
    d = dt.date(2026, 10, 9)
    out = render_date(d, {})
    assert "10/09/2026" not in out and "09/10/2026" not in out
    assert "2026-10-09" in out and "October 9, 2026" in out
    assert render_date(d, {"placeholder": "DD/MM/YYYY"})[0] == "09/10/2026"
    late = render_date(dt.date(2026, 10, 20), {})
    assert "10/20/2026" in late and "20/10/2026" in late
    assert parse_dates("on 02/10/2026", TODAY) == []  # ambiguous numeric order in the goal is not resolved


def test_diacritic_variants():
    c = candidates("Trains from Zürich to Genève", {}, today=TODAY)
    assert "Zürich" in c and "Zurich" in c
    assert c.index("Zurich") == c.index("Zürich") + 1
    assert "Genève" in c and "Geneve" in c


def test_limit_and_length_caps():
    goal = " ".join(f"Word{i}" for i in range(60)) + ' "' + "x" * 130 + '"'
    c = candidates(goal, {}, limit=40, today=TODAY)
    assert len(c) == 40 and all(len(x) <= 120 for x in c)


def test_no_candidates_for_empty_goal():
    assert candidates("", {}) == [] and candidates("   ", {}) == []


def test_emails_and_urls():
    c = candidates("Invite anna@example.test to https://example.test/board", {}, today=TODAY)
    assert "anna@example.test" in c and "https://example.test/board" in c
