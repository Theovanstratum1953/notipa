/*
 * Web Push subscribe/unsubscribe helpers — the client-side half of the
 * last Phase 1 build-sequence item (proposal: "PWA install + web push
 * notifications"). Used by core/app_notifications.html's Enable/Disable
 * button; not loaded or run on any other page, since requesting
 * notification permission should only ever happen from an explicit user
 * action on that one page, never automatically on app load.
 */

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

function getCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

function postJson(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
    body: JSON.stringify(body),
  });
}

/**
 * Checks whether *this* browser currently has an active push
 * subscription — used to decide which state the Enable/Disable button
 * should render in on page load. Deliberately client-side: the server
 * only knows "a subscription with this endpoint exists," not which
 * device a given page load is running on.
 */
async function getExistingSubscription() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    return null;
  }
  const registration = await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}

async function enablePushNotifications(vapidPublicKey) {
  const registration = await navigator.serviceWorker.ready;
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("Notification permission was not granted.");
  }
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
  });
  const json = subscription.toJSON();
  const response = await postJson("/push/subscribe/", {
    endpoint: json.endpoint,
    keys: json.keys,
    user_agent: navigator.userAgent,
  });
  if (!response.ok) {
    throw new Error("Saving the subscription on the server failed.");
  }
  return subscription;
}

async function disablePushNotifications() {
  const subscription = await getExistingSubscription();
  if (!subscription) return;
  const endpoint = subscription.endpoint;
  await subscription.unsubscribe();
  await postJson("/push/unsubscribe/", { endpoint });
}
