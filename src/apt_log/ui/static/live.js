/* One socket. No page reloads.
 *
 * What this replaces mattered more than it sounds. Every form on this page used
 * to answer with a redirect, so acknowledging a message or sending a code threw
 * away her scroll position and the screen she was looking at — mid-visit, on a
 * phone, four floors from the device. And the screen itself was polled on a
 * timer, which is both slower than it needs to be and wrong in the other
 * direction: it refetches when nothing moved.
 *
 * Everything the socket sends is finished HTML or plain values. The client never
 * assembles a sentence, because assembling one means owning a copy of the
 * message catalog, and a page that renders in Spanish until it updates itself
 * into English is worse than one that never updates at all.
 *
 * The socket is an enhancement. Every form here still submits normally without
 * it — she may be on whatever browser her phone has, in someone's kitchen.
 */
(function () {
  const body = document.body;
  const i18n = window.APTLOG_PORTAL || {};
  const status = document.getElementById('portal-status');

  let socket = null;
  let frame = { id: '', img: '', size: [0, 0], elements: [] };
  let busy = false;
  let backoff = 1000;

  function say(msg) { if (status) status.textContent = msg || ''; }

  // ------------------------------------------------------------------ overlay
  function shot() { return document.getElementById('shot'); }
  function layer() { return document.getElementById('overlay'); }

  function draw() {
    const img = shot(), over = layer();
    if (!img || !over) return;
    const k = (frame.size && frame.size[0]) ? img.clientWidth / frame.size[0] : 0;
    if (!k) return;

    over.textContent = '';
    over.style.height = img.clientHeight + 'px';
    for (const el of frame.elements) {
      const [x1, y1, x2, y2] = el.b;
      const box = document.createElement('button');
      box.type = 'button';
      box.className = 'hit' + (el.focused ? ' focused' : '')
                            + (el.selected ? ' selected' : '');
      box.style.left = (x1 * k) + 'px';
      box.style.top = (y1 * k) + 'px';
      box.style.width = ((x2 - x1) * k) + 'px';
      box.style.height = ((y2 - y1) * k) + 'px';
      // Announces what a control is, never what it says — the element map
      // carries no text, and this is where that would otherwise leak back in.
      box.setAttribute('aria-label', el.cls + (el.rid ? ' ' + el.rid : ''));
      box.addEventListener('click', (ev) => { ev.preventDefault(); sendTap(el); });
      over.appendChild(box);
    }
  }

  function applyFrame(next) {
    const moved = next.id !== frame.id;
    const repainted = next.img !== frame.img;
    frame = next;
    const img = shot();
    if (img && (moved || repainted)) {
      img.src = '/screen.jpg?f=' + encodeURIComponent(frame.img || frame.id);
    }
    draw();
  }

  function sendTap(el) {
    if (busy || !socket || socket.readyState !== 1) return;
    busy = true;
    say(i18n.sending || '');
    socket.send(JSON.stringify({ type: 'tap', frame: frame.id, element: el }));
  }

  // -------------------------------------------------------------------- socket
  function connect() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(scheme + '://' + location.host + '/ws');

    socket.addEventListener('open', () => { backoff = 1000; });

    socket.addEventListener('message', (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }

      if (msg.type === 'tap_result') {
        busy = false;
        // A refusal is "look again", not a failure she caused: the screen moved
        // under her aim and the server declined rather than guessing.
        say(msg.ok ? '' : (msg.reason === 'stale' ? i18n.moved : i18n.failed));
        return;
      }
      if (msg.type !== 'state') return;

      if (msg.frame) applyFrame(msg.frame);
      if (msg.relay_html !== undefined) {
        // Never swap the panel out from under a half-drawn signature.
        if (!window.APTLOG_DRAWING) {
          const panel = document.getElementById('relay');
          if (panel) {
            panel.innerHTML = msg.relay_html;
            panel.classList.toggle('asking', !!msg.relay_nonce);
            body.dataset.relayNonce = msg.relay_nonce || '';
            wireForms(panel);
          }
        }
      }
      if (msg.mirror) {
        const where = document.getElementById('mirror-where');
        const step = document.getElementById('mirror-step');
        if (where && msg.mirror.text_where) where.textContent = msg.mirror.text_where;
        if (step && msg.mirror.text_step) step.textContent = msg.mirror.text_step;
        const img = shot();
        if (img) img.classList.toggle('stale', !!msg.mirror.stale);
      }
    });

    socket.addEventListener('close', () => {
      // Reconnect with a ceiling. The page keeps showing its last state, which
      // is stale rather than wrong, and the mirror's own staleness marks say so.
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 15000);
    });
  }

  // --------------------------------------------------------------------- forms
  function wireForms(root) {
    for (const form of root.querySelectorAll('form[method="post"]')) {
      if (form.dataset.live) continue;
      form.dataset.live = '1';
      form.addEventListener('submit', async (ev) => {
        // Language is a genuine navigation — the whole page changes language,
        // and re-rendering it server-side is exactly right.
        if (form.action.endsWith('/language')) return;
        ev.preventDefault();
        try {
          await fetch(form.action, {
            method: 'POST',
            body: new URLSearchParams(new FormData(form)),
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            redirect: 'follow'
          });
        } catch (e) { /* the socket will show the real state either way */ }
      });
    }
  }

  window.addEventListener('resize', draw);
  document.addEventListener('DOMContentLoaded', () => {
    const img = shot();
    if (img) img.addEventListener('load', draw);
    wireForms(document);
  });
  const img0 = shot();
  if (img0) img0.addEventListener('load', draw);
  wireForms(document);
  connect();
})();
