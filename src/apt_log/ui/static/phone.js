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

  // COACH MODE: watch, do not drive.
  //
  // Two people share this portal from two states and every control on it
  // drives ONE phone. Watching over her shoulder to talk her through a screen
  // meant one stray thumb away from pressing Enviar on her behalf, on a
  // patient's signature.
  //
  // The CSS makes it visible and unreachable; this makes it true. Every path
  // that can move the phone asks here first, so a mode that is on cannot be
  // half on — and reading the page, taking a photograph and switching views
  // are untouched, because none of them reach the phone.
  let coaching = false;
  function driving() {
    if (!coaching) return true;
    toast(i18n.coachHeld || '');
    return false;
  }

  function setCoaching(on) {
    coaching = !!on;
    body.classList.toggle('coaching', coaching);
    const btn = document.getElementById('coach');
    if (btn) {
      const label = btn.querySelector('.coach-t');
      if (label) label.textContent = coaching ? (i18n.coachOff || '')
                                              : (i18n.coachOn || '');
      btn.setAttribute('aria-pressed', coaching ? 'true' : 'false');
    }
    try { sessionStorage.setItem('aptlog-coach', coaching ? '1' : ''); }
    catch (e) { /* private mode */ }
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

  // WHAT EACH APP CALLS ITS OWN CLEAR, on a signature sheet. The same words
  // the controller matches on (`sign._CLEAR_WORDS`), kept in step with it by
  // a test rather than by memory — the two lists disagreeing would mean the
  // replay wiping a canvas whose button the pad still shows, or the pad
  // hiding a button nothing wipes.
  //
  // Anchored: "borrar" as a whole caption, never as a fragment of a longer
  // one, because dropping a button on a substring match is how an
  // affirmative with an unlucky name disappears from the only row that
  // finishes a signature.
  const CLEAR_WORDS = /^(borrar|clear|limpiar)$/i;

  // Every element carrying a verified aim — a reflow control, or an app tab
  // lifted into the control bar — posts the same tap the overlay always did.
  function bindAims(root) {
    for (const el of root.querySelectorAll('[data-aim]')) {
      el.addEventListener('click', (ev) => {
        ev.preventDefault();
        if (!driving()) return;
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
          // A SEARCH BOX TAKES TEXT TOO. Tapping one used to focus it on the
          // phone and stop there — the phone's keyboard came up on a screen
          // nobody is looking at, and from the portal there was no way to put
          // a name in. The bar was held back because a "type here" prompt on
          // the patients list read as the sign-in code asking again; the fix
          // for that is the field's OWN words, which is what the hint carries.
          if (el.dataset.hint) {
            openTypeBar(aim, el.dataset.hint, 'search');
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
  // Which shape of thing is being typed. A code is letters and digits; a
  // name has spaces, accents and the odd apostrophe in it, and stripping
  // those left "Rojas Batista" arriving as "RojasBatista" — a search that
  // matches nothing. The server holds the same two shapes.
  let typeKind = 'code';
  function openTypeBar(aim, label, kind) {
    typeAim = aim;
    typeKind = kind === 'search' ? 'search' : 'code';
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
    if (!driving()) return;
    const box = document.getElementById('typebox');
    const raw = box.value || '';
    const value = (typeKind === 'search'
      // A person's name: letters in any language, spaces, and the marks
      // names actually carry. Everything else is dropped here and refused
      // again on the server — this is a search box, not a shell.
      ? raw.replace(/[^\p{L}\p{N} .'\-]/gu, '').replace(/\s+/g, ' ').trim()
      : raw.replace(/[^A-Za-z0-9]/g, '')).slice(0, 32);
    if (!value || !typeAim || !socket || socket.readyState !== 1) return;
    // WHAT SHE SENT, KEPT SO SHE CAN CHECK IT.
    //
    // The field she typed into never comes back: editable text is left out
    // of the published screen on purpose (it is where typed credentials
    // live), so the reflow redraws the code box empty a second after she
    // fills it. Reported from the field — the prompt updates, the digits do
    // not appear, and there is no way to tell a mistyped code from a wrong
    // one. On a code that EXPIRES that is the difference between fixing a
    // typo and starting the whole walk again.
    //
    // Nothing new crosses the wire: this browser is where the value was
    // typed. Held in memory only — never storage — so a reload forgets it,
    // which is the right lifetime for a one-time code.
    sentEcho = (typeKind === 'code')
      ? { aim: JSON.stringify(typeAim), value: value } : null;
    tapping(true);
    socket.send(JSON.stringify({ type: 'text', frame: frameId,
                                 element: typeAim, value: value }));
    closeTypeBar();
  }

  // The echo, re-applied after every render because the reflow is rebuilt
  // from the server each frame and knows nothing about it.
  let sentEcho = null;
  function sameAim(a, b) {
    try {
      return JSON.stringify(JSON.parse(a)) === JSON.stringify(JSON.parse(b));
    } catch (e) {
      return false;
    }
  }
  function paintSentCode() {
    if (!sentEcho) return;
    const root = wrap();
    if (!root) return;
    // Both sides go through the same parse-and-restringify. The server
    // writes this attribute with Python's json, which puts a space after
    // every colon; JSON.stringify writes none, so comparing the raw strings
    // matches nothing at all and the echo would simply never appear.
    const field = Array.from(root.querySelectorAll('.a-field'))
      .find(f => sameAim(f.dataset.aim, sentEcho.aim));
    // The field is gone: the app moved on, or this is a different screen.
    // Either way the echo has outlived what it was about.
    if (!field) { sentEcho = null; return; }
    field.textContent = sentEcho.value;
    field.classList.remove('empty');
    field.classList.add('sent');
    // Named as the PORTAL's echo, not the app's display. Without this the
    // digits look like something the phone reported back, and "the app has
    // my code" is exactly the wrong thing to believe when the next step is
    // deciding whether to press Verify or ask for a new one.
    const wrapEl = field.closest('.a-fieldwrap') || field.parentElement;
    if (wrapEl && !wrapEl.querySelector('.a-senttag')) {
      const tag = document.createElement('span');
      tag.className = 'a-senttag';
      tag.textContent = i18n.sentLabel || '';
      wrapEl.appendChild(tag);
    }
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

  // Walking a breadcrumb: how long one Back press is given to land, and how
  // many spare presses beyond the pops it claims. The slack exists because a
  // press can be swallowed by a screen's own state instead of popping — seen
  // live on inMyTeam's work log, where two presses only undid a tab switch.
  // Small on purpose: overshooting a trail lands outside the app.
  const CRUMB_WAIT = 2500;
  const CRUMB_SLACK = 2;
  let screenWaiters = [];

  // Resolves the next time a screen actually arrives, or false on timeout.
  function nextScreen(ms) {
    return new Promise((resolve) => {
      let done = false;
      const settle = (v) => { if (!done) { done = true; resolve(v); } };
      screenWaiters.push(() => settle(true));
      setTimeout(() => settle(false), ms);
    });
  }

  // The app a launch is waiting on. While set, the overlay is a single solid
  // state: sketch updates do not clear it, macro "done" does not clear it —
  // only the target app actually being in front (or failure, or the ceiling).
  let pendingApp = '';

  function applyScreen(meta, html) {
    // Anyone waiting for the screen to move — see nextScreen — hears first,
    // before any of the rendering below can throw.
    if (screenWaiters.length) {
      const waiting = screenWaiters;
      screenWaiters = [];
      for (const w of waiting) { try { w(); } catch (e) { /* ignore */ } }
    }
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
        paintSentCode();
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
    // Turned only when the PHOTOGRAPH is the wrong way up, which is not
    // the same as the screen being wide: HHAeXchange+ rotates the device
    // and its screencap arrives upright already. Turning that one anyway
    // is what went black on the signature screen.
    body.classList.toggle('sideways', !!meta.turn);
    // And whether the photograph is WIDE, which is a different fact
    // again: a landscape frame stretched across a portrait box covers
    // a fifth of it and leaves the rest bare.
    body.classList.toggle('wide', !!meta.landscape);
    // The app-side Borrar/Salvar only exist while a signature canvas is
    // in front — the buttons press real pixels there and nothing else.
    // The app's OWN buttons for this sheet, drawn INSIDE the pad so she is
    // not switching between the phone view and the front end to finish one
    // signature. Ordinary aims, pressed through the ordinary verified tap.
    const approw = document.getElementById('sign-approw');
    // ONE CONTROL IN STEP TWO, NOT TWO.
    //
    // The app's Borrar used to sit here beside its Done, equally weighted,
    // because a second replay lands ON TOP of the first — the pad sends her
    // whole signature every time, so anything already on the canvas had to be
    // wiped or the two overlapped. Redoing meant Borrar, redraw, send, Done:
    // four presses across two screens for one signature, and it was reported
    // as exactly that back and forth.
    //
    // The replay clears the canvas itself now (`sign._wipe_the_canvas`), so
    // this button has nothing left to do and redrawing is just pressing Send
    // again. Filtered here rather than skipped in the loop below, so the row's
    // cache key, its emphasis pass and the decision to show step two at all
    // are all made against what is actually going to be on screen — an app
    // publishing ONLY a clear must not leave step two headed and empty.
    const sheetActions = (meta.sheet_actions || []).filter(
      (a) => !CLEAR_WORDS.test((a.txt || '').trim()));
    // Out here rather than inside the block below, because the pencil's own
    // gate needs it too and a const scoped to that `if` is invisible from
    // there — the kind of thing that throws at exactly the moment a signature
    // screen appears and nowhere else.
    const legacyUsable = !sheetActions.length && !!meta.legacy_pad;
    if (approw) {
      const slot = document.getElementById('sign-appbtns');
      const key = JSON.stringify(sheetActions);
      if (slot && slot.dataset.key !== key) {
        slot.dataset.key = key;
        slot.innerHTML = '';
        for (const a of sheetActions) {
          const b = document.createElement('button');
          b.type = 'button';
          // The pad's own button dress: this row sits inside the pad and
          // must read as part of it, not as a transplant from the phone view.
          b.dataset.aim = JSON.stringify(a.aim);
          b.textContent = a.txt || '·';
          slot.appendChild(b);
        }
        if (sheetActions.length) {
          // Only an AFFIRMATIVE button leads. The last one led at first, the
          // way the sheet's own action row does — and on the signature sheet
          // the last one is Clear, so the pad filled in the destructive
          // button and left Done looking secondary. A word this does not
          // recognise simply gets no emphasis, which is the safe direction.
          for (const b of slot.children) {
            if (/^(done|save|salvar|guardar|confirm|confirmar|ok|aceptar|enviar|submit|send)$/i
                .test((b.textContent || '').trim())) {
              b.classList.add('primary');
            }
          }
          bindAims(slot);
        }
      }
      // A rebuild mid-replay hands back live buttons — the row is redrawn
      // from the screen payload, and the payload changes while the ink is
      // landing. Re-apply the lock over anything freshly built.
      if (pad.waitingId) padWaiting(true);
      // The legacy pair presses a COORDINATE, and the controller derives it
      // only for the legacy app — `button_targets` answers None everywhere
      // else, so off that app both of its buttons are wired to a refusal.
      // It used to fill in whenever the named buttons came back empty, on
      // any app, which is how the pad came to show a Borrar and a Salvar on
      // HHAeXchange+ that could not press anything: "the drop down pencil
      // enviar did not work". It shows only where it works now.
      // Shown when the app has given us real buttons to press, or on the
      // legacy rotated pages where the pair is pressed by coordinate.
      approw.hidden = !sheetActions.length && !legacyUsable;
      const legacy = document.getElementById('sign-legacyrow');
      if (legacy) legacy.hidden = !legacyUsable;
    }
    const onLauncher = meta.name === 'launcher';
    // While a macro is working — and for a grace period after it ends —
    // the launcher is not a destination, it is scenery: the sign-in path
    // restarts the app, and the phone crosses its home screen for a second
    // on the way. Ejecting her to the picker mid-"Signing in…" was watched
    // live. The flip only means something when nothing is in flight.
    const macroQuiet = !awaitingMacro && !pendingApp
      && Date.now() - macroEndedAt > 8000;
    // BACK MUST NOT CHANGE WHICH APP SHE IS IN.
    //
    // The first version of this only noticed Back landing on the LAUNCHER,
    // which is one of the two ways Android leaves an app and not the common
    // one. Back from an app's root pops the task stack, and what is under it
    // is whatever she was in before — so pressing Back in Exchange+ put her
    // in Mobile Caregiver+, silently, with the header renaming itself.
    // Reported as "the back button even takes me to the previous app I was
    // on, which is not ok".
    //
    // So the test is "did the front app change", not "is this the launcher".
    // Leaving an app is Home's job and the picker's; Back's job is to move
    // WITHIN one, and where the app has nowhere further back to go the
    // honest outcome is that Back did nothing.
    const leftTheApp = wasPackage && currentPackage !== wasPackage
      && (onLauncher || appOpen[currentPackage] !== undefined);
    if (leftTheApp && wasScreen && wasScreen !== 'launcher' && macroQuiet) {
      // Android kept the app's state, so bounce it straight back: to her,
      // that Back simply did nothing, which beats being teleported. Any
      // other arrival (the Home button, the phone's own drift) means she
      // left on purpose, and leaving means the picker.
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
      } else if (onLauncher && body.dataset.view === 'screen') {
        // Only the launcher sends her to the picker. Drifting into ANOTHER
        // care app is the phone doing something she did not ask for, and the
        // right answer is to put her back, not to eject her.
        view('launcher');
      }
    }
    body.classList.toggle('offapp', onLauncher);
    // A system panel is over the app. Said plainly, with the one act that
    // helps, instead of a sketch of a screen nobody is being shown — and
    // instead of her tapping controls that cannot possibly answer, which is
    // what "somehow inMyTeam ended up in the phone settings" felt like from
    // the other end.
    body.classList.toggle('covered', !!meta.covered);
    // The app in front will not do anything until it is updated. Its own
    // screen has one button, which opens the Store and gets bounced back —
    // so the page says what is happening and offers the act that works.
    body.classList.toggle('walled', !!meta.walled);
    // On the code screen: the way to a NEW code, which the app itself does
    // not offer. Shown for the whole screen rather than only once a code has
    // expired — a mistyped code needs the same escape, and the expired
    // wording has never been read off the live phone.
    body.classList.toggle('codescreen', !!meta.code_screen);
    // The plan of care's own button, offered only where there is something
    // to tick. The count is the server's, read off the same page the macro
    // will read, so what the button says is what it will do.
    const tasksBtn = document.getElementById('btn-tasks');
    if (tasksBtn) {
      const left = Number(meta.tasks) || 0;
      tasksBtn.hidden = left <= 0;
      const badge = document.getElementById('btn-tasks-n');
      if (badge) badge.textContent = left > 0 ? String(left) : '';
    }

    // The day's record, offered on the one app that publishes a readable
    // one. Hidden elsewhere rather than shown and refusing: a button that
    // sometimes says "this app's work log is not walked" teaches nothing
    // except not to press it.
    const checksBtn = document.getElementById('btn-checks');
    if (checksBtn) checksBtn.hidden = !meta.checks_app || onLauncher;

    // The other agency, on the one app that has one. Same rule as the button
    // above it: offered where the picker exists, absent where it does not,
    // rather than standing in every toolbar and failing on two of three apps.
    const agencyBtn = document.getElementById('btn-agency');
    if (agencyBtn) agencyBtn.hidden = !meta.agency_app || onLauncher;

    // THE PENCIL IS FOR A SIGNATURE SCREEN, AND ONLY A SIGNATURE SCREEN.
    //
    // It stood in the toolbar on every screen in both apps, so most of the
    // time it opened a pad onto a page with nothing to draw on: her strokes
    // went into the sheet, "Draw it on the phone" replayed them at a screen
    // with no canvas, and the answer came back "this screen has no signature
    // box". A control that is present everywhere and works in one place
    // teaches that it does not work.
    //
    // THE FIRST GATE WAS WRONG IN TWO WAYS AND ITS COMMENT CLAIMED OTHERWISE.
    // It read `meta.canvas`, saying that was "the same fact step two gates
    // on". Step two gates on no such thing: `_sheet_actions` refuses to use
    // that flag and says why — it FLICKERS, caught reading False with the
    // signature sheet plainly open. And `meta.canvas` means canvas AND
    // SIDEWAYS, while inMyTeam's "Firma del Paciente" sheet is portrait. So
    // on the one screen the pad exists for, the pencil was hidden and an
    // adopted signature could not be applied at all. Reported from the room,
    // with the sheet open and Carmen's signature unreachable.
    //
    // A union of three, so no single flickering signal can take it away: the
    // app's own buttons for this sheet, the legacy coordinate pair, or a
    // drawing surface reported at any orientation. Hidden only when all three
    // say there is nothing to sign on.
    const padHere = !!sheetActions.length || legacyUsable || !!meta.pad;
    const signBtn = document.getElementById('btn-sign');
    if (signBtn) signBtn.hidden = !padHere || onLauncher;

    // Whose pad this is. Kept as state rather than read at press time: the
    // sheet can be open while the screen underneath changes, and the name on
    // the heading has to change with it.
    signerNamed = meta.signer || '';
    signerAdopted = meta.signer_adopted || '';
    markSigner();

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

  // WHICH PAD IS IN FRONT.
  //
  // There are two, and they are two because they are two different moments.
  // `#signpad` belongs to a patient signing at check-out: it draws, replays
  // onto the app's canvas, and is followed by the app's own confirm. Nothing
  // in that sequence has anything to do with REGISTERING a signature, and
  // reusing it for registration put a caregiver in front of "draw it on the
  // phone" and a step two about an app that was not open — reported as
  // exactly that.
  //
  // `#enrolpad` is the registration one: a name, a pad, save. No
  // phone, no replay, no step two.
  //
  // The drawing machinery is shared because drawing is drawing — the same
  // clamping, the same capture guard, the same dark-mode ink. Only one sheet
  // can be open at a time, so one `pad` state serves both.
  function padCanvas() {
    return document.getElementById(
      body.classList.contains('enrolling') ? 'enrolpad' : 'signpad');
  }

  // Held inside the pad. A finger that slides a little past the edge is still
  // signing — the pointer keeps reporting while it is captured — and those
  // points came back outside 0..1, which the server refused OUTRIGHT: one
  // stray point and the whole signature bounced with "failed", strokes still
  // on the pad. Reported as "if I try to sign a bit out of bounds it can't
  // send it to the phone", and reproduced here by a curve that strays 4% off
  // the left edge.
  //
  // Clamped rather than dropped, and clamped HERE so that what she sees in
  // the preview is exactly what is sent: a signature that runs off the edge
  // rides the edge, which is what every pad on paper or glass does.
  function padPoint(ev, rect) {
    const clamp = (n) => (n < 0 ? 0 : n > 1 ? 1 : n);
    return [clamp((ev.clientX - rect.left) / rect.width),
            clamp((ev.clientY - rect.top) / rect.height)];
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
    // Drawing again means she is starting over, so the sheet stops claiming
    // the last signature is on the phone.
    if (pad.strokes.length && !pad.waitingId) markDrawn(false);
    const c = padCanvas();
    // Taken from the canvas that is actually in front rather than cached at
    // wiring time: two pads exist now, and a context held from the other one
    // draws into a canvas nobody is looking at.
    if (c) {
      pad.ctx = c.getContext('2d');
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
    // "How this will land in the app's own box" is a rehearsal for a replay.
    // Registration does not replay anything, so there is nothing to rehearse
    // and the enrolment sheet has no preview to draw into.
    if (body.classList.contains('enrolling')) return;
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

  // Wires ONE canvas. Called for both pads: a handler bound to whichever
  // happened to be in front at boot would leave the other one dead.
  function padWire(c) {
    if (!c) return;
    c.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      // Capture is an OPTIMISATION — it keeps the strokes coming while the
      // finger wanders off the canvas — and it throws if the browser has
      // already let go of that pointer id, which happens on a quick lift and
      // re-touch. Unguarded, the throw aborted the handler before the stroke
      // below was ever created, and the whole stroke vanished: exactly the
      // "signature with breaks has issues drawing on the pad" shape, where
      // the first stroke lands and one of the later ones does not.
      try { c.setPointerCapture(ev.pointerId); } catch (e) { /* draw anyway */ }
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

  // Swipe a sheet down to close it, the way every sheet on this phone
  // closes. Without it the only way out of the pad was pressing the pencil
  // again — "can't scroll down on the signature pad to dismiss have to press
  // the signature icon again".
  //
  // Only from the TOP of the sheet's own scroll, so a sheet with content
  // below the fold still scrolls normally; and never from the canvas, which
  // owns every touch that lands on it because those touches are ink.
  function swipeToDismiss(sheet, close) {
    if (!sheet) return;
    let y0 = 0, dy = 0, dragging = false;
    sheet.addEventListener('touchstart', (ev) => {
      if (ev.touches.length !== 1) return;
      if (ev.target.closest('canvas, button, input, textarea')) return;
      if (sheet.scrollTop > 0) return;
      y0 = ev.touches[0].clientY;
      dy = 0;
      dragging = true;
    }, { passive: true });
    sheet.addEventListener('touchmove', (ev) => {
      if (!dragging) return;
      dy = ev.touches[0].clientY - y0;
      if (dy <= 0) { sheet.style.transform = ''; return; }
      sheet.style.transition = 'none';
      sheet.style.transform = 'translateY(' + dy + 'px)';
    }, { passive: true });
    const end = () => {
      if (!dragging) return;
      dragging = false;
      sheet.style.transition = '';
      sheet.style.transform = '';
      // A short drag springs back; a decisive one closes. 90px is about a
      // thumb's travel and is the same threshold iOS uses by feel.
      if (dy > 90) close();
    };
    sheet.addEventListener('touchend', end);
    sheet.addEventListener('touchcancel', end);
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

  // THE APP'S OWN BUTTONS ARE NOT PRESSABLE WHILE THE INK IS STILL LANDING.
  //
  // A replay takes five to six seconds — measured on the phone: the canvas
  // reads 0 at two seconds, 1764 at four, complete at six. The busy overlay
  // that is supposed to cover that is `position:absolute; z-index:6` inside
  // the stage, and this sheet is `position:fixed; z-index:8` OVER it, so
  // every button in the pad stayed live throughout.
  //
  // That did not matter until the app's own Done was moved INTO the pad,
  // inches from Send. After that, pressing Send and then Done reads as one
  // gesture — and Done at two seconds commits whatever has landed so far.
  // Reported as a stroke never making it "after the submit", and as waiting
  // five to ten seconds making it work, which is exactly the length of the
  // replay.
  //
  // A half-drawn signature committed to a visit record is not a cosmetic
  // fault, so this is a lock rather than a hint: the controls that reach the
  // phone go dead until the replay says it has finished.
  //
  // WITH A CEILING, for the same reason the busy overlay has one. The lock
  // lifts on the replay's status push, and a push can be missed — a dropped
  // socket, a controller restarted mid-replay. Without a ceiling that leaves
  // step two dead for good and no way at all to press the app's own Done,
  // which is a worse failure than the one this prevents. Far longer than any
  // real replay: five to six seconds of drawing, plus the settle before the
  // ink goes in and up to two redraws of a stroke that left none.
  const PAD_LOCK_CEILING = 30000;
  let padLockTimer = 0;

  function padWaiting(on) {
    const sheet = document.getElementById('signsheet');
    if (sheet) sheet.classList.toggle('waiting', !!on);
    const rows = [document.getElementById('sign-appbtns'),
                  document.getElementById('sign-legacyrow')];
    for (const row of rows) {
      if (!row) continue;
      for (const b of row.querySelectorAll('button')) b.disabled = !!on;
    }
    const send = document.getElementById('sign-send');
    if (send) send.disabled = !!on;
    if (!on) { clearTimeout(padLockTimer); padLockTimer = 0; return; }
    // Armed once per wait, never re-armed: the row is re-locked every time
    // the app-button row is rebuilt, and restarting the clock on each of
    // those would let a chatty screen hold the lock open indefinitely.
    if (padLockTimer) return;
    padLockTimer = setTimeout(() => {
      padLockTimer = 0;
      pad.waitingId = '';
      padWaiting(false);
      toast(i18n.signLockLapsed || '');
    }, PAD_LOCK_CEILING);
  }

  function padSend() {
    if (!driving()) return;
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
      if (!r.ok) {
        unbusy();
        // Say WHICH failure. "failed" over a refused shape read as the phone
        // being unreachable, and the strokes are still on the pad either way
        // — so the difference between "nothing was drawn" and "the controller
        // could not be reached" is the difference between drawing again and
        // waiting.
        toast((out.error === 'empty' ? i18n.signEmpty : i18n.failed) || '');
        return;
      }
      pad.waitingId = out.id || '';
      padWaiting(true);
      // Cleared on the status push; nothing reusable is left behind.
      pad.strokes = []; padRedraw();
    }).catch(() => { unbusy(); toast(i18n.failed || ''); });
  }

  function applySign(s) {
    if (!pad.waitingId || s.id !== pad.waitingId) return;
    if (s.state === 'running') { busy(s.text || ''); return; }
    pad.waitingId = '';
    padWaiting(false);
    unbusy();
    // done or failed, the sentence arrives rendered; show it either way. On
    // done it says to check the screen and press the app's own save.
    toast(s.text || '');
    // THE INK HAS LANDED, SO THE PAD'S BUTTON IS NO LONGER THE ONE TO PRESS.
    //
    // It only ever redrew, and pressing it again is exactly what happened six
    // times in seven minutes: draw, press what looks like Send, watch it be
    // redrawn, press again. So on success the button restyles and renames
    // itself to what it actually does, a line appears saying where the finish
    // is, and step two is outlined.
    markDrawn(s.state === 'done');
  }

  function markDrawn(on) {
    const sheet = document.getElementById('signsheet');
    if (sheet) sheet.classList.toggle('drawn', !!on);
    const hint = document.getElementById('sign-hint');
    if (hint) hint.hidden = !on;
    const send = document.getElementById('sign-send');
    if (send && i18n.signSendAgain) {
      send.textContent = on ? i18n.signSendAgain : i18n.signSend;
    }
  }

  // ------------------------------------------------------- the live code
  // Polled rather than pushed. The age has to keep counting up on a page
  // nobody is touching — she is looking at it, not interacting with it — and
  // a code labelled "1 minute ago" that is really nine is the one failure
  // this card exists to prevent.
  const CODE_EVERY = 20000;
  // Past this the code is certainly dead and the card says so loudly rather
  // than quietly. Matches sms.SHOW_WITHIN, beyond which the server stops
  // answering at all.
  const CODE_STALE = 5 * 60;

  // WHOLE SENTENCES OUT OF THE CATALOG, never a number glued to a noun glued
  // to a preposition. English puts "ago" last and Spanish puts "hace" first,
  // so a template assembled here can only be right in one of them — which is
  // the rule test_i18n holds, and it caught this on the first run.
  function codeAgeSays(seconds) {
    const mins = Math.floor(seconds / 60);
    if (mins < 1) return i18n.codeJustNow || '';
    if (mins === 1) return i18n.codeAgoOne || '';
    return (i18n.codeAgoMany || '').replace('{n}', mins);
  }

  // COPYING IT, because the alternative is reading six digits off one phone
  // while typing them into another, which is where a 6 becomes an 8.
  //
  // Two paths on purpose. `navigator.clipboard` needs a secure context — the
  // portal has one through `tailscale serve`, so this is the path that runs —
  // but it is also absent in a plain-http preview and refused outright by
  // some in-app browsers, and a copy button that silently does nothing is
  // worse than no copy button. The textarea fallback works everywhere.
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise((resolve, reject) => {
      const pad = document.createElement('textarea');
      pad.value = text;
      // Off-screen rather than hidden: a display:none element cannot be
      // selected, and the copy silently fails.
      pad.setAttribute('readonly', '');
      pad.style.position = 'fixed';
      pad.style.top = '-1000px';
      document.body.appendChild(pad);
      pad.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(pad);
      ok ? resolve() : reject(new Error('copy refused'));
    });
  }

  function copyCode() {
    const digits = document.getElementById('code-digits');
    const card = document.getElementById('codecard');
    const text = digits ? (digits.textContent || '').trim() : '';
    if (!text) return;
    copyText(text).then(() => {
      toast(i18n.codeCopied || '');
      // The card says so too, because a toast is gone in two seconds and she
      // may be looking at the other phone by then.
      if (card) {
        card.classList.add('copied');
        setTimeout(() => card.classList.remove('copied'), 4000);
      }
    }).catch(() => toast(i18n.codeCopyFailed || ''));
  }

  // ONE OF THE TWO, NEVER BOTH.
  //
  // The card and the full-width Send button are the same feature in two
  // states, and having them stacked was most of what made the home screen
  // crowded. There is never a live code and no live code at once, so exactly
  // one of them is on the page at any moment.
  //
  // The button is the fallback rather than the card: with no code to show,
  // "send the latest one" is the only thing left that can help, and it is
  // also what the page looks like before the first fetch answers.
  function showCodeAs(hasCode) {
    const card = document.getElementById('codecard');
    const btn = document.getElementById('btn-code');
    if (card) card.hidden = !hasCode;
    if (btn) btn.hidden = !!hasCode;
  }

  function refreshCode() {
    const card = document.getElementById('codecard');
    if (!card) return;
    fetch('/code/latest')
      .then((r) => (r.ok ? r.json() : { found: false }))
      .then((d) => {
        if (!d.found) { showCodeAs(false); return; }
        const digits = document.getElementById('code-digits');
        const age = document.getElementById('code-age');
        // textContent, never innerHTML — this is read off a phone's inbox.
        if (digits) digits.textContent = d.code || '';
        if (age) {
          age.textContent = d.said
            ? codeAgeSays(d.age) + ' · ' + d.said
            : codeAgeSays(d.age);
        }
        card.classList.toggle('stale', (d.age || 0) >= CODE_STALE);
        showCodeAs(true);
      })
      // A failed poll leaves whatever is on screen alone. Hiding the card on
      // a dropped request would take a live code off the page from somebody
      // mid-way through reading it.
      .catch(() => {});
  }

  // ---------------------------------------------------------- adopted (10.6a)
  // A party who cannot hold a stylus adopts a signature once and afterwards
  // applies it with one press of their own. The press is the whole point —
  // nothing on a timer reaches any of this, and the server enforces that far
  // more seriously than this file can (see enrolled.py).
  //
  // What crosses the wire is a NAME. The strokes stay on the Pi, so this
  // screen can put somebody's signature in front of them and cannot obtain it.
  // Whose pad is in front, as the app itself names it, and which adopted
  // party that resolves to. Both come from the screen push; both are "" on
  // every screen that is not a signature pad.
  let signerNamed = '';
  let signerAdopted = '';

  function renderAdopted(parties) {
    const wrap = document.getElementById('sign-adopted');
    const row = document.getElementById('sign-adopted-row');
    if (!wrap || !row) return;
    row.textContent = '';
    for (const p of (parties || [])) {
      if (!p || !p.name) continue;
      const b = document.createElement('button');
      b.type = 'button';
      // textContent, never innerHTML: this is a person's name out of a file
      // somebody types into, and it is going onto a page.
      b.textContent = p.name;
      b.dataset.name = p.name;
      row.appendChild(b);
    }
    wrap.hidden = !row.children.length;
    markSigner();
  }

  // THE APP SAYS WHOSE SIGNATURE IT IS ASKING FOR, SO THE PAD SAYS IT TOO.
  //
  // inMyTeam's exit is two identical pads back to back — the patient's, then
  // the caregiver's — and until now the sheet looked the same on both. A row
  // of names with nothing marking which one this screen wants is how the
  // wrong person's signature goes onto a visit record.
  //
  // It MARKS; it does not press, and it does not pick on her behalf. Every
  // adopted party stays on the row and stays pressable, because REQ-10.6a
  // rests on the press belonging to the person it belongs to — and because a
  // match this got wrong must be correctable by the person looking at it.
  //
  // The matching is the server's (`enrolled.who_signs`): it is tolerant of a
  // middle initial and of case, and it returns NOTHING when more than one
  // party could be meant. This file only compares two strings it was handed.
  function markSigner() {
    const row = document.getElementById('sign-adopted-row');
    const heading = document.getElementById('sign-whose');
    if (heading) {
      // textContent: the app's own rendering of somebody's legal name.
      heading.textContent = signerNamed
        ? (i18n.signWhose || '').replace('{who}', signerNamed) : '';
      heading.hidden = !signerNamed;
    }
    if (!row) return;
    // Only when the server resolved exactly one party. Without it every pill
    // stays filled, which is an honest picture of two people who could sign;
    // with it the one this screen asked for is the only filled one.
    row.classList.toggle('aimed', !!signerAdopted);
    for (const b of row.children) {
      const mine = !!signerAdopted && b.dataset.name === signerAdopted;
      b.classList.toggle('primary', mine);
      // Said out loud as well as drawn, because the difference between the
      // two buttons is which person's signature goes on a legal record.
      if (mine) b.setAttribute('aria-current', 'true');
      else b.removeAttribute('aria-current');
    }
  }

  function loadAdopted() {
    fetch('/signature/roster')
      .then((r) => (r.ok ? r.json() : { parties: [] }))
      .then((d) => renderAdopted(d.parties))
      .catch(() => {});
  }

  // ------------------------------------------------- the mapping (REQ-10.6a)
  // WHO WILL BE ASKED TO SIGN, AND WHO IS NOT SET UP YET.
  //
  // Adoption used to happen in one place only — inside the pad, which opens
  // on a signature screen — so the question worth asking BEFORE a visit had
  // nowhere to be asked: is this patient set up, or do we find out standing in
  // her living room?
  //
  // Built from the schedule rather than from the store, because the roster
  // answers the wrong half: it lists who HAS adopted, and a list of the
  // finished ones can never tell you what is left.
  function renderMap(people) {
    const list = document.getElementById('sigmap-list');
    const empty = document.getElementById('sigmap-empty');
    const count = document.getElementById('sigmap-count');
    if (count) {
      const have = (people || []).filter((p) => p.adopted).length;
      // Drawn even at zero: nought of three is the reading worth seeing.
      count.textContent = people && people.length
        ? have + '/' + people.length : '';
    }
    if (!list) return;
    list.textContent = '';
    for (const p of (people || [])) {
      if (!p || !p.name) continue;
      const row = document.createElement('div');
      row.className = 'sigrow ' + (p.adopted ? 'has' : 'none')
        + (p.on_schedule ? '' : ' stray');

      const grow = document.createElement('div');
      grow.className = 'grow';
      const who = document.createElement('div');
      who.className = 'who';
      // textContent everywhere below: these are patients' names.
      who.textContent = p.name;
      // WHICH SIGNATURE THIS IS. Every check-out collects two — the
      // patient's and the caregiver's — and a list of bare names cannot say
      // which row is which, on the one screen where confusing them means a
      // signature registered against the wrong party.
      if (p.role) {
        const tag = document.createElement('span');
        tag.className = 'sigrole ' + p.role;
        tag.textContent = p.role === 'staff' ? (i18n.sigRoleStaff || '')
                                             : (i18n.sigRolePatient || '');
        who.appendChild(tag);
      }
      grow.appendChild(who);

      const meta = document.createElement('div');
      meta.className = 'meta';
      const state = document.createElement('span');
      state.className = 'state';
      state.textContent = p.adopted ? (i18n.sigOnFile || '')
        : (i18n.sigMissing || '');
      meta.appendChild(state);
      // What else is true about the row, in one line: which apps will put
      // this person in front of a pad, whether the adoption is filed under
      // a different spelling than the schedule uses — which is worth saying,
      // because that difference is exactly what the matcher had to be
      // tolerant of — and the witness line, on the older records that still
      // carry one.
      const bits = [];
      if (p.app) bits.push(p.app);
      if (!p.on_schedule) bits.push(i18n.sigNotScheduled || '');
      if (p.adopted && p.adopted_as && p.adopted_as !== p.name) {
        bits.push((i18n.sigFiledAs || '').replace('{as}', p.adopted_as));
      }
      if (p.witness) bits.push(p.witness);
      if (bits.length) {
        const rest = document.createElement('span');
        rest.textContent = ' · ' + bits.join(' · ');
        meta.appendChild(rest);
      }
      grow.appendChild(meta);
      row.appendChild(grow);

      // WITHDRAWING ONE, on the rows that have one. The first signature ever
      // registered here was a test scribble saved under a patient's name by
      // accident, and there was no way to take it off the machine from the
      // machine — the only route to `/signature/forget` was a shell.
      //
      // A signature under the wrong person's name is the exact failure this
      // whole feature is careful about, so undoing it cannot be the one thing
      // that needs a laptop and a tailnet.
      if (p.adopted) {
        const rm = document.createElement('button');
        rm.type = 'button';
        rm.className = 'sigforget';
        rm.textContent = i18n.sigForget || '';
        rm.dataset.forgetFor = p.adopted_as || p.name;
        // The adoption's OWN apps, not the row's: withdrawing the mark she
        // uses for two apps from one of their rows must take that mark, and
        // nothing else of hers.
        rm.dataset.forgetApps = JSON.stringify(p.adopted_for || []);
        row.appendChild(rm);
      }

      // ONE BUTTON, AND IT DOES NOT DRAW ANYTHING. Adoption still means the
      // pad and the person; this only carries the name across so
      // nobody types it twice and so the adoption lands under the spelling
      // the schedule uses.
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = p.adopted ? (i18n.sigReplace || '') : (i18n.sigAdopt || '');
      b.dataset.adoptFor = p.name;
      // WHICH APP THIS ROW IS FOR, carried through to the registration so the
      // mark lands scoped to it. The caregiver signs inMyTeam one way and the
      // other two another, so a signature saved without an app would be drawn
      // on all three.
      b.dataset.adoptApp = p.package || '';
      row.appendChild(b);
      list.appendChild(row);
    }
    if (empty) empty.hidden = !!list.children.length;
  }

  function loadMap() {
    fetch('/signature/map')
      .then((r) => (r.ok ? r.json() : { people: [] }))
      .then((d) => renderMap(d.people))
      .catch(() => {});
  }

  // From a row to the pad, with the name already in the box. The pad is
  // where an adoption has to happen — the strokes being adopted are the ones
  // on that canvas, drawn by the person in the room — so this opens it and
  // fills the field, and stops there.
  // TWO PRESSES, BECAUSE IT DELETES THE ONLY COPY.
  //
  // The strokes are not archived anywhere — that is the point of them never
  // leaving the machine — so withdrawing an adoption is not undoable, and the
  // person has to be in the room again to make another. A stray thumb on a
  // list of patients must not be able to do that.
  //
  // The button asks on the button itself rather than in a dialog: this page
  // has no modal, and adding one for this would be a new pattern to learn for
  // the least-used control on it. It disarms itself after a few seconds, so a
  // press somebody thought better of does not sit there armed.
  const FORGET_ARMED = 4000;
  let forgetTimer = 0;

  function disarmForget() {
    clearTimeout(forgetTimer);
    forgetTimer = 0;
    const armed = document.querySelector('.sigforget.armed');
    if (armed) {
      armed.classList.remove('armed');
      armed.textContent = i18n.sigForget || '';
    }
  }

  function forgetPressed(btn) {
    const name = btn.dataset.forgetFor || '';
    if (!btn.classList.contains('armed')) {
      disarmForget();
      btn.classList.add('armed');
      btn.textContent = i18n.sigForgetSure || '';
      forgetTimer = setTimeout(disarmForget, FORGET_ARMED);
      return;
    }
    disarmForget();
    let apps = [];
    try { apps = JSON.parse(btn.dataset.forgetApps || '[]'); }
    catch (e) { apps = []; }
    fetch('/signature/forget', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, apps: apps })
    }).then((r) => {
      if (!r.ok) { toast(i18n.failed || ''); return; }
      toast(i18n.sigForgotten || '');
      // Both lists: the row here goes back to "not registered", and the pad's
      // own adopted row must stop offering a signature that no longer exists.
      loadMap();
      loadAdopted();
    }).catch(() => toast(i18n.failed || ''));
  }

  // Which app the registration in progress is for. "" means every app, which
  // is what a row without one asks for and what every adoption made before
  // per-app marks existed already is.
  let adoptForApp = '';

  function adoptFrom(name, forApp) {
    // THE REGISTRATION SHEET, NOT THE CHECK-OUT PAD.
    //
    // This used to open `#signsheet`, which is the pad a patient signs on at
    // the end of a visit: numbered steps, "Draw it on the phone", the app's
    // own confirm, a preview rehearsing the replay. Opening it to register a
    // signature showed all of that with no app in front and nothing to
    // replay onto — the wrong component for the job, and reported as one.
    adoptForApp = forApp || '';
    const field = document.getElementById('enrol-name');
    if (field) field.value = name || '';
    const forWhat = document.getElementById('enrol-forapp');
    if (forWhat) {
      forWhat.textContent = adoptForApp
        ? (i18n.sigForApp || '').replace('{app}', appNames[adoptForApp]
                                         || adoptForApp) : '';
      forWhat.hidden = !adoptForApp;
    }
    // A fresh canvas every time. Strokes are shared state between the two
    // pads, so whatever was on the other one is not this person's signature.
    pad.strokes = [];
    body.classList.remove('signing');
    // And any leftover "saved" state from the last person: opening this sheet
    // is always the start of a registration, never the end of one.
    body.classList.remove('enrolled');
    body.classList.add('enrolling');
    padFit();
    padRedraw();
  }

  function applyAdopted(name) {
    if (!driving() || !name) return;
    busy(i18n.signSending || '');
    fetch('/signature/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, package: currentPackage || '' })
    }).then(async (r) => {
      const out = await r.json().catch(() => ({}));
      if (!r.ok) {
        unbusy();
        // An adoption withdrawn on another device, or a store that has moved
        // underneath this page. Saying which beats a bare "failed" — one means
        // draw it by hand, the other means try again.
        toast((out.error === 'not_enrolled' ? i18n.adoptGone : i18n.failed)
              || '');
        loadAdopted();
        return;
      }
      // From here it is the ordinary replay: same status push, same lock, same
      // step two. An adopted signature is not a different kind of ink.
      pad.waitingId = out.id || '';
      padWaiting(true);
    }).catch(() => { unbusy(); toast(i18n.failed || ''); });
  }

  // ONE POST, FROM TWO PLACES, because there are two honest moments to adopt
  // a signature in and only one set of rules about doing it.
  //
  //   * the check-out pad, straight after the patient has drawn one for real;
  //   * the registration sheet, sat down with them ahead of a visit.
  //
  // Two copies of this fetch would be two places for the empty-pad check and
  // the refresh afterwards to drift apart, on a feature whose refusals are
  // the requirement (REQ-10.6a).
  // NO WITNESS FIELD. It asked, in front of a patient, a question whose answer
  // was always the same two people — and a box filled in the same way every
  // time is not a record of anything. Removed on the owner's instruction; the
  // date, the name and the audit of every later use are what is kept, and an
  // honest empty field beats an attestation nobody meant. The server no longer
  // refuses an adoption for the want of one.
  function postEnrolment(name, done) {
    if (!name.trim()) { toast(i18n.adoptNeedName || ''); return; }
    if (!pad.strokes.length) { toast(i18n.signEmpty || ''); return; }
    const rect = padCanvas().getBoundingClientRect();
    fetch('/signature/enroll', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim(), strokes: pad.strokes,
                             aspect: rect.width / rect.height,
                             apps: adoptForApp ? [adoptForApp] : [] })
    }).then(async (r) => {
      if (!r.ok) {
        // NAME THE FAULT. A generic failure here said "that didn't reach the
        // phone" over a Pi that could not write its own store — a sentence
        // pointing at the one thing that was working. The store's own answer
        // gets the store's own sentence.
        const out = await r.json().catch(() => ({}));
        toast((out.error === 'store_unwritable'
               ? i18n.adoptNoStore : i18n.failed) || '');
        return;
      }
      toast(i18n.adoptSaved || '');
      loadAdopted();
      // And the mapping, which is now one person further along. Without this
      // the front page's count keeps saying what it said before the adoption
      // that just happened in front of two people.
      loadMap();
      if (done) done();
    }).catch(() => toast(i18n.failed || ''));
  }

  function adoptSave() {
    postEnrolment((document.getElementById('adopt-name') || {}).value || '',
                  () => {
                    const form = document.getElementById('sign-adopt');
                    if (form) form.hidden = true;
                  });
  }

  // The registration sheet's own save. Closes the sheet and empties the pad:
  // the next person to register one must not find the last person's strokes
  // waiting on the canvas.
  function enrolSave() {
    postEnrolment((document.getElementById('enrol-name') || {}).value || '',
                  enrolDone);
  }

  // WHAT WAS ACTUALLY SAVED, SHOWN BEFORE ANYBODY WALKS AWAY.
  //
  // The first registration on this machine was a test scribble stored under a
  // patient's name, and it was found out afterwards. The moment to catch that
  // is while both people are still sitting there.
  //
  // Drawn from `pad.strokes` — the strokes this browser JUST SENT, already in
  // hand. Nothing is fetched back and no route returns a signature, which is
  // REQ-10.6a condition 2 and is not negotiable for a picture. It is a preview
  // of what went in, not a reading of what is on file.
  //
  // The pad goes inert underneath: the strokes on it are now a record of
  // something saved, and a stray touch must not add to them.
  function enrolDone() {
    body.classList.add('enrolled');
    const said = document.getElementById('enrol-saved-who');
    const name = (document.getElementById('enrol-name') || {}).value || '';
    if (said) {
      said.textContent = (i18n.sigSavedFor || '').replace('{who}', name.trim());
    }
  }

  // Not right. Straight back to a blank pad with the name still in the box —
  // the row already carries a signature at this point, and saving again
  // replaces it, so there is nothing to undo first.
  function enrolAgain() {
    body.classList.remove('enrolled');
    pad.strokes = [];
    padRedraw();
  }

  function closeEnrol() {
    body.classList.remove('enrolling');
    body.classList.remove('enrolled');
    pad.strokes = [];
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
      // How many of us are on. Shown only when it is more than one: a badge
      // reading "1" all day is furniture, and the fact worth having is that
      // somebody ELSE is here — one phone, two sets of hands.
      if (msg.viewers !== undefined) {
        const badge = document.getElementById('watchers');
        const n = Number(msg.viewers) || 0;
        if (badge) {
          badge.hidden = n < 2;
          const count = document.getElementById('watchers-n');
          if (count) count.textContent = String(n);
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

  // ---------------------------------------------------------- the schedule
  //
  // The page arrives with the answer already in it — server-rendered, because
  // this is the module she opens the portal to read and a skeleton that fills
  // in half a second later looks broken on a bad connection. Everything here
  // is about what happens AFTER that first paint.
  //
  // One job: the refresh, because the next visit becomes the current one
  // while the page sits open on a kitchen counter. It re-reads rather than
  // reloads — a reload would take her out of whatever else she was doing.
  //
  // Nothing here animates. Two cards, both still, both a way into their own
  // app. The version between these two turned a drum on a timer and it was
  // the wrong answer twice over: motion nobody asked for, on the one part of
  // the page somebody is actually trying to read.
  const SCHEDULE_EVERY = 60000;

  function paintUpNext(plan) {
    const card = document.getElementById('upnext');
    if (!card || !plan || !plan.ok) return;
    const v = plan.current || plan.next;
    if (!v) return;
    const cap = card.querySelector('.cap');
    const who = card.querySelector('.who');
    const when = card.querySelector('.when');
    const where = card.querySelector('.where');
    card.dataset.running = v.running ? '1' : '0';
    if (cap) {
      cap.innerHTML = '';
      if (v.running) {
        const dot = document.createElement('span');
        dot.className = 'live';
        cap.appendChild(dot);
      }
      // textContent, not innerHTML: a patient's name is somebody else's
      // words and this page has no business interpreting them as markup.
      cap.appendChild(document.createTextNode(
        v.running ? i18n.schedNow : i18n.schedUpcoming));
    }
    // The app's mark, beside the name, in the app's own colour — the same
    // one the springboard tile carries, so the card and the tile match by
    // sight. Rebuilt rather than left alone because the visit on this card
    // changes, and with it the app: a badge that stayed put would say iMT
    // over a patient who is now on Exchange+.
    if (who) {
      who.textContent = '';
      if (v.mark) {
        const badge = document.createElement('span');
        badge.className = 'appbadge';
        badge.style.background = v.accent || '#666';
        badge.textContent = v.mark;
        who.appendChild(badge);
      }
      // textContent for the name itself: a patient's name is somebody
      // else's words and this page has no business reading them as markup.
      who.appendChild(document.createTextNode(v.patient));
    }
    if (when) {
      when.textContent = v.fires + ' – ' + v.ends;
      if (v.buffered) {
        const tag = document.createElement('span');
        tag.className = 'buffered';
        tag.textContent = i18n.schedBuffered;
        when.appendChild(document.createTextNode(' '));
        when.appendChild(tag);
      }
    }
    if (where) {
      where.textContent = v.day + ' · ' + v.app
        + (v.agency ? ' · ' + v.agency : '');
    }
    // The app moves with the visit. Forgetting this is how a card that says
    // "Marina · Mobile Caregiver+" ends up opening Exchange+ because that is
    // who was on it an hour ago.
    card.dataset.package = v.package || '';
    card.dataset.macro = v.macro || '';
    card.dataset.open = v.open || '';
    card.dataset.name = v.app || '';
    card.dataset.agency = v.agency || '';
  }

  // The card under it: the one visit after the one above, and nothing else.
  //
  // IT WAS A TURNING DRUM. The wheel showed one at a time, which was right,
  // and it moved on its own, which was not — asked for plainly: "I don't like
  // the behaviour of the wheel, maybe simple is better and just displays the
  // next patient". A card that sits still says the same thing with nothing to
  // wait for and nothing competing for the eye of somebody reading the card
  // above it.
  function paintAfter(plan) {
    const card = document.getElementById('after');
    if (!card || !plan || !plan.ok) return;
    const v = (plan.queue || [])[0];
    card.hidden = !v;
    if (!v) return;
    const who = card.querySelector('.who');
    const sub = card.querySelector('.sub');
    // textContent throughout: a patient's name is somebody else's words and
    // this page has no business interpreting them as markup.
    // The badge rides the NAME here too, as it does on the card above — it
    // used to lead this card's grey sub-line, and two cards putting the same
    // fact in two different places is something that has to be learned
    // rather than seen.
    if (who) {
      who.textContent = '';
      if (v.mark) {
        const badge = document.createElement('span');
        badge.className = 'appbadge';
        badge.style.background = v.accent || '#666';
        badge.textContent = v.mark;
        who.appendChild(badge);
      }
      who.appendChild(document.createTextNode(v.patient));
    }
    if (sub) {
      sub.textContent = v.day + ' · ' + v.fires + ' – ' + v.ends;
    }
    card.dataset.package = v.package || '';
    card.dataset.macro = v.macro || '';
    card.dataset.open = v.open || '';
    card.dataset.name = v.app || '';
    card.dataset.agency = v.agency || '';
  }

  function refreshSchedule() {
    fetch('/api/schedule', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : null))
      .then((plan) => { if (plan) { paintUpNext(plan); paintAfter(plan); } })
      .catch(() => { /* the card keeps the last answer it had */ });
  }

  // A visit is a way INTO the app that holds it. The card and every face of
  // the drum carry a tile's three attributes, so this hands the element
  // straight to `launch` — the same path the springboard uses, which means
  // the already-in-front shortcut and the sign-in ceremony behave identically
  // wherever she pressed. A visit whose app is not one of the tiles (a
  // schedule naming a package that is not installed) carries no macro, and
  // `launch` already refuses those.
  // The one app with more than one agency on the account. A visit row knows
  // which of them its patient belongs to, so pressing the row can answer that
  // question instead of asking it.
  const MULTI_AGENCY = 'com.hhaexchange.uma';

  function openVisitsApp(el) {
    const target = el.closest('[data-macro]');
    if (!target) return;
    const agency = target.dataset.agency || '';
    if (target.dataset.package === MULTI_AGENCY && agency) {
      // The agency walk subsumes opening the app — it activates it first —
      // so this is one macro, not two. Running `launch` as well would race
      // the sign-in ceremony against a provider switch on the same phone.
      awaitingMacro = true;
      pendingApp = target.dataset.package;
      view('screen');
      busy((i18n.opening || '').replace('{app}', target.dataset.name || ''),
           60000);
      fetch('/macro', {
        method: 'POST',
        body: new URLSearchParams({ name: 'uma_agency_for', arg: agency }),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        redirect: 'follow'
      }).catch(() => {
        awaitingMacro = false; pendingApp = ''; unbusy();
        toast(i18n.failed || '');
      });
      return;
    }
    launch(target);
  }

  function wireSchedule() {
    const after = document.getElementById('after');
    if (after) after.addEventListener('click', () => openVisitsApp(after));
    const card = document.getElementById('upnext');
    if (card && card.dataset.macro) {
      card.addEventListener('click', () => openVisitsApp(card));
    }
    // The week. One listener on the list rather than one per row, because the
    // rows are server-rendered and there can be forty of them.
    const week = document.querySelector('#scheduleview .body');
    if (week) week.addEventListener('click', (ev) => openVisitsApp(ev.target));
    const open = document.getElementById('btn-schedule');
    if (open) open.addEventListener('click', () => view('schedule'));

    // The sign-in code, to the phones that do not have it. Disabled while it
    // runs: reading the inbox is an adb round trip against a phone that is
    // usually busy, and a second press would send everybody a second text.
    // The live code, polled so its age stays true. A code whose age is
    // stale-by-a-minute is worse than no code: it is the one she types.
    refreshCode();
    setInterval(refreshCode, CODE_EVERY);
    const digits = document.getElementById('code-digits');
    if (digits) digits.addEventListener('click', copyCode);

    // The same broadcast from either control — the full-width button when
    // there is no code to show, the small one inside the card when there is.
    // One function, because two copies of a fetch that texts three people is
    // two places for the disable-while-running guard to be forgotten in.
    function broadcastCode(pressed) {
      pressed.disabled = true;
      busy(i18n.codeSending || '');
      fetch('/code/broadcast', { method: 'POST' })
        .then(async (r) => {
          const out = await r.json().catch(() => ({}));
          unbusy();
          if (!r.ok) { toast(i18n.codeFailed || ''); return; }
          // Three different answers, because they need three different
          // things done about them. "Nobody is on the list" is a setup
          // problem, "no code arrived" means sign in again to make one, and
          // a count means look at your phone.
          if (!out.of) toast(i18n.codeNobody || '');
          else if (!out.found) toast(i18n.codeNone || '');
          else toast((i18n.codeSent || '').replace('{n}', out.sent)
                     .replace('{of}', out.of));
        })
        .catch(() => { unbusy(); toast(i18n.codeFailed || ''); })
        .then(() => { pressed.disabled = false; });
    }
    for (const id of ['btn-code', 'btn-code-again']) {
      const el = document.getElementById(id);
      if (el) el.addEventListener('click', () => broadcastCode(el));
    }
    // Back to Home, in the same place and with the same chevron the screen
    // view puts its way back — one habit rather than two.
    const back = document.getElementById('btn-sched-home');
    if (back) back.addEventListener('click', () => view('launcher'));
    const arm = document.getElementById('btn-arm');
    if (arm) arm.addEventListener('click', () => view('arm'));
    const armBack = document.getElementById('btn-arm-home');
    if (armBack) armBack.addEventListener('click', () => view('launcher'));

    // Adopted signatures. Read on the way in as well as at boot: an adoption
    // made on the pad while this view was closed has to be reflected the next
    // time it opens, or the count on the front page lies about who is set up.
    loadMap();
    const sigs = document.getElementById('btn-signatures');
    if (sigs) sigs.addEventListener('click', () => { loadMap(); view('signatures'); });
    const sigBack = document.getElementById('btn-sig-home');
    if (sigBack) sigBack.addEventListener('click', () => view('launcher'));
    // Delegated: the rows are rebuilt on every read, and a handler bound to a
    // row would go with it.
    const sigList = document.getElementById('sigmap-list');
    if (sigList) sigList.addEventListener('click', (ev) => {
      if (!ev.target.closest) return;
      const drop = ev.target.closest('[data-forget-for]');
      if (drop) { forgetPressed(drop); return; }
      const hit = ev.target.closest('[data-adopt-for]');
      if (!hit) return;
      // Any other press on the list is an answer of "no" to a button left
      // armed — safer than leaving it primed behind whatever she does next.
      disarmForget();
      adoptFrom(hit.dataset.adoptFor, hit.dataset.adoptApp);
    });
    wireArming();
    setInterval(refreshSchedule, SCHEDULE_EVERY);
  }

  // ---------------------------------------------------------- what is armed
  //
  // A switch here changes what the machine is ALLOWED to do, so it is not
  // moved until the server says it moved. An optimistic flip would leave a
  // control reading "on" over a machine that never recorded it — which, for
  // this particular switch, is the worst possible way to be wrong.
  //
  // Not gated on `driving()`: this touches a file on the controller, not the
  // phone, and the coach-mode rule is about not pressing things on somebody
  // else's screen.
  function wireArming() {
    for (const sw of document.querySelectorAll('.sw')) {
      sw.addEventListener('click', () => {
        if (sw.disabled) return;
        const want = sw.getAttribute('aria-pressed') !== 'true';
        sw.disabled = true;
        fetch('/schedule/arm', {
          method: 'POST',
          body: new URLSearchParams({ key: sw.dataset.key, on: want ? '1' : '0' }),
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
        }).then((r) => (r.ok ? r.json() : null))
          .then((doc) => {
            if (doc) sw.setAttribute('aria-pressed', doc.armed ? 'true' : 'false');
            else toast(i18n.failed || '');
          })
          .catch(() => toast(i18n.failed || ''))
          .finally(() => { sw.disabled = false; });
      });
    }
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
    // 'schedule' joins 'screen' as a view a caller may ask for. Both are
    // remembered; neither is guessed — anything else falls through to the
    // picker, which is where the page boots.
    const DEEP = { screen: 1, schedule: 1 };
    try {
      const remembered = sessionStorage.getItem('aptlog-view') || '';
      if (DEEP[asked]) view(asked);
      else if (DEEP[remembered]) view(remembered);
    } catch (e) {
      if (DEEP[asked]) view(asked);
    }

    wireSchedule();

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
      // The view change is HERS ALONE and happens either way — going back to
      // her own picker is not touching the phone. Sending the phone home is,
      // and that half waits on watch-only like everything else. Found by the
      // test that derives this list from the source rather than from memory.
      view('launcher');
      if (!driving()) return;
      if (socket && socket.readyState === 1) {
        socket.send(JSON.stringify({ type: 'device', action: 'home' }));
      }
    });
    const offapp = document.getElementById('offapp-apps');
    if (offapp) offapp.addEventListener('click', () => view('launcher'));
    // The way out from under a system panel. It is a macro rather than a
    // keypress because a keypress is exactly what does not work here: this
    // phone swallows the collapse command, Back and Home alike, and only a
    // swipe from the top actually shuts the shade. The macro swipes, checks,
    // and brings the care app back — nothing is force-stopped and no visit
    // is touched, so pressing it at a bad moment costs a second.
    const uncover = document.getElementById('covered-clear');
    if (uncover) uncover.addEventListener('click', () => {
      if (!driving()) return;
      awaitingMacro = true;
      busy(i18n.clearing || '', 30000);
      fetch('/macro', {
        method: 'POST',
        body: new URLSearchParams({ name: 'clear_screen' }),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        redirect: 'follow'
      }).catch(() => { awaitingMacro = false; unbusy(); toast(i18n.failed || ''); });
    });
    // Answer the app's own update wall. Unlike every other button on this
    // page, this one asks first: it replaces the version of the app on the
    // phone, there is no going back to the old build from here, and every
    // rule the portal has for reading that app was written against the build
    // it is about to remove. The ceiling is generous because the install is
    // a download — a spinner that gives up while Play is still working is
    // how a finished update gets reported as a failure.
    // Ask inMyTeam to text another code. Asks first, because it closes the
    // app, signs in again and sends a real message — and the code she is
    // already holding stops working the moment a new one is issued.
    const resendRun = document.getElementById('resend-code');
    if (resendRun) resendRun.addEventListener('click', () => {
      if (!driving()) return;
      const ask = resendRun.getAttribute('data-confirm');
      if (ask && !window.confirm(ask)) return;
      // The echo is about the code that is being replaced.
      sentEcho = null;
      awaitingMacro = true;
      busy(i18n.resending || '', 90000);
      fetch('/macro', {
        method: 'POST',
        body: new URLSearchParams({ name: 'inmyteam_resend_code' }),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        redirect: 'follow'
      }).catch(() => { awaitingMacro = false; unbusy(); toast(i18n.failed || ''); });
    });
    const walledRun = document.getElementById('walled-update');
    if (walledRun) walledRun.addEventListener('click', () => {
      if (!driving()) return;
      const ask = walledRun.getAttribute('data-confirm');
      if (ask && !window.confirm(ask)) return;
      awaitingMacro = true;
      busy(i18n.updating || '', 480000);
      fetch('/macro', {
        method: 'POST',
        body: new URLSearchParams({ name: 'update_app' }),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        redirect: 'follow'
      }).catch(() => { awaitingMacro = false; unbusy(); toast(i18n.failed || ''); });
    });
    // Tick every required task on the plan of care in front. It only ADDS
    // ticks and it never presses Salvar or Check out, so the worst a press
    // at a bad moment costs is a page she then reads back — which she was
    // going to do anyway. Saving stays hers.
    const tasksRun = document.getElementById('btn-tasks');
    if (tasksRun) tasksRun.addEventListener('click', () => {
      if (!driving()) return;
      awaitingMacro = true;
      busy(i18n.checkingTasks || '', 60000);
      fetch('/macro', {
        method: 'POST',
        body: new URLSearchParams({ name: 'check_tasks' }),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        redirect: 'follow'
      }).catch(() => { awaitingMacro = false; unbusy(); toast(i18n.failed || ''); });
    });
    // The work log, in one press. It names the patient itself — the visit
    // running now, or the soonest one whose record can be read — because
    // asking the person watching an armed entry to name which patient the
    // scheduler picked is asking the wrong person.
    const checksRun = document.getElementById('btn-checks');
    if (checksRun) checksRun.addEventListener('click', () => {
      if (!driving()) return;
      awaitingMacro = true;
      busy(i18n.readingLog || '', 90000);
      fetch('/macro', {
        method: 'POST',
        body: new URLSearchParams({ name: 'evv_checks' }),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        redirect: 'follow'
      }).catch(() => { awaitingMacro = false; unbusy(); toast(i18n.failed || ''); });
    });

    // Back to the app's own first page. A different move from the picker
    // button beside it, and the answer to "if I click on the wrong patient
    // then what, I'm stuck?" — the session is alive, the app is just three
    // screens deep, and reopening it would spend a sign-in for nothing.
    const appHome = document.getElementById('btn-apphome');
    if (appHome) appHome.addEventListener('click', () => {
      if (!driving()) return;
      awaitingMacro = true;
      busy(i18n.goingHome || '', 40000);
      fetch('/macro', {
        method: 'POST',
        body: new URLSearchParams({ name: 'app_home' }),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        redirect: 'follow'
      }).catch(() => { awaitingMacro = false; unbusy(); toast(i18n.failed || ''); });
    });
    // To the provider picker, so the rest of the round's patients can be
    // reached. This one STOPS at the picker rather than choosing: pressed
    // from the toolbar there is no patient to read an agency off, and
    // guessing "the other one" would be wrong the moment a third appears.
    // Pressing a patient is the path that knows the answer — see
    // `openVisitsApp` — and this is the path for when nobody pressed one.
    //
    // A minute, not forty seconds: choosing a provider makes the app drop
    // its whole schedule and re-ask the server, and the walk to the picker
    // alone is four taps with a settle after each.
    const agencyRun = document.getElementById('btn-agency');
    if (agencyRun) agencyRun.addEventListener('click', () => {
      if (!driving()) return;
      awaitingMacro = true;
      busy(i18n.switchingAgency || '', 60000);
      fetch('/macro', {
        method: 'POST',
        body: new URLSearchParams({ name: 'uma_agency' }),
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        redirect: 'follow'
      }).catch(() => { awaitingMacro = false; unbusy(); toast(i18n.failed || ''); });
    });
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

    const coachBtn = document.getElementById('coach');
    if (coachBtn) coachBtn.addEventListener('click', () => setCoaching(!coaching));
    // Remembered per browser, like the view is: a reload in the middle of
    // watching somebody work must not quietly hand the phone back.
    let wasCoaching = '';
    try { wasCoaching = sessionStorage.getItem('aptlog-coach') || ''; }
    catch (e) { /* private mode */ }
    setCoaching(!!wasCoaching);

    // READ THE ROSTER EVERY TIME THE PAD OPENS.
    //
    // It was read once, at page load, and after an enrolment made in THIS
    // browser. So a signature registered anywhere else — the other person's
    // phone, a second tab, this page left open since before the registration
    // — never appeared here at all: the pad opened on a check-out screen with
    // no adopted button and no way to get one without a reload.
    //
    // Reported from the room, with Carmen registered on the Pi and her button
    // missing from the pad. Two people share this portal, so "the state this
    // tab last saw" was never a safe thing to draw a signature list from.
    const openPad = () => {
      body.classList.toggle('signing');
      if (body.classList.contains('signing')) loadAdopted();
      padFit();
    };
    const sign = document.getElementById('btn-sign');
    if (sign) sign.addEventListener('click', openPad);
    // The canvas drawn into the page opens the same pad. Delegated, because
    // the page is re-rendered from the socket and a handler bound to the
    // element itself would go with it on the first repaint.
    const signSurface = document.getElementById('screenwrap');
    if (signSurface) signSurface.addEventListener('click', (ev) => {
      const hit = ev.target.closest && ev.target.closest('[data-sign]');
      if (!hit) return;
      ev.preventDefault();
      if (!body.classList.contains('signing')) openPad();
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
        // The building's hour, with the zone said out loud — this clock
        // mirrors a phone standing in Florida, and the person reading it
        // may not be. Falls back to the reader's own clock only if the
        // zone will not resolve, which is better than an empty face.
        const opts = { hour: 'numeric', minute: '2-digit' };
        const zone = (window.APTLOG_APP || {}).zone;
        if (zone) { opts.timeZone = zone; opts.timeZoneName = 'short'; }
        try {
          clock.textContent = new Date().toLocaleTimeString([], opts);
        } catch (e) {
          clock.textContent = new Date().toLocaleTimeString([], {
            hour: 'numeric', minute: '2-digit' });
        }
      };
      tick();
      setInterval(tick, 15000);
    }
    padWire(document.getElementById('signpad'));
    padWire(document.getElementById('enrolpad'));
    swipeToDismiss(document.getElementById('signsheet'),
                   () => body.classList.remove('signing'));
    swipeToDismiss(document.getElementById('scansheet'),
                   () => body.classList.remove('reading'));
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

    // Adopted signatures (REQ-10.6a). Delegated, because the row is rebuilt
    // every time the roster is read and per-button listeners would not
    // survive it.
    const adoptedRow = document.getElementById('sign-adopted-row');
    if (adoptedRow) adoptedRow.addEventListener('click', (ev) => {
      const b = ev.target.closest('button');
      if (b && b.dataset.name) applyAdopted(b.dataset.name);
    });
    const adoptOpen = document.getElementById('sign-adopt-open');
    if (adoptOpen) adoptOpen.addEventListener('click', () => {
      const form = document.getElementById('sign-adopt');
      if (form) form.hidden = !form.hidden;
    });
    const adoptBtn = document.getElementById('adopt-save');
    if (adoptBtn) adoptBtn.addEventListener('click', adoptSave);
    loadAdopted();

    // The registration sheet's own controls. Its Undo and Erase act on the
    // same `pad.strokes` the check-out pad uses — only one sheet is ever
    // open, so one set of strokes is the whole state either way.
    const enrolClear = document.getElementById('enrol-clear');
    if (enrolClear) enrolClear.addEventListener('click', () => {
      pad.strokes = []; padRedraw();
    });
    const enrolUndo = document.getElementById('enrol-undo');
    if (enrolUndo) enrolUndo.addEventListener('click', () => {
      pad.strokes.pop(); padRedraw();
    });
    const enrolBtn = document.getElementById('enrol-save');
    if (enrolBtn) enrolBtn.addEventListener('click', enrolSave);
    const enrolCancel = document.getElementById('enrol-cancel');
    if (enrolCancel) enrolCancel.addEventListener('click', closeEnrol);
    const enrolAgainBtn = document.getElementById('enrol-again');
    if (enrolAgainBtn) enrolAgainBtn.addEventListener('click', enrolAgain);
    const enrolClose = document.getElementById('enrol-close');
    if (enrolClose) enrolClose.addEventListener('click', closeEnrol);
    swipeToDismiss(document.getElementById('enrolsheet'), closeEnrol);

    // The app's own Borrar/Salvar, relayed. The outcome rides the same
    // sign-status push the replay uses, so the toast comes back rendered
    // ("done" / "this screen has no signature box") without new plumbing.
    const appAction = (kind) => {
      if (!driving()) return;
      busy(i18n.signSending || '');
      fetch('/sign-action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: kind })
      }).then(async (r) => {
        const out = await r.json().catch(() => ({}));
        if (!r.ok) { unbusy(); toast(i18n.failed || ''); return; }
        pad.waitingId = out.id || '';
        // Same lock as a replay: the app's Clear takes seconds too, and a
        // Done pressed into the middle of one commits whatever survived it.
        padWaiting(true);
      }).catch(() => { unbusy(); toast(i18n.failed || ''); });
    };
    const appClear = document.getElementById('app-clear');
    if (appClear) appClear.addEventListener('click', () => appAction('clear'));
    const appSave = document.getElementById('app-save');
    if (appSave) appSave.addEventListener('click', () => appAction('confirm'));

    for (const btn of document.querySelectorAll('[data-act]')) {
      btn.addEventListener('click', () => {
        if (!driving()) return;
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

    // BREADCRUMBS, walked rather than counted.
    //
    // A step says how many fragment POPS separate it from here, and pops are
    // not presses: watched live, two Back presses on inMyTeam's work log were
    // swallowed undoing its tab selection and popped nothing, while the
    // screen's own Back arrow popped cleanly. Firing N presses blindly would
    // have overshot straight out of the app.
    //
    // So this sends one Back and waits for the next screen to arrive before
    // deciding whether to send another — and stops early the moment the trail
    // says we are there, or the server refuses the press because the app is
    // on its own first page. Bounded either way.
    document.addEventListener('click', async (ev) => {
      const crumb = ev.target.closest && ev.target.closest('.a-crumb[data-back]');
      if (!crumb) return;
      let want = parseInt(crumb.dataset.back || '0', 10);
      if (!(want > 0)) return;
      if (!socket || socket.readyState !== 1) {
        toast(i18n.explainOffline || '');
        return;
      }
      const label = crumb.textContent.trim();
      tapping(true);
      for (let press = 0; press < want + CRUMB_SLACK; press++) {
        const here = document.querySelector('.a-crumb.here');
        if (here && here.textContent.trim() === label) break;
        backSentAt = Date.now();
        socket.send(JSON.stringify({ type: 'device', action: 'back' }));
        const moved = await nextScreen(CRUMB_WAIT);
        if (!moved) break;
      }
      tapping(false);
    });

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
