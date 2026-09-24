// Ported from browser-use/jev-ultrafast jev_ultrafast/snapshot.js (MIT). See NOTICE.
// Extended for jev-browse: sensitive/personal classification with salted SipHash digests, regions, surfaces,
// the full editable-field list, structural commit signals, unsalted row context, off-viewport candidates,
// visible-text lines, and a file-input activation flag. Evaluated as `(<this file>)(cfg)` in one synchronous
// Runtime.evaluate. cfg: {salt: hex32, sensitive: [folded patterns], personal_labels: [...],
// personal_ac: [...], personal_ac_prefixes: [...], offscreen: bool, lines: bool, cap: int}.
((cfg) => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids: new WeakMap(), nodes: new Map(), next: 1, fileActivated: false};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e, cache.next++);
    const id = cache.ids.get(e); cache.nodes.set(id, e); return id;
  };
  for (const [id, e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);

  // --- file-input activation backstop (installed once per document) ---
  if (!cache.fileHook) {
    cache.fileHook = true;
    const isFile = t => t && t.tagName === 'INPUT' && t.type === 'file';
    document.addEventListener('click', ev => {
      const t = ev.target;
      if (isFile(t) || (t?.tagName === 'LABEL' && isFile(t.control))) cache.fileActivated = true;
    }, true);
    const orig = HTMLInputElement.prototype.showPicker;
    if (orig) HTMLInputElement.prototype.showPicker = function (...a) {
      if (this.type === 'file') cache.fileActivated = true;
      return orig.apply(this, a);
    };
  }

  // --- SipHash-2-4 keyed digest (synchronous; BigInt) ---
  // <siphash>
  const M64 = (1n << 64n) - 1n;
  const rotl = (x, b) => ((x << BigInt(b)) | (x >> BigInt(64 - b))) & M64;
  const salt = String(cfg.salt || '').padEnd(32, '0');
  const k0 = BigInt('0x' + salt.slice(0, 16)), k1 = BigInt('0x' + salt.slice(16, 32));
  const siphash = str => {
    const bytes = new TextEncoder().encode(String(str));
    let v0 = 0x736f6d6570736575n ^ k0, v1 = 0x646f72616e646f6dn ^ k1;
    let v2 = 0x6c7967656e657261n ^ k0, v3 = 0x7465646279746573n ^ k1;
    const round = () => {
      v0 = (v0 + v1) & M64; v1 = rotl(v1, 13); v1 ^= v0; v0 = rotl(v0, 32);
      v2 = (v2 + v3) & M64; v3 = rotl(v3, 16); v3 ^= v2;
      v0 = (v0 + v3) & M64; v3 = rotl(v3, 21); v3 ^= v0;
      v2 = (v2 + v1) & M64; v1 = rotl(v1, 17); v1 ^= v2; v2 = rotl(v2, 32);
    };
    const len = bytes.length, end = len - (len % 8);
    for (let i = 0; i < end; i += 8) {
      let m = 0n;
      for (let j = 7; j >= 0; j--) m = (m << 8n) | BigInt(bytes[i + j]);
      v3 ^= m; round(); round(); v0 ^= m;
    }
    let b = BigInt(len & 0xff) << 56n;
    for (let j = len - 1; j >= end; j--) b |= BigInt(bytes[j]) << BigInt(8 * (j - end));
    v3 ^= b; round(); round(); v0 ^= b;
    v2 ^= 0xffn; round(); round(); round(); round();
    return ((v0 ^ v1 ^ v2 ^ v3) & M64).toString(16).padStart(16, '0');
  };
  // </siphash>
  // Unsalted deterministic FNV-1a for row context, so references compare equal across processes.
  const fnv = str => {
    let h = 0x811c9dc5;
    for (const ch of String(str)) { h ^= ch.codePointAt(0); h = Math.imul(h, 0x01000193) >>> 0; }
    return h.toString(16).padStart(8, '0');
  };

  // --- folding and whole-word matching, mirroring jev_browse.textnorm ---
  const fold = s => String(s || '').normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .replace(/[‘’]/g, "'").toLowerCase().replace(/\s+/g, ' ').trim();
  const words = s => fold(s).match(/[0-9a-z]+(?:'[0-9a-z]+)?/g) || [];
  const wholeWord = (needle, hay) => {
    const n = words(needle), h = words(hay);
    if (!n.length || n.length > h.length) return false;
    for (let i = 0; i + n.length <= h.length; i++) if (n.every((w, j) => h[i + j] === w)) return true;
    return false;
  };

  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  const name = (e, seen = new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced = (e.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => name(document.getElementById(id), seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels || [])].map(l => name(l, seen)).filter(Boolean).join(' ') ||
      (['button', 'submit', 'reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName === 'INPUT' ? '' : [...e.childNodes].map(n => n.nodeType === 3 ? n.textContent :
        n.nodeType === 1 && n.getAttribute('aria-hidden') !== 'true' ? name(n, seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles = ['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemradio',
    'option', 'gridcell', 'combobox', 'textbox', 'searchbox', 'spinbutton'];
  const selector = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],' +
    roles.map(role => '[role="' + role + '"]').join(',');
  const role = e => {
    if (e.tagName === 'LABEL') return 'button';
    const explicit = e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName === 'BUTTON' || e.tagName === 'SUMMARY') return 'button';
    if (e.tagName === 'A') return 'link';
    if (e.tagName === 'SELECT') return 'combobox';
    if (e.tagName === 'TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName === 'INPUT') {
      if (['checkbox', 'radio'].includes(e.type)) return e.type;
      if (['button', 'submit', 'reset', 'image'].includes(e.type)) return 'button';
      if (e.type === 'search') return 'searchbox';
      if (e.type === 'number') return 'spinbutton';
      if (['text', 'email', 'url', 'tel', 'date', 'datetime-local', 'month', 'week', 'time', ''].includes(e.type))
        return 'textbox';
    }
    return null;
  };

  // --- field classification ---
  const acTokens = e => fold(e.getAttribute('autocomplete') || '').split(' ').filter(Boolean);
  const classText = e => [name(e), e.getAttribute('name'), e.id, e.getAttribute('placeholder')]
    .filter(Boolean).join(' ').replace(/[_\-]+/g, ' ');
  const isSensitive = e => {
    if (e.tagName === 'INPUT' && ['password', 'file', 'hidden'].includes(e.type)) return true;
    const ac = acTokens(e);
    if (ac.some(t => t.startsWith('cc-') || ['one-time-code', 'current-password', 'new-password'].includes(t)))
      return true;
    const text = classText(e);
    return (cfg.sensitive || []).some(p => wholeWord(p, text));
  };
  const isPersonal = e => {
    const ac = acTokens(e);
    if (ac.some(t => (cfg.personal_ac || []).includes(t) ||
        (cfg.personal_ac_prefixes || []).some(p => t.startsWith(p)))) return true;
    if (e.tagName === 'INPUT' && ['email', 'tel'].includes(e.type)) return true;
    const label = [name(e), e.getAttribute('name'), e.id].filter(Boolean).join(' ').replace(/[_\-]+/g, ' ');
    return (cfg.personal_labels || []).some(p => wholeWord(p, label));
  };
  const editableEl = e => ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName) || e.isContentEditable;
  const valueOf = e => 'value' in e ? String(e.value) : e.isContentEditable ? e.innerText.trim() : '';
  // Sensitive and personal values never leave the page raw in guards: only a salted digest.
  const safeValue = e => {
    if (!editableEl(e)) return e.value ?? null;
    if (isSensitive(e) || isPersonal(e)) return 'd:' + siphash(valueOf(e));
    return e.value ?? null;
  };

  // --- regions ---
  const regionSel = 'main,nav,aside,header,footer,section,form,article,ul,ol,table,dialog,' +
    '[role="main"],[role="navigation"],[role="region"],[role="search"],[role="dialog"],[role="alertdialog"],' +
    '[role="list"],[role="grid"],[role="listbox"],[role="menu"],[role="tablist"]';
  const headingOf = r => {
    if (!r) return '';
    const labelled = r.getAttribute('aria-label') ||
      (r.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.innerText || '')
        .join(' ').trim();
    if (labelled) return labelled.slice(0, 120);
    const h = r.querySelector('h1,h2,h3,h4,h5,h6,legend,caption,[role="heading"]');
    return (h?.innerText || '').trim().slice(0, 120);
  };
  const regions = {};
  const regionOf = e => {
    const r = e.parentElement?.closest(regionSel);
    if (!r) return 'r0';
    const id = 'r' + identity(r);
    if (!regions[id]) regions[id] = {heading: headingOf(r), tag: r.tagName.toLowerCase(), first_labels: []};
    return id;
  };
  if (!regions.r0) regions.r0 = {heading: document.title.slice(0, 120), tag: 'body', first_labels: []};
  const headingCache = new Map();
  const precedingHeading = e => {
    // Nearest heading before e in document order, within 400 elements; used to split dense regions.
    let n = e, steps = 0;
    while (n && steps++ < 400) {
      if (n.previousElementSibling) {
        n = n.previousElementSibling;
        while (n.lastElementChild && steps++ < 400) n = n.lastElementChild;
      } else n = n.parentElement;
      if (!n) break;
      if (headingCache.has(n)) return headingCache.get(n);
      if (/^H[1-6]$/.test(n.tagName) || n.getAttribute?.('role') === 'heading') {
        const t = n.innerText.trim().slice(0, 120); headingCache.set(n, t); return t;
      }
    }
    return '';
  };
  const rowText = e => {
    const row = e.closest('li,tr,[role="row"],[role="listitem"],article');
    return row ? row.innerText.trim().replace(/\s+/g, ' ').slice(0, 300) : '';
  };
  const dialogOf = e => e.closest('dialog,[role="dialog"],[role="alertdialog"]');
  const contextOf = e => {
    const scope = dialogOf(e) || e.closest('form') || e.closest('section,article,[role="region"]');
    if (!scope) return '';
    const heading = headingOf(scope);
    const clone = scope.innerText.replace(/\s+/g, ' ').trim();
    return (heading && !clone.startsWith(heading) ? heading + '. ' : '') + clone.slice(0, 400);
  };
  // Only user-facing fields count: a hidden input is form plumbing, not data the user entered.
  const formHas = (form, pred) => !!form &&
    [...form.querySelectorAll('input,textarea,select')].some(f => f.type !== 'hidden' && pred(f));
  const fileInputFor = e => {
    if (e.tagName === 'INPUT' && e.type === 'file') return true;
    if (e.tagName === 'LABEL' && e.control?.type === 'file') return true;
    const lbl = e.closest('label');
    if (lbl?.control?.type === 'file') return true;
    return !!e.querySelector?.('input[type="file"]');
  };

  // --- page key, guard, marker (ported; sensitive/personal values digested) ---
  const allFields = () => [...document.querySelectorAll('input,textarea,select')]
    .filter(e => !(e.tagName === 'INPUT' && ['hidden'].includes(e.type)));
  cache.pageKey = () => [performance.timeOrigin, location.href, scrollX, scrollY, innerWidth, innerHeight,
    allFields().map(e => [identity(e), safeValue(e), e.checked, e.selectedIndex, e.disabled, e.readOnly])];
  cache.guard = e => {
    if (!e?.isConnected || !visible(e)) return null;
    const scope = e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e), role(e), name(e), safeValue(e), e.checked ?? null, e.selectedIndex ?? null,
      e.readOnly ?? null, e.matches(':disabled'), e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'), e.getAttribute('aria-checked'), e.getAttribute('aria-selected'),
      e.getAttribute('href'), scope?.innerText?.slice(0, 6000) || ''];
  };
  cache.ctx = e => fnv(fold(headingOf(e.parentElement?.closest(regionSel)) + '|' + rowText(e)));
  cache.label = e => name(e) || role(e) || '';
  cache.resolve = (node, scroll = true) => {
    // For jev_find/jev_click: re-resolve a code-owned id, scroll it to the centre only if it is off-screen,
    // then hit-test it at its fresh centre.
    const e = cache.nodes.get(node);
    if (!e?.isConnected) return {state: 'stale'};
    if (!visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]'))
      return {state: 'stale', why: 'hidden_or_disabled'};
    let r = e.getBoundingClientRect();
    const inView = r.x + r.width / 2 >= 0 && r.y + r.height / 2 >= 0 &&
      r.x + r.width / 2 < innerWidth && r.y + r.height / 2 < innerHeight;
    if (!inView && scroll) { e.scrollIntoView({block: 'center', inline: 'center'}); r = e.getBoundingClientRect(); }
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight)
      return {state: 'stale', why: 'offscreen'};
    const hit = document.elementFromPoint(x, y);
    const form = e.closest('form');
    return {
      state: e.contains(hit) ? 'ok' : 'occluded', x, y, rect: {x: r.x, y: r.y, w: r.width, h: r.height},
      time_origin: String(performance.timeOrigin), label: name(e) || role(e) || '', ctx: cache.ctx(e),
      role: role(e), in_dialog: !!dialogOf(e), unnamed: !name(e),
      submit: (e.type === 'submit' || (e.tagName === 'BUTTON' && !e.getAttribute('type') && !!form)),
      form_sensitive: formHas(form, isSensitive), form_personal: formHas(form, isPersonal),
      context: contextOf(e), upload_trigger: fileInputFor(e),
    };
  };

  // --- actions ---
  const actions = [];
  const vw = innerWidth, vh = innerHeight;
  const counts = {unnamed: 0, total: 0};
  for (const e of document.querySelectorAll(selector + ',label')) {
    if (e.tagName === 'INPUT' && ['hidden'].includes(e.type)) continue;
    // A <label> is an action only when it opens a file input (an upload trigger the caller must handle).
    if (e.tagName === 'LABEL' && !(e.control?.type === 'file' && !e.querySelector(selector))) continue;
    if (!visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const isFileInput = e.tagName === 'INPUT' && e.type === 'file';
    const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2, rname = role(e);
    if (!rname && !isFileInput) continue;
    if (r.width <= 0 || r.height <= 0) continue;
    const inView = x >= 0 && y >= 0 && x < vw && y < vh;
    if (!inView && !cfg.offscreen) continue;
    if (rname === 'gridcell' && e.querySelector('button,[role="button"]')) continue;
    const sensitive = isSensitive(e), personal = !sensitive && editableEl(e) && isPersonal(e);
    const label = name(e) || rname || 'file';
    const form = e.closest('form');
    const base = {
      node: identity(e), role: rname || 'file', label: label.slice(0, 300),
      rect: {x: r.x, y: r.y, w: r.width, h: r.height}, offscreen: !inView, region: regionOf(e),
      heading: precedingHeading(e), sensitive, personal,
      placeholder: e.getAttribute('placeholder') || '', autocomplete: e.getAttribute('autocomplete') || '',
      name_attr: e.getAttribute('name') || '', id_attr: e.id || '', input_type: e.type || '',
      upload_trigger: fileInputFor(e), in_dialog: !!dialogOf(e), unnamed: !name(e),
      submit: (e.type === 'submit' || (e.tagName === 'BUTTON' && !e.getAttribute('type') && !!form)),
      form_sensitive: formHas(form, isSensitive), form_personal: formHas(form, isPersonal),
      ctx: cache.ctx(e),
    };
    if (['button', 'link'].includes(rname)) { counts.total++; if (!name(e)) counts.unnamed++; }
    for (const key of ['checked', 'selected', 'expanded']) {
      const value = e.getAttribute('aria-' + key);
      if (value !== null) base[key] = value;
    }
    if (['checkbox', 'radio'].includes(e.type)) base.checked = String(e.checked);
    if (e.tagName === 'SELECT') {
      base.option_count = [...e.options].filter(o => !o.selected && !o.disabled).length;
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base, kind: 'select', value: o.value,
          current_value: [...e.selectedOptions].map(o => o.label).join(', '), label: base.label + ' → ' + o.label});
    } else if (isFileInput || (e.tagName === 'INPUT' && e.type === 'password')) {
      // Never an executable target: offered to Python only as a hand-back-only field.
      actions.push({...base, kind: 'fill', value: '<redacted>', handback_only: true});
    } else {
      const editable = !e.readOnly && e.getAttribute('aria-readonly') !== 'true' &&
        (['textbox', 'searchbox', 'spinbutton'].includes(rname) ||
          (rname === 'combobox' && ['INPUT', 'TEXTAREA'].includes(e.tagName)));
      const raw = 'value' in e ? String(e.value) :
        e.isContentEditable || rname === 'combobox' ? e.innerText.trim() : '';
      const value = sensitive ? '<redacted>' : raw;
      actions.push({...base, kind: editable ? 'fill' : 'click', value, handback_only: editable && sensitive});
      if (editable) actions.push({...base, kind: 'click', value, label: 'Open ' + base.label});
    }
    if (actions.length >= (cfg.cap || 2000)) break;
  }
  for (const a of actions) {
    const reg = regions[a.region];
    if (reg && reg.first_labels.length < 3 && !reg.first_labels.includes(a.label)) reg.first_labels.push(a.label.slice(0, 60));
  }

  // --- all editable fields (for text_value_unavailable and evidence) ---
  const fields = [];
  for (const e of document.querySelectorAll('input,textarea,select,[contenteditable="true"]')) {
    if (e.tagName === 'INPUT' && ['hidden', 'checkbox', 'radio', 'button', 'submit', 'reset', 'image', 'file', 'range', 'color']
      .includes(e.type)) continue;
    const vis = visible(e);
    const sensitive = isSensitive(e), personal = !sensitive && isPersonal(e);
    const raw = valueOf(e);
    fields.push({
      node: identity(e), label: (name(e) || '').slice(0, 300), role: role(e) || 'textbox', visible: vis,
      name_attr: e.getAttribute('name') || '', id_attr: e.id || '', placeholder: e.getAttribute('placeholder') || '',
      input_type: e.type || '', autocomplete: e.getAttribute('autocomplete') || '', region: regionOf(e),
      heading: precedingHeading(e), sensitive, personal,
      value: sensitive ? '<redacted>' : raw, filled: raw.trim() !== '',
      readonly: !!e.readOnly || e.disabled,
    });
  }

  // --- surfaces ---
  const area = r => Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0)) *
    Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0)) / (vw * vh);
  const frames = [...document.querySelectorAll('iframe,frame')].map(f => {
    let origin = '';
    try { origin = new URL(f.src, location.href).origin; } catch (err) { origin = ''; }
    return {origin, src: (f.src || '').slice(0, 200), area_ratio: +area(f.getBoundingClientRect()).toFixed(3),
      visible: f.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})};
  });
  const shadowHosts = [];
  for (const h of document.querySelectorAll('*')) {
    if (!h.shadowRoot) continue;
    shadowHosts.push({area_ratio: +area(h.getBoundingClientRect()).toFixed(3),
      interactive: !!h.shadowRoot.querySelector(selector)});
    if (shadowHosts.length > 50) break;
  }
  let canvas = 0;
  for (const c of document.querySelectorAll('canvas,img[usemap],object,embed')) canvas += area(c.getBoundingClientRect());
  const fileInputs = [...document.querySelectorAll('input[type="file"]')];
  const surfaces = {
    frames, shadow_hosts: shadowHosts, canvas_area_ratio: +Math.min(1, canvas).toFixed(3),
    file_inputs: {visible: fileInputs.filter(visible).length, hidden: fileInputs.filter(f => !visible(f)).length},
    upload_triggers: actions.filter(a => a.upload_trigger).length,
    password_fields: [...document.querySelectorAll('input[type="password"]')].filter(visible).length,
    otp_fields: [...document.querySelectorAll('input')].filter(e => visible(e) &&
      (acTokens(e).includes('one-time-code') || (cfg.otp || []).some(p => wholeWord(p, classText(e))))).length,
    sensitive_fields: fields.filter(f => f.sensitive && f.visible).length,
    unnamed_icon_ratio: counts.total ? +(counts.unnamed / counts.total).toFixed(3) : 0,
  };

  // --- visible text (ported) and optional lines for jev_check ---
  const words_ = [], lines = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange(); let node, length = 0, lastBlock = null;
  const blockOf = el => el.closest('p,li,h1,h2,h3,h4,h5,h6,td,th,dt,dd,caption,figcaption,label,button,a,' +
    'blockquote,pre,summary,legend,[role="heading"],[role="cell"],[role="listitem"]') || el.parentElement || el;
  while ((node = walker.nextNode()) && length < 6000) {
    const value = node.textContent.trim(), parent = node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r = range.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw) {
      words_.push(value); length += value.length;
      if (cfg.lines) {
        const block = blockOf(parent);
        if (block === lastBlock && lines.length) lines[lines.length - 1] += ' ' + value;
        else lines.push(value);
        lastBlock = block;
      }
    }
  }
  const text = words_.join('\n').slice(0, 6000), height = document.documentElement.scrollHeight;
  const page_key = cache.pageKey(), guards = {};
  for (const a of actions) if (!(a.node in guards)) guards[a.node] = cache.guard(cache.nodes.get(a.node));
  const semantics = actions.filter(a => !a.offscreen).map(({rect, heading, ...action}) => action);
  const marker = [performance.timeOrigin, location.href, scrollX, scrollY, innerWidth, innerHeight,
    document.title, text, semantics, page_key[6]];
  const fileActivated = cache.fileActivated; cache.fileActivated = false;
  const omitted_actions = Math.max(0, document.querySelectorAll(selector).length - actions.length);
  actions.forEach((a, i) => a.id = 'e' + (i + 1));
  const delta = Math.round(innerHeight * 0.7);
  if (scrollY + innerHeight < height - 2) actions.push({id: 'scroll_down', kind: 'scroll', label: 'Scroll down', delta});
  if (scrollY > 0) actions.push({id: 'scroll_up', kind: 'scroll', label: 'Scroll up', delta: -delta});
  actions.push({id: 'wait', kind: 'wait', label: 'Wait for the page to update'});
  return {url: location.href, href: location.href, title: document.title, w: innerWidth, h: innerHeight, text,
    lines: cfg.lines ? lines.slice(0, 1000) : [], doc: String(performance.timeOrigin),
    scroll: {y: scrollY, height}, actions, fields, regions, surfaces, marker, page_key, guards,
    omitted_actions: actions.length >= (cfg.cap || 2000) ? omitted_actions : 0, file_activated: fileActivated};
})
