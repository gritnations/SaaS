// Stamps a version number onto every script, stylesheet and module link in a folder that is about to be uploaded.
//
// Browsers keep their own copy of scripts and stylesheets. After an upload, a browser can then run an old script
// against a new page and stop with a half-drawn screen. A link such as /assets/app.js?v=1afb199 is a new address
// for the browser whenever the version changes, so it always fetches the new file.
//
// Usage: node site/stamp-version.mjs <folder> <version>
// The folder is the copy made for uploading, never the files in the repository. Running it again with a new
// version replaces the old one.

import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import process from "node:process";

const [folder, version] = process.argv.slice(2);
if (!folder || !version || !/^[\w.-]{1,40}$/.test(version)) {
  console.error("Usage: node site/stamp-version.mjs <folder> <version>   (version: letters, digits, dots, dashes)");
  process.exit(1);
}

/**
 * List every file under a folder.
 * @param {string} dir The folder.
 * @returns {string[]} The file paths.
 */
function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

/** Links in pages: src="/assets/x.js" and href="/assets/x.css", with or without an earlier version. */
const PAGE_LINK = /((?:src|href)=")(\/assets\/[\w./-]+\.(?:js|css))(?:\?v=[\w.-]+)?(")/g;
/** Module imports in scripts: from "./x.js", with or without an earlier version. */
const IMPORT = /(from ")(\.\/[\w.-]+\.js)(?:\?v=[\w.-]+)?(")/g;

let changed = 0;
for (const file of walk(folder)) {
  const isPage = file.endsWith(".html");
  const isScript = file.endsWith(".js");
  if (!isPage && !isScript) continue;
  const before = readFileSync(file, "utf8");
  const after = before.replace(isPage ? PAGE_LINK : IMPORT, `$1$2?v=${version}$3`);
  if (after !== before) {
    writeFileSync(file, after);
    changed += 1;
  }
}
console.log(`Stamped version ${version} onto ${changed} files in ${folder}.`);
