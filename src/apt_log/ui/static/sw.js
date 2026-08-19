/* The portal's service worker — Web Push only.
 *
 * phone.html has said "no service worker on purpose" since it was written,
 * and the reason was right: offline caching would keep copies of what the
 * phone's screen said, and this page is a window, not a document. So this
 * one caches NOTHING. There is no `fetch` handler here at all — only push
 * receipt and what happens when the notification is tapped. The objection
 * was to caching, not to the file.
 *
 * Why it exists: the login code inMyTeam texts is the one thing this system
 * cannot do for itself. A notification from a third-party relay opens that
 * relay's idea of a browser — Safari, at a URL — and she is left looking at
 * the wrong app. A notification from the portal, through this, opens the
 * portal she installed on her home screen, already at the field.
 *
 * Served from the site root (see the /sw.js route) rather than /static/,
 * because a worker's scope is its own directory: registered under /static/
 * it could never control /app, which is the only page worth opening.
 */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "Patient Time Log", {
      body: data.body || "",
      // One tag, so a second notice about the same thing replaces the first
      // rather than stacking: two identical "type the code" banners tell her
      // nothing the first did not.
      tag: data.tag || "aptlog",
      renotify: true,
      data: { url: data.url || "/app" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/app";
  event.waitUntil(
    (async () => {
      // An already-open window is focused and steered, rather than a second
      // one opened beside it. On a phone that is the difference between
      // arriving where she was and arriving somewhere new.
      const windows = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of windows) {
        if ("focus" in client) {
          if ("navigate" in client) {
            try {
              await client.navigate(target);
            } catch (e) {
              /* not allowed here — focusing what is open is still right */
            }
          }
          return client.focus();
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })()
  );
});
