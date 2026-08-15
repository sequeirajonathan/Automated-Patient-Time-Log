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
  function bindWire() {
    const root = wrap();
    if (!root) return;
    for (const el of root.querySelectorAll('[data-aim]')) {
      el.addEventListener('click', (ev) => {
        ev.preventDefault();
        if (!socket || socket.readyState !== 1) return;
        let aim;
        try { aim = JSON.parse(el.dataset.aim); } catch (e) { return; }
        busy(i18n.waiting || '');
        socket.send(JSON.stringify({ type: 'tap', frame: frameId, element: aim }));
      });
    }
  }

  function applyScreen(meta, html) {
    if (html !== undefined) {
      const root = wrap();
      if (root) {
        root.innerHTML = html;
        bindWire();
        const wire = root.querySelector('.wire');
        frameId = wire ? (wire.dataset.frame || '') : '';
      }
      unbusy();
    }
    if (!meta) return;

    const where = document.getElementById('where');
    if (where) where.textContent = (i18n.screens || {})[meta.name] || '';

    // Why there is no photograph, in her language — the wireframe itself is
    // unaffected, which is most of the point of having it.
    const note = document.getElementById('blocked-note');
    if (note) {
      const text = meta.blocked
        ? ((i18n.blocked || {})[meta.blocked] || i18n.blockedOther || '') : '';
      note.textContent = text;
      note.hidden = !text;
    }
    const notice = document.getElementById('notice');
    const said = document.getElementById('notice-said');
    if (notice && said) {
      said.textContent = meta.notice || '';
      notice.hidden = !meta.notice;
    }
    if (meta.blocked) body.classList.remove('peeking');
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

    socket.addEventListener('message', (ev) => {
      if (ev.data instanceof ArrayBuffer) return;   // video is the other view's
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }

      if (msg.type === 'tap_result') {
        if (!msg.ok) {
          unbusy();
          toast(msg.reason === 'stale' ? (i18n.moved || '') : (i18n.failed || ''));
        }
        return;
      }
      if (msg.type === 'device_result') {
        if (!msg.ok) { unbusy(); toast(i18n.devfail || ''); }
        return;
      }
      if (msg.type !== 'state') return;

      if (msg.screen_html !== undefined || msg.screen) {
        applyScreen(msg.screen, msg.screen_html);
      }
      if (msg.frame) applyFrame(msg.frame);
      if (msg.macro) applyMacro(msg.macro);
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

    for (const btn of document.querySelectorAll('.navbar [data-act]')) {
      btn.addEventListener('click', () => {
        if (!socket || socket.readyState !== 1) return;
        // No overlay for these: the wireframe follows within a second or two,
        // and a spinner over every Back press would make the page feel slower
        // than it is. Failures still surface as a toast.
        socket.send(JSON.stringify({ type: 'device', action: btn.dataset.act }));
      });
    }

    bindWire();
    wireForms(document);
  });

  document.addEventListener('visibilitychange', () => {
    // iOS suspends a backgrounded tab and its socket with it.
    if (!document.hidden && (!socket || socket.readyState > 1)) connect();
  });

  connect();
})();
