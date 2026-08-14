/* The live half of the mirror.
 *
 * The page is server-rendered and correct at the moment it loads. That is not
 * enough here: the controller stops mid-visit and waits for her, and a page
 * showing "nothing needs your answer" while the controller sits waiting looks
 * identical to a page showing it because nothing does. REQ-10.2 says the same
 * thing about signatures — pushed, not polled, because a page she has to
 * remember to refresh is a page she will not refresh.
 *
 * Two things move without a reload:
 *
 *  - the phone screenshot, re-fetched when the controller reports a newer one
 *  - the description of where the controller is
 *
 * Anything structural — a request opening, closing, or being replaced by a
 * different one — reloads the page instead of patching it, so the markup and
 * the translated strings come from the server rather than being assembled here
 * in one language.
 */
(function () {
  if (!window.EventSource) return;   // page is still correct, just not live

  const body = document.body;
  const shot = document.getElementById('shot');
  const where = document.getElementById('mirror-where');
  const step = document.getElementById('mirror-step');
  let lastShotAt = null;

  function reloadUnlessSheIsBusy() {
    // Reloading out from under a half-drawn signature would throw away strokes
    // she has already made, and she would have to start again without being
    // told why.
    if (window.APTLOG_DRAWING) return;
    window.location.reload();
  }

  const source = new EventSource('/events');

  source.addEventListener('message', (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }

    const shown = body.dataset.relayNonce || '';
    const live = (data.relay && data.relay.nonce) || '';
    if (shown !== live) { reloadUnlessSheIsBusy(); return; }

    const m = data.mirror || {};

    if (shot && m.screen_at && m.screen_at !== lastShotAt) {
      lastShotAt = m.screen_at;
      // Cache-busted: the response is no-store, but a phone browser that has
      // decided otherwise would leave her looking at a frozen screen.
      shot.src = '/screen.png?t=' + encodeURIComponent(m.screen_at);
    }
    if (shot) shot.classList.toggle('stale', !!m.stale);

    // Translated on the server and carried here as text; this file never picks
    // a wording, so it cannot pick an English one.
    if (where && m.text_where) where.textContent = m.text_where;
    if (step && m.text_step) step.textContent = m.text_step;
  });

  source.addEventListener('error', () => {
    // EventSource reconnects on its own. Nothing to do, and no message worth
    // showing — the page is stale, not wrong, and the stale marks say so.
  });
})();
