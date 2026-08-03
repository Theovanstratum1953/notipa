/*
 * Notipa service worker.
 *
 * Served at the domain root (see WHITENOISE_ROOT in notipa/settings.py) so
 * its default scope is "/" and it can control the whole app, not just one
 * static subdirectory.
 *
 * Strategy is deliberately simple, matching what Notipa actually is: a
 * server-rendered, per-request-personalized Django app (dashboards, fee
 * balances, homework lists), not a static single-page app. There is no
 * general-purpose offline mode for authenticated pages — that data has to
 * come from the server. What the service worker *does* provide:
 *
 *   1. The app shell (CSS + icons + manifest) is precached, so the app
 *      installs cleanly and repeat loads of static assets are instant.
 *   2. Page navigations are network-first: always try the network so
 *      logged-in users see live data, and only fall back to a cached copy
 *      or the offline page when the network is unavailable.
 *   3. Static assets are cache-first with a network fallback, so CSS/icons
 *      still render if a request is briefly offline.
 */

const CACHE_VERSION = "notipa-v1";
const APP_SHELL = [
  "/static/core/css/app.css",
  "/static/core/manifest.json",
  "/static/core/icons/icon-192.png",
  "/static/core/icons/icon-512.png",
  "/offline/",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_VERSION)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Only handle same-origin GET requests — let everything else (POST form
  // submissions, cross-origin requests) pass straight through untouched.
  if (request.method !== "GET" || new URL(request.url).origin !== self.location.origin) {
    return;
  }

  // Page navigations: network-first, falling back to a cached copy of the
  // same URL, then the offline page.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_VERSION).then((cache) => cache.put(request, copy));
          return response;
        })
        .catch(() => caches.match(request).then((cached) => cached || caches.match("/offline/")))
    );
    return;
  }

  // Static assets (CSS/JS/images): cache-first, falling back to network,
  // and quietly updating the cache with whatever the network returns.
  if (request.url.includes("/static/")) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ||
          fetch(request).then((response) => {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(request, copy));
            return response;
          })
      )
    );
  }
});

// --- Web Push (Phase 1 build-sequence item 8) -----------------------------
//
// A push arrives here even if no Notipa tab is open — that's the whole
// point of push over, say, a foreground-only in-app banner. The payload is
// whatever core.push.send_push_notification sent as JSON (see that
// function's docstring): { title, body, url }. Falls back to generic copy
// if a push somehow arrives with no payload at all, rather than showing a
// blank notification.
self.addEventListener("push", (event) => {
  let payload = { title: "Notipa", body: "You have a new update.", url: "/" };
  if (event.data) {
    try {
      payload = event.data.json();
    } catch (err) {
      payload.body = event.data.text() || payload.body;
    }
  }

  event.waitUntil(
    self.registration.showNotification(payload.title || "Notipa", {
      body: payload.body || "",
      icon: "/static/core/icons/icon-192.png",
      badge: "/static/core/icons/icon-192.png",
      data: { url: payload.url || "/" },
    })
  );
});

// Clicking a notification focuses an already-open Notipa tab and
// navigates it to the relevant page if one exists, rather than always
// opening a new tab — closer to how a native app's notification tap
// behaves than spawning duplicate windows every time.
self.addEventListener("notificationclick", (event) => {
  const targetUrl = (event.notification.data && event.notification.data.url) || "/";
  event.notification.close();

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});
