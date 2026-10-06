import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { STRINGS, LANGS, t, detectLang } from "../www/i18n.js";

test("German and English have exactly the same keys", () => {
  const de = Object.keys(STRINGS.de).sort();
  const en = Object.keys(STRINGS.en).sort();
  assert.deepEqual(
    de.filter((k) => !en.includes(k)),
    [],
    "keys only in German",
  );
  assert.deepEqual(
    en.filter((k) => !de.includes(k)),
    [],
    "keys only in English",
  );
  assert.deepEqual(LANGS, Object.keys(STRINGS));
});

test("no string is empty, and both languages use the same placeholders", () => {
  for (const key of Object.keys(STRINGS.de)) {
    for (const lang of LANGS) assert.ok(STRINGS[lang][key].trim(), `${lang} ${key} is empty`);
    const vars = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
    assert.deepEqual(vars(STRINGS.de[key]), vars(STRINGS.en[key]), `${key}: placeholders differ`);
  }
});

test("every key the screens ask for exists", () => {
  const www = new URL("../www/", import.meta.url).pathname;
  const walk = (d) => readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? walk(join(d, f)) : [join(d, f)]));
  const used = new Set();
  for (const f of walk(www).filter((f) => f.endsWith(".js"))) {
    for (const m of readFileSync(f, "utf8").matchAll(/\bt\(\s*"([\w.]+)"/g)) used.add(m[1]);
  }
  assert.ok(used.size > 50, "found the t() calls");
  for (const key of used) assert.ok(key in STRINGS.de, `${key} is used but not defined`);
});

test("placeholders are filled, a missing key shows itself", () => {
  assert.equal(t("city.more", { n: 3 }, "de"), "+3 weitere");
  assert.equal(t("city.more", { n: 3 }, "en"), "+3 more");
  assert.equal(t("no.such.key", {}, "en"), "no.such.key");
});

test("the device language picks German or English", () => {
  assert.equal(detectLang("de-DE"), "de");
  assert.equal(detectLang("de-AT"), "de");
  assert.equal(detectLang("en-GB"), "en");
  assert.equal(detectLang("fr-FR"), "en");
  assert.equal(detectLang(undefined), "en");
});
