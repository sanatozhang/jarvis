/**
 * Lightweight analytics tracking.
 * Sends events to POST /api/analytics/track
 */

export function trackEvent(eventType: string, detail?: Record<string, any>) {
  const username = typeof window !== "undefined" ? localStorage.getItem("jarvis_username") || "" : "";
  fetch("/api/analytics/track", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      event_type: eventType,
      username,
      detail: { ...detail, url: typeof window !== "undefined" ? window.location.pathname : "" },
    }),
  }).catch(() => {}); // fire and forget
}

export function trackPageVisit(page: string) {
  trackEvent("page_visit", { page });
}
