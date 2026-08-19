/* The phone view's client. One socket, no navigations, no sentences of its own.
 *
 * Everything textual arrives rendered — the wireframe fragment, the relay
 * sheet, macro step text — because a client that assembles sentences owns a
 * copy of the message catalog, and that ends with a page half in one language.
 * What this file owns is choreography: which view is showing, when the loading
 * overlay is up, and where a tap goes.
 *
 * A tap posts the element's identity from the frame she is looking at, exactly
 * as the dashboard overlay does, and the server refuses if the screen moved.
 * The wireframe changes what she sees, not what a tap is allowed to do.
 */
(function () {
  const body = document.body;
  const i18n = window.APTLOG_APP || {};
  const wrap = () => document.getElementById('screenwrap');

  let socket = null;
  let backoff = 1000;
  let frameId = '';
  let frameImg = '';
  let lastScreenHtml = '';
  let awaitingMacro = false;
  let awaitingScan = false;
  let macroEndedAt = 0;
  let busyTimer = 0;
  let toastTimer = 0;

  // ------------------------------------------------------------------ helpers
  function toast(text) {
    const el = document.getElementById('toast');
    if (!el || !text) return;
    el.textContent = text;
    body.classList.add('toasting');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => body.classList.remove('toasting'), 2600);
  }

  function busy(text, ceiling) {
    const label = document.getElementById('busy-text');
    if (label) label.textContent = text || '';
    body.classList.add('busy');
    // A stuck overlay is worse than a missing one: whatever happens, the
    // screen underneath is still live and still hers. This is the ceiling,
    // not the plan — and a sign-in walk needs a taller one than a tap.
    clearTimeout(busyTimer);
    busyTimer = setTimeout(() => { pendingApp = ''; unbusy(); },
                           ceiling || 12000);
  }

  function unbusy() {
    clearTimeout(busyTimer);
    body.classList.remove('busy');
  }

  function view(name) {
    body.dataset.view = name;
    // Remembered across reloads: the shell reloads itself once after every
    // deploy (and the browser reloads a discarded tab whenever it likes),
    // and the page always boots on the picker — so mid-sign-in she was
    // kicked from the phone view back to the front page by an update she
    // never asked for. The view she chose survives the reload.
    try { sessionStorage.setItem('aptlog-view', name); } catch (e) { /* private mode */ }
  }

  // ---------------------------------------------------------------- wireframe
  let tapTimer = 0;
  let blockedCode = '';
  let isStale = false;

  // One word beside the dot, whichever state wins: red "Disconnected", amber
  // "Syncing…", green "Live". The connection story in a glance, never a
  // paragraph — the paragraph was the complaint.
  function statusLabel() {
    const label = document.getElementById('live-label');
    if (!label) return;
    if (body.classList.contains('offline')) {
      label.textContent = i18n.offlineShort || '';
    } else if (isStale) {
      label.textContent = i18n.syncing || '';
    } else {
      label.textContent = i18n.live || '';
    }
  }

  function tapping(on) {
    body.classList.toggle('tapping', on);
    clearTimeout(tapTimer);
    // A stuck shimmer is worse than a missing one; the ceiling, not the plan.
    if (on) tapTimer = setTimeout(() => tapping(false), 12000);
  }

  function bindWire() {
    const root = wrap();
    if (!root) return;
    // The read-the-whole-page offer: the phone scrolls its own screen end
    // to end and the reading arrives as a sheet when the walk finishes.
    const scan = root.querySelector('#btn-scan');
    if (scan) scan.addEventListener('click', () => {
      awaitingMacro = true;
      awaitingScan = true;
      busy(i18n.scanning || '', 60000);
      fetch('/macro', {
        method: 'POST',
        body: new URLSearchParams({ name: 'read_page' }),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        redirect: 'follow'
      }).catch(() => { awaitingMacro = false; awaitingScan = false; unbusy(); });
    });
    bindAims(root);
  }

  // Every element carrying a verified aim — a reflow control, or an app tab
  // lifted into the control bar — posts the same tap the overlay always did.
  function bindAims(root) {
    for (const el of root.querySelectorAll('[data-aim]')) {
      el.addEventListener('click', (ev) => {
        ev.preventDefault();
        if (!socket || socket.readyState !== 1) {
          toast(i18n.explainOffline || '');
          return;
        }
        let aim;
        try { aim = JSON.parse(el.dataset.aim); } catch (e) { return; }
        // The type bar exists for ONE moment: a screen asking for a
        // verification code. Offering it for every text field put a
        // "type here" prompt over the patients list's search box the
        // moment sign-in finished — read, fairly, as the OTP asking
        // again. A field on any other screen taps like anything else
        // (the phone focuses it); only a code-asking screen types.
        if (/EditText|AutoCompleteTextView|SearchView/.test(aim.cls || '')) {
          const page = (document.getElementById('screenwrap') || {})
            .textContent || '';
          if (/\bcode\b|c[oó]digo|verif/i.test(page)) {
            openTypeBar(aim, (el.textContent || '').trim());
            return;
          }
        }
        // No overlays and no sentences for a tap: the screen dims and
        // shimmers until its successor arrives, the way a native app treats
        // a moment of work as a state rather than an event.
        tapping(true);
        socket.send(JSON.stringify({ type: 'tap', frame: frameId, element: aim }));
      });
    }
  }

  // The type bar: one short plain code into the field she aimed at. Exists
  // for the OTP moment — a verification code lands on a family member's
  // phone and gets typed here. Letters and digits, capped short; the
  // server enforces the same and refuses if the field moved.
  let typeAim = null;
  function openTypeBar(aim, label) {
    typeAim = aim;
    const bar = document.getElementById('typebar');
    if (!bar) return;
    // The hint names the FIELD when the field names itself; the generic
    // sentence otherwise. The first wording said "the code" for every
    // field, and a search box tapped after sign-in read as the OTP
    // prompt coming back.
    const hint = bar.querySelector('.typehint');
    if (hint) hint.textContent = label || i18n.typeHint || '';
    bar.hidden = false;
    // The phone's own controls step back while she types: Cancel is the way
    // out of this bar, and it used to sit right on top of Home.
    body.classList.add('typing');
    const box = document.getElementById('typebox');
    box.value = '';
    // FOCUS SYNCHRONOUSLY, INSIDE THE TAP THAT OPENED THIS. iOS raises the
    // keyboard only for a focus() that happens while a user gesture is still
    // on the stack; from a setTimeout — where this used to be — the field
    // takes focus and draws its ring and NO KEYBOARD APPEARS. Reported from
    // the field as the bar rendering "grayed out": it looked active, it was
    // active, and there was no way to put a character in it. Reading the
    // layout first so the element is displayed before it is focused.
    void bar.offsetHeight;
    box.focus();
    // Belt and braces for anything that refuses a focus mid-gesture; harmless
    // where the line above already worked.
    setTimeout(() => { if (document.activeElement !== box) box.focus(); }, 50);
  }
  function closeTypeBar() {
    typeAim = null;
    const bar = document.getElementById('typebar');
    if (bar) { bar.hidden = true; }
    body.classList.remove('typing');
  }
  function sendTyped() {
    const box = document.getElementById('typebox');
    const value = (box.value || '').replace(/[^A-Za-z0-9]/g, '').slice(0, 32);
    if (!value || !typeAim || !socket || socket.readyState !== 1) return;
    tapping(true);
    socket.send(JSON.stringify({ type: 'text', frame: frameId,
                                 element: typeAim, value: value }));
    closeTypeBar();
  }

  // The app's own tab bar, lifted into the control bar. Rebuilt whenever the
  // screen changes: a page with no tab bar hides the strip entirely, so Back
  // and Home never share a crowded row with tabs that are not there.
  function renderAppTabs(tabs) {
    const strip = document.getElementById('apptabs');
    if (!strip) return;
    tabs = tabs || [];
    if (!tabs.length) { strip.hidden = true; strip.innerHTML = ''; return; }
    strip.innerHTML = '';
    for (const t of tabs) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'apptab' + (t.current ? ' current' : '');
      // The selected tab has no aim (Compose leaves it unclickable) — it is
      // lit as where-you-are, not a control. Every other tab posts a tap.
      if (t.aim) b.dataset.aim = JSON.stringify(t.aim);
      else b.setAttribute('aria-current', 'page');
      b.textContent = t.txt || '·';
      strip.appendChild(b);
    }
    strip.hidden = false;
    bindAims(strip);
  }

  let currentPackage = '';
  // The atlas's name for the page in front — 'home' marks an app's root,
  // where one more Back would exit to the Android launcher.
  let currentScreen = '';
  // Package -> display name, read off the tiles at bind time so this file
  // still owns no catalog of its own. Chrome is the one extra: it hosts
  // HHAeXchange+'s web sign-in form.
  const appNames = {};
  // Package -> its open-only macro, for bouncing an app back when a Back
  // press turns out to have exited it (see applyScreen).
  const appOpen = {};
  // When the last Back was sent: a launcher arriving right after one means
  // Back exited the app, which is never what leaving-by-Back should do.
  let backSentAt = 0;
  // The app a launch is waiting on. While set, the overlay is a single solid
  // state: sketch updates do not clear it, macro "done" does not clear it —
  // only the target app actually being in front (or failure, or the ceiling).
  let pendingApp = '';

  function applyScreen(meta, html) {
    // Belt to the server's braces: identical markup must never re-swap the
    // DOM — a swap costs a repaint she can see and the scroll position she
    // had. The server already de-duplicates; this holds if it ever slips.
    if (html !== undefined && html === lastScreenHtml) {
      html = undefined;
    }
    // A screen change retires the type bar: its aim belongs to the page
    // that had the field, and a bar lingering over the NEXT page read as
    // "it is asking for the code again" the moment a sign-in succeeded.
    if (html !== undefined && typeAim) closeTypeBar();
    if (html !== undefined) {
      lastScreenHtml = html;
      const root = wrap();
      if (root) {
        const previous = frameId;
        root.innerHTML = html;
        bindWire();
        const wire = root.querySelector('.wire');
        frameId = wire ? (wire.dataset.frame || '') : '';
        // Whether the peek's scroll arrows have anything to do. The phone is
        // the only thing that knows, and most screens fit whole at the tuned
        // density — so on those the arrows would be furniture sitting over
        // the phone's own controls.
        body.classList.toggle('scrolls',
                              !!wire && wire.dataset.scrolls === '1');
        // Entrance only when the screen actually changed — a checkbox flip
        // re-renders the list and must not re-run the animation under her.
        root.classList.remove('enter');
        if (frameId !== previous) {
          void root.offsetWidth;
          root.classList.add('enter');
        }
      }
      tapping(false);
      if (!pendingApp) unbusy();
    }
    if (!meta) return;
    const wasScreen = currentScreen;
    const wasPackage = currentPackage;
    currentPackage = meta.app || '';
    currentScreen = meta.name || '';

    // The app's own tabs ride the control bar. Hidden on the launcher (no
    // app in front) and while a launch overlay holds, so they never point
    // at a screen that is not there.
    renderAppTabs((meta.name === 'launcher' || pendingApp)
                  ? [] : meta.apptabs);

    // The moment a launch is truly over: the app she asked for is the app in
    // front, awake, with *its own screen* rendered — h_app is whose sketch
    // the page is showing, and until it catches up the content on screen is
    // the previous app's. Dropping the overlay on focus alone showed the
    // launcher's rows under the new app's title, "finished loading".
    if (pendingApp && currentPackage === pendingApp
        && (!meta.h_app || meta.h_app === pendingApp)
        && meta.blocked !== 'no_focus') {
      pendingApp = '';
      unbusy();
    }

    // The phone is on its own home screen: a card saying so, never a reflow
    // of the icon grid — and never under a care app's title. Arriving there
    // from an app (one Back too many, or the Home button) means she left
    // the app, and leaving an app here means the picker: flip to it once,
    // on the transition, so she is never stranded staring at a dead end.
    // A sideways screen (the signature moment) turns the peek upright.
    body.classList.toggle('sideways', !!meta.landscape);
    // The app-side Borrar/Salvar only exist while a signature canvas is
    // in front — the buttons press real pixels there and nothing else.
    const approw = document.getElementById('sign-approw');
    if (approw) approw.hidden = !meta.canvas;
    const onLauncher = meta.name === 'launcher';
    // While a macro is working — and for a grace period after it ends —
    // the launcher is not a destination, it is scenery: the sign-in path
    // restarts the app, and the phone crosses its home screen for a second
    // on the way. Ejecting her to the picker mid-"Signing in…" was watched
    // live. The flip only means something when nothing is in flight.
    const macroQuiet = !awaitingMacro && !pendingApp
      && Date.now() - macroEndedAt > 8000;
    if (onLauncher && wasScreen && wasScreen !== 'launcher' && macroQuiet) {
      // A launcher right after a Back means Back exited the app — mid-flow,
      // reported as "a bug for sure as an experience". Android kept the
      // app's state, so bounce it straight back: to her, that Back simply
      // did nothing, which beats being teleported to the picker. Any other
      // arrival (the Home button, the phone's own drift) means she left
      // the app, and leaving means the picker.
      const bounce = Date.now() - backSentAt < 3500 && appOpen[wasPackage];
      if (bounce && body.dataset.view === 'screen') {
        pendingApp = wasPackage;
        busy((i18n.opening || '').replace('{app}',
          appNames[wasPackage] || ''), 30000);
        fetch('/macro', {
          method: 'POST',
          body: new URLSearchParams({ name: appOpen[wasPackage] }),
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          redirect: 'follow'
        }).catch(() => { pendingApp = ''; unbusy(); });
      } else if (body.dataset.view === 'screen') {
        view('launcher');
      }
    }
    body.classList.toggle('offapp', onLauncher);

    // The large title names what is actually in front: the screen's own
    // nav-bar title, else the app the *phone* is showing — never the last
    // tile pressed, which is how the launcher got rendered under a header
    // still claiming HHAeXchange. Never one of the classifier's sentences
    // either — "a screen it did not recognise" in a title slot is exactly
    // the alarm this view exists not to raise.
    const where = document.getElementById('where');
    if (where) {
      const own = !onLauncher && wrap() && wrap().querySelector('.a-title');
      where.textContent = (own && own.textContent.trim())
        || (!onLauncher && appNames[currentPackage])
        || i18n.appTitle || '';
    }

    // No photograph of this screen: a quiet lock in the toolbar, whose
    // explanation is one tap away rather than a standing banner. no_focus is
    // different in kind — the display is off, the kept sketch describes a
    // screen nobody is being shown, and rendering it as current is a lie
    // with boxes. Truth plus the one useful act: Wake.
    blockedCode = meta.blocked || '';
    const asleep = blockedCode === 'no_focus';
    body.classList.toggle('asleep', asleep);
    body.classList.toggle('blocked', !!blockedCode && !asleep);
    if (blockedCode) body.classList.remove('peeking');
  }

  function applyFrame(frame) {
    // The photograph rides behind the wireframe, fetched only while she is
    // actually peeking at it.
    frameImg = frame.img || '';
    if (body.classList.contains('peeking')) refreshPeek();
  }

  function refreshPeek() {
    const img = document.getElementById('peek');
    if (img && frameImg) img.src = '/screen.jpg?f=' + encodeURIComponent(frameImg);
  }

  // ------------------------------------------------------------------- macros
  // Reflected whether or not this page started it: the controller signs in by
  // itself when the app lands on its login screen, and she should watch that
  // as "Signing in…" rather than wonder at a darkened page doing nothing.
  function applyMacro(m) {
    if (m.state === 'running') {
      busy(m.text || '', 45000);
      awaitingMacro = true;
    } else if (m.state === 'done') {
      // The walk finished, but the launch is over only when the app is in
      // front — until then the overlay holds with a quiet waiting line
      // instead of dropping to a half-drawn sketch and coming back.
      if (awaitingMacro && !pendingApp) unbusy();
      else if (pendingApp) busy(i18n.waiting || '', 20000);
      if (awaitingScan && m.name === 'read_page') {
        awaitingScan = false;
        fetch('/scan').then((r) => r.text()).then((html) => {
          const box = document.getElementById('scancontent');
          if (box) { box.innerHTML = html; body.classList.add('reading'); }
        }).catch(() => {});
      }
      awaitingMacro = false;
      macroEndedAt = Date.now();
    } else if (m.state === 'failed') {
      if (awaitingMacro || pendingApp) { unbusy(); toast(m.state_text || ''); }
      awaitingMacro = false;
      awaitingScan = false;
      macroEndedAt = Date.now();
      pendingApp = '';
    }
  }

  function launch(tile) {
    const name = tile.dataset.macro;
    if (!name) return;

    // The tile of the app already in front is a switch, not a ceremony. The
    // screen on the phone is signed in and live; running the sign-in walk
    // over it is fifteen seconds of state churn she can only read as "why is
    // it signing in again?"
    if (tile.dataset.package && tile.dataset.package === currentPackage) {
      view('screen');
      return;
    }

    awaitingMacro = true;
    pendingApp = tile.dataset.package || '';
    view('screen');
    busy((i18n.opening || '').replace('{app}', tile.dataset.name || ''), 45000);
    fetch('/macro', {
      method: 'POST',
      body: new URLSearchParams({ name: name }),
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      redirect: 'follow'
    }).catch(() => { awaitingMacro = false; unbusy(); toast(i18n.failed || ''); });
  }

  // ---------------------------------------------------------------- signature
  // A small pad of its own rather than a reuse of signature.js: that pad is
  // welded to the relay flow — it posts a nonce the controller issued. This
  // one is hers to start, posts to /sign, and the feed process replays the
  // strokes onto the app's canvas. The app's save button stays her tap.
  const pad = { strokes: [], current: null, ctx: null, waitingId: '' };

  function padCanvas() { return document.getElementById('signpad'); }

  function padPoint(ev, rect) {
    return [(ev.clientX - rect.left) / rect.width,
            (ev.clientY - rect.top) / rect.height];
  }

  function drawStrokes(ctx, c, place) {
    ctx.clearRect(0, 0, c.width, c.height);
    ctx.lineWidth = 2.5;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    // Ink follows the surface's own color, so a dark pad gets light ink —
    // the invisible-signature-in-dark-mode bug was a hard-coded near-black.
    ctx.strokeStyle = getComputedStyle(c).color;
    for (const s of pad.strokes) {
      ctx.beginPath();
      s.forEach((p, i) => {
        const [x, y] = place(p, c);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
  }

  function padRedraw() {
    const c = padCanvas();
    if (c && pad.ctx) {
      drawStrokes(pad.ctx, c, (p, cc) => [p[0] * cc.width, p[1] * cc.height]);
    }
    previewRedraw();
  }

  // How the strokes will land in the app's own box: the same mapping the
  // controller applies — a margin off the border, then the pad's rectangle
  // fitted uniformly and centred, so the shape she drew is the shape that
  // lands. This is the rehearsal for a screen there is no safe way to visit
  // outside a real clock-out.
  function previewRedraw() {
    const c = document.getElementById('signpreview');
    const padEl = padCanvas();
    if (!c || !padEl) return;
    const rect = c.getBoundingClientRect();
    if (rect.width && c.width !== Math.round(rect.width)) {
      c.width = Math.round(rect.width);
      c.height = Math.round(rect.height);
    }
    const ctx = c.getContext('2d');
    const inset = 0.06;
    const left = c.width * inset, top = c.height * inset;
    const w = c.width * (1 - 2 * inset), h = c.height * (1 - 2 * inset);
    const padRect = padEl.getBoundingClientRect();
    const aspect = padRect.height ? padRect.width / padRect.height : 2.2;
    const k = Math.min(w / aspect, h);
    const dw = k * aspect, dh = k;
    const ox = left + (w - dw) / 2, oy = top + (h - dh) / 2;
    drawStrokes(ctx, c, (p) => [ox + p[0] * dw, oy + p[1] * dh]);
  }

  function padWire() {
    const c = padCanvas();
    if (!c) return;
    pad.ctx = c.getContext('2d');
    c.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      c.setPointerCapture(ev.pointerId);
      pad.current = [padPoint(ev, c.getBoundingClientRect())];
      pad.strokes.push(pad.current);
    });
    c.addEventListener('pointermove', (ev) => {
      if (!pad.current) return;
      pad.current.push(padPoint(ev, c.getBoundingClientRect()));
      padRedraw();
    });
    const up = () => { pad.current = null; };
    c.addEventListener('pointerup', up);
    c.addEventListener('pointercancel', up);
  }

  function padFit() {
    // Backing store matches CSS pixels so strokes land where the finger is.
    const c = padCanvas();
    if (!c) return;
    const rect = c.getBoundingClientRect();
    if (rect.width && (c.width !== Math.round(rect.width))) {
      c.width = Math.round(rect.width);
      c.height = Math.round(rect.height);
      padRedraw();
    }
  }

  function padSend() {
    if (!pad.strokes.length) { toast(i18n.signEmpty || ''); return; }
    const c = padCanvas();
    const rect = c.getBoundingClientRect();
    body.classList.remove('signing');
    busy(i18n.signSending || '');
    fetch('/sign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ strokes: pad.strokes,
                             aspect: rect.width / rect.height })
    }).then(async (r) => {
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { unbusy(); toast(i18n.failed || ''); return; }
      pad.waitingId = out.id || '';
      // Cleared on the status push; nothing reusable is left behind.
      pad.strokes = []; padRedraw();
    }).catch(() => { unbusy(); toast(i18n.failed || ''); });
  }

  function applySign(s) {
    if (!pad.waitingId || s.id !== pad.waitingId) return;
    if (s.state === 'running') { busy(s.text || ''); return; }
    pad.waitingId = '';
    unbusy();
    // done or failed, the sentence arrives rendered; show it either way. On
    // done it says to check the screen and press the app's own save.
    toast(s.text || '');
  }

  // -------------------------------------------------------------------- relay
  function applyRelay(html, nonce) {
    if (window.APTLOG_DRAWING) return;   // never yank a half-drawn signature
    const panel = document.getElementById('relay');
    if (panel && html !== undefined) {
      panel.innerHTML = html;
      wireForms(panel);
    }
    body.classList.toggle('asking', !!nonce);
    body.dataset.relayNonce = nonce || '';
  }

  function wireForms(root) {
    for (const form of root.querySelectorAll('form[method="post"]')) {
      if (form.dataset.live) continue;
      // SOME FORMS HAVE TO NAVIGATE. This was written for the relay panel,
      // whose answers come back over the socket and so need no reload — but
      // it took every POST form on the page, including the language switch,
      // whose whole job is to reload the page in the other language. Posting
      // that one in the background changed the stored language and left every
      // word on screen as it was, which reads as a button that does nothing.
      if (form.dataset.navigate) continue;
      form.dataset.live = '1';
      // getAttribute, never form.action — a field named "action" shadows the
      // property. The dashboard learned this the hard way; see live.js.
      const target = form.getAttribute('action') || '';
      form.addEventListener('submit', (ev) => {
        ev.preventDefault();
        const data = new FormData(form);
        // A SUBMIT BUTTON'S name/value IS NOT IN FormData(form). That is the
        // spec, and it is why the language switch posted `next=/app` with no
        // `language` at all and the server answered 422 — the value lived on
        // the button that was pressed, which only the browser's own submit
        // carries. The language form navigates now and never reaches here,
        // but any form built the same way would have died the same way.
        const hit = ev.submitter;
        if (hit && hit.name && !data.has(hit.name)) {
          data.append(hit.name, hit.value);
        }
        fetch(target, {
          method: 'POST',
          body: new URLSearchParams(data),
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          redirect: 'follow'
        }).catch(() => { /* the socket shows the real state either way */ });
      });
    }
  }

  // ------------------------------------------------------------------- socket
  function connect() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(scheme + '://' + location.host + '/ws');
    socket.binaryType = 'arraybuffer';

    socket.addEventListener('open', () => {
      backoff = 1000;
      body.classList.remove('offline');
      statusLabel();
    });

    // The splash owns the moment until the first real state arrives; the
    // failsafe below lifts it regardless, because a launch screen that can
    // wedge is worse than none.
    const arrived = () => body.classList.add('ready');
    socket.addEventListener('message', arrived, { once: true });

    socket.addEventListener('message', (ev) => {
      if (ev.data instanceof ArrayBuffer) return;   // video is the other view's
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }

      if (msg.type === 'tap_result') {
        if (!msg.ok) {
          tapping(false);
          toast(msg.reason === 'stale' ? (i18n.moved || '') : (i18n.failed || ''));
        }
        return;
      }
      if (msg.type === 'text_result') {
        if (!msg.ok) {
          tapping(false);
          toast(msg.reason === 'stale' ? (i18n.moved || '') : (i18n.failed || ''));
        }
        return;
      }
      if (msg.type === 'device_result') {
        if (!msg.ok) { unbusy(); toast(i18n.devfail || ''); }
        return;
      }
      if (msg.type !== 'state') return;

      // A shell from a previous server is a stale skin rendering fresh
      // fragments — new markup, none of the new styles, and it reads as a
      // broken design rather than a cache. Reload once and become current.
      // The timestamp guard keeps a genuinely broken deploy from turning
      // this into a reload loop.
      if (msg.boot && body.dataset.boot && msg.boot !== body.dataset.boot) {
        const last = Number(sessionStorage.getItem('aptlog-reloaded') || 0);
        if (Date.now() - last > 30000) {
          sessionStorage.setItem('aptlog-reloaded', String(Date.now()));
          location.reload();
        }
        return;
      }

      if (msg.screen_html !== undefined || msg.screen) {
        applyScreen(msg.screen, msg.screen_html);
      }
      if (msg.screen_stale !== undefined) {
        // "Live" is a claim. When the document behind the sketch stops being
        // refreshed the page stops claiming it: the dot goes amber, the label
        // says syncing, and the sketch dims — stale, and saying so.
        isStale = !!msg.screen_stale;
        body.classList.toggle('stale', isStale);
        // No stale references anywhere: while the loading page stands in,
        // the old sketch's own page title must not stay in the header. The
        // app the phone is on is current truth; the page name is not.
        if (isStale) {
          const where = document.getElementById('where');
          if (where) {
            where.textContent = appNames[currentPackage]
              || i18n.appTitle || '';
          }
        }
        statusLabel();
      }
      if (msg.frame) applyFrame(msg.frame);
      if (msg.mirror) {
        // The photo carries its own age while she is peeking at it — a
        // 40-minute-old photograph passing as current was the incident.
        const cap = document.getElementById('peek-cap');
        if (cap) {
          cap.textContent = msg.mirror.taken_text || '';
          cap.hidden = !msg.mirror.taken_text;
          cap.classList.toggle('old', !!msg.mirror.stale);
        }
      }
      if (msg.macro) applyMacro(msg.macro);
      if (msg.sign) applySign(msg.sign);
      if (msg.relay_html !== undefined || msg.relay_nonce !== undefined) {
        applyRelay(msg.relay_html, msg.relay_nonce);
      }
    });

    socket.addEventListener('close', () => {
      body.classList.add('offline');
      statusLabel();
      unbusy();
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 15000);
    });
  }

  // -------------------------------------------------------------------- wire
  document.addEventListener('DOMContentLoaded', () => {
    // Where to open. `?view=screen` wins, because that is a caller ASKING for
    // the phone view rather than a browser remembering one — it is how the
    // code notification deep-links, and a notification that lands on the app
    // picker has failed at the one job it has. sessionStorage is the fallback
    // for a reload that interrupted her mid-screen; a window opened from a
    // notification is a fresh session and has none, which is exactly why the
    // notice used to arrive at the front page.
    let asked = '';
    try {
      asked = new URLSearchParams(location.search).get('view') || '';
    } catch (e) { /* no URL API */ }
    try {
      if (asked === 'screen' || sessionStorage.getItem('aptlog-view') === 'screen') {
        view('screen');
      }
    } catch (e) {
      if (asked === 'screen') view('screen');
    }

    const tsend = document.getElementById('typesend');
    const tcancel = document.getElementById('typecancel');
    const tbox = document.getElementById('typebox');
    if (tsend) tsend.addEventListener('click', sendTyped);
    if (tcancel) tcancel.addEventListener('click', closeTypeBar);
    if (tbox) tbox.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); sendTyped(); }
    });

    for (const tile of document.querySelectorAll('.tile')) {
      tile.addEventListener('click', () => launch(tile));
      if (tile.dataset.package) {
        appNames[tile.dataset.package] = tile.dataset.name || '';
        appOpen[tile.dataset.package] = tile.dataset.open || '';
      }
    }
    if (i18n.webTitle) appNames['com.android.chrome'] = i18n.webTitle;
    const apps = document.getElementById('btn-apps');
    if (apps) apps.addEventListener('click', () => view('launcher'));
    // Home moves both worlds at once — the owner's spec: the phone goes to
    // its own home screen AND the front end to the picker. The view change
    // never waits on the socket, so Home still works offline.
    const home = document.getElementById('btn-home');
    if (home) home.addEventListener('click', () => {
      view('launcher');
      if (socket && socket.readyState === 1) {
        socket.send(JSON.stringify({ type: 'device', action: 'home' }));
      }
    });
    const offapp = document.getElementById('offapp-apps');
    if (offapp) offapp.addEventListener('click', () => view('launcher'));
    const scanClose = document.getElementById('scan-close');
    if (scanClose) scanClose.addEventListener('click',
      () => body.classList.remove('reading'));

    // The dot, explained on demand: tap the status row and the current
    // colour says what it means — green/yellow/red each get a sentence.
    const sub = document.querySelector('.hdr-sub');
    if (sub) sub.addEventListener('click', () => {
      if (body.classList.contains('offline')) {
        toast(i18n.explainOffline || '');
      } else if (isStale) {
        toast(i18n.explainSyncing || '');
      } else {
        toast(i18n.explainLive || '');
      }
    });

    const peek = document.getElementById('btn-peek');
    if (peek) peek.addEventListener('click', () => {
      body.classList.toggle('peeking');
      if (body.classList.contains('peeking')) {
        // The overlay anchors to the top of the stage's content: a list
        // scrolled before opening the photo left the overlay half
        // off-screen, reflow bleeding through beneath it.
        const stage = document.getElementById('stage');
        if (stage) stage.scrollTop = 0;
        refreshPeek();
      }
    });

    const sign = document.getElementById('btn-sign');
    if (sign) sign.addEventListener('click', () => {
      body.classList.toggle('signing');
      padFit();
    });

    // The lock explains itself when asked and stays quiet otherwise.
    const title = document.querySelector('.p-nav .title');
    if (title) title.addEventListener('click', () => {
      if (!blockedCode) return;
      toast((i18n.blocked || {})[blockedCode] || i18n.blockedOther || '');
    });

    // The launcher's clock — the one part of a home screen that makes it feel
    // inhabited. Local time, hers.
    const clock = document.getElementById('clock');
    if (clock) {
      const tick = () => {
        clock.textContent = new Date().toLocaleTimeString([], {
          hour: 'numeric', minute: '2-digit' });
      };
      tick();
      setInterval(tick, 15000);
    }
    padWire();
    window.addEventListener('resize', padFit);
    const scheme = window.matchMedia('(prefers-color-scheme: dark)');
    if (scheme.addEventListener) scheme.addEventListener('change', padRedraw);
    const clear = document.getElementById('sign-clear');
    if (clear) clear.addEventListener('click', () => {
      pad.strokes = []; padRedraw();
    });
    const undo = document.getElementById('sign-undo');
    if (undo) undo.addEventListener('click', () => {
      pad.strokes.pop(); padRedraw();
    });
    const send = document.getElementById('sign-send');
    if (send) send.addEventListener('click', padSend);

    // The app's own Borrar/Salvar, relayed. The outcome rides the same
    // sign-status push the replay uses, so the toast comes back rendered
    // ("done" / "this screen has no signature box") without new plumbing.
    const appAction = (kind) => {
      busy(i18n.signSending || '');
      fetch('/sign-action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: kind })
      }).then(async (r) => {
        const out = await r.json().catch(() => ({}));
        if (!r.ok) { unbusy(); toast(i18n.failed || ''); return; }
        pad.waitingId = out.id || '';
      }).catch(() => { unbusy(); toast(i18n.failed || ''); });
    };
    const appClear = document.getElementById('app-clear');
    if (appClear) appClear.addEventListener('click', () => appAction('clear'));
    const appSave = document.getElementById('app-save');
    if (appSave) appSave.addEventListener('click', () => appAction('confirm'));

    for (const btn of document.querySelectorAll('[data-act]')) {
      btn.addEventListener('click', () => {
        // Back is the way out of a page she did not mean to be on, so a
        // Back that quietly does nothing is the worst of the offline
        // failures: she presses, the screen does not move, and the portal
        // looks broken rather than disconnected. Home needs no socket and
        // still works — the picker is pure navigation.
        if (!socket || socket.readyState !== 1) {
          toast(i18n.explainOffline || '');
          return;
        }
        // Back is always the phone's own Back — it closes slide-overs and
        // backs out of pages, and guessing which press is the last one
        // proved impossible (HHAeXchange+ keeps its whole app under one
        // activity, so an activity-level guard swallowed every press).
        // The overshoot is handled at the destination instead: a launcher
        // arriving right after a Back bounces the app back. See applyScreen.
        if (btn.dataset.act === 'back') backSentAt = Date.now();
        socket.send(JSON.stringify({ type: 'device', action: btn.dataset.act }));
        // The command went: the icon pops, and for the three that change the
        // screen, the sketch shows in-flight the same way a tap does — the
        // press has a visible consequence instead of a silent wait.
        btn.classList.remove('sent');
        void btn.offsetWidth;
        btn.classList.add('sent');
        if (btn.dataset.act !== 'wake') tapping(true);
      });
    }

    // iOS large-title convention: the header's hairline appears only once
    // content has scrolled beneath it.
    const stage = document.getElementById('stage');
    if (stage) stage.addEventListener('scroll', () => {
      body.classList.toggle('scrolled', stage.scrollTop > 8);
    }, { passive: true });

    bindWire();
    wireForms(document);
  });

  // -------------------------------------------------------------- push
  // Notifications from this portal, so a tap opens THIS app. Everything is
  // guarded: iOS grants Web Push only to a site added to the Home Screen, on
  // 16.4+, over a real certificate — and the permission prompt only rises
  // from a genuine press. Where any of that is missing the control stays
  // hidden rather than offering something that cannot work.
  function pushSupported() {
    return 'serviceWorker' in navigator && 'PushManager' in window
           && 'Notification' in window;
  }

  function base64ToBytes(value) {
    const padded = (value + '='.repeat((4 - value.length % 4) % 4))
      .replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(padded);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  async function serverKey() {
    try {
      const r = await fetch('/api/push/key');
      return (await r.json()).key || '';
    } catch (e) { return ''; }
  }

  function paintNotify(state) {
    const button = document.getElementById('notify-toggle');
    const label = document.getElementById('notify-label');
    if (!button || !label) return;
    button.hidden = false;
    button.classList.toggle('on', state === 'on');
    label.textContent = state === 'on' ? i18n.notifyOff
                      : state === 'denied' ? i18n.notifyDenied
                      : i18n.notifyOn;
    button.disabled = state === 'denied';
  }

  function sameBytes(a, b) {
    if (!a || !b || a.byteLength !== b.byteLength) return false;
    const x = new Uint8Array(a), y = new Uint8Array(b);
    for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return false;
    return true;
  }

  async function keyMatches(subscription) {
    const applied = subscription.options
                  && subscription.options.applicationServerKey;
    if (!applied) return true;          // nothing to compare — leave it alone
    const key = await serverKey();
    if (!key) return true;
    return sameBytes(applied, base64ToBytes(key).buffer);
  }

  async function resubscribe(registration) {
    const key = await serverKey();
    if (!key) return null;
    const sub = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: base64ToBytes(key)
    });
    await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ subscription: sub.toJSON() })
    });
    return sub;
  }

  async function setUpNotify() {
    const button = document.getElementById('notify-toggle');
    if (!button || !pushSupported()) return;
    if (!(await serverKey())) return;      // push not available on this Pi

    try {
      const registration =
        await navigator.serviceWorker.getRegistration('/sw.js');
      let existing = registration
        ? await registration.pushManager.getSubscription() : null;
      // A subscription made against a DIFFERENT server key can never be
      // signed for — the push service refuses it (Apple: 403 BadJwtToken)
      // while the toggle still says notifications are on, which is the worst
      // of both. Compare and re-subscribe silently; permission is already
      // granted, so this needs no prompt and no press.
      if (existing && !(await keyMatches(existing))) {
        try {
          await existing.unsubscribe();
          existing = null;
          if (Notification.permission === 'granted') {
            existing = await resubscribe(registration);
          }
        } catch (e) { existing = null; }
      }
      // Re-register whatever this browser holds, every load. The server can
      // lose a subscription the browser still has — a pruned store, a lost
      // file, a mistake in the sender — and nothing in the browser would
      // ever notice: it holds a valid subscription, so it asks for nothing
      // and the phone goes quiet. Posting it again is idempotent (the store
      // keys on the endpoint), costs one request, and makes the server heal
      // itself without her tapping anything.
      if (existing) {
        try {
          await fetch('/api/push/subscribe', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ subscription: existing.toJSON() })
          });
        } catch (e) { /* offline; the next load tries again */ }
      }
      paintNotify(Notification.permission === 'denied' ? 'denied'
                  : existing ? 'on' : 'off');
    } catch (e) {
      paintNotify('off');
    }

    button.addEventListener('click', async () => {
      try {
        const reg = await navigator.serviceWorker.getRegistration('/sw.js');
        const current = reg ? await reg.pushManager.getSubscription() : null;
        if (current) {
          const endpoint = current.endpoint;
          await current.unsubscribe();
          await fetch('/api/push/subscribe', {
            method: 'DELETE',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ endpoint: endpoint })
          });
          paintNotify('off');
          return;
        }
        // Permission is requested INSIDE the click handler: iOS refuses a
        // prompt that is not a direct consequence of a press.
        const permission = await Notification.requestPermission();
        if (permission !== 'granted') { paintNotify(permission === 'denied'
                                                    ? 'denied' : 'off'); return; }
        const worker = reg || await navigator.serviceWorker.register('/sw.js');
        await navigator.serviceWorker.ready;
        const key = await serverKey();
        const sub = await worker.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: base64ToBytes(key)
        });
        const res = await fetch('/api/push/subscribe', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ subscription: sub.toJSON() })
        });
        paintNotify(res.ok ? 'on' : 'off');
      } catch (e) {
        paintNotify('off');
      }
    });
  }
  setUpNotify();

  // How tall the floating chrome actually is, published to CSS so the
  // content's tail and the type bar can clear it instead of guessing.
  //
  // It is not one height. The pill alone is one; the pill under the app's
  // own tab row is taller, and the tab row comes and goes with the screen.
  // Two constants were written for it — 104px of padding under the content
  // and a type bar 88px up — and both were the no-tabs measurement, so on
  // the schedule the last visit hid behind the tabs and the type bar's
  // Cancel button sat on top of Home.
  function measureChrome() {
    const nav = document.querySelector('.navbar');
    if (!nav) return;
    const h = Math.round(nav.getBoundingClientRect().height);
    if (h > 0) document.documentElement.style.setProperty('--chrome-h', h + 'px');
  }
  if (window.ResizeObserver) {
    const nav = document.querySelector('.navbar');
    if (nav) new ResizeObserver(measureChrome).observe(nav);
  }
  window.addEventListener('resize', measureChrome);
  measureChrome();

  // Failsafe for the splash: 2.8s and it lifts no matter what.
  setTimeout(() => body.classList.add('ready'), 2800);

  // The ancient iOS incantation: without any touchstart listener on the
  // document, Safari withholds :active states — every press style this page
  // has would simply not fire under a finger.
  document.addEventListener('touchstart', () => {}, { passive: true });

  document.addEventListener('visibilitychange', () => {
    // iOS suspends a backgrounded tab and its socket with it.
    if (!document.hidden && (!socket || socket.readyState > 1)) connect();
  });

  connect();
})();
