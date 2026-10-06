// `npm run build`. www/ is the source itself (no framework, no bundler, no
// copy step), so the build only checks it: every module a script imports and
// every file index.html loads has to exist, or the app opens on a blank page
// that no compiler would have caught.
import { readFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const www = join(dirname(fileURLToPath(import.meta.url)), "www");
const problems = [];

const walk = (dir) =>
  readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });

for (const file of walk(www).filter((f) => f.endsWith(".js"))) {
  const src = readFileSync(file, "utf8");
  for (const [, spec] of src.matchAll(/^\s*(?:import|export)\s[^"']*?from\s+["']([^"']+)["']/gm)) {
    if (!spec.startsWith(".")) problems.push(`${relative(www, file)}: bare import "${spec}" (no bundler here)`);
    else if (!existsSync(join(dirname(file), spec))) problems.push(`${relative(www, file)}: imports missing ${spec}`);
  }
}
const page = readFileSync(join(www, "index.html"), "utf8");
for (const [, ref] of page.matchAll(/(?:src|href)="([^"#:]+)"/g)) {
  if (!existsSync(join(www, ref))) problems.push(`index.html: loads missing ${ref}`);
}
if (problems.length) {
  console.error(problems.join("\n"));
  process.exit(1);
}
console.log("www/ checked: every import and every page reference resolves");
