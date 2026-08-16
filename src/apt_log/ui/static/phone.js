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
    for (const el of root.querySelectorAll('[data-aim]')) {
      el.addEventListener('click', (ev) => {
        ev.preventDefault();
        if (!socket || socket.readyState !== 1) return;
        let aim;
        try { aim = JSON.parse(el.dataset.aim); } catch (e) { return; }
        // No overlays and no sentences for a tap: the screen dims and
        // shimmers until its successor arrives, the way a native app treats
        // a moment of work as a state rather than an event.
        tapping(true);
        socket.send(JSON.stringify({ type: 'tap', frame: frameId, element: aim }));
      });
    }
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
    if (html !== undefined) {
      lastScreenHtml = html;
      const root = wrap();
      if (root) {
        const previous = frameId;
        root.innerHTML = html;
        bindWire();
        const wire = root.querySelector('.wire');
        frameId = wire ? (wire.dataset.frame || '') : '';
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
    const onLauncher = meta.name === 'launcher';
    if (onLauncher && wasScreen && wasScreen !== 'launcher' && !pendingApp) {
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
      awaitingMacro = false;
    } else if (m.state === 'failed') {
      if (awaitingMacro || pendingApp) { unbusy(); toast(m.state_text || ''); }
      awaitingMacro = false;
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
      form.dataset.live = '1';
      // getAttribute, never form.action — a field named "action" shadows the
      // property. The dashboard learned this the hard way; see live.js.
      const target = form.getAttribute('action') || '';
      form.addEventListener('submit', (ev) => {
        ev.preventDefault();
        fetch(target, {
          method: 'POST',
          body: new URLSearchParams(new FormData(form)),
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
    // Back where she was, if a reload interrupted her mid-screen.
    try {
      if (sessionStorage.getItem('aptlog-view') === 'screen') view('screen');
    } catch (e) { /* private mode */ }

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

    const peek = document.getElementById('btn-peek');
    if (peek) peek.addEventListener('click', () => {
      body.classList.toggle('peeking');
      if (body.classList.contains('peeking')) refreshPeek();
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

    for (const btn of document.querySelectorAll('[data-act]')) {
      btn.addEventListener('click', () => {
        if (!socket || socket.readyState !== 1) return;
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
