/* The portal: tap the phone through its own screen.
 *
 * The page shows a picture of the phone and, over it, a box for every tappable
 * thing on that screen. She taps a box; the box is what gets sent.
 *
 * Coordinates are never sent. That is the whole design. A point replayed against
 * a screen that has moved lands on whatever occupies that spot now, and on this
 * app that can be the verification prompt that produces a call from the agency.
 * An element carries the identity of the frame it came from, so the server can
 * notice the screen changed and refuse — and a refusal is a fresh frame and
 * another try, not a wrong tap.
 *
 * The boxes come from the accessibility tree and carry no text, so nothing about
 * who is on the screen crosses the wire on the way back.
 */
(function () {
  const shot = document.getElementById('shot');
  const layer = document.getElementById('overlay');
  if (!shot || !layer) return;

  const i18n = window.APTLOG_PORTAL || {};
  const status = document.getElementById('portal-status');
  let frame = { id: '', img: '', size: [0, 0], elements: [] };
  let busy = false;

  function say(msg) { if (status) status.textContent = msg || ''; }

  function scale() {
    // The screenshot is the device's native resolution, so one factor covers
    // both axes and the boxes cannot drift out of register with the picture.
    const w = shot.clientWidth;
    return (frame.size && frame.size[0]) ? w / frame.size[0] : 0;
  }

  function draw() {
    layer.textContent = '';
    const k = scale();
    if (!k) return;
    layer.style.height = shot.clientHeight + 'px';

    for (const el of frame.elements) {
      const [x1, y1, x2, y2] = el.b;
      const box = document.createElement('button');
      box.type = 'button';
      box.className = 'hit'
        + (el.focused ? ' focused' : '')
        + (el.selected ? ' selected' : '');
      box.style.left = (x1 * k) + 'px';
      box.style.top = (y1 * k) + 'px';
      box.style.width = ((x2 - x1) * k) + 'px';
      box.style.height = ((y2 - y1) * k) + 'px';
      // Screen-reader style: the box announces what it is, never what it says.
      box.setAttribute('aria-label', el.cls + (el.rid ? ' ' + el.rid : ''));
      box.addEventListener('click', (ev) => {
        ev.preventDefault();
        send(el);
      });
      layer.appendChild(box);
    }
  }

  async function refresh() {
    try {
      const res = await fetch('/frame.json', { cache: 'no-store' });
      if (!res.ok) return;
      const next = await res.json();
      // Two reasons to refetch the picture, and only one of them is structural.
      // Typing into a field moves no targets at all, so refreshing on `id`
      // alone would show her a screen with none of her own keystrokes in it.
      const moved = next.id !== frame.id;
      const repainted = next.img !== frame.img;
      frame = next;
      if (moved || repainted) {
        shot.src = '/screen.jpg?f=' + encodeURIComponent(frame.img || frame.id);
      }
      draw();
    } catch (e) { /* transient; the next tick tries again */ }
  }

  async function send(el) {
    if (busy) return;
    busy = true;
    say(i18n.sending || '');
    try {
      const res = await fetch('/tap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ frame: frame.id, element: el })
      });
      if (res.status === 409) {
        // Not an error she did anything wrong: the screen moved under her.
        say(i18n.moved || '');
        await refresh();
      } else if (!res.ok) {
        say(i18n.failed || '');
      } else {
        say('');
        // Give the app a beat to react before repainting, so she sees the
        // result of her tap rather than the screen she tapped.
        setTimeout(refresh, 600);
      }
    } catch (e) {
      say(i18n.failed || '');
    } finally {
      busy = false;
    }
  }

  shot.addEventListener('load', draw);
  window.addEventListener('resize', draw);
  refresh();
  setInterval(refresh, 2000);
})();
