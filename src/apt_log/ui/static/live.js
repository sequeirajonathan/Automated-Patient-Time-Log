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

  // ------------------------------------------------------------------ video
  // H.264 straight from the phone's hardware encoder. Measured against the JPEG
  // mirror: 34 KB for three seconds of a static screen, versus 77 KB for one
  // still. A codec sends differences, and a screen that is not moving is nearly
  // free — which is the whole reason this path exists.
  //
  // WebCodecs only. No software fallback is attempted: where it is missing the
  // JPEG mirror is already there and already correct, and an H.264 decoder in
  // JavaScript would be slower than the pictures it replaced.
  const video = {
    decoder: null, canvas: null, ctx: null,
    ready: false, waitingForKey: true,
    supported: typeof window.VideoDecoder === 'function'
  };

  function annexBNalType(bytes) {
    // Start code is 3 or 4 bytes; the NAL header's low 5 bits are the type.
    for (let i = 0; i + 4 < bytes.length && i < 64; i++) {
      if (bytes[i] === 0 && bytes[i + 1] === 0) {
        if (bytes[i + 2] === 1) return bytes[i + 3] & 0x1f;
        if (bytes[i + 2] === 0 && bytes[i + 3] === 1) return bytes[i + 4] & 0x1f;
      }
    }
    return -1;
  }

  function startVideo(codec) {
    if (!video.supported) return;
    video.canvas = document.getElementById('screen-video');
    if (!video.canvas) return;
    video.ctx = video.canvas.getContext('2d');

    video.decoder = new VideoDecoder({
      output: (frame) => {
        if (video.canvas.width !== frame.displayWidth) {
          video.canvas.width = frame.displayWidth;
          video.canvas.height = frame.displayHeight;
        }
        video.ctx.drawImage(frame, 0, 0);
        frame.close();
        if (!video.ready) {
          video.ready = true;
          document.body.classList.add('video-live');
          draw();
        }
      },
      error: (e) => {
        // Fall back rather than retry: the JPEG mirror underneath is still
        // running and still correct.
        console.warn('video decode failed', e);
        stopVideo();
      }
    });
    // annexb, so no description is supplied — SPS and PPS arrive inline.
    video.decoder.configure({ codec: codec, optimizeForLatency: true });
    video.waitingForKey = true;
  }

  function stopVideo() {
    document.body.classList.remove('video-live');
    video.ready = false;
    if (video.decoder) {
      try { video.decoder.close(); } catch (e) { /* already closed */ }
      video.decoder = null;
    }
    draw();
  }

  function feedVideo(buffer) {
    if (!video.decoder || video.decoder.state !== 'configured') return;
    const bytes = new Uint8Array(buffer);
    const nal = annexBNalType(bytes);

    // A decoder cannot start mid-stream. Anything before the first SPS or
    // keyframe is dropped rather than fed, because feeding it raises an error
    // that closes the decoder for good.
    if (video.waitingForKey) {
      if (nal !== 7 && nal !== 5) return;
      video.waitingForKey = false;
    }
    try {
      video.decoder.decode(new EncodedVideoChunk({
        type: (nal === 5 || nal === 7) ? 'key' : 'delta',
        timestamp: performance.now() * 1000,
        data: bytes
      }));
    } catch (e) {
      console.warn('decode threw', e);
      stopVideo();
    }
  }

  let socket = null;
  let frame = { id: '', img: '', size: [0, 0], elements: [], blocked: '', notice: '' };
  let busy = false;
  let backoff = 1000;

  let fading = 0;
  function say(msg) { if (status) status.textContent = msg || ''; }

  // A dead socket used to look exactly like a quiet screen. She sat in front of
  // a page frozen at load time with no way to tell — the picture was two minutes
  // old and the "Taken" line beside it, rendered once by the server, said so
  // without meaning to. Silence that looks like success is the failure mode this
  // whole project keeps rediscovering.
  function connectionState(live) {
    body.classList.toggle('offline', !live);
  }

  // ------------------------------------------------------------------ overlay
  function shot() { return document.getElementById('shot'); }
  function layer() { return document.getElementById('overlay'); }

  function draw() {
    const over = layer();
    // The boxes sit over whichever surface is showing. The canvas replaces the
    // still when video is live, and the coordinate space is identical either
    // way because both render the device's own resolution.
    const surface = (video.ready && video.canvas) ? video.canvas : shot();
    if (!surface || !over) return;
    const k = (frame.size && frame.size[0]) ? surface.clientWidth / frame.size[0] : 0;
    if (!k) return;

    over.textContent = '';
    over.style.height = surface.clientHeight + 'px';
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
      // Announces what a control is. On a screen with a picture that is all it
      // may say — the element map carries no text and this is where it would
      // otherwise leak back in. Where the picture is refused the server sends
      // `txt` instead, because a box she cannot read is a box she cannot use.
      box.setAttribute('aria-label', el.txt || (el.cls + (el.rid ? ' ' + el.rid : '')));
      if (el.txt) {
        const label = document.createElement('span');
        label.className = 'label';
        label.textContent = el.txt;
        box.appendChild(label);
      }
      box.addEventListener('click', (ev) => { ev.preventDefault(); sendTap(el); });
      over.appendChild(box);
    }
  }

  // No picture of this screen. The last capture is still sitting on disk and is
  // of some *earlier* screen, so leaving it visible under boxes drawn from this
  // one shows her a control where there is none. Hide the picture, name the
  // reason, and let the boxes carry their own labels.
  // Set when /screen.jpg has no bytes to give — a controller that has been on a
  // sign-in screen since boot has never written one. Same treatment as a
  // refusal: hide the broken image, keep the boxes, say why.
  let noPicture = false;

  function applyBlind(code, notice) {
    body.classList.toggle('blind', !!code || noPicture);

    // The picture is hidden but still holds the rectangle the boxes are laid
    // out in, so it needs the device's shape even when it has no bytes — which
    // is the case on a controller that has been on a sign-in screen since boot.
    const img = shot();
    if (img && frame.size && frame.size[0] && frame.size[1]) {
      img.style.aspectRatio = code ? (frame.size[0] + ' / ' + frame.size[1]) : '';
    }

    const note = document.getElementById('blind-note');
    if (note) {
      const table = i18n.blocked || {};
      note.textContent = code ? (table[code] || i18n.blockedOther || '')
                              : (noPicture ? (i18n.blockedOther || '') : '');
      // An empty banner is a box with nothing in it, which reads as a fault.
      note.hidden = !note.textContent;
    }

    // The app's own words, verbatim, in whatever language the phone runs. Set
    // as text rather than markup: this is the one string on the page that comes
    // off the device, and it is never trusted as HTML.
    const panel = document.getElementById('screen-notice');
    const said = document.getElementById('screen-said');
    if (panel && said) {
      said.textContent = notice || '';
      panel.hidden = !notice;
    }
  }

  function applyFrame(next) {
    const moved = next.id !== frame.id;
    const repainted = next.img !== frame.img;
    frame = next;
    const img = shot();
    // Only ever refetch a picture that exists. Asking for one while capture is
    // refused re-serves the previous screen, which is the thing being fixed.
    if (img && !frame.blocked && (moved || repainted)) {
      img.src = '/screen.jpg?f=' + encodeURIComponent(frame.img || frame.id);
    }
    applyBlind(frame.blocked || '', frame.notice || '');
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

    socket.binaryType = 'arraybuffer';

    socket.addEventListener('open', () => {
      backoff = 1000;
      connectionState(true);
      if (video.supported) socket.send(JSON.stringify({ type: 'video', on: true }));
    });

    socket.addEventListener('message', (ev) => {
      if (ev.data instanceof ArrayBuffer) { feedVideo(ev.data); return; }

      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }

      if (msg.type === 'video') {
        if (msg.on) startVideo(msg.codec); else stopVideo();
        return;
      }

      if (msg.type === 'device_result') {
        const said = msg.ok ? (i18n.deviceSent || '') : (i18n.deviceFailed || '');
        say(said);
        // Clears itself. "Sent to the phone" left standing for the rest of the
        // shift describes a button she pressed twenty minutes ago.
        clearTimeout(fading);
        fading = setTimeout(() => { if (status && status.textContent === said) say(''); },
                            4000);
        return;
      }

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
        // The server renders this line once at page load and it would otherwise
        // sit there ageing, which is worse than showing nothing: a stale
        // timestamp reads as a fresh one to anyone not doing the arithmetic.
        const taken = document.getElementById('shot-taken');
        if (taken && msg.mirror.taken_text) taken.textContent = msg.mirror.taken_text;
        const where = document.getElementById('mirror-where');
        const step = document.getElementById('mirror-step');
        if (where && msg.mirror.text_where) where.textContent = msg.mirror.text_where;
        if (step && msg.mirror.text_step) step.textContent = msg.mirror.text_step;
        const img = shot();
        if (img) img.classList.toggle('stale', !!msg.mirror.stale);
      }
    });

    socket.addEventListener('close', () => {
      connectionState(false);
      stopVideo();
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

      // getAttribute, never form.action. A form control named "action" shadows
      // the property, so on every /device form — each of which posts a hidden
      // <input name="action"> — form.action returned that input element and
      // form.action.endsWith threw. The listener died before preventDefault and
      // the browser submitted normally, which is why Back, Home, Recents and
      // Wake reloaded the whole page while everything else stayed live. Pause
      // and Resume had it too.
      const target = form.getAttribute('action') || '';

      form.addEventListener('submit', (ev) => {
        // Language is a genuine navigation — the whole page changes language,
        // and re-rendering it server-side is exactly right.
        if (target.endsWith('/language')) return;
        ev.preventDefault();

        // Device actions go over the socket, like taps: the connection is
        // already open, and a POST that answers with a redirect is a page
        // navigation waiting for one missing preventDefault.
        if (target.endsWith('/device') && socket && socket.readyState === 1) {
          const field = form.querySelector('[name="action"]');
          say(i18n.sending || '');
          socket.send(JSON.stringify({ type: 'device',
                                       action: field ? field.value : '' }));
          return;
        }

        post(target, form);
      });
    }
  }

  // The fallback, and the path for everything that is not a device action.
  // Still no navigation: the socket reports whatever actually happened.
  function post(target, form) {
    fetch(target, {
      method: 'POST',
      body: new URLSearchParams(new FormData(form)),
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      redirect: 'follow'
    }).catch(() => { /* the socket will show the real state either way */ });
  }

  document.addEventListener('visibilitychange', () => {
    // iOS suspends a backgrounded tab and its socket with it. Returning to the
    // page should reconnect immediately rather than wait out a backoff she has
    // no way of knowing about.
    if (!document.hidden && (!socket || socket.readyState > 1)) connect();
  });

  function watchPicture(img) {
    if (!img || img.dataset.watched) return;
    img.dataset.watched = '1';
    img.addEventListener('load', () => {
      noPicture = false;
      draw();
    });
    img.addEventListener('error', () => {
      noPicture = true;
      applyBlind(frame.blocked || '', frame.notice || '');
      draw();
    });
  }

  window.addEventListener('resize', draw);
  document.addEventListener('DOMContentLoaded', () => {
    watchPicture(shot());
    wireForms(document);
  });
  watchPicture(shot());
  wireForms(document);
  connect();
})();
