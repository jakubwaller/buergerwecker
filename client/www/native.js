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
// browser on Android), but on iOS only while the app is active, so a push tap
// that lands a beat before activation waits for it. Not the in-app Safari
// view: App Review took a city's booking page shown inside the app for the
// app's own content (5.1.1(ix), 2026-10-21).
// shortcut: @capacitor/browser stays in package.json unused; npm uninstall it and cap sync in a later change.
export async function openExternal(url) {
  if (!/^https?:\/\//i.test(url)) return;
  if (!isNative()) {
    globalThis.open?.(url, "_blank", "noopener");
    return;
  }
  const app = plugin("App");
  if (app?.getState && !(await app.getState()).isActive) {
    await new Promise((resolve) => {
      const handle = app.addListener("appStateChange", ({ isActive }) => {
        if (!isActive) return;
        Promise.resolve(handle).then((h) => h?.remove?.());
        resolve();
      });
    });
  }
  globalThis.location.href = url;
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
