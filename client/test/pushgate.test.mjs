// Every PushGate method the page calls has to exist on the Android plugin:
// a method missing there is silently undefined in the WebView, and the
// button that calls it does nothing (review round 1 of PR #99).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const www = new URL("../www/", import.meta.url).pathname;
const java = readFileSync(
  new URL("../android/app/src/main/java/de/buergerwecker/app/PushGatePlugin.java", import.meta.url),
  "utf8",
);
const walk = (d) => readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? walk(join(d, f)) : [join(d, f)]));

test("every PushGate method the page calls is a @PluginMethod in PushGatePlugin.java", () => {
  const called = new Set();
  for (const f of walk(www).filter((f) => f.endsWith(".js"))) {
    const src = readFileSync(f, "utf8");
    // `const gate = plugin("PushGate")` … `gate.x(` / `gate?.x`
    if (!src.includes('plugin("PushGate")')) continue;
    for (const m of src.matchAll(/\bgate\??\.(\w+)/g)) called.add(m[1]);
  }
  assert.ok(called.has("status") && called.has("openSettings"), `found the calls: ${[...called]}`);
  for (const name of called) {
    assert.match(java, new RegExp(`@PluginMethod\\s+public void ${name}\\(PluginCall call\\)`), `${name} is not a plugin method`);
  }
});
