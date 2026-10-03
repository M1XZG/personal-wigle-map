const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const web = join(__dirname, "..", "web");
const groupingSource = readFileSync(join(web, "marker-groups.js"), "utf8");
const helpers = vm.createContext({});
vm.runInContext(groupingSource, helpers);
const { groupScreenMarkers, stackMarkerSize } = helpers;
const plain = value => JSON.parse(JSON.stringify(value));
const item = (x, count, radius = stackMarkerSize(count) / 2, y = 0) =>
  ({ x, y, count, radius });

test("7 and 8 overlapping stacks combine to 15 without modifying input", () => {
  const items = [item(0, 7), item(24, 8)];
  const before = plain(items);
  const groups = groupScreenMarkers(items);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].count, 15);
  assert.equal(groups[0].members.length, 2);
  assert.deepEqual(items, before);
  assert.deepEqual(plain(groupScreenMarkers([...items].reverse())), plain(groups));
});

test("zoom separates two locations again from the original inputs", () => {
  assert.equal(groupScreenMarkers([item(0, 7), item(24, 8)]).length, 1);
  assert.equal(groupScreenMarkers([item(0, 7), item(24 * 4, 8)]).length, 2);
});

test("singles and density-sized markers cannot hide behind stacks", () => {
  assert.equal(groupScreenMarkers([item(0, 7), item(25, 1, 7)])[0].count, 8);
  assert.equal(groupScreenMarkers([item(0, 1, 7), item(30, 1, 7)]).length, 2);
  assert.equal(groupScreenMarkers([item(0, 1, 25), item(30, 1, 25)]).length, 1);
});

test("merging accounts for transitive overlap and newly enlarged badges", () => {
  const groups = groupScreenMarkers([item(0, 1, 7), item(15, 1, 7), item(35, 1, 7)]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].count, 3);
  const expanded = groupScreenMarkers([item(0, 1, 7), item(10, 1, 7), item(34, 1, 7)]);
  assert.equal(expanded.length, 1);
});

test("cell boundaries, negative positions and distant markers are correct", () => {
  const groups = groupScreenMarkers([item(-1, 7), item(1, 8), item(400, 2)]);
  assert.deepEqual(plain(groups).map(group => group.count).sort((a, b) => a - b), [2, 15]);
  assert.equal(groupScreenMarkers([]).length, 0);
  assert.equal(groupScreenMarkers([item(1, 1, 7)])[0].radius, 7);
});

test("5,000 locations preserve counts and leave no overlapping output badges", () => {
  const items = Array.from({ length: 5000 }, (_, i) =>
    item((i % 100) * 40, 2 + i % 10, undefined, Math.floor(i / 100) * 90));
  const groups = groupScreenMarkers(items);
  assert.equal(groups.reduce((sum, group) => sum + group.count, 0),
    items.reduce((sum, point) => sum + point.count, 0));
  assert.equal(groups.reduce((sum, group) => sum + group.members.length, 0), 5000);
  for (let i = 0; i < groups.length; i++) {
    for (let j = i + 1; j < groups.length; j++) {
      assert.ok(Math.hypot(groups[i].x - groups[j].x, groups[i].y - groups[j].y)
        > groups[i].radius + groups[j].radius + 6);
    }
  }
});

function appHarness() {
  class Element {
    constructor() {
      this.children = [];
      this.dataset = {};
      this.style = {};
      this.handlers = {};
      this.checked = false;
      this.value = "";
      this.selectedOptions = [];
      this.classList = { toggle() {}, contains() { return false; } };
    }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    addEventListener(name, callback) { this.handlers[name] = callback; }
    focus() {}
    querySelector() { return new Element(); }
    querySelectorAll() { return this.children; }
  }
  const nodes = new Map();
  const document = {
    activeElement: null,
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, new Element());
      return nodes.get(selector);
    },
    querySelectorAll(selector) {
      return selector.includes("network-type")
        ? [Object.assign(new Element(), { value: "WIFI,BLE" })]
        : [];
    },
    createElement() { return new Element(); }
  };
  const layer = () => ({
    items: [],
    addTo() { return this; },
    clearLayers() { this.items = []; },
    addLayer(item) { this.items.push(item); },
    bindTooltip() { return this; },
    bindPopup() { return this; },
    on() { return this; }
  });
  let scale = 1;
  const map = {
    setView() { return this; }, createPane() {}, getPane() { return { style: {} }; },
    on() {}, closePopup() {},
    latLngToLayerPoint([lat, lng]) { return { x: lng * scale, y: lat * scale }; },
    layerPointToLatLng({ x, y }) { return { lng: x / scale, lat: y / scale }; }
  };
  let popupPoint;
  const context = vm.createContext({
    document, URLSearchParams, AbortController, console,
    fetch: () => new Promise(() => {}),
    window: { addEventListener() {} },
    L: {
      map: () => map, tileLayer: layer, layerGroup: layer,
      point: (x, y) => ({ x, y }), polyline: layer, circleMarker: layer,
      popup: () => ({
        setLatLng(point) { popupPoint = point; return this; },
        setContent() { return this; }, openOn() { return this; }
      })
    }
  });
  vm.runInContext(groupingSource, context);
  vm.runInContext(readFileSync(join(web, "app.js"), "utf8"), context);
  return {
    context, nodes, document,
    selection: () => vm.runInContext("state.stackSelection", context),
    controller: () => vm.runInContext("state.stackController", context),
    popupPoint: () => plain(popupPoint),
    setScale(value) { scale = value; }
  };
}

const stack = (x, count, id) => ({
  type: "Feature", geometry: { type: "Point", coordinates: [x, 10] },
  properties: { stack: true, count, stack_id: id, type_counts: { BLE: count } }
});
const single = (x, id) => ({
  type: "Feature", geometry: { type: "Point", coordinates: [x, 10] },
  properties: { id, type: "WIFI", observation_count: 1 }
});
const settle = () => new Promise(resolve => setImmediate(resolve));

test("screen projection groups stacks and singles, retaining their original locations", () => {
  const app = appHarness();
  const inputs = [stack(0, 7, "a"), stack(20, 8, "b"), single(40, 9)];
  const grouped = app.context.screenGroupedFeatures(inputs);
  assert.equal(grouped.length, 1);
  assert.equal(grouped[0].properties.count, 16);
  assert.equal(grouped[0].properties.location_count, 3);
  assert.deepEqual(plain(grouped[0].properties.type_counts), { BLE: 15, WIFI: 1 });
  assert.deepEqual(plain(grouped[0].locations), inputs);
  app.setScale(8);
  assert.equal(app.context.screenGroupedFeatures(inputs).length, 3);
});

test("combined drawer pages across both stacks without duplicate or missing networks", async () => {
  const app = appHarness();
  const locations = [stack(0, 30, "a"), stack(20, 8, "b"), single(40, 200)];
  const calls = [];
  app.context.fetchJson = async url => {
    calls.push(url);
    const parsed = new URL(url, "http://example.test");
    const isA = parsed.pathname.endsWith("/a");
    const count = isA ? 30 : 8;
    const start = (Number(parsed.searchParams.get("page")) - 1) * 25;
    const length = Math.min(25, count - start);
    return {
      total: count, has_more: start + length < count,
      features: Array.from({ length }, (_, i) => single(isA ? 0 : 20, (isA ? 1 : 101) + start + i))
    };
  };
  app.nodes.get("#from-date").value = "2026-01-01";
  app.nodes.get("#device-filter").selectedOptions = [{ value: "scanner" }];
  app.context.browseStackLocations(locations, [10, 10]);
  await settle();
  assert.equal(app.selection().rows.length, 25);
  assert.equal(app.nodes.get("#stack-load-more").hidden, false);
  await app.context.loadStackPage();
  assert.equal(app.selection().rows.length, 39);
  assert.equal(new Set(app.selection().rows.map(row => row.feature.properties.id)).size, 39);
  assert.equal(app.nodes.get("#stack-load-more").hidden, true);
  assert.ok(calls.every(url => url.includes("devices=scanner") && url.includes("from=2026-01-01") && url.includes("types=WIFI%2CBLE")));
  assert.equal(calls.length, 3);
  const row = app.context.stackRow(app.selection().rows[30].feature, 2, true);
  app.context.loadNetworkObservations = () => {};
  row.handlers.click();
  assert.deepEqual(app.popupPoint(), [10, 20]);
  app.context.browseStackLocations(locations, [10, 10], 1);
  await settle();
  assert.equal(app.selection().rows.length, 8);
  assert.ok(app.selection().rows.every(row => row.locationNumber === 2));
});

test("failed page rolls back cursors and retry preserves all members", async () => {
  const app = appHarness();
  let fail = true;
  app.context.fetchJson = async url => {
    const isB = url.includes("/b?");
    if (isB && fail) throw new Error("Temporary failure");
    return {
      total: isB ? 8 : 7, has_more: false,
      features: Array.from({ length: isB ? 8 : 7 }, (_, i) => single(0, (isB ? 100 : 1) + i))
    };
  };
  app.context.browseStackLocations([stack(0, 7, "a"), stack(20, 8, "b")], [10, 10]);
  await settle();
  assert.equal(app.selection().rows.length, 0);
  assert.equal(app.selection().index, 0);
  assert.equal(app.nodes.get("#stack-load-more").textContent, "Retry loading networks");
  fail = false;
  await app.context.loadStackPage();
  assert.equal(app.selection().rows.length, 15);
});

test("late responses cannot overwrite a new selection or unlock its loading button", async () => {
  const app = appHarness();
  const resolve = [];
  app.context.fetchJson = () => new Promise(done => resolve.push(done));
  app.context.browseStackLocations([stack(0, 7, "a")], [10, 0]);
  app.context.browseStackLocations([stack(20, 8, "b")], [10, 20]);
  resolve[0]({ total: 7, has_more: false, features: [single(0, 1)] });
  await settle();
  assert.equal(app.selection().rows.length, 0);
  assert.ok(app.controller());
  assert.equal(app.nodes.get("#stack-load-more").disabled, true);
  resolve[1]({ total: 8, has_more: false, features: [single(20, 2)] });
  await settle();
  assert.deepEqual(plain(app.selection().rows).map(row => row.feature.properties.id), [2]);
  app.context.closeStackDrawer();
  assert.equal(app.selection(), null);
});
