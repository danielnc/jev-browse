"""Owned tabs on explicit CDP sessions, the per-daemon registry, and the ported observe/act executor.

Observe/fresh/act are ported from browser-use/jev-ultrafast jev_ultrafast/browser.py (MIT). See NOTICE.
Changes: every page call carries an explicit `session_id` on a target jev-browse created (or was handed via
jev_adopt); focus emulation lives on a long-lived keeper session with no Page domain; no device-metrics
override (D9); scroll dispatches at the observed viewport centre.
"""

import atexit
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

from . import config, harness_api
from .textnorm import fold

SNAPSHOT_JS = Path(__file__).with_name("snapshot.js").read_text()
SALT = secrets.token_hex(16)  # per-process key for in-page digests of sensitive/personal values
OTP_PATTERNS = ["otp", "one-time", "one time", "passcode", "verification code"]
SNAPSHOT_TIMEOUT = 10.0
SCREENSHOT_TIMEOUT = 60.0
LOAD_TIMEOUT = 30.0


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class BrowserGone(RuntimeError):
    """The owned target or session is gone, or the daemon is unreachable."""

    def __init__(self, detail, target_id=None):
        super().__init__(detail)
        self.target_id = target_id


class DialogSuspected(RuntimeError):
    """An IPC timeout on an owned-session call: a native dialog may be blocking the page."""

    def __init__(self, method, during_act=False):
        super().__init__(f"{method} timed out; a dialog may be open")
        self.method = method
        self.during_act = during_act


class NotOwned(PermissionError):
    """The target is not registered (or, for adopt, was not created by new_tab)."""


class Busy(BlockingIOError):
    pass


_GONE = ("Session with given id not found", "No target with given id", "Target closed", "No session with given id",
         "Cannot find context", "target was closed")


RETRY_TIMEOUT = 15.0


def _dialog_pending():
    try:
        return bool(harness_api.pending_dialog())
    except Exception:
        return False


def _call(method, session_id=None, timeout=5.0, during_act=False, _retried=False, **params):
    try:
        return harness_api.cdp(method, session_id=session_id, _response_timeout=timeout, **params)
    except Exception as exc:
        if harness_api.is_ipc_timeout(exc):
            # A busy browser (many tabs, another agent) can stall a read past the IPC timeout. A read with no
            # dialog pending is retried once with a longer timeout; input is never retried (it may have run).
            if not during_act and not _retried and not method.startswith("Input.") and not _dialog_pending():
                return _call(method, session_id=session_id, timeout=max(timeout, RETRY_TIMEOUT),
                             during_act=during_act, _retried=True, **params)
            if session_id is None:
                raise BrowserGone(f"{method}: page unresponsive") from None
            raise DialogSuspected(method, during_act=during_act) from None
        if harness_api.is_unreachable(exc):
            raise BrowserGone("browser-harness daemon unreachable") from None
        if isinstance(exc, RuntimeError) and any(g in str(exc) for g in _GONE):
            raise BrowserGone(str(exc)) from None
        raise


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


# ------------------------------------------------------------------------------------------------ registry
class Registry:
    """A flock-guarded, atomically written JSON store per daemon (`jev-browse-<kind>-<daemon>.json`, mode 600).

    kind="owned" is the owned-tab registry; kind="created" is the new_tab provenance file.
    """

    def __init__(self, directory, daemon, kind="owned"):
        self.dir = Path(directory)
        self.path = self.dir / f"jev-browse-{kind}-{daemon}.json"
        self.lock_path = self.dir / f"jev-browse-{kind}-{daemon}.json.lock"

    @contextlib.contextmanager
    def _locked(self, blocking=True):
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise Busy("registry lock busy") from None
            yield
        finally:
            os.close(fd)

    def _read(self):
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and isinstance(data.get("targets"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"targets": {}}

    def _write(self, data):
        tmp = self.dir / f".{self.path.name}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def entries(self):
        with self._locked():
            return self._read()["targets"]

    def update(self, fn, blocking=True):
        with self._locked(blocking):
            data = self._read()
            result = fn(data["targets"])
            self._write(data)
            return result

    def add(self, tid, blocking=True, **entry):
        def _add(targets):
            targets.setdefault(tid, {"created_at": time.time(), "sessions": {}}).update(entry)
        self.update(_add, blocking=blocking)

    def has(self, tid):
        return tid in self.entries()

    def get(self, tid):
        return self.entries().get(tid)

    def remove(self, tid):
        self.update(lambda t: t.pop(tid, None))

    def prune(self, live_ids):
        live = set(live_ids)
        self.update(lambda t: [t.pop(k) for k in list(t) if k not in live])

    def live(self, target_infos):
        ids = {t["targetId"] for t in target_infos if t.get("type", "page") == "page"}
        self.prune(ids)
        return self.entries()

    # provenance-file helpers
    def record(self, tid):
        """Non-blocking add for the new_tab recorder: a busy lock simply skips recording."""
        try:
            self.add(tid, blocking=False)
            return True
        except (Busy, OSError):
            return False


BLANK_PREFIXES = ("about:blank", "chrome://newtab", "chrome://new-tab-page", "edge://newtab", "about:newtab",
                  "chrome-search://local-ntp")


def is_blank_url(url):
    return not url or str(url).startswith(BLANK_PREFIXES)


def default_registry(kind="owned"):
    return Registry(harness_api.socket_dir(), harness_api.daemon_name(), kind)


def live_targets():
    return _call("Target.getTargets").get("targetInfos", [])


# ------------------------------------------------------------------------------------------------ snapshot
def _snapshot_cfg(offscreen=False, lines=False):
    return {
        "salt": SALT,
        "sensitive": config.sensitive_patterns(),
        "personal_labels": [fold(p) for p in config.PERSONAL_LABELS],
        "personal_ac": config.PERSONAL_AUTOCOMPLETE,
        "personal_ac_prefixes": config.PERSONAL_AUTOCOMPLETE_PREFIXES,
        "otp": OTP_PATTERNS,
        "offscreen": offscreen,
        "lines": lines,
        "cap": config.SNAPSHOT_CAP,
    }


def snapshot_expression(offscreen=False, lines=False):
    return f"({SNAPSHOT_JS})({json.dumps(_snapshot_cfg(offscreen, lines))})"


def marker_expression():
    return f"(() => {{ const s=({SNAPSHOT_JS})({json.dumps(_snapshot_cfg())}); return s?.marker ?? null; }})()"


def fingerprint(state):
    content = {k: state.get(k) for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ owned tab
_open_tabs = []


@atexit.register
def _detach_all():
    for tab in list(_open_tabs):
        try:
            tab.detach()
        except Exception:
            pass


class OwnedTab:
    def __init__(self, target_id, registry=None):
        self.target_id = target_id
        self.registry = registry or default_registry()
        self.session = None
        self.keeper = None
        self.keep_for_dialog = False
        self.after_input = None
        self.known_popups = set()

    def __repr__(self):
        return f"OwnedTab({self.target_id})"

    # ---- lifecycle -------------------------------------------------------------------------------------
    @classmethod
    def create(cls, url, *, registry=None, deadline=None, owner=None):
        registry = registry or default_registry()
        tid = _call("Target.createTarget", url="about:blank", background=True)["targetId"]
        registry.add(tid, owner=owner or config.owner_tag(), sessions={})
        tab = cls(tid, registry)
        tab._ensure_keeper()
        tab._attach_session()
        tab.navigate(url, deadline=deadline)
        return tab

    @classmethod
    def attach(cls, target_id, *, registry=None):
        registry = registry or default_registry()
        if not registry.has(target_id):
            raise NotOwned(f"target {target_id} is not registered with jev-browse")
        tab = cls(target_id, registry)
        tab._hygiene()
        tab._ensure_keeper()
        tab._attach_session()
        return tab

    @classmethod
    def adopt(cls, target_id, *, registry=None, created=None, owner=None):
        registry = registry or default_registry()
        created = created or default_registry("created")
        if registry.has(target_id):
            return cls.attach(target_id, registry=registry)
        info = _call("Target.getTargetInfo", targetId=target_id).get("targetInfo", {})  # BrowserGone if missing
        if not created.has(target_id):
            if is_blank_url(info.get("url")):
                raise NotOwned("refusing a blank/new-tab page jev-browse did not create (it may be the user's); "
                               "use jev_open")
            raise NotOwned("not created by new_tab; use jev_open")
        registry.add(target_id, owner=owner or config.owner_tag(), sessions={}, adopted=True)
        tab = cls(target_id, registry)
        tab._ensure_keeper()
        tab._attach_session()
        return tab

    def _ensure_keeper(self):
        """Probe the recorded keeper; re-attach one (focus emulation only, no Page) if it is gone."""
        entry = self.registry.get(self.target_id) or {}
        keeper = entry.get("keeper")
        if keeper:
            try:
                _call("Emulation.setFocusEmulationEnabled", session_id=keeper, enabled=True)
                self.keeper = keeper
                return keeper
            except BrowserGone:
                pass

        def _reattach(targets):
            sid = _call("Target.attachToTarget", targetId=self.target_id, flatten=True)["sessionId"]
            _call("Emulation.setFocusEmulationEnabled", session_id=sid, enabled=True)
            targets.setdefault(self.target_id, {"created_at": time.time(), "sessions": {}})["keeper"] = sid
            return sid

        self.keeper = self.registry.update(_reattach)
        return self.keeper

    def _attach_session(self):
        sid = _call("Target.attachToTarget", targetId=self.target_id, flatten=True)["sessionId"]
        self.session = sid
        _call("Page.enable", session_id=sid)

        def _rec(targets):
            entry = targets.setdefault(self.target_id, {"created_at": time.time(), "sessions": {}})
            entry.setdefault("sessions", {})[sid] = {"pid": os.getpid(), "dialog": False}
        self.registry.update(_rec)
        if self not in _open_tabs:
            _open_tabs.append(self)
        return sid

    def _hygiene(self):
        """Detach per-call sessions left by dead processes; a kept dialog session only once its dialog cleared."""
        entry = self.registry.get(self.target_id) or {}
        stale = []
        dialog_now = None
        for sid, info in (entry.get("sessions") or {}).items():
            if info.get("dialog"):
                if dialog_now is None:
                    try:
                        dialog_now = harness_api.pending_dialog() or False
                    except Exception:
                        dialog_now = False
                if not dialog_now:
                    stale.append(sid)
            elif not _pid_alive(info.get("pid")):
                stale.append(sid)
        for sid in stale:
            with contextlib.suppress(Exception):
                _call("Target.detachFromTarget", sessionId=sid)
        if stale:
            def _drop(targets):
                for sid in stale:
                    targets.get(self.target_id, {}).get("sessions", {}).pop(sid, None)
            self.registry.update(_drop)

    def detach(self):
        """Detach this process's per-call session (never the keeper); a dialog session is kept for the caller."""
        sid = self.session
        if not sid:
            return
        self.session = None
        if self in _open_tabs:
            _open_tabs.remove(self)
        if self.keep_for_dialog:
            def _mark(targets):
                s = targets.get(self.target_id, {}).get("sessions", {}).get(sid)
                if s is not None:
                    s["dialog"] = True
            with contextlib.suppress(Exception):
                self.registry.update(_mark)
            return
        with contextlib.suppress(Exception):
            _call("Target.detachFromTarget", sessionId=sid)
        with contextlib.suppress(Exception):
            self.registry.update(lambda t: t.get(self.target_id, {}).get("sessions", {}).pop(sid, None))

    def close(self):
        entry = self.registry.get(self.target_id) or {}
        self.keep_for_dialog = False
        self.detach()
        for sid in list((entry.get("sessions") or {})) + [entry.get("keeper")]:
            if sid:
                with contextlib.suppress(Exception):
                    _call("Target.detachFromTarget", sessionId=sid)
        with contextlib.suppress(BrowserGone):
            _call("Target.closeTarget", targetId=self.target_id)
        self.registry.remove(self.target_id)

    # ---- page calls ------------------------------------------------------------------------------------
    def call(self, method, timeout=5.0, during_act=False, **params):
        if not self.session:
            raise BrowserGone("no attached session", self.target_id)
        return _call(method, session_id=self.session, timeout=timeout, during_act=during_act, **params)

    def evaluate(self, expression, timeout=5.0, await_promise=False, during_act=False):
        response = self.call("Runtime.evaluate", timeout=timeout, during_act=during_act, expression=expression,
                             returnByValue=True, awaitPromise=await_promise)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def target_url(self):
        return _call("Target.getTargetInfo", targetId=self.target_id)["targetInfo"].get("url", "")

    def navigate(self, url, deadline=None):
        remaining = LOAD_TIMEOUT if deadline is None else max(0.5, min(LOAD_TIMEOUT, deadline - time.monotonic()))
        try:
            result = self.call("Page.navigate", timeout=remaining, url=url)
        except DialogSuspected:
            if harness_api.pending_dialog():
                raise
            raise BrowserGone("navigation timed out", self.target_id) from None
        if result.get("errorText"):
            raise BrowserGone(f"navigation failed: {result['errorText']}", self.target_id)
        end = time.monotonic() + remaining
        while time.monotonic() < end:
            try:
                if self.evaluate("document.readyState", timeout=min(5.0, max(0.5, end - time.monotonic()))) \
                        == "complete":
                    return
            except StalePage:
                pass
            time.sleep(0.05)
        raise BrowserGone("page load timed out", self.target_id)

    def observe(self, *, offscreen=False, lines=False, screenshot=False, deadline=None):
        if self.after_input:
            action, self.after_input = self.after_input, None
            self._settle(action)
        timeout = SNAPSHOT_TIMEOUT if deadline is None else max(0.5, min(SNAPSHOT_TIMEOUT, deadline - time.monotonic()))
        for attempt in range(10):
            try:
                info = self.evaluate(snapshot_expression(offscreen, lines), timeout=timeout)
                if info is None:
                    raise StalePage("Document is navigating")
                info["fingerprint"] = fingerprint(info)
                info["target_id"] = self.target_id
                if screenshot:
                    info["screenshot"] = self.call("Page.captureScreenshot", timeout=SCREENSHOT_TIMEOUT,
                                                   format="jpeg", quality=72)["data"]
                return info
            except StalePage:
                if attempt == 9 or (deadline is not None and time.monotonic() >= deadline):
                    raise
                # A navigation can take a second or more: back off (0.05 s doubling, capped at 1 s; ~5.5 s total).
                time.sleep(min(1.0, 0.05 * 2 ** attempt))
        raise StalePage("Page did not settle")

    def _settle(self, action):
        # Ported post-input wait: 2 frames, or up to 200 ms for autocomplete options after typing into a combobox.
        expression = """(action => new Promise(resolve => {
          const field=window.__jevFast?.nodes.get(action.node);
          const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
          let frames=0, stopped=false;
          const finish=()=>{stopped=true;resolve()};
          setTimeout(finish,autocomplete ? 200 : 50);
          const ready=()=>{
            if (stopped) return;
            const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
              .split(/\\s+/).filter(Boolean);
            const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
            const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
            if (++frames>=2 && (!autocomplete || options.some(e=>{
              const r=e.getBoundingClientRect();
              return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
            }))) finish();
            else requestAnimationFrame(ready);
          };
          requestAnimationFrame(ready);
        }))(""" + json.dumps({"node": action.get("node"), "kind": action.get("kind")}) + ")"
        with contextlib.suppress(StalePage, RuntimeError):
            self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)

    def fresh(self, page, action=None):
        if action is not None and action.get("kind") in {"click", "select"}:
            node = action.get("node")
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(marker_expression()) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
            return {"executed": action["id"]}
        result = self._operate(action, page, text)
        self.after_input = action
        return result

    def _operate(self, action, page, text):
        kind = action["kind"]

        def input_call(method, **params):
            return self.call(method, during_act=True, **params)

        if kind == "scroll":
            input_call("Input.dispatchMouseEvent", type="mouseWheel", x=page.get("w", 800) / 2,
                       y=page.get("h", 600) / 2, deltaX=0, deltaY=action["delta"])
            return {"executed": action["id"]}
        if type(action.get("node")) is not int:
            raise ValueError("Invalid observed node")
        # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
        response = self.call("Runtime.evaluate", during_act=True, returnByValue=True, expression="""(action => {
          const e=window.__jevFast?.nodes.get(action.node);
          if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
              !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
          if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
          const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
          if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
          if (!e.contains(document.elementFromPoint(x,y))) return null;
          if (action.kind==='select') {
            if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                !o.disabled && !o.closest('optgroup[disabled]'))) return null;
            e.value=action.value;
            e.dispatchEvent(new Event('input',{bubbles:true}));
            e.dispatchEvent(new Event('change',{bubbles:true}));
          }
          return {x,y};
        })(""" + json.dumps({"node": action["node"], "kind": kind, "value": action.get("value")}) + ")")
        if response.get("exceptionDetails"):
            if kind == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        target = response.get("result", {}).get("value")
        if target is None:
            if kind == "select":
                raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
            raise StalePage("Target changed or is covered. Observe again.")
        if kind == "select":
            return {"executed": action["id"]}
        x, y = target["x"], target["y"]
        for event in ("mousePressed", "mouseReleased"):
            input_call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
        if kind == "fill":
            modifiers = 4 if sys.platform == "darwin" else 2
            input_call("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA", modifiers=modifiers,
                       commands=["selectAll"])
            input_call("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", modifiers=modifiers)
            input_call("Input.insertText", text=text or "")
        return {"executed": action["id"], "x": x, "y": y}

    # ---- interactive support ---------------------------------------------------------------------------
    def resolve_node(self, node, scroll=True):
        """Re-resolve a code-owned id; scroll it into view only if off-screen; hit-test at its fresh centre."""
        if type(node) is not int:
            return {"state": "stale"}
        flag = "true" if scroll else "false"
        value = self.evaluate(f"(() => window.__jevFast?.resolve ? window.__jevFast.resolve({node}, {flag}) : null)()")
        return value or {"state": "stale"}

    def click_at(self, x, y):
        for event in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", during_act=True, type=event, x=x, y=y, button="left", clickCount=1)
        self.after_input = {"node": None, "kind": "click"}

    def screenshot(self, path):
        data = self.call("Page.captureScreenshot", timeout=SCREENSHOT_TIMEOUT, format="png")["data"]
        import base64

        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(data))
        return str(path)

    # ---- events ----------------------------------------------------------------------------------------
    def popups(self):
        """New targets opened by this tab (openerId), registered as owned."""
        infos = live_targets()
        found = []
        for info in infos:
            if info.get("openerId") == self.target_id and info.get("type", "page") == "page" \
                    and info["targetId"] not in self.known_popups:
                self.known_popups.add(info["targetId"])
                if not self.registry.has(info["targetId"]):
                    self.registry.add(info["targetId"], owner=config.owner_tag(), sessions={},
                                      opener=self.target_id)
                found.append(info["targetId"])
        return found

    def snapshot_popups(self):
        self.known_popups |= {i["targetId"] for i in live_targets() if i.get("openerId") == self.target_id}

    def dialog_open(self):
        """Confirm a suspected dialog through the daemon's pending_dialog slot, matched to owned targets by URL."""
        dialog = harness_api.pending_dialog()
        if not dialog:
            return None
        infos = live_targets()
        owned = self.registry.entries()
        matches = [i["targetId"] for i in infos if i["targetId"] in owned and i.get("url") == dialog.get("url")]
        info = {"type": dialog.get("type"), "message": (dialog.get("message") or "")[:300], "url": dialog.get("url")}
        if len(matches) > 1:
            return {**info, "ambiguous_targets": matches}
        if matches == [self.target_id]:
            self.keep_for_dialog = True
            return {**info, "session_id": self.session}
        return {**info, "suspected": True}
