/* Calibrated building footprint conversion, no browser or server required. */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const init = source.lastIndexOf("\ninstallHandlers();");
assert.ok(init > 0);
const ctx = vm.createContext({Math});
vm.runInContext(source.slice(0, init), ctx);

test("metre conversion allows precise fractional percentages", () => {
  ctx.m = 1.25;
  ctx.plan = 30;
  assert.equal(vm.runInContext("metersToPercent(m,plan)", ctx), 4.1667);
  assert.ok(Math.abs(vm.runInContext("percentToMeters(4.1667,30)", ctx) - 1.25) < 0.001);
});
test("building labels switch between percent and metres", () => {
  ctx.building = {building_width: 30, building_height: 20};
  vm.runInContext("state.map_scale=null", ctx);
  assert.equal(vm.runInContext("buildingDimensionLabel(building)",ctx), "30 × 20%");
  vm.runInContext("state.map_scale={width_m:30,height_m:40}",ctx);
  assert.equal(vm.runInContext("buildingDimensionLabel(building)",ctx), "9 × 8 м");
});
test("invalid input cannot be converted to valid numeric size", () => {
  ctx.nan = NaN;
  assert.ok(Number.isNaN(vm.runInContext("metersToPercent(nan,30)",ctx)));
  assert.ok(Number.isNaN(vm.runInContext("metersToPercent(1,0)",ctx)));
});
