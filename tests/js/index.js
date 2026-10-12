// Node 24 reads `node --test tests/js/` as "run the module at tests/js", which is this file.
// It loads every *.test.mjs file in this folder, so that command runs all JavaScript tests.
// (`node --test tests/js/*.test.mjs` also works and runs the files directly.)
"use strict";
const { readdirSync } = require("node:fs");
const { join } = require("node:path");
const { pathToFileURL } = require("node:url");

(async () => {
  const files = readdirSync(__dirname).filter((f) => f.endsWith(".test.mjs")).sort();
  for (const f of files) await import(pathToFileURL(join(__dirname, f)).href);
})();
