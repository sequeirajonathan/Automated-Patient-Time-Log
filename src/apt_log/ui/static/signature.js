/* Signature capture (REQ-10.4).
 *
 * Strokes are stored normalised to 0..1 against the canvas, with a millisecond
 * delta per point. Two reasons, both from the spec:
 *
 *  - the target field on the phone is a different size and aspect than this
 *    canvas, and REQ-10.7 maps into its bounds rather than the screen's
 *  - REQ-10.8 replays the original timing, because some signature widgets apply
 *    velocity-based smoothing and a uniform-speed replay looks visibly wrong
 *
 * Nothing is kept after a successful send. REQ-10.6 is the line between
 * transmitting her signing and signing on her behalf.
 */
(function () {
  const pad = document.getElementById('pad');
  if (!pad) return;

  const msg = document.getElementById('sigmsg');
  const i18n = window.APTLOG_I18N || {};
  const ctx = pad.getContext('2d');
  let strokes = [];
  let current = null;
  let started = 0;

  function resize() {
    const ratio = window.devicePixelRatio || 1;
    const rect = pad.getBoundingClientRect();
    pad.width = rect.width * ratio;
    pad.height = rect.height * ratio;
    ctx.scale(ratio, ratio);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = '#16202c';
    redraw();
  }

  function width() {
    return parseInt(document.getElementById('brush').value, 10) || 3;
  }

  function redraw() {
    const rect = pad.getBoundingClientRect();
    ctx.clearRect(0, 0, rect.width, rect.height);
    for (const s of strokes) {
      ctx.lineWidth = s.width;
      ctx.beginPath();
      s.points.forEach((p, i) => {
        const x = p[0] * rect.width, y = p[1] * rect.height;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
  }

  function point(ev) {
    const rect = pad.getBoundingClientRect();
    return [
      (ev.clientX - rect.left) / rect.width,   // normalised, not pixels
      (ev.clientY - rect.top) / rect.height,
      Date.now() - started
    ];
  }

  pad.addEventListener('pointerdown', (ev) => {
    pad.setPointerCapture(ev.pointerId);
    started = Date.now();
    current = { width: width(), points: [point(ev)] };
    strokes.push(current);
  });

  pad.addEventListener('pointermove', (ev) => {
    if (!current) return;
    current.points.push(point(ev));
    redraw();
  });

  const finish = () => { current = null; };
  pad.addEventListener('pointerup', finish);
  pad.addEventListener('pointercancel', finish);

  document.getElementById('clear').addEventListener('click', () => {
    strokes = []; redraw(); msg.textContent = '';
  });

  document.getElementById('undo').addEventListener('click', () => {
    strokes.pop(); redraw();
  });

  document.getElementById('send').addEventListener('click', async () => {
    if (!strokes.length) { msg.textContent = i18n.empty || ''; return; }
    msg.textContent = i18n.sending || '';
    try {
      const res = await fetch('/signature', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nonce: pad.dataset.nonce, strokes: strokes })
      });
      if (res.status === 409) { msg.textContent = i18n.expired || ''; return; }
      if (!res.ok) { msg.textContent = i18n.failed || ''; return; }
      msg.textContent = i18n.sent || '';
      strokes = []; redraw();          // nothing reusable is left behind
      setTimeout(() => window.location.reload(), 1200);
    } catch (e) {
      msg.textContent = i18n.failed || '';
    }
  });

  window.addEventListener('resize', resize);
  resize();
})();
