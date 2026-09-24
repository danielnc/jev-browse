# Ported from browser-use/jev-ultrafast scripts/check_guards.py (MIT). See NOTICE.
"""Live browser freshness/execution/guard checks on an owned background tab. No model calls, no external sites.

Run: PYTHONPATH=<checkout> BH_TELEMETRY=0 browser-harness < scripts/check_guards.py
(browser-harness execs stdin in its own module, so main() is called at top level.)
"""

import json
import time
from urllib.parse import quote

from jev_browse import harness_api
from jev_browse.tab import DialogSuspected, OwnedTab, StalePage

HTML = """<!doctype html><title>Guard checks</title>
<style>body{margin:30px}button{width:180px;height:50px}#outside{position:absolute;top:3000px}</style>
<p id="context">Cart total: $10</p>
<button id="target" onclick="window.clicks=(window.clicks||0)+1">Continue</button>
<label>City<input id="field" value="Zurich"></label>
<label><input id="toggle" type="checkbox">Refundable</label>
<select aria-label="Category"><option>All</option><option>Design</option></select>
<p id="outside">Unrelated offscreen text</p>"""


def data_url(html):
    return "data:text/html," + quote(html)


def main():
    browser = OwnedTab.create(data_url(HTML))
    passed, notes = [], {}
    try:
        page = browser.observe()
        action = next(a for a in page["actions"] if a["label"] == "Continue")
        browser.evaluate("document.querySelector('#target').style.transform='translateX(200px)'")
        assert browser.fresh(page), "Movement should use fresh geometry, not another model call"
        browser.act(action, page)
        assert browser.evaluate("window.clicks") == 1
        passed.append("moving target clicked at its current location")

        browser.evaluate("document.querySelector('#outside').textContent='Updated outside the viewport'")
        assert browser.fresh(page)
        passed.append("unrelated offscreen text does not invalidate")

        mutations = {
            "visible context": "document.querySelector('#context').textContent='Cart total: $100'",
            "accessible label": "document.querySelector('#target').setAttribute('aria-label','Delete account')",
            "field property": "document.querySelector('#field').value='London'",
            "checkbox property": "document.querySelector('#toggle').checked=true",
            "disabled target": "document.querySelector('#target').disabled=true",
            "read-only field": "document.querySelector('#field').readOnly=true",
            "hidden target": "document.querySelector('#target').style.display='none'",
            "replaced node": "document.querySelector('#target').outerHTML=document.querySelector('#target').outerHTML",
            "dropdown option": "document.querySelector('select').options[1].text='Coastal'",
        }
        for label, expression in mutations.items():
            browser.evaluate("document.querySelector('#target').style.display='block'; "
                             "document.querySelector('#target').disabled=false")
            page = browser.observe()
            browser.evaluate(expression)
            assert not browser.fresh(page), label
            passed.append(label + " invalidates")

        browser.evaluate("document.querySelector('#target').disabled=false; "
                         "document.querySelector('#target').style.display='block'")
        page = browser.observe()
        action = next(a for a in page["actions"] if a["label"] == "Delete account")
        browser.evaluate("const cover=document.createElement('div'); "
                         "cover.style.cssText='position:fixed;inset:0;z-index:9999;background:white'; "
                         "document.body.append(cover)")
        assert browser.fresh(page)
        try:
            browser.act(action, page)
        except (RuntimeError, StalePage):
            pass
        else:
            raise AssertionError("Covered target was clicked")
        assert browser.evaluate("window.clicks") == 1
        passed.append("overlay blocked before input")

        browser.evaluate("document.body.innerHTML=" + json.dumps("""
          <form><p id="price">Total $10</p>
          <button type="button" id="buy">Buy</button>
          <label>Search <input id="query" role="combobox" aria-controls="suggestions"></label>
          <div role="listbox" id="suggestions"></div>
          <label><input id="check" type="checkbox">Enabled</label>
          <label><input id="radio" type="radio">Choice</label>
          <input id="readonly" aria-label="Read only" readonly>
          <input id="secret" type="password" value="never expose this">
          <button id="off" disabled>Disabled</button>
          <select id="category" aria-label="Category">
            <option>All</option><option>Design</option><option disabled>Unavailable</option>
          </select></form><aside id="unrelated">News</aside>
        """))
        page = browser.observe()
        buy = next(a for a in page["actions"] if a["label"] == "Buy")
        browser.evaluate("document.querySelector('#unrelated').textContent='New unrelated news'")
        assert browser.fresh(page, buy)
        assert not browser.fresh(page)
        passed.append("click guard accepts unrelated visible updates; terminal guard rejects them")
        for label, expression in {
            "nearby price": "document.querySelector('#price').textContent='Total $100'",
            "form value": "document.querySelector('#query').value='changed'",
            "form toggle": "document.querySelector('#check').checked=true",
            "target replacement": "document.querySelector('#buy').outerHTML=document.querySelector('#buy').outerHTML",
        }.items():
            page = browser.observe()
            buy = next(a for a in page["actions"] if a["label"] == "Buy")
            browser.evaluate(expression)
            assert not browser.fresh(page, buy), label
            passed.append(label + " invalidates action-specific guard")

        page = browser.observe()
        actions = page["actions"]
        for role in ("checkbox", "radio"):
            assert {a["kind"] for a in actions if a.get("role") == role} == {"click"}
        assert {a["kind"] for a in actions if a["label"] == "Read only"} == {"click"}
        assert "never expose this" not in json.dumps(page)
        assert not any(a["label"] == "Disabled" for a in actions)
        assert [a["value"] for a in actions if a["kind"] == "select"] == ["Design"]
        passed.append("native controls expose only supported operations and safe values")

        select = next(a for a in actions if a["kind"] == "select")
        browser.act(select, page)
        assert browser.evaluate("document.querySelector('#category').value") == "Design"
        passed.append("native dropdown selects an observed option")

        browser.evaluate("document.querySelector('#query').addEventListener('input',()=>setTimeout(()=>{"
                         "document.querySelector('#suggestions').innerHTML='<div role=option>Generated</div>'"
                         "},60))")
        page = browser.observe()
        field = next(a for a in page["actions"] if a["kind"] == "fill" and not a.get("handback_only"))
        browser.act(field, page, text="Generated")
        page = browser.observe()
        value = browser.evaluate("document.querySelector('#query').value")
        assert value == "Generated", repr(value)
        assert any(a.get("role") == "option" for a in page["actions"])
        passed.append("real text input waits for asynchronous combobox suggestions")

        # --- jev-browse extensions -------------------------------------------------------------------------
        browser.evaluate("document.body.innerHTML=" + json.dumps("""
          <form><label>Card number <input id="cc" autocomplete="cc-number" value="4111111111111111"></label>
          <label>Code <input id="otp" autocomplete="one-time-code"></label>
          <label>Email <input id="em" type="email" value="person@example.test"></label>
          <label for="up">Upload receipt</label><input id="up" type="file" style="display:none"></form>"""))
        page = browser.observe()
        payload = json.dumps(page)
        assert "4111111111111111" not in payload
        passed.append("1. planted cc-number value appears nowhere in the evaluate payload")
        assert page["surfaces"]["otp_fields"] >= 1
        assert any(f["sensitive"] for f in page["fields"] if f["id_attr"] == "otp")
        passed.append("2. OTP input flagged (surfaces.otp_fields, sensitive)")
        assert page["surfaces"]["file_inputs"]["hidden"] == 1
        assert any(a["upload_trigger"] for a in page["actions"] if a["label"] == "Upload receipt")
        passed.append("4. hidden file input counted; its <label for> is an upload_trigger")
        assert any(f["personal"] for f in page["fields"] if f["id_attr"] == "em")
        passed.append("email field classified personal")

        links = "".join(f'<a href="#l{i}">Link {i}</a> ' for i in range(600))
        browser.evaluate("document.body.innerHTML=" + json.dumps(f"<main><h1>Many</h1>{links}</main>"))
        page = browser.observe(offscreen=True)
        link_actions = [a for a in page["actions"] if a.get("role") == "link"]
        assert len(link_actions) == 600, len(link_actions)
        assert len({a["region"] for a in link_actions}) == 1 and page["regions"][link_actions[0]["region"]]["tag"] == "main"
        passed.append("3. a 600-link <main> returns 600 actions with regions")

        # 5a. file-input activation flag (a synthetic click never opens a picker)
        browser.evaluate("document.body.innerHTML='<input id=f type=file>'; "
                         "document.getElementById('f').dispatchEvent(new MouseEvent('click',{bubbles:true}))")
        page = browser.observe()
        notes["file_input_activation_flag"] = bool(page["file_activated"])
        # 5b + 6. dialog from a click: Input.dispatchMouseEvent blocks, pending_dialog records it
        browser.evaluate("document.body.innerHTML='<button id=c onclick=\"window.r=confirm(\\'Sure?\\')\" "
                         "style=\"width:200px;height:60px\">Ask</button>'")
        page = browser.observe()
        ask = next(a for a in page["actions"] if a["label"] == "Ask")
        started = time.monotonic()
        try:
            browser.act(ask, page)
            notes["confirm_click_times_out"] = False
        except DialogSuspected as exc:
            notes["confirm_click_times_out"] = exc.during_act
        notes["confirm_click_elapsed_s"] = round(time.monotonic() - started, 2)
        dialog = browser.dialog_open()
        notes["pending_dialog_seen_for_owned_session"] = bool(dialog and dialog.get("session_id"))
        if dialog and dialog.get("session_id"):
            harness_api.cdp("Page.handleJavaScriptDialog", session_id=dialog["session_id"], accept=False)
            time.sleep(0.2)
            notes["pending_dialog_clears_after_handling"] = harness_api.pending_dialog() is None
        browser.keep_for_dialog = False

        # 7. emulation after detach: the keeper keeps focus emulation alive
        tid = browser.target_id
        browser.detach()
        again = OwnedTab.attach(tid)
        notes["has_focus_after_detach"] = again.evaluate("document.hasFocus()")
        browser = again
        browser.call("Page.navigate", url="about:blank")
        passed.append("event paths recorded")
    finally:
        browser.close()
    print("\n".join(passed))
    print("NOTES " + json.dumps(notes))
    print(f"PASS: {len(passed)} browser guard checks; no model calls")


main()
