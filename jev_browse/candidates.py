"""Goal-derived candidate values for text fields: select instead of generate.

Sources, in order: quoted spans, emails/URLs, parsed dates rendered in the field's format, capitalised spans,
then 1–5-token n-grams that neither start nor end with a stopword. Each diacritic span also gets an ASCII
variant. Jev picks among these strings; code never lets a model compute a date.
"""

import calendar
import datetime as dt
import re
import unicodedata

MAX_LEN = 120

STOPWORDS = set("""
a an the and or but of in on at to for from by with about into onto over under than then this that these those
is are was were be been being it its as if so do does did not no yes my your our their his her me you we they i
please can could would should will just also only any some all each every up down out off very there here
what which who whom whose when where why how
open find search look show go get click select set choose pick take make use enter type fill press book
o a os as de da do das dos e em no na nos nas um uma para por com sem que se ao aos
""".split())

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
WEEKDAYS = {d.lower(): i for i, d in enumerate(calendar.day_name)}
WEEKDAYS.update({d.lower(): i for i, d in enumerate(calendar.day_abbr)})

_QUOTED = re.compile(r'"([^"]{1,120})"|“([^”]{1,120})”|«([^»]{1,120})»|(?:(?<=\s)|^)\'([^\']{1,120})\'(?=[\s.,;:!?]|$)|‘([^’]{1,120})’')
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"https?://[^\s\"'<>]+|www\.[^\s\"'<>]+")
_TOKEN = re.compile(r"[\w][\w'’\-.]*[\w]|[\w]", re.UNICODE)
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
_DATE_PATTERNS = [
    # October 20, 2026 / Oct 20 2026 / Oct 20
    (re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", re.I), "mdy"),
    # 20 October 2026 / 20th Oct
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\.?(?:,?\s+(\d{{4}}))?\b", re.I), "dmy"),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "slash"),
]
_REL_IN = re.compile(r"\bin\s+(\d{1,3}|a|one|two|three|four|five|six)\s+(day|week|month)s?\b", re.I)
_REL_NEXT = re.compile(rf"\b(next|this|on)\s+({'|'.join(sorted(WEEKDAYS, key=len, reverse=True))})\b", re.I)
_ORDINAL_DAY = re.compile(r"\bon\s+the\s+(\d{1,2})(?:st|nd|rd|th)\b", re.I)
_WORDNUM = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def ascii_variant(s):
    folded = unicodedata.normalize("NFKD", s)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return folded.replace("’", "'").replace("ß", "ss")


def _year_for(month, day, today):
    try:
        candidate = dt.date(today.year, month, day)
    except ValueError:
        return None
    return candidate if candidate >= today else _safe_date(today.year + 1, month, day)


def _safe_date(y, m, d):
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def parse_dates(goal, today=None):
    """Absolute and relative dates in the goal, resolved in code against `today`."""
    today = today or dt.date.today()
    found = []

    def add(d):
        if d and d not in found:
            found.append(d)

    for pattern, kind in _DATE_PATTERNS:
        for m in pattern.finditer(goal):
            if kind == "mdy":
                month, day, year = MONTHS[m.group(1).lower().rstrip(".")], int(m.group(2)), m.group(3)
                add(_safe_date(int(year), month, day) if year else _year_for(month, day, today))
            elif kind == "dmy":
                day, month, year = int(m.group(1)), MONTHS[m.group(2).lower().rstrip(".")], m.group(3)
                add(_safe_date(int(year), month, day) if year else _year_for(month, day, today))
            elif kind == "iso":
                add(_safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            elif kind == "slash":
                a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if a > 12:
                    add(_safe_date(y, b, a))
                elif b > 12:
                    add(_safe_date(y, a, b))
                # both <= 12: ambiguous order; keep the goal's literal text only (quoted/n-gram paths)
    low = goal.lower()
    if re.search(r"\btoday\b", low):
        add(today)
    if re.search(r"\btomorrow\b", low):
        add(today + dt.timedelta(days=1))
    if re.search(r"\bday after tomorrow\b", low):
        add(today + dt.timedelta(days=2))
    for m in _REL_IN.finditer(goal):
        n = _WORDNUM.get(m.group(1).lower()) or int(m.group(1))
        unit = m.group(2).lower()
        if unit == "day":
            add(today + dt.timedelta(days=n))
        elif unit == "week":
            add(today + dt.timedelta(weeks=n))
        else:
            month = today.month - 1 + n
            y, mth = today.year + month // 12, month % 12 + 1
            add(_safe_date(y, mth, min(today.day, calendar.monthrange(y, mth)[1])))
    for m in _REL_NEXT.finditer(goal):
        target = WEEKDAYS[m.group(2).lower()]
        delta = (target - today.weekday()) % 7
        if m.group(1).lower() == "next":
            # "next Friday" is ambiguous (this coming one, or the one in next week): offer both
            add(today + dt.timedelta(days=delta or 7))
            add(today + dt.timedelta(days=(delta or 7) + 7))
        else:
            add(today + dt.timedelta(days=delta))
    for m in _ORDINAL_DAY.finditer(goal):
        day = int(m.group(1))
        d = _safe_date(today.year, today.month, day)
        if d and d < today:
            nm = today.month % 12 + 1
            d = _safe_date(today.year + (today.month == 12), nm, day)
        add(d)
    return found


# --- rendering ---------------------------------------------------------------------------------------------
def _fmt(d, spec):
    return (spec.replace("YYYY", f"{d.year:04d}").replace("YY", f"{d.year % 100:02d}")
            .replace("MM", f"{d.month:02d}").replace("DD", f"{d.day:02d}"))


_PLACEHOLDER = re.compile(r"(?i)\b(mm|dd|yyyy|yy)([/.\-])(mm|dd)\2(yyyy|yy|mm|dd)\b|\b(yyyy)([/.\-])(mm)\6(dd)\b")


def field_date_format(field):
    """(renderer, order_known) for the field's observed date format, or (None, False)."""
    ph = (field.get("placeholder") or "")
    m = _PLACEHOLDER.search(ph)
    if m:
        spec = m.group(0).upper()
        return (lambda d, s=spec: _fmt(d, s)), True
    if (field.get("input_type") or "") == "date":
        return (lambda d: d.isoformat()), True
    value = (field.get("value") or "").strip()
    if value:
        return _shape_renderer(value)
    return None, False


def _shape_renderer(value):
    # "Sun, Sep 20" / "Sunday, September 20" / "Sep 20, 2026" / "20 Sep 2026" / ISO / numeric
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return (lambda d: d.isoformat()), True
    m = re.fullmatch(r"([A-Za-z]{3,9}),\s+([A-Za-z]{3,9})\.?\s+(\d{1,2})(,?\s+\d{4})?", value)
    if m and m.group(1).lower() in WEEKDAYS and m.group(2).lower() in MONTHS:
        long_dow, long_mon, year = len(m.group(1)) > 3, len(m.group(2)) > 3, bool(m.group(4))

        def r(d, long_dow=long_dow, long_mon=long_mon, year=year):
            dow = calendar.day_name[d.weekday()] if long_dow else calendar.day_abbr[d.weekday()]
            mon = calendar.month_name[d.month] if long_mon else calendar.month_abbr[d.month]
            return f"{dow}, {mon} {d.day}" + (f", {d.year}" if year else "")
        return r, True
    m = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(,?\s+\d{4})?", value)
    if m and m.group(1).lower() in MONTHS:
        long_mon, year = len(m.group(1)) > 3, bool(m.group(3))
        return (lambda d, lm=long_mon, y=year: f"{(calendar.month_name if lm else calendar.month_abbr)[d.month]} "
                f"{d.day}" + (f", {d.year}" if y else "")), True
    m = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?(\s+\d{4})?", value)
    if m and m.group(2).lower() in MONTHS:
        long_mon, year = len(m.group(2)) > 3, bool(m.group(3))
        return (lambda d, lm=long_mon, y=year: f"{d.day} {(calendar.month_name if lm else calendar.month_abbr)[d.month]}"
                + (f" {d.year}" if y else "")), True
    m = re.fullmatch(r"(\d{1,2})([/.])(\d{1,2})\2(\d{4})", value)
    if m:
        a, sep, b = int(m.group(1)), m.group(2), int(m.group(3))
        if a > 12:
            return (lambda d, s=sep: f"{d.day:02d}{s}{d.month:02d}{s}{d.year}"), True
        if b > 12:
            return (lambda d, s=sep: f"{d.month:02d}{s}{d.day:02d}{s}{d.year}"), True
    return None, False


def is_date_field(field):
    renderer, known = field_date_format(field)
    if known:
        return True
    label = (field.get("label") or "").lower()
    return bool(re.search(r"\b(date|departure|return|check-?in|check-?out|arrival|birthday|when)\b", label))


def unambiguous_renderings(d):
    return [d.isoformat(), f"{calendar.month_name[d.month]} {d.day}, {d.year}",
            f"{calendar.month_abbr[d.month]} {d.day}, {d.year}", f"{d.day} {calendar.month_abbr[d.month]} {d.year}",
            f"{calendar.day_abbr[d.weekday()]}, {calendar.month_abbr[d.month]} {d.day}"]


def render_date(d, field):
    """Renderings for `d`, the field's observed format first. Numeric DD/MM or MM/DD only when unambiguous."""
    out = []
    renderer, known = field_date_format(field)
    if renderer:
        out.append(renderer(d))
    out += unambiguous_renderings(d)
    if d.day > 12:  # a numeric DD/MM or MM/DD form is unambiguous only then (or when the field fixed the order)
        out += [f"{d.month:02d}/{d.day:02d}/{d.year}", f"{d.day:02d}/{d.month:02d}/{d.year}"]
    return _dedupe(out)


# --- spans ---------------------------------------------------------------------------------------------------
def _tokens(goal):
    return [(m.group(0).rstrip(".,;:!?"), m.start()) for m in _TOKEN.finditer(goal)]


def _is_stop(tok):
    return tok.lower().strip("'’") in STOPWORDS


def capitalised_spans(goal):
    toks = _tokens(goal)
    spans, cur = [], []
    for i, (tok, _) in enumerate(toks):
        cap = tok[:1].isupper() and not tok.isupper() or (tok.isupper() and len(tok) > 1 and tok.isalpha())
        if cap and not (i == 0 and _is_stop(tok)):
            cur.append(tok)
        else:
            if cur:
                spans.append(" ".join(cur))
            cur = []
    if cur:
        spans.append(" ".join(cur))
    return [s for s in spans if not _is_stop(s)]


def ngrams(goal, max_n=5):
    toks = [t for t, _ in _tokens(goal)]
    out = []
    for n in range(max_n, 0, -1):
        for i in range(len(toks) - n + 1):
            gram = toks[i:i + n]
            if _is_stop(gram[0]) or _is_stop(gram[-1]):
                continue
            out.append(" ".join(gram))
    return out


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        item = item.strip()
        if item and len(item) <= MAX_LEN and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _with_variants(items):
    out = []
    for item in items:
        out.append(item)
        variant = ascii_variant(item)
        if variant != item:
            out.append(variant)
    return out


def candidates(goal, field=None, *, limit=40, today=None):
    """Ordered, deduped candidate strings for `field`, each ≤ 120 chars; high-precision sources first."""
    if not goal or not goal.strip():
        return []
    field = field or {}
    quoted = [next(g for g in m.groups() if g) for m in _QUOTED.finditer(goal)]
    emails = _EMAIL.findall(goal)
    urls = _URL.findall(goal)
    dates = []
    for d in parse_dates(goal, today):
        dates += render_date(d, field)
    caps = capitalised_spans(goal)
    grams = ngrams(goal)
    ordered = _with_variants(quoted) + emails + urls + dates + _with_variants(caps) + _with_variants(grams)
    return _dedupe(ordered)[:limit]
