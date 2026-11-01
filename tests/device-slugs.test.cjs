const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const context = vm.createContext({});
vm.runInContext(readFileSync(join(__dirname, "..", "web", "device-slugs.js"), "utf8"), context);
const { filterDeviceSlugs } = context;
const plain = value => JSON.parse(JSON.stringify(value));

const slugs = ["survey-phone", "car-tablet", "Phone-Backup"];

test("empty query lists every slug", () => {
  assert.deepEqual(plain(filterDeviceSlugs(slugs, "")), slugs);
  assert.deepEqual(plain(filterDeviceSlugs(slugs, "  ")), slugs);
});

test("query filters by case-insensitive substring", () => {
  assert.deepEqual(plain(filterDeviceSlugs(slugs, "PHONE")), ["survey-phone", "Phone-Backup"]);
  assert.deepEqual(plain(filterDeviceSlugs(slugs, "tab")), ["car-tablet"]);
});

test("no match returns an empty list", () => {
  assert.deepEqual(plain(filterDeviceSlugs(slugs, "zzz")), []);
});
