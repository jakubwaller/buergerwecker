import test from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, verify } from "node:crypto";
import { token, profileName, BUNDLE_IDS } from "./asc.mjs";

test("the token is an ES256 JWT Apple's side can verify", () => {
  const { privateKey, publicKey } = generateKeyPairSync("ec", { namedCurve: "P-256" });
  const p8 = privateKey.export({ type: "pkcs8", format: "pem" });
  const [head, body, sig] = token({ issuer: "issuer-uuid", keyId: "KEY1234567", p8 }, 1_000).split(".");
  assert.deepEqual(JSON.parse(Buffer.from(head, "base64url")), { alg: "ES256", kid: "KEY1234567", typ: "JWT" });
  assert.deepEqual(JSON.parse(Buffer.from(body, "base64url")),
                   { iss: "issuer-uuid", iat: 1_000, exp: 1_600, aud: "appstoreconnect-v1" });
  // 64 raw bytes (r||s): a DER signature would be ~70 and Apple answers 401.
  assert.equal(Buffer.from(sig, "base64url").length, 64);
  assert.ok(verify("sha256", Buffer.from(`${head}.${body}`),
                   { key: publicKey, dsaEncoding: "ieee-p1363" }, Buffer.from(sig, "base64url")));
});

test("profile names are the ones the Xcode project asks for", async () => {
  const { readFileSync } = await import("node:fs");
  const pbx = readFileSync(new URL("./App/App.xcodeproj/project.pbxproj", import.meta.url), "utf8");
  for (const { identifier } of BUNDLE_IDS) {
    assert.ok(pbx.includes(`PROVISIONING_PROFILE_SPECIFIER = "${profileName(identifier)}";`), identifier);
  }
});
