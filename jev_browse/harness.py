"""The names exported into browser-harness scripts, and the new_tab provenance recorder."""

import functools

from .interactive import jev_adopt, jev_check, jev_click, jev_close, jev_find, jev_open
from .run import fast_run

PUBLIC = ("fast_run", "jev_open", "jev_adopt", "jev_find", "jev_click", "jev_check", "jev_close")


def wrap_new_tab(original):
    """A transparent recorder around the harness's new_tab: same return value and exceptions. It records the
    returned target id as jev_adopt-able only if that id did not exist before the call (a reused blank or New
    Tab Page tab is never recorded). Recording never blocks and never changes new_tab's behaviour."""
    original = getattr(original, "__jev_browse_original__", original)

    @functools.wraps(original)
    def new_tab(*args, **kwargs):
        before = None
        try:
            from .tab import live_targets

            before = {t["targetId"] for t in live_targets()}
        except Exception:
            before = None
        result = original(*args, **kwargs)
        if before is not None:
            try:
                if isinstance(result, str) and result not in before:
                    from .tab import default_registry

                    default_registry("created").record(result)
            except Exception:
                pass
        return result

    new_tab.__jev_browse_original__ = original
    return new_tab


__all__ = ["fast_run", "jev_open", "jev_adopt", "jev_find", "jev_click", "jev_check", "jev_close", "wrap_new_tab"]
