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

  function busy(text) {
    const label = document.getElementById('busy-text');
    if (label) label.textContent = text || '';
    body.classList.add('busy');
    // A stuck overlay is worse than a missing one: whatever happens, the
    // screen underneath is still live and still hers. Cleared early by the
    // next wireframe; this is the ceiling, not the plan.
    clearTimeout(busyTimer);
    busyTimer = setTimeout(unbusy, 12000);
  }

  function unbusy() {
    clearTimeout(busyTimer);
    body.classList.remove('busy');
  }

  function view(name) {
    body.dataset.view = name;
  }

  // ---------------------------------------------------------------- wireframe
  let tapTimer = 0;
  let blockedCode = '';

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

  let lastApp = '';
  let currentPackage = '';

  function applyScreen(meta, html) {
    if (html !== undefined) {
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
      unbusy();
    }
    if (!meta) return;
    currentPackage = meta.app || '';

    // The large title: the app's own nav-bar title, else the app she opened,
    // else the portal's name. Never one of the classifier's sentences — "It
    // is on a screen it did not recognise" in a title slot is exactly the
    // alarm this view exists not to raise.
    const where = document.getElementById('where');
    if (where) {
      const own = wrap() && wrap().querySelector('.a-title');
      where.textContent = (own && own.textContent.trim())
        || lastApp || i18n.appTitle || '';
    }

    // No photograph of this screen: a quiet lock in the toolbar, whose
    // explanation is one tap away rather than a standing banner.
    blockedCode = meta.blocked || '';
    body.classList.toggle('blocked', !!blockedCode);
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
  function applyMacro(m) {
    if (!awaitingMacro) return;
    if (m.state === 'running') {
      busy(m.text || '');
    } else if (m.state === 'done') {
      awaitingMacro = false;
      unbusy();
    } else if (m.state === 'failed') {
      awaitingMacro = false;
      unbusy();
      toast(m.state_text || '');
    }
  }

  function launch(tile) {
    const name = tile.dataset.macro;
    if (!name) return;
    lastApp = tile.dataset.name || '';

    // The tile of the app already in front is a switch, not a ceremony. The
    // screen on the phone is signed in and live; running the sign-in walk
    // over it is fifteen seconds of state churn she can only read as "why is
    // it signing in again?"
    if (tile.dataset.package && tile.dataset.package === currentPackage) {
      view('screen');
      return;
    }

    awaitingMacro = true;
    view('screen');
    busy((i18n.opening || '').replace('{app}', tile.dataset.name || ''));
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

  function padRedraw() {
    const c = padCanvas();
    if (!c || !pad.ctx) return;
    pad.ctx.clearRect(0, 0, c.width, c.height);
    pad.ctx.lineWidth = 2.5;
    pad.ctx.lineCap = 'round';
    pad.ctx.lineJoin = 'round';
    pad.ctx.strokeStyle = '#1c1c2e';
    for (const s of pad.strokes) {
      pad.ctx.beginPath();
      s.forEach((p, i) => {
        const x = p[0] * c.width, y = p[1] * c.height;
        if (i === 0) pad.ctx.moveTo(x, y); else pad.ctx.lineTo(x, y);
      });
      pad.ctx.stroke();
    }
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
        body.classList.toggle('stale', !!msg.screen_stale);
        const label = document.getElementById('live-label');
        if (label) label.textContent = msg.screen_stale
          ? (i18n.syncing || '') : (i18n.live || '');
      }
      if (msg.frame) applyFrame(msg.frame);
      if (msg.macro) applyMacro(msg.macro);
      if (msg.sign) applySign(msg.sign);
      if (msg.relay_html !== undefined || msg.relay_nonce !== undefined) {
        applyRelay(msg.relay_html, msg.relay_nonce);
      }
    });

    socket.addEventListener('close', () => {
      body.classList.add('offline');
      unbusy();
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 15000);
    });
  }

  // -------------------------------------------------------------------- wire
  document.addEventListener('DOMContentLoaded', () => {
    for (const tile of document.querySelectorAll('.tile')) {
      tile.addEventListener('click', () => launch(tile));
    }
    const apps = document.getElementById('btn-apps');
    if (apps) apps.addEventListener('click', () => view('launcher'));

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

    for (const btn of document.querySelectorAll('.navbar [data-act]')) {
      btn.addEventListener('click', () => {
        if (!socket || socket.readyState !== 1) return;
        // No overlay for these: the wireframe follows within a second or two,
        // and a spinner over every Back press would make the page feel slower
        // than it is. Failures still surface as a toast.
        socket.send(JSON.stringify({ type: 'device', action: btn.dataset.act }));
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

  document.addEventListener('visibilitychange', () => {
    // iOS suspends a backgrounded tab and its socket with it.
    if (!document.hidden && (!socket || socket.readyState > 1)) connect();
  });

  connect();
})();
