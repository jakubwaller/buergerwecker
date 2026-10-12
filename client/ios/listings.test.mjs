import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { readListings, LIMITS, ASC_LOCALES } from "./asc.mjs";

test("the committed listings are complete, within Apple's limits, one per store locale", () => {
  const listings = readListings();
  assert.deepEqual(listings.map((l) => l.lang), Object.keys(ASC_LOCALES).sort());
  for (const l of listings) for (const key of Object.keys(LIMITS)) assert.ok(l[key].length <= LIMITS[key], `${l.lang} ${key}`);
});

test("an over-long field, a stray field and an unknown language are refused before any request", () => {
  const { lang: _l, locale: _c, ...ok } = readListings()[0];
  const write = (lang, listing) => {
    const dir = mkdtempSync(join(tmpdir(), "buergerwecker-listings-"));
    writeFileSync(join(dir, `${lang}.json`), JSON.stringify(listing));
    return dir;
  };
  assert.throws(() => readListings(write("de", { ...ok, subtitle: "x".repeat(31) })), /over Apple's 30/);
  assert.throws(() => readListings(write("de", { ...ok, whatsNew: "x" })), /unknown field/);
  assert.throws(() => readListings(write("de", { ...ok, description: " padded" })), /whitespace/);
  assert.throws(() => readListings(write("fr", ok)), /no App Store locale/);
});
