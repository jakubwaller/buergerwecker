// Small persisted values through the Preferences plugin (UserDefaults on iOS,
// SharedPreferences on Android). The device credentials live here and
// nowhere else. Outside the app (a desktop browser while developing) the
// values are kept in memory only, never in localStorage.
import { plugin } from "./native.js";

const memory = new Map();

export async function get(key) {
  const p = plugin("Preferences");
  const raw = p ? (await p.get({ key })).value : memory.get(key) ?? null;
  if (raw == null) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export async function set(key, value) {
  const raw = JSON.stringify(value);
  const p = plugin("Preferences");
  if (p) await p.set({ key, value: raw });
  else memory.set(key, raw);
}

export async function remove(key) {
  const p = plugin("Preferences");
  if (p) await p.remove({ key });
  else memory.delete(key);
}

export async function clear() {
  const p = plugin("Preferences");
  if (p) await p.clear();
  else memory.clear();
}
