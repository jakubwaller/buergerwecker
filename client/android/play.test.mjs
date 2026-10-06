import { test } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, verify } from "node:crypto";
import { assertion, trackBody, parseArgs, retryable, withRetry } from "./play.mjs";

test("the assertion is an RS256 JWT Google's token endpoint accepts", () => {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const key = {
    client_email: "ci@buergerwecker.iam.gserviceaccount.example.com",
    private_key: privateKey.export({ type: "pkcs8", format: "pem" }),
    token_uri: "https://oauth2.googleapis.com/token",
  };
  const jwt = assertion(key, 1_000_000);
  const [head, body, sig] = jwt.split(".");
  assert.deepEqual(JSON.parse(Buffer.from(head, "base64url")), { alg: "RS256", typ: "JWT" });
  assert.deepEqual(JSON.parse(Buffer.from(body, "base64url")), {
    iss: key.client_email,
    scope: "https://www.googleapis.com/auth/androidpublisher",
    aud: key.token_uri,
    iat: 1_000_000,
    exp: 1_003_600,   // Google's ceiling is an hour
  });
  assert.ok(verify("sha256", Buffer.from(`${head}.${body}`), publicKey, Buffer.from(sig, "base64url")));
});

test("the track gets exactly the uploaded version code, as a string", () => {
  assert.deepEqual(trackBody("alpha", 212, "draft"),
                   { track: "alpha", releases: [{ versionCodes: ["212"], status: "draft" }] });
});

test("closed testing and draft unless told otherwise; a typo'd status is refused", () => {
  assert.deepEqual(parseArgs(["app.aab"]), { track: "alpha", status: "draft", files: ["app.aab"] });
  assert.deepEqual(parseArgs(["app.aab", "--track", "internal", "--status", "completed"]),
                   { track: "internal", status: "completed", files: ["app.aab"] });
  assert.throws(() => parseArgs(["app.aab", "--status", "complete"]));
});

test("429 and 5xx are worth another try, other failures are not", () => {
  for (const s of [429, 500, 502, 503]) assert.equal(retryable(s), true, s);
  for (const s of [200, 400, 401, 403, 404, 409]) assert.equal(retryable(s), false, s);
});

test("a transient 503 is retried with a doubling pause until Play answers", async () => {
  const replies = [503, 503, 200];
  const slept = [];
  const res = await withRetry(async () => ({ status: replies.shift() }), { sleep: async (ms) => slept.push(ms) });
  assert.equal(res.status, 200);
  assert.deepEqual(slept, [2000, 4000]);
});

test("retries stop after the last try and hand back the failure", async () => {
  let sent = 0;
  const res = await withRetry(async () => (sent++, { status: 503 }), { tries: 3, sleep: async () => {} });
  assert.equal(res.status, 503);
  assert.equal(sent, 3);
});

test("a 403 fails at once, without a retry", async () => {
  let sent = 0;
  const res = await withRetry(async () => (sent++, { status: 403 }), { sleep: async () => {} });
  assert.equal(res.status, 403);
  assert.equal(sent, 1);
});
