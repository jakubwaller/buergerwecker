// The widget extension's App ID next to the app's: ids() switches on what each
// needs, profiles() refuses a profile without the App Group (the one thing no
// API key can set up) before an eight-minute archive finds out.
import test from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { BUNDLE_IDS, APP_GROUP, KEYCHAIN_GROUP, ids, profiles, profileName } from "./asc.mjs";

const { privateKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
process.env.ASC_ISSUER_ID = "test-issuer";
process.env.ASC_KEY_ID = "TESTKEY123";
process.env.ASC_API_KEY_P8 = privateKey.export({ type: "pkcs8", format: "pem" });

// Records every call; `state.bundles` is what the account holds, as
// { identifier, caps }.
function fakeAccount(t, state) {
  const calls = [];
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; t.mock?.restoreAll?.(); });
  const ok = (body, status = 200) => ({ status, ok: status < 300, json: async () => body });
  globalThis.fetch = async (url, opts = {}) => {
    const u = new URL(url);
    const method = opts.method ?? "GET";
    const body = opts.body ? JSON.parse(opts.body) : undefined;
    calls.push({ method, path: u.pathname, body });
    if (method === "GET" && u.pathname === "/v1/bundleIds") {
      const wanted = u.searchParams.get("filter[identifier]");
      // A prefix match, as Apple's filter is.
      const hits = state.bundles.filter((b) => b.identifier.startsWith(wanted));
      return ok({
        data: hits.map((b) => ({
          id: `id-${b.identifier}`,
          attributes: { identifier: b.identifier },
          relationships: { bundleIdCapabilities: { data: b.caps.map((c) => ({ id: `${b.identifier}/${c}` })) } },
        })),
        included: hits.flatMap((b) => b.caps.map((c) => ({ id: `${b.identifier}/${c}`, attributes: { capabilityType: c } }))),
      });
    }
    if (method === "POST" && u.pathname === "/v1/bundleIds") {
      const identifier = body.data.attributes.identifier;
      state.bundles.push({ identifier, caps: [] });
      return ok({ data: { id: `id-${identifier}` } }, 201);
    }
    if (method === "POST" && u.pathname === "/v1/bundleIdCapabilities") {
      const id = body.data.relationships.bundleId.data.id.replace(/^id-/, "");
      state.bundles.find((b) => b.identifier === id).caps.push(body.data.attributes.capabilityType);
      return ok({ data: {} }, 201);
    }
    if (method === "GET" && u.pathname === "/v1/certificates") return ok({ data: [{ id: "cert-1" }] });
    if (method === "GET" && u.pathname === "/v1/profiles") return ok({ data: [] });
    if (method === "POST" && u.pathname === "/v1/profiles") {
      const name = body.data.attributes.name;
      const identifier = name.replace(/^Buergerwecker CI /, "");
      const text = state.profileText(identifier);
      return ok({ data: { attributes: { uuid: `uuid-${identifier}`, profileContent: Buffer.from(text).toString("base64") } } }, 201);
    }
    throw new Error(`no fake route for ${method} ${u.pathname}`);
  };
  return calls;
}

const silence = (t) => t.mock.method(console, "log", () => {});

test("the widget has its own bundle id under the app's, and shares the App Group", () => {
  assert.deepEqual(BUNDLE_IDS.map((b) => b.identifier), ["de.buergerwecker.app", "de.buergerwecker.app.widget"]);
  assert.match(APP_GROUP, /^group\.de\.buergerwecker\.app$/);
  for (const b of BUNDLE_IDS) assert.ok(b.capabilities.includes("APP_GROUPS"), b.identifier);
  assert.deepEqual(BUNDLE_IDS[1].capabilities, ["APP_GROUPS"], "the widget needs no push");
});

test("ids: registers both and switches on what each needs, once", async (t) => {
  silence(t);
  const state = { bundles: [] };
  const calls = fakeAccount(t, state);
  await ids();
  assert.deepEqual(state.bundles, [
    { identifier: "de.buergerwecker.app", caps: ["PUSH_NOTIFICATIONS", "APP_GROUPS"] },
    { identifier: "de.buergerwecker.app.widget", caps: ["APP_GROUPS"] },
  ]);
  const before = calls.filter((c) => c.method === "POST").length;
  await ids();
  assert.equal(calls.filter((c) => c.method === "POST").length, before, "a second run makes nothing");
});

test("ids: an app registered before the widget existed only gets the new App ID and the group", async (t) => {
  silence(t);
  const state = { bundles: [{ identifier: "de.buergerwecker.app", caps: ["PUSH_NOTIFICATIONS"] }] };
  const calls = fakeAccount(t, state);
  await ids();
  const posts = calls.filter((c) => c.method === "POST").map((c) => c.body.data.attributes.capabilityType ?? c.body.data.attributes.identifier);
  assert.deepEqual(posts, ["APP_GROUPS", "de.buergerwecker.app.widget", "APP_GROUPS"]);
});

test("profiles: both profiles are made, and one without the App Group is refused with the fix", async (t) => {
  silence(t);
  const bundles = BUNDLE_IDS.map((b) => ({ identifier: b.identifier, caps: b.capabilities }));
  const dir = () => mkdtempSync(join(tmpdir(), "buergerwecker-profiles-"));
  // What Apple puts in every profile: keychain-access-groups <team>.*.
  const full = (identifier) => `aps-environment ${APP_GROUP} keychain-access-groups TEAM.* ${identifier}`;
  const calls = fakeAccount(t, { bundles, profileText: full });
  await profiles(dir());
  const names = calls.filter((c) => c.method === "POST" && c.body.data.type === "profiles").map((c) => c.body.data.attributes.name);
  assert.deepEqual(names, BUNDLE_IDS.map((b) => profileName(b.identifier)));

  fakeAccount(t, { bundles, profileText: (identifier) => (identifier.endsWith(".widget") ? "nothing" : full(identifier)) });
  await assert.rejects(() => profiles(dir()), (e) => e.message.includes(APP_GROUP) && e.message.includes("de.buergerwecker.app.widget"));

  fakeAccount(t, { bundles, profileText: (identifier) => `${APP_GROUP} keychain-access-groups ${identifier}` });
  await assert.rejects(() => profiles(dir()), /Push Notifications/, "the app still needs its aps-environment");

  fakeAccount(t, { bundles, profileText: (identifier) => `aps-environment ${APP_GROUP} ${identifier}` });
  await assert.rejects(() => profiles(dir()), (e) => e.message.includes(KEYCHAIN_GROUP), "the keychain group needs the wildcard");
});
