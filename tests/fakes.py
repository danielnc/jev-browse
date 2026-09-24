"""Test doubles: FakeCDP (the harness seam), FakeTypeSafe (the Jev client), FakeProc (a Popen double).

FakeCDP reproduces harness 0.1.13's failure surface: a RuntimeError carrying the daemon's error string for a
stale or unknown explicit session, an IPC response timeout (a TimeoutError subclass, like
helpers._IPCResponseTimeout), and FileNotFoundError/ConnectionRefusedError for an unreachable daemon.
"""

import io
import itertools
import json


class IPCResponseTimeout(TimeoutError):
    pass


SESSION_NOT_FOUND = "Session with given id not found."


class FakeCDP:
    """Callable like harness_api.cdp. `handlers` maps a CDP method to a value or a callable(params, session_id).

    Unknown methods return {}. Every call is recorded as (method, session_id, params).
    """

    def __init__(self, handlers=None, *, targets=None, current="T-current"):
        self.handlers = dict(handlers or {})
        self.calls = []
        self.targets = targets if targets is not None else {}
        self.current = current
        self.dialog = None
        self.dead_sessions = set()
        self.timeout_methods = set()
        self.timeout_once = set()
        self.unreachable = False
        self._ids = itertools.count(1)

    # --- the harness_api surface -------------------------------------------------------------
    def __call__(self, method, session_id=None, _response_timeout=5.0, **params):
        self.calls.append((method, session_id, params))
        if self.unreachable:
            raise ConnectionRefusedError("daemon unreachable")
        if session_id is not None and session_id in self.dead_sessions:
            raise RuntimeError(SESSION_NOT_FOUND)
        if method in self.timeout_once:
            self.timeout_once.discard(method)
            raise IPCResponseTimeout(f"{method} timed out once")
        if method in self.timeout_methods:
            raise IPCResponseTimeout(f"{method} timed out after {_response_timeout:g}s waiting for the daemon")
        handler = self.handlers.get(method)
        if handler is None:
            return self._default(method, session_id, params)
        if callable(handler):
            return handler(params, session_id)
        return handler

    def _default(self, method, session_id, params):
        if method == "Target.createTarget":
            tid = f"T{next(self._ids)}"
            self.targets[tid] = {"targetId": tid, "type": "page", "url": params.get("url", "about:blank"),
                                 "title": "", "openerId": None}
            return {"targetId": tid}
        if method == "Target.attachToTarget":
            return {"sessionId": f"S{next(self._ids)}-{params.get('targetId')}"}
        if method == "Target.getTargets":
            return {"targetInfos": list(self.targets.values())}
        if method == "Target.getTargetInfo":
            tid = params.get("targetId")
            if tid not in self.targets:
                raise RuntimeError("No target with given id found")
            return {"targetInfo": self.targets[tid]}
        if method == "Target.closeTarget":
            self.targets.pop(params.get("targetId"), None)
            return {"success": True}
        return {}

    def current_tab(self):
        info = self.targets.get(self.current, {"url": "about:blank", "title": ""})
        return {"targetId": self.current, "target_id": self.current, "url": info["url"], "title": info["title"]}

    def pending_dialog(self):
        return self.dialog

    def methods(self):
        return [c[0] for c in self.calls]


def choice_answer(ids, selected, confidence=1.0):
    ids = list(ids)
    rest = (1.0 - confidence) / max(1, len(ids) - 1) if len(ids) > 1 else 0.0
    probs = {i: (confidence if i == selected else rest) for i in ids}
    if len(ids) == 1:
        probs[selected] = 1.0
    return {"type": "choice", "choice": selected, "confidence": confidence, "probabilities": probs}


def noul_answer(p):
    return {"type": "noul", "noul": p}


class FakeTypeSafe:
    """Stands in for typesafe.Client. `script` is a list of callables(body) -> answers dict, used in order;
    or a single callable used for every request."""

    def __init__(self, script):
        self.script = script
        self.requests = []
        self.model = "jev-test"

    def ask(self, state, questions, *, deadline=None):
        from jev_browse.typesafe import Answer

        body = {"state": state, "questions": questions}
        self.requests.append(body)
        fn = self.script if callable(self.script) else self.script[len(self.requests) - 1]
        answers = fn(body)
        return Answer(answers=answers, model=self.model, usage={"input_tokens": 100, "output_tokens": 10},
                      latency_ms=5)


class FakeProc:
    """A Popen double. `stdout_text` is returned by communicate(); `hang=True` makes wait/communicate time out."""

    instances = []

    def __init__(self, argv=None, *, stdout_text="", returncode=0, hang=False, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.stdout_text = stdout_text
        self.returncode_value = returncode
        self.hang = hang
        self.pid = 4242
        self.returncode = None
        self.killed = False
        self.stdin = io.StringIO()
        self.input = None
        FakeProc.instances.append(self)

    def communicate(self, input=None, timeout=None):
        import subprocess

        self.input = input
        if self.hang:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.returncode = self.returncode_value
        return self.stdout_text, ""

    def wait(self, timeout=None):
        import subprocess

        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.returncode = self.returncode_value if not self.killed else -9
        return self.returncode

    def poll(self):
        """None while running (until communicate/wait finished or the process was killed)."""
        if self.killed:
            return -9
        return self.returncode

    def kill(self):
        self.killed = True


def claude_json(result_text, *, is_error=False, input_tokens=500, output_tokens=20, cost=0.0007):
    return json.dumps({"type": "result", "is_error": is_error, "result": result_text,
                       "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                       "total_cost_usd": cost})
