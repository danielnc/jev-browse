"""The ONLY browser-harness touchpoint. Lazy, so tests and plain imports never load the harness or its .env.

Sources (browser-harness 0.1.13; underscore names are private API pinned to that version):
`helpers.cdp`, `helpers.current_tab`, `helpers._send({"meta": "pending_dialog"})`, `_ipc._RUNTIME` (socket dir),
`paths.tmp_dir()`, `helpers.NAME`. jev-browse never calls `drain_events` (it clears the daemon's shared buffer)
and never calls `switch_tab` (it moves the daemon's shared current tab).
"""

from pathlib import Path

_fake = None


def install_fake(fake):
    """Route every call to a test double (tests only). Pass None to restore the real harness."""
    global _fake
    _fake = fake


def _helpers():
    from browser_harness import helpers

    return helpers


def cdp(method, session_id=None, _response_timeout=5.0, **params):
    """Send one raw CDP command through the harness daemon (to `session_id`'s target if given); returns its result."""
    if _fake is not None:
        return _fake(method, session_id=session_id, _response_timeout=_response_timeout, **params)
    return _helpers().cdp(method, session_id=session_id, _response_timeout=_response_timeout, **params)


def current_tab():
    """The harness daemon's current tab (read-only: jev-browse never switches it)."""
    if _fake is not None:
        return _fake.current_tab()
    return _helpers().current_tab()


def pending_dialog():
    """The daemon's single, session-less dialog slot (non-destructive), or None."""
    if _fake is not None:
        return _fake.pending_dialog()
    return _helpers()._send({"meta": "pending_dialog"}).get("dialog")


def socket_dir():
    """Directory of the daemon IPC socket; honours BH_RUNTIME_DIR (and the harness home)."""
    if _fake is not None:
        return Path(getattr(_fake, "socket_dir", "/tmp"))
    from browser_harness import _ipc

    return Path(_ipc._RUNTIME)


def tmp_dir():
    """The harness tmp dir (run files, traces, caches); resolved as the harness does when it is not importable."""
    if _fake is not None:
        return Path(getattr(_fake, "tmp_dir", "/tmp"))
    try:
        from browser_harness import paths
    except ImportError:  # outside the harness (e.g. bench/text_eval.py): resolve it as the harness does
        import os

        if os.environ.get("BH_TMP_DIR"):
            return Path(os.environ["BH_TMP_DIR"]).expanduser()
        home = os.environ.get("BH_HOME") or os.environ.get("BROWSER_HARNESS_HOME")
        base = Path(home).expanduser() if home else \
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "browser-harness"
        return base / "tmp"
    return Path(paths.tmp_dir())


def daemon_name():
    """The harness daemon's name (BU_NAME), which scopes jev-browse's tab registries."""
    if _fake is not None:
        return getattr(_fake, "daemon_name", "default")
    return _helpers().NAME


def is_ipc_timeout(exc):
    """True if `exc` is the harness IPC timing out (the daemon is alive but slow)."""
    return isinstance(exc, TimeoutError)


def is_unreachable(exc):
    """True if `exc` means the harness daemon or its socket is gone."""
    return isinstance(exc, (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, BrokenPipeError))
