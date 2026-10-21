// The seam to Capacitor. The runtime injects window.Capacitor before app.js
// runs; plugins are reached by name, with no bundler in between. Outside the
// app every helper degrades to the browser's own behaviour.

const cap = () => globalThis.Capacitor;

export const plugin = (name) => {
  const c = cap();
  if (!c?.isNativePlatform?.()) return null;
  if (c.isPluginAvailable && !c.isPluginAvailable(name)) return null;
  return c.Plugins?.[name] ?? null;
};

export const platform = () => cap()?.getPlatform?.() ?? "web";
export const isNative = () => !!cap()?.isNativePlatform?.();

// Opens a page in the system browser, leaving the app. Capacitor hands any
// top-level navigation to an outside origin to the OS (Safari, the default
// browser on Android). Not the in-app Safari view: App Review took a city's
// booking page shown inside the app for the app's own content (5.1.1(ix),
// 2026-10-21).
// shortcut: @capacitor/browser stays installed but unused, drop it with the next cap sync.
export async function openExternal(url) {
  if (isNative()) globalThis.location.href = url;
  else globalThis.open?.(url, "_blank", "noopener");
}

// The OS settings page for this app, where notifications are switched on.
// iOS: the app-settings: URL, which Capacitor hands to the system because it
// is not the app's own origin. Android: the app's own PushGate plugin.
export async function openAppSettings() {
  const gate = plugin("PushGate");
  if (gate?.openSettings) return gate.openSettings();
  if (platform() === "ios") globalThis.location.href = "app-settings:";
}

// Asks the App Store or Google Play for its rating sheet. Whether it shows is
// the store's call and nothing comes back; an older build without the plugin,
// or a failing one, is no reason to bother the reader.
export function requestReview() {
  plugin("StoreReview")?.requestReview().catch(() => {});
}
