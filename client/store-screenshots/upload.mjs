// Puts shoot.mjs's final/ shots on the App Store version being prepared, one
// set per locale and device, replacing whatever the set held. Run with the
// local App Manager key (asc-env.sh); not a CI task.
//
//   node upload.mjs <out-dir>/final
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { api, appId, EDITABLE } from "../ios/asc.mjs";

const DIR = process.argv[2];
if (!DIR) { console.error("usage: node upload.mjs <dir with the final PNGs>"); process.exit(1); }
// App Store Connect's display types: the 6.7" slot takes the 6.9" size too,
// the 12.9" slot the 13" one.
const SETS = { iphone69: "APP_IPHONE_67", ipad13: "APP_IPAD_PRO_3GEN_129" };
const LOCALES = { de: "de-DE", en: "en-US" };

const app = await appId();
const version = (await api("GET", `/apps/${app}/appStoreVersions?filter[platform]=IOS&limit=200`)).data
  .find((v) => EDITABLE.has(v.attributes.appVersionState));
if (!version) throw new Error("no App Store version is being prepared or was rejected");
const locs = (await api("GET", `/appStoreVersions/${version.id}/appStoreVersionLocalizations?limit=200`)).data;
const sets = [];
for (const [lang, locale] of Object.entries(LOCALES)) {
  const loc = locs.find((l) => l.attributes.locale === locale);
  if (!loc) throw new Error(`${locale}: no localization on version ${version.attributes.versionString}; run listings push first`);
  const have = (await api("GET", `/appStoreVersionLocalizations/${loc.id}/appScreenshotSets?limit=200`)).data;
  for (const [device, screenshotDisplayType] of Object.entries(SETS)) {
    const files = readdirSync(DIR).filter((f) => f.startsWith(`${device}-${lang}-`) && f.endsWith(".png")).sort();
    if (!files.length) throw new Error(`no ${device}-${lang}-*.png in ${DIR}`);
    let set = have.find((s) => s.attributes.screenshotDisplayType === screenshotDisplayType);
    if (!set) {
      set = (await api("POST", "/appScreenshotSets", { data: { type: "appScreenshotSets", attributes: { screenshotDisplayType },
        relationships: { appStoreVersionLocalization: { data: { type: "appStoreVersionLocalizations", id: loc.id } } } } })).data;
    }
    for (const old of (await api("GET", `/appScreenshotSets/${set.id}/appScreenshots?limit=200`)).data) await api("DELETE", `/appScreenshots/${old.id}`);
    for (const fileName of files) {
      const buf = readFileSync(join(DIR, fileName));
      const shot = (await api("POST", "/appScreenshots", { data: { type: "appScreenshots", attributes: { fileName, fileSize: buf.length },
        relationships: { appScreenshotSet: { data: { type: "appScreenshotSets", id: set.id } } } } })).data;
      for (const op of shot.attributes.uploadOperations) {
        const r = await fetch(op.url, { method: op.method, headers: Object.fromEntries(op.requestHeaders.map((h) => [h.name, h.value])),
                                        body: buf.subarray(op.offset, op.offset + op.length) });
        if (!r.ok) throw new Error(`${fileName}: upload part → ${r.status}`);
      }
      await api("PATCH", `/appScreenshots/${shot.id}`, { data: { type: "appScreenshots", id: shot.id,
        attributes: { uploaded: true, sourceFileChecksum: createHash("md5").update(buf).digest("hex") } } });
      console.log(`${locale} ${screenshotDisplayType} ${fileName}: uploaded`);
    }
    sets.push({ locale, screenshotDisplayType, id: set.id, n: files.length });
  }
}
// Apple processes the uploads for a moment; FAILED here means a size or format it refuses.
await new Promise((r) => setTimeout(r, 20_000));
for (const s of sets) {
  const states = (await api("GET", `/appScreenshotSets/${s.id}/appScreenshots?limit=200`)).data
    .map((x) => `${x.attributes.fileName}: ${x.attributes.assetDeliveryState?.state}` +
                (x.attributes.assetDeliveryState?.errors?.length ? ` ${JSON.stringify(x.attributes.assetDeliveryState.errors)}` : ""));
  console.log(`${s.locale} ${s.screenshotDisplayType}:\n  ${states.join("\n  ")}`);
}
