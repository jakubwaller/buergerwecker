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

// Opens a page in the system browser (SFSafariViewController / Custom Tabs).
export async function openExternal(url) {
  const b = plugin("Browser");
  if (b) await b.open({ url });
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
