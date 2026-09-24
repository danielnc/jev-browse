"""Case- and diacritic-folding plus whole-word matching, shared by candidates, grounding, and the gates."""

import re
import unicodedata

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[0-9a-z]+(?:'[0-9a-z]+)?")


def fold(s):
    """Case-, width-, and diacritic-folded, whitespace-collapsed text: 'Zürich  Straße' -> 'zurich strasse'."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("’", "'").replace("‘", "'")
    return _WS.sub(" ", s.casefold()).strip()


def words(s):
    """Folded word tokens."""
    return _WORD.findall(fold(s))


def whole_word(needle, haystack):
    """True if the folded words of `needle` occur as a contiguous run of whole words in `haystack`."""
    n, h = words(needle), words(haystack)
    if not n or len(n) > len(h):
        return False
    return any(h[i:i + len(n)] == n for i in range(len(h) - len(n) + 1))


def any_whole_word(needles, haystack):
    """The first needle that matches `haystack` as whole words, else None."""
    for needle in needles:
        if whole_word(needle, haystack):
            return needle
    return None
