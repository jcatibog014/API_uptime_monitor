// Pulse Check's service worker. It makes the app installable on a phone, shows alerts
// sent to this device (web push), and keeps
// the look (styles, scripts, icons) ready even on a slow connection.
// Pages always come from the network, so states and numbers are never out of date;
// without a connection a small "You're offline" page shows instead.
"use strict";

const CACHE = "pulsecheck-v4"; // change the number when these files change
const FILES = [
  "/static/style.css",
  "/static/app.js",
  "/static/theme.js",
  "/static/fonts/inter-latin-400-normal.woff2",
  "/static/fonts/space-grotesk-latin-700-normal.woff2",
  "/static/icons/icon-192.png",
  "/static/offline.html",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(FILES)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // Throw away the caches of older versions.
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name)))),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" || new URL(request.url).origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    // A page: always the network; the offline page only when there's none.
    event.respondWith(fetch(request).catch(() => caches.match("/static/offline.html")));
    return;
  }
  if (FILES.includes(new URL(request.url).pathname)) {
    // Our own styles/scripts/icons: the network first (so updates show), the cache as a backup.
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
          return response;
        })
        .catch(() => caches.match(request)),
    );
  }
});
self.addEventListener("push", (event) => {
  let notice = { title: "Pulse Check", body: "", url: "/" };
  try {
    notice = { ...notice, ...event.data.json() };
  } catch (error) {
    if (event.data) notice.body = event.data.text();
  }
  event.waitUntil(self.registration.showNotification(notice.title, {
    body: notice.body,
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    data: { url: notice.url || "/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data && event.notification.data.url ? event.notification.data.url : "/";
  event.waitUntil(self.clients.matchAll({ type: "window" }).then((windows) => {
    const open = windows.find((client) => client.url === url);
    return open ? open.focus() : self.clients.openWindow(url);
  }));
});
