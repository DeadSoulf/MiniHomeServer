/* Unit tests for the map upload flow, without a browser or external services. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const initialization = source.lastIndexOf("\ninstallHandlers();");
assert.ok(initialization > 0);

async function upload(outcome) {
  const calls = [], notices = [];
  const input = { value: "selected.png" };
  let image;
  const context = vm.createContext({
    document: { getElementById: () => input },
    FileReader: class {
      readAsDataURL() {
        this.result = "data:image/png;base64,test-fixture";
        queueMicrotask(() => this.onload());
      }
    },
    Image: class {
      constructor() { image = this; }
      set src(value) { this.url = value; }
    },
    calls, notices,
  });
  vm.runInContext(source.slice(0, initialization), context);
  vm.runInContext(`
    api = async (...args) => { calls.push(args); };
    notify = (...args) => { notices.push(args); };
    refresh = async () => {};
    navigate = () => {};
  `, context);
  const pending = vm.runInContext('uploadMap({type:"image/png",size:64})', context);
  // File read completes before the decoder; nothing may be sent yet.
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(image);
  assert.equal(calls.length, 0);
  assert.equal(input.value, "selected.png");
  if (outcome === "error") image.onerror();
  else {
    image.naturalWidth = outcome === "valid" ? 100 : 0;
    image.naturalHeight = outcome === "valid" ? 80 : 0;
    image.onload();
  }
  await pending;
  assert.equal(input.value, "");
  assert.equal(vm.runInContext("busy", context), false);
  return { calls, notices };
}

test("unreadable map is rejected before POST", async () => {
  const { calls, notices } = await upload("error");
  assert.equal(calls.length, 0);
  assert.ok(notices.some(([message, error]) => error && message.includes("Прежняя карта сохранена")));
});
test("zero-dimension map is rejected before POST", async () => {
  assert.equal((await upload("empty")).calls.length, 0);
});
test("decoded map is posted only after successful decode", async () => {
  const { calls } = await upload("valid");
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/map");
  assert.equal(calls[0][1], "POST");
  assert.equal(calls[0][2].data_url, "data:image/png;base64,test-fixture");
});
