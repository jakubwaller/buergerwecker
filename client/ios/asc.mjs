// The App Store Connect API, as far as signing on a runner needs it. No Xcode
// account, no fastlane, no dependency: Node's own crypto signs the token and
// fetch does the rest. Called by .github/workflows/app-build.yml. Adapted
// from PapaMap's app/ios/asc.mjs, without its store-listing commands, its
// widget and its App Group.
//
//   node ios/asc.mjs ids                  register the bundle id, Push Notifications on
//   node ios/asc.mjs cert <csr> <out.cer> one distribution certificate from a CSR (once a year)
//   node ios/asc.mjs profiles <dir>       a fresh App Store profile for the bundle id
//   node ios/asc.mjs beta-text pull                                   print what's live in TestFlight
//   node ios/asc.mjs beta-text push --build <n>                      testflight/what-to-test.*.txt → that build
//   node ios/asc.mjs beta-text distribute --build <n> --group <name> add to a beta group, submit for review
//
// Reads ASC_ISSUER_ID, ASC_KEY_ID and ASC_API_KEY_P8 (the key's text) from the
// environment — the repository secrets of the same names.
//
// What the API cannot do: create the App Store Connect app record for
// de.buergerwecker.app (My Apps → +), and the APNs auth key the server sends
// with (developer.apple.com → Keys). client/README.md has the checklist.
import { createPrivateKey, sign } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const API = "https://api.appstoreconnect.apple.com/v1";

export const BUNDLE_IDS = [
  { identifier: "de.buergerwecker.app", name: "Buergerwecker" },
];
// The name the Xcode project's Release configuration asks for
// (PROVISIONING_PROFILE_SPECIFIER) and the export options repeat.
export const profileName = (identifier) => `Buergerwecker CI ${identifier}`;

const b64url = (buf) => Buffer.from(buf).toString("base64url");

// ES256 over header.payload, raw r||s (ieee-p1363) as JWS wants it, not DER.
// Apple caps the lifetime at 20 minutes; ten is plenty for one command.
export function token({ issuer, keyId, p8 }, now = Math.floor(Date.now() / 1000)) {
  const head = b64url(JSON.stringify({ alg: "ES256", kid: keyId, typ: "JWT" }));
  const body = b64url(JSON.stringify({ iss: issuer, iat: now, exp: now + 600, aud: "appstoreconnect-v1" }));
  const sig = sign("sha256", Buffer.from(`${head}.${body}`),
                   { key: createPrivateKey(p8), dsaEncoding: "ieee-p1363" });
  return `${head}.${body}.${b64url(sig)}`;
}

function credentials() {
  const { ASC_ISSUER_ID: issuer, ASC_KEY_ID: keyId, ASC_API_KEY_P8: p8 } = process.env;
  if (!issuer || !keyId || !p8) throw new Error("ASC_ISSUER_ID, ASC_KEY_ID and ASC_API_KEY_P8 must be set");
  return { issuer, keyId, p8 };
}

// token()'s own exp is 600 s out. One command used to mean one token, but
// `beta-text push` can poll for up to 25 minutes, so a single bearer minted
// at the start would be rejected with 401 well before the wait is over.
// Re-mint once within a minute of that 600 s lifetime instead of once per
// process.
const TOKEN_LIFETIME_S = 600;
const TOKEN_REFRESH_MARGIN_S = 60;
let bearer = null; // { value, mintedAt } in epoch seconds, or null before the first call

export function freshToken(now = Math.floor(Date.now() / 1000)) {
  if (!bearer || now - bearer.mintedAt >= TOKEN_LIFETIME_S - TOKEN_REFRESH_MARGIN_S) {
    bearer = { value: token(credentials(), now), mintedAt: now };
  }
  return bearer.value;
}

// Test-only seam: forces the next freshToken() call to mint again, so a test
// can start clean regardless of what an earlier test already cached.
export function _resetBearerForTests() {
  bearer = null;
}

async function api(method, path, body) {
  const r = await fetch(path.startsWith("http") ? path : API + path, {
    method,
    headers: { Authorization: `Bearer ${freshToken()}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 204) return null;
  const json = await r.json().catch(() => null);
  if (!r.ok) {
    const why = (json?.errors ?? []).map((e) => `${e.code}: ${e.detail}`).join("; ");
    const err = new Error(`${method} ${path} → ${r.status} ${why}`);
    err.status = r.status;
    throw err;
  }
  return json;
}

// filter[identifier] is a prefix match (de.buergerwecker.app would also
// answer a later de.buergerwecker.app.widget), so the exact one is picked here.
async function bundleId(identifier) {
  const r = await api("GET", `/bundleIds?filter[identifier]=${identifier}&filter[platform]=IOS&include=bundleIdCapabilities&limit=200`);
  const hit = r.data.find((b) => b.attributes.identifier === identifier);
  if (!hit) return null;
  const caps = (r.included ?? [])
    .filter((c) => hit.relationships.bundleIdCapabilities.data.some((d) => d.id === c.id))
    .map((c) => c.attributes.capabilityType);
  return { id: hit.id, caps };
}

async function ids() {
  for (const { identifier, name } of BUNDLE_IDS) {
    let b = await bundleId(identifier);
    if (!b) {
      const made = await api("POST", "/bundleIds", {
        data: { type: "bundleIds", attributes: { identifier, name, platform: "IOS" } },
      });
      b = { id: made.data.id, caps: [] };
      console.log(`${identifier}: registered`);
    } else {
      console.log(`${identifier}: already registered`);
    }
    if (!b.caps.includes("PUSH_NOTIFICATIONS")) {
      await api("POST", "/bundleIdCapabilities", {
        data: {
          type: "bundleIdCapabilities",
          attributes: { capabilityType: "PUSH_NOTIFICATIONS" },
          relationships: { bundleId: { data: { type: "bundleIds", id: b.id } } },
        },
      });
      console.log(`${identifier}: Push Notifications switched on`);
    }
  }
}

// Idempotent on purpose: a second distribution certificate would be signed for
// the same CSR and buy nothing, and the account only holds a handful. One that
// is already there and is NOT the runner's (made in Xcode, say) has to be
// revoked by hand before this makes a new one — the private key decides.
async function cert(csrPath, outPath) {
  const have = (await api("GET", "/certificates?filter[certificateType]=DISTRIBUTION&limit=200")).data;
  if (have.length) {
    for (const c of have) console.log(`certificate ${c.attributes.serialNumber} exists, expires ${c.attributes.expirationDate} — nothing made`);
    return;
  }
  const made = await api("POST", "/certificates", {
    data: {
      type: "certificates",
      attributes: { certificateType: "DISTRIBUTION", csrContent: readFileSync(csrPath, "utf8") },
    },
  });
  const a = made.data.attributes;
  writeFileSync(outPath, Buffer.from(a.certificateContent, "base64"));
  console.log(`certificate ${a.serialNumber}, expires ${a.expirationDate} → ${outPath}`);
}

// A profile is a snapshot of its App ID: one made before Push Notifications
// was switched on never learns of it and the archive fails on the
// aps-environment entitlement. So the profile is not kept — every build
// deletes it and asks for a new one, which costs two requests and no mail to
// anybody.
async function profiles(dir) {
  mkdirSync(dir, { recursive: true });
  const certs = (await api("GET", "/certificates?filter[certificateType]=DISTRIBUTION&limit=200")).data;
  if (!certs.length) throw new Error("no distribution certificate on the account — run `cert` first");
  for (const { identifier } of BUNDLE_IDS) {
    const b = await bundleId(identifier);
    if (!b) throw new Error(`${identifier} is not registered — run \`ids\` first`);
    const name = profileName(identifier);
    const old = await api("GET", `/profiles?filter[name]=${encodeURIComponent(name)}&limit=200`);
    for (const p of old.data.filter((p) => p.attributes.name === name)) await api("DELETE", `/profiles/${p.id}`);
    const made = await api("POST", "/profiles", {
      data: {
        type: "profiles",
        attributes: { name, profileType: "IOS_APP_STORE" },
        relationships: {
          bundleId: { data: { type: "bundleIds", id: b.id } },
          certificates: { data: certs.map((c) => ({ type: "certificates", id: c.id })) },
        },
      },
    });
    const a = made.data.attributes;
    const file = join(dir, `${a.uuid}.mobileprovision`);
    const content = Buffer.from(a.profileContent, "base64");
    writeFileSync(file, content);
    // The profile is a CMS envelope around a plain-text plist, so the one
    // thing worth checking before an eight-minute archive can be read off it.
    if (!content.toString("latin1").includes("aps-environment")) {
      throw new Error(`${name} carries no Push Notifications entitlement: run \`ids\` (task apple-setup), ` +
                      `or tick Push Notifications on ${identifier} on developer.apple.com (Identifiers)`);
    }
    console.log(`${name} → ${file}`);
  }
}

// --- TestFlight "What to Test" text -----------------------------------------
//
// Docs consulted (2026-09-21):
//   https://developer.apple.com/documentation/appstoreconnectapi/betaapplocalization
//   https://developer.apple.com/documentation/appstoreconnectapi/betabuildlocalization
//   https://developer.apple.com/documentation/appstoreconnectapi/betabuildlocalization/attributes-data.dictionary
//     (the locale table for a build-level "What to Test" entry: de-DE, en-US among them)
//   https://developer.apple.com/documentation/appstoreconnectapi/get-v1-builds
//     (processingState: PROCESSING | FAILED | INVALID | VALID)
//   https://developer.apple.com/documentation/appstoreconnectapi/betagroup
//   https://developer.apple.com/documentation/appstoreconnectapi/post-v1-betagroups-_id_-relationships-builds
//   https://developer.apple.com/documentation/appstoreconnectapi/betaappreviewsubmission
//
// Apple's help pages and fastlane's pilot (which has shipped this for years)
// agree on a 4000-character whatsNew limit; nothing here can check that
// against Apple directly, so it is enforced locally before any request.
export const WHATS_NEW_LIMIT = 4000;
export const POLL_INTERVAL_MS = 30_000;
export const POLL_TIMEOUT_MS = 25 * 60 * 1000;

// One `what-to-test.<locale>.txt` file per locale, read from this directory
// unless a test points elsewhere. The filename IS the locale, so a new
// language needs a new file and no code change.
const testflightDir = join(dirname(fileURLToPath(import.meta.url)), "testflight");

export function whatsNewFiles(dir) {
  return readdirSync(dir)
    .map((name) => /^what-to-test\.([\w-]+)\.txt$/.exec(name))
    .filter(Boolean)
    .map(([name, locale]) => ({ locale, file: name, text: readFileSync(join(dir, name), "utf8").trim() }))
    .sort((a, b) => a.locale.localeCompare(b.locale));
}

// filter[bundleId] is a prefix match, same as bundleId()'s filter[identifier]
// above, so the exact match is picked here rather than trusting data[0].
export async function appId() {
  const r = await api("GET", `/apps?filter[bundleId]=${BUNDLE_IDS[0].identifier}&limit=200`);
  const hit = r.data.find((a) => a.attributes.bundleId === BUNDLE_IDS[0].identifier);
  if (!hit) throw new Error(`no App Store Connect app found for bundle id ${BUNDLE_IDS[0].identifier}`);
  return hit.id;
}

// filter[version] is exact, but nothing stops two builds (different
// platforms, say) from sharing a CFBundleVersion for one app, and taking the
// first of an unsorted list would then be a guess. Newest first, and refuse
// outright rather than silently act on the wrong build.
export async function findBuild(app, version) {
  const r = await api("GET", `/builds?filter[app]=${app}&filter[version]=${encodeURIComponent(version)}&sort=-uploadedDate&limit=2`);
  if (r.data.length > 1) {
    throw new Error(`build ${version} is ambiguous for this app — more than one build shares that version`);
  }
  return r.data[0] ?? null;
}

async function latestBuild(app) {
  const r = await api("GET", `/builds?filter[app]=${app}&sort=-uploadedDate&limit=1`);
  return r.data[0] ?? null;
}

async function betaAppLocalizations(app) {
  return (await api("GET", `/betaAppLocalizations?filter[app]=${app}&limit=200`)).data;
}

async function betaBuildLocalizations(build) {
  return (await api("GET", `/betaBuildLocalizations?filter[build]=${build}&limit=200`)).data;
}

// A rate limit or a transient 5xx is not App Store Connect's opinion of the
// build, just noise over a 25-minute wait — worth a retry next tick rather
// than aborting the whole push. Anything else 4xx (a bad key, a renamed
// filter) will not fix itself by waiting, so it is left to propagate.
async function findBuildTolerantly(app, version) {
  try {
    return await findBuild(app, version);
  } catch (e) {
    if (e.status === 429 || (e.status && e.status >= 500)) {
      console.log(`build ${version}: ${e.message} — temporary, trying again next poll`);
      return null;
    }
    throw e;
  }
}

// Apple processes an upload after it lands, so the freshly-uploaded build
// answers PROCESSING for a while. Poll until VALID; INVALID/FAILED are dead
// ends (a bad Info.plist, encryption declaration, …) and stop right away
// rather than waiting out the clock for something that will never change.
export async function pollBuild(app, version, { intervalMs = POLL_INTERVAL_MS, timeoutMs = POLL_TIMEOUT_MS } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const build = await findBuildTolerantly(app, version);
    const state = build?.attributes.processingState;
    if (state === "VALID") return build;
    if (state === "INVALID" || state === "FAILED") {
      throw new Error(`build ${version} is ${state} — App Store Connect rejected the upload, this will not change on its own`);
    }
    if (Date.now() >= deadline) {
      throw new Error(`build ${version} did not reach VALID within ${Math.round(timeoutMs / 60_000)} minutes ` +
                      `(last seen: ${state ?? "not uploaded yet"})`);
    }
    console.log(`build ${version}: ${state ?? "not visible yet"}, waiting…`);
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

export async function betaTextPull() {
  const app = await appId();
  console.log(`app ${BUNDLE_IDS[0].identifier} → ${app}`);
  console.log("betaAppLocalizations:");
  for (const l of await betaAppLocalizations(app)) console.log(`  ${l.attributes.locale}: ${JSON.stringify(l.attributes.description)}`);
  const build = await latestBuild(app);
  if (!build) { console.log("no builds uploaded yet"); return; }
  console.log(`latest build: ${build.attributes.version} (${build.attributes.processingState})`);
  console.log("betaBuildLocalizations (What to Test):");
  for (const l of await betaBuildLocalizations(build.id)) console.log(`  ${l.attributes.locale}: ${JSON.stringify(l.attributes.whatsNew)}`);
}

// Idempotent: an existing locale is PATCHed, a missing one POSTed. Waits for
// the build to be VALID first — an upload just off the runner is still
// PROCESSING for a few minutes.
export async function betaTextPush(build, { dir = testflightDir, intervalMs, timeoutMs } = {}) {
  if (!build) throw new Error("beta-text push: --build <CFBundleVersion> is required");
  const files = whatsNewFiles(dir);
  if (!files.length) throw new Error(`no what-to-test.*.txt files in ${dir}`);
  for (const f of files) {
    if (!f.text) {
      throw new Error(`${f.file}: empty (or whitespace-only) — refusing to push a blank "What to Test"`);
    }
    if (f.text.length > WHATS_NEW_LIMIT) {
      throw new Error(`${f.file}: ${f.text.length} characters, over Apple's ${WHATS_NEW_LIMIT}-character whatsNew limit`);
    }
  }
  const app = await appId();
  console.log(`waiting for build ${build} to finish processing…`);
  const b = await pollBuild(app, build, { intervalMs, timeoutMs });
  console.log(`build ${build} is VALID (${b.id})`);
  const existing = await betaBuildLocalizations(b.id);
  for (const { locale, text } of files) {
    const have = existing.find((l) => l.attributes.locale === locale);
    if (have) {
      await api("PATCH", `/betaBuildLocalizations/${have.id}`, {
        data: { type: "betaBuildLocalizations", id: have.id, attributes: { whatsNew: text } },
      });
      console.log(`${locale}: updated`);
    } else {
      const made = await api("POST", "/betaBuildLocalizations", {
        data: {
          type: "betaBuildLocalizations",
          attributes: { locale, whatsNew: text },
          relationships: { build: { data: { type: "builds", id: b.id } } },
        },
      });
      console.log(`${locale}: created (${made.data.id})`);
    }
  }
}

// Adds the build to the named group if it is not already a member, then, for
// an external group, submits it for Beta App Review — unless a submission
// for this build already exists, in which case that submission (whatever
// its state: waiting, in review, approved) is treated as done.
export async function betaTextDistribute(build, group) {
  if (!build || !group) throw new Error("beta-text distribute: --build <n> and --group <name> are required");
  const app = await appId();
  const b = await findBuild(app, build);
  if (!b) throw new Error(`build ${build} not found for ${BUNDLE_IDS[0].identifier}`);
  const groups = (await api("GET", `/betaGroups?filter[app]=${app}&filter[name]=${encodeURIComponent(group)}&limit=200`)).data;
  const g = groups.find((x) => x.attributes.name === group);
  if (!g) throw new Error(`no beta group named "${group}" for ${BUNDLE_IDS[0].identifier}`);
  const members = (await api("GET", `/betaGroups/${g.id}/relationships/builds?limit=200`)).data;
  if (members.some((m) => m.id === b.id)) {
    console.log(`${group}: build ${build} already in the group`);
  } else {
    await api("POST", `/betaGroups/${g.id}/relationships/builds`, { data: [{ type: "builds", id: b.id }] });
    console.log(`${group}: build ${build} added to the group`);
  }
  if (g.attributes.isInternalGroup) {
    console.log(`${group}: internal group, no Beta App Review needed`);
    return;
  }
  const submissions = (await api("GET", `/betaAppReviewSubmissions?filter[build]=${b.id}&limit=1`)).data;
  if (submissions.length) {
    console.log(`beta app review for build ${build}: already ${submissions[0].attributes.betaReviewState}`);
  } else {
    await api("POST", "/betaAppReviewSubmissions", {
      data: { type: "betaAppReviewSubmissions", relationships: { build: { data: { type: "builds", id: b.id } } } },
    });
    console.log(`beta app review: submitted build ${build}`);
  }
}

// "--build 125 --group Friends" → { build: "125", group: "Friends" }
function flags(args) {
  const out = {};
  for (let i = 0; i < args.length; i += 2) out[(args[i] ?? "").replace(/^--/, "")] = args[i + 1];
  return out;
}

async function betaText(sub, ...args) {
  const f = flags(args);
  if (sub === "pull") return betaTextPull();
  if (sub === "push") return betaTextPush(f.build);
  if (sub === "distribute") return betaTextDistribute(f.build, f.group);
  throw new Error(`beta-text: unknown subcommand "${sub}" (pull | push | distribute)`);
}

if (process.argv[1] && import.meta.url.endsWith(process.argv[1].split("/").pop())) {
  const [cmd, ...args] = process.argv.slice(2);
  const run = { ids, cert, profiles, "beta-text": betaText }[cmd];
  if (!run || (cmd === "cert" && args.length !== 2) || (cmd === "profiles" && args.length !== 1) ||
      (cmd === "beta-text" && args.length < 1)) {
    console.error("usage: asc.mjs ids | cert <csr> <out.cer> | profiles <dir> | beta-text pull | " +
                  "beta-text push --build <n> | beta-text distribute --build <n> --group <name>");
    process.exit(2);
  }
  run(...args).catch((e) => { console.error(String(e.message ?? e)); process.exit(1); });
}
