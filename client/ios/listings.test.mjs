import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, readdirSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { readListings, LIMITS, ASC_LOCALES } from "./asc.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

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

// The texts say how many cities there are and how long an alert runs; both
// are written by hand, so a new city or a changed term must fail here.
test("the numbers in the store texts are the catalog's and the config's", () => {
  const catalog = join(root, "catalog");
  const cities = new Set(readdirSync(catalog)
    .filter((d) => existsSync(join(catalog, d, "display.json")))
    .map((d) => JSON.parse(readFileSync(join(catalog, d, "display.json"), "utf8")).city_name.de));
  const ttl = /^SUBSCRIPTION_TTL_DAYS=(\d+)$/m.exec(readFileSync(join(root, ".env.example"), "utf8"))[1];
  const screenshots = readFileSync(join(root, "client", "store-screenshots", "texts.json"), "utf8");
  for (const l of readListings()) {
    for (const text of [l.promotionalText, l.description, screenshots]) {
      assert.match(text, new RegExp(`\\b${cities.size} (Städten?|(German )?cities)\\b`), `${l.lang}: ${cities.size} cities`);
    }
    assert.match(l.description, new RegExp(`\\b${ttl} (Tage|days)\\b`), `${l.lang}: ${ttl} days`);
  }
});
