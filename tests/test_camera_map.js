/* Camera cone geometry and version labels without a browser. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const init = source.lastIndexOf("\ninstallHandlers();");
assert.ok(init > 0);
function context() {
  const c = vm.createContext({Math});
  vm.runInContext(source.slice(0, init), c);
  return c;
}

test("camera view cone rotates clockwise from north", () => {
  const c = context();
  const north = vm.runInContext("cameraSectorPath(100,100,50,90,0)",c);
  const east = vm.runInContext("cameraSectorPath(100,100,50,90,90)",c);
  assert.match(north, /^M 100 100 L /);
  assert.match(east, /^M 100 100 L /);
  assert.notEqual(north, east);
  assert.ok(north.includes(" A 50 50 0 0 1 "));
});
test("wide-angle cone uses major arc; full circle uses SVG circle", () => {
  const c = context();
  const wide = vm.runInContext("cameraSectorPath(100,100,50,270,180)",c);
  const all = vm.runInContext("cameraSectorPath(100,100,50,360,180)",c);
  assert.ok(wide.includes(" A 50 50 0 1 1 "));
  assert.equal(all, "");
});
test("version and 12-character commit form one update label", () => {
  const c = context();
  c.sha = "ab8987d896b72979030ea30cd865282b7243de92";
  assert.equal(vm.runInContext("releaseLabel('0.3.6',sha)",c),
    "v0.3.6 · ab8987d896b7");
  assert.equal(vm.runInContext("releaseLabel(null,sha)",c),
    "Версия неизвестна · ab8987d896b7");
});
