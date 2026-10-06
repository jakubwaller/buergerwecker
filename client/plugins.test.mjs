import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// `npx cap sync` regenerates Package.swift from whatever it finds actually
// installed under node_modules; run it over a stale install (a merge that
// added a plugin to package.json, say, without an `npm ci` in between) and it
// silently drops that plugin from the tracked file, with nothing to say so
// until a Swift build fails on a missing symbol (client/README.md). This
// holds the two lists to each other instead.
const pkg = JSON.parse(readFileSync(new URL("package.json", import.meta.url), "utf8"));
const swift = readFileSync(new URL("ios/App/CapApp-SPM/Package.swift", import.meta.url), "utf8");

// @capacitor/core, /cli, /ios and /android are the Capacitor framework
// itself, not a plugin with its own CapacitorX Swift package.
const FRAMEWORK = new Set(["@capacitor/core", "@capacitor/cli", "@capacitor/ios", "@capacitor/android"]);
const plugins = Object.keys(pkg.dependencies)
  .filter((d) => d.startsWith("@capacitor/") && !FRAMEWORK.has(d))
  .map((d) => d.slice("@capacitor/".length));

const pascal = (name) => "Capacitor" + name.replace(/(^|-)([a-z])/g, (_, __, c) => c.toUpperCase());

test("every Capacitor plugin dependency has its package and product in Package.swift", () => {
  assert.ok(plugins.length, "package.json lists no @capacitor/* plugin at all");
  for (const name of plugins) {
    const product = pascal(name);
    assert.match(swift, new RegExp(`node_modules/@capacitor/${name}"`), `${name}: no .package(path:) entry`);
    assert.match(swift, new RegExp(`product\\(name: "${product}"`), `${name}: not linked into the CapApp-SPM target`);
  }
});

test("Package.swift names no plugin package.json does not also list", () => {
  const declared = [...swift.matchAll(/node_modules\/@capacitor\/([\w-]+)"/g)].map((m) => m[1]);
  assert.deepEqual([...declared].sort(), [...plugins].sort());
});

test("Package.swift targets the project's iOS 18", () => {
  assert.match(swift, /platforms: \[\.iOS\(\.v18\)\]/);
});
