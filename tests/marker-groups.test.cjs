const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const web = join(__dirname, "..", "web");
const groupingSource = readFileSync(join(web, "marker-groups.js"), "utf8");
const helpers = vm.createContext({});
vm.runInContext(groupingSource, helpers);
const { screenMarkersAtPoint } = helpers;
const plain = value => JSON.parse(JSON.stringify(value));
const item = (x, count = 1, radius = 7, y = 0) =>
  ({ x, y, count, radius });

test("click selects nearby 7 and 8 stacks without moving their dots", () => {
  const items = [item(0, 7), item(20, 8)];
  const before = plain(items);
  const found = screenMarkersAtPoint(items, { x: 10, y: 0 });
  assert.equal(found.length, 2);
  assert.equal(found.reduce((sum, point) => sum + point.count, 0), 15);
  assert.deepEqual(items, before);
});

test("zoom limits the hit to the nearer location", () => {
  const found = screenMarkersAtPoint([item(0, 7), item(20 * 4, 8)], { x: 0, y: 0 });
  assert.equal(found.length, 1);
  assert.equal(found[0].count, 7);
});

test("hit testing respects dot size including density emphasis", () => {
  const click = { x: 25, y: 0 };
  assert.equal(screenMarkersAtPoint([item(0)], click).length, 0);
  assert.equal(screenMarkersAtPoint([item(0, 1, 25)], click).length, 1);
});

test("overlapping chains never extend the selection beyond the click radius", () => {
  const points = Array.from({ length: 500 }, (_, i) => item(i * 10));
  const found = screenMarkersAtPoint(points, { x: 100, y: 0 });
  assert.deepEqual(plain(found).map(point => point.x), [100, 90, 110]);
  assert.equal(points.length, 500);
});

test("empty route sections have no hidden clickable groups", () => {
  const points = [item(-10, 7), item(10, 8), item(400, 2)];
  assert.equal(screenMarkersAtPoint(points, { x: 200, y: 0 }).length, 0);
  assert.equal(screenMarkersAtPoint([], { x: 0, y: 0 }).length, 0);
  assert.equal(screenMarkersAtPoint(points, { x: 0, y: 50 }).length, 0);
});

test("5,000 locations retain original coordinates after repeated hits", () => {
  const items = Array.from({ length: 5000 }, (_, i) => item(i * 10, i % 8 + 1));
  const before = plain(items);
  assert.equal(screenMarkersAtPoint(items, { x: 100, y: 0 }).length, 3);
  assert.equal(screenMarkersAtPoint(items, { x: 200, y: 0 }).length, 3);
  assert.deepEqual(items, before);
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
      this.classList = { toggle() {}, add() {}, contains() { return false; } };
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
  const layer = (point, options) => ({
    point, options, handlers: {},
    items: [],
    addTo() { return this; },
    clearLayers() { this.items = []; },
    addLayer(item) { this.items.push(item); },
    bindTooltip() { return this; },
    bindPopup() { return this; },
    on(name, callback) { this.handlers[name] = callback; return this; }
  });
  let scale = 1;
  let flownTo;
  const map = {
    setView() { return this; }, createPane() {}, getPane() { return { style: {} }; },
    on() {}, closePopup() {}, flyTo(point, zoom) { flownTo = { point, zoom }; },
    mouseEventToLatLng(event) { return event.pointer; },
    latLngToLayerPoint(value) {
      const [lat, lng] = Array.isArray(value) ? value : [value.lat, value.lng];
      return { x: lng * scale, y: lat * scale };
    },
    layerPointToLatLng({ x, y }) { return { lng: x / scale, lat: y / scale }; }
  };
  let popupPoint;
  const context = vm.createContext({
    document, URLSearchParams, AbortController, console,
    fetch: () => new Promise(() => {}),
    window: { addEventListener() {}, matchMedia: () => ({ matches: false }) },
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
    networkLayers: () => vm.runInContext("networkLayer.items", context),
    setData(data) {
      context.testData = data;
      vm.runInContext("state.networkData = testData", context);
    },
    popupPoint: () => plain(popupPoint),
    flownTo: () => flownTo,
    setScale(value) { scale = value; }
  };
}

test("coordinate search moves to the surrounding area without an API request", async () => {
  const app = appHarness();
  app.nodes.get("#area-search-input").value = "51.5208, -0.1955";
  app.context.fetchJson = () => { throw new Error("Unexpected API request"); };
  await app.context.searchArea({ preventDefault() {} });
  assert.deepEqual(plain(app.flownTo()), { point: [51.5208, -0.1955], zoom: 13 });
  assert.match(app.nodes.get("#area-search-status").textContent, /Showing the area/);
});

test("what3words search uses the server and moves to the surrounding area", async () => {
  const app = appHarness();
  app.nodes.get("#area-search-input").value = "///filled.count.soap";
  let request;
  app.context.fetchJson = async (url, options) => {
    request = { url, options };
    return { latitude: 51.520847, longitude: -0.195521 };
  };
  await app.context.searchArea({ preventDefault() {} });
  assert.equal(request.url, "/api/locations/what3words");
  assert.equal(request.options.method, "POST");
  assert.deepEqual(JSON.parse(request.options.body), { words: "///filled.count.soap" });
  assert.deepEqual(plain(app.flownTo()), { point: [51.520847, -0.195521], zoom: 13 });
});

test("bad coordinates and invalid search text do not move the map", async () => {
  const app = appHarness();
  for (const input of ["91, 0", "51, -181", "not a location"]) {
    app.nodes.get("#area-search-input").value = input;
    await app.context.searchArea({ preventDefault() {} });
    assert.equal(app.flownTo(), undefined);
    assert.ok(app.nodes.get("#area-search-status").textContent.length);
  }
});

const stack = (x, count, id) => ({
  type: "Feature", geometry: { type: "Point", coordinates: [x, 10] },
  properties: { stack: true, count, stack_id: id, type_counts: { BLE: count } }
});
const single = (x, id) => ({
  type: "Feature", geometry: { type: "Point", coordinates: [x, 10] },
  properties: { id, type: "WIFI", observation_count: 1 }
});
const settle = () => new Promise(resolve => setImmediate(resolve));

test("every source location stays drawn, regardless of overlap or network count", () => {
  const app = appHarness();
  const inputs = [stack(0, 1990, "a"), stack(10, 8, "b"), single(20, 9)];
  const data = { mode: "points", features: inputs };
  app.setData(data);
  assert.equal(app.context.renderNetworks(data), 3);
  assert.equal(app.networkLayers().length, 3);
  assert.deepEqual(plain(app.networkLayers()).map(layer => layer.point), [[10, 0], [10, 10], [10, 20]]);
  assert.ok(app.networkLayers().every(layer => layer.options.radius <= 7));
  const found = app.context.screenLocationsAtPoint([10, 0]);
  assert.deepEqual(plain(found), inputs.slice(0, 2));
  app.setScale(8);
  assert.equal(app.context.renderNetworks(data), 3);
  assert.deepEqual(plain(app.context.screenLocationsAtPoint([10, 0])), inputs.slice(0, 1));
});

test("a 500-location route remains 500 dots while its click picker stays local", () => {
  const app = appHarness();
  const data = { mode: "points", features: Array.from({ length: 500 }, (_, i) => stack(i * 10, 7, `p${i}`)) };
  app.setData(data);
  assert.equal(app.context.renderNetworks(data), 500);
  assert.equal(app.networkLayers().length, 500);
  let chosen;
  app.context.openNetworkStack = feature => { chosen = feature; };
  app.context.selectLocationsAtPoint({ lat: 10, lng: 100 });
  assert.equal(chosen.locations.length, 3);
  assert.equal(chosen.properties.count, 21);
  assert.deepEqual(plain(chosen.locations).map(feature => feature.geometry.coordinates[0]), [100, 90, 110]);
});

test("a lone network opens its details while an empty hit does nothing", () => {
  const app = appHarness();
  const data = { mode: "points", features: [single(20, 9)] };
  app.setData(data);
  let chosen;
  app.context.selectNetworkFeature = feature => { chosen = feature; };
  app.context.selectLocationsAtPoint([10, 20]);
  assert.equal(chosen.properties.id, 9);
  chosen = null;
  app.context.selectLocationsAtPoint([10, 200]);
  assert.equal(chosen, null);
  app.setData({ mode: "clusters", features: data.features });
  assert.equal(app.context.screenLocationsAtPoint([10, 20]).length, 0);
});

test("small-circle clicks use the actual pointer, not Leaflet's snapped centre", () => {
  const app = appHarness();
  const data = { mode: "points", features: [stack(0, 7, "a"), stack(11, 8, "b"), stack(22, 7, "c")] };
  app.setData(data);
  app.context.renderNetworks(data);
  let chosen;
  app.context.openNetworkStack = feature => { chosen = feature; };
  app.networkLayers()[1].handlers.click({
    latlng: [10, 11],
    originalEvent: { pointer: [10, 5] }
  });
  assert.equal(chosen.locations.length, 2);
  assert.equal(chosen.properties.count, 15);
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
