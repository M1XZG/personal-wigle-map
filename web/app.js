"use strict";

const TYPE_META = {
  WIFI: { label: "Wi-Fi", color: "#bd3bd1" },
  BLUETOOTH: { label: "Bluetooth", color: "#0284c7" },
  BLE: { label: "Bluetooth LE", color: "#0ea5e9" },
  GSM: { label: "Cellular", color: "#ea580c" },
  LTE: { label: "LTE", color: "#f97316" },
  CDMA: { label: "CDMA", color: "#fb923c" },
  WCDMA: { label: "WCDMA", color: "#fb923c" },
  NR: { label: "5G NR", color: "#f97316" }
};

const ROUTE_COLORS = [
  "#f43f5e", "#8b5cf6", "#06b6d4", "#f59e0b", "#10b981",
  "#ec4899", "#6366f1", "#84cc16", "#e879f9", "#14b8a6"
];

const state = {
  summary: null,
  networkData: null,
  routeData: null,
  selectedFiles: [],
  routeColors: new Map(),
  requestNumber: 0,
  mapController: null,
  debounceTimer: null
};

const elements = {
  sidebar: document.querySelector("#sidebar"),
  sidebarToggle: document.querySelector("#sidebar-toggle"),
  toggleLabel: document.querySelector(".toggle-label"),
  brandTitle: document.querySelector("#brand-title"),
  brandEyebrow: document.querySelector("#brand-eyebrow"),
  wigleBadge: document.querySelector("#wigle-badge"),
  wigleBadgeImage: document.querySelector("#wigle-badge-image"),
  apiAlert: document.querySelector("#api-alert"),
  mapState: document.querySelector("#map-state"),
  mapStateText: document.querySelector("#map-state-text"),
  summaryStatus: document.querySelector("#summary-status"),
  summaryNetworks: document.querySelector("#summary-networks"),
  summaryObservations: document.querySelector("#summary-observations"),
  summaryRoutes: document.querySelector("#summary-routes"),
  summaryDates: document.querySelector("#summary-dates"),
  deviceFilter: document.querySelector("#device-filter"),
  fromDate: document.querySelector("#from-date"),
  toDate: document.querySelector("#to-date"),
  showRoutes: document.querySelector("#show-routes"),
  densityMode: document.querySelector("#density-mode"),
  resetFilters: document.querySelector("#reset-filters"),
  routeLegend: document.querySelector("#route-legend"),
  uploadForm: document.querySelector("#upload-form"),
  uploadDevice: document.querySelector("#upload-device"),
  dropZone: document.querySelector("#drop-zone"),
  fileInput: document.querySelector("#file-input"),
  fileList: document.querySelector("#file-list"),
  uploadButton: document.querySelector("#upload-button"),
  uploadProgressWrap: document.querySelector("#upload-progress-wrap"),
  uploadProgress: document.querySelector("#upload-progress"),
  uploadProgressLabel: document.querySelector("#upload-progress-label"),
  uploadStatus: document.querySelector("#upload-status"),
  rescanButton: document.querySelector("#rescan-button"),
  refreshImports: document.querySelector("#refresh-imports"),
  importsList: document.querySelector("#imports-list")
};

const map = L.map("map", {
  zoomControl: true,
  preferCanvas: true,
  worldCopyJump: true,
  minZoom: 2
}).setView([25, 0], 3);

L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors"
}).addTo(map);

const networkLayer = L.layerGroup().addTo(map);
const routeLayer = L.layerGroup().addTo(map);

function pick(object, keys, fallback = null) {
  for (const key of keys) {
    if (object && object[key] !== undefined && object[key] !== null) {
      return object[key];
    }
  }
  return fallback;
}

function formatNumber(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? new Intl.NumberFormat().format(numeric) : "—";
}

function formatDate(value, includeTime = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, includeTime
    ? { dateStyle: "medium", timeStyle: "short" }
    : { dateStyle: "medium" }).format(date);
}

function dateInputValue(value) {
  if (!value) return "";
  const match = String(value).match(/^\d{4}-\d{2}-\d{2}/);
  if (match) return match[0];
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString().slice(0, 10);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  })[character]);
}

function errorMessage(error, fallback) {
  return error instanceof Error && error.message ? error.message : fallback;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { Accept: "application/json", ...(options.headers || {}) },
    ...options
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    throw new Error(pick(body, ["detail", "error", "message"], `${response.status} ${response.statusText}`));
  }
  return body;
}

async function loadConfig() {
  const config = await fetchJson("/api/config");
  const title = String(config?.title || "Personal WiGLE Map");
  const eyebrow = String(config?.eyebrow || "Wireless survey archive");
  const imageUrl = String(config?.badge?.image_url || "");
  const linkUrl = String(config?.badge?.link_url || "https://wigle.net");

  document.title = title;
  elements.brandTitle.textContent = title;
  elements.brandEyebrow.textContent = eyebrow;

  if (imageUrl) {
    elements.wigleBadge.href = linkUrl;
    elements.wigleBadgeImage.src = imageUrl;
    elements.wigleBadge.hidden = false;
  } else {
    elements.wigleBadge.hidden = true;
    elements.wigleBadge.removeAttribute("href");
    elements.wigleBadgeImage.removeAttribute("src");
  }
}

function showApiAlert(message) {
  elements.apiAlert.textContent = message;
  elements.apiAlert.hidden = false;
}

function clearApiAlert() {
  elements.apiAlert.hidden = true;
  elements.apiAlert.textContent = "";
}

function showMapState(message, mode = "loading") {
  elements.mapState.hidden = false;
  elements.mapState.className = `map-state ${mode}`;
  elements.mapStateText.textContent = message;
  const spinner = elements.mapState.querySelector(".spinner");
  spinner.hidden = mode !== "loading";
}

function hideMapState() {
  elements.mapState.hidden = true;
}

function normalizedDevices(summary) {
  const devices = pick(summary, ["devices", "device_names", "device_slugs"], []);
  if (devices && typeof devices === "object" && !Array.isArray(devices)) {
    return Object.keys(devices).sort().map(device => ({ value: device, label: device }));
  }
  if (!Array.isArray(devices)) return [];
  return devices.map(device => {
    if (typeof device === "string") return { value: device, label: device };
    return {
      value: String(pick(device, ["slug", "id", "name", "device"], "")),
      label: String(pick(device, ["name", "label", "slug", "device"], "Unknown device"))
    };
  }).filter(device => device.value);
}

function summaryDates(summary) {
  const coverage = pick(summary, ["date_coverage", "dates", "coverage"], {});
  return {
    first: pick(summary, ["first_seen", "min_date", "from_date"], pick(coverage, ["first", "start", "min", "from"])),
    last: pick(summary, ["last_seen", "max_date", "to_date"], pick(coverage, ["last", "end", "max", "to"]))
  };
}

function renderSummary(summary) {
  const dates = summaryDates(summary);
  elements.summaryNetworks.textContent = formatNumber(summary?.networks?.total);
  elements.summaryObservations.textContent = formatNumber(summary?.observations?.total);
  elements.summaryRoutes.textContent = formatNumber(summary?.routes?.segments);
  elements.summaryDates.textContent = dates.first || dates.last
    ? `${formatDate(dates.first)} – ${formatDate(dates.last)}`
    : "No dated records";

  elements.fromDate.min = dateInputValue(dates.first);
  elements.fromDate.max = dateInputValue(dates.last);
  elements.toDate.min = dateInputValue(dates.first);
  elements.toDate.max = dateInputValue(dates.last);

  const previous = new Set(Array.from(elements.deviceFilter.selectedOptions, option => option.value));
  elements.deviceFilter.replaceChildren();
  normalizedDevices(summary).forEach(device => {
    const option = document.createElement("option");
    option.value = device.value;
    option.textContent = device.label;
    option.selected = previous.has(device.value);
    elements.deviceFilter.append(option);
  });

  elements.summaryStatus.className = "status-dot ready";
  elements.summaryStatus.title = "Summary loaded";
  elements.summaryStatus.replaceChildren();
  const statusText = document.createElement("span");
  statusText.className = "sr-only";
  statusText.textContent = "Summary loaded";
  elements.summaryStatus.append(statusText);
}

async function loadSummary() {
  try {
    const summary = await fetchJson("/api/summary");
    state.summary = summary || {};
    renderSummary(state.summary);
    clearApiAlert();
  } catch (error) {
    elements.summaryStatus.className = "status-dot error";
    elements.summaryStatus.title = "Summary failed to load";
    showApiAlert(`Summary unavailable: ${errorMessage(error, "Unknown API error")}`);
  }
}

function activeTypes() {
  return Array.from(document.querySelectorAll('input[name="network-type"]:checked'))
    .flatMap(input => input.value.split(","));
}

function selectedDevices() {
  return Array.from(elements.deviceFilter.selectedOptions, option => option.value);
}

function filterParams(includeTypes) {
  const bounds = map.getBounds();
  const params = new URLSearchParams({
    bbox: [
      Math.max(-180, bounds.getWest()).toFixed(6),
      Math.max(-90, bounds.getSouth()).toFixed(6),
      Math.min(180, bounds.getEast()).toFixed(6),
      Math.min(90, bounds.getNorth()).toFixed(6)
    ].join(","),
    zoom: String(map.getZoom())
  });
  if (includeTypes) params.set("types", activeTypes().join(","));
  const devices = selectedDevices();
  if (devices.length) params.set("devices", devices.join(","));
  if (elements.fromDate.value) params.set("from", elements.fromDate.value);
  if (elements.toDate.value) params.set("to", elements.toDate.value);
  return params;
}

function typeCode(properties) {
  const raw = String(pick(properties, ["type", "network_type", "kind"], "WIFI")).toUpperCase();
  if (TYPE_META[raw]) return raw;
  if (raw.includes("BLE") || raw === "E") return "BLE";
  if (raw.includes("BLUETOOTH") || raw === "BT" || raw === "B") return "BLUETOOTH";
  if (raw.includes("LTE") || raw.includes("5G") || raw === "L") return "LTE";
  if (raw === "NR") return "NR";
  if (raw.includes("WCDMA")) return "WCDMA";
  if (raw.includes("CDMA") || raw === "C") return "CDMA";
  if (raw.includes("CELL") || raw.includes("GSM") || raw === "G") return "GSM";
  return "WIFI";
}

function featurePoint(feature) {
  if (!feature || !feature.geometry || feature.geometry.type !== "Point") return null;
  const [longitude, latitude] = feature.geometry.coordinates || [];
  if (!Number.isFinite(Number(latitude)) || !Number.isFinite(Number(longitude))) return null;
  return [Number(latitude), Number(longitude)];
}

function clusterMarker(feature) {
  const point = featurePoint(feature);
  if (!point) return null;
  const properties = feature.properties || {};
  const count = Math.max(1, Number(pick(properties, ["count", "point_count", "network_count"], 1)));
  const size = Math.max(32, Math.min(68, 25 + Math.sqrt(count) * 3.2));
  const displayCount = count > 999 ? `${Math.round(count / 100) / 10}k` : String(count);
  const className = elements.densityMode.checked ? "cluster-icon density" : "cluster-icon";
  const icon = L.divIcon({
    className: "",
    html: `<div class="${className}" style="width:${size}px;height:${size}px" aria-label="${escapeHtml(count)} networks">${escapeHtml(displayCount)}</div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2]
  });
  return L.marker(point, {
    icon,
    keyboard: true,
    title: `${formatNumber(count)} networks in this area`
  }).on("click", () => map.flyTo(point, Math.min(map.getZoom() + 2, 18)));
}

function popupHtml(properties) {
  const code = typeCode(properties);
  const meta = TYPE_META[code];
  const name = pick(properties, ["ssid", "name", "network_name", "label"], "Unnamed network");
  const first = pick(properties, ["first_seen", "firstseen", "first", "earliest"]);
  const last = pick(properties, ["last_seen", "lastseen", "last", "latest"]);
  const observations = pick(properties, ["observations", "observation_count", "count", "samples"]);
  const signal = pick(properties, ["signal", "signal_dbm", "rssi", "best_signal"]);
  const rows = [
    ["First seen", formatDate(first, true)],
    ["Last seen", formatDate(last, true)],
    ["Observations", formatNumber(observations)]
  ];
  if (signal !== null && signal !== undefined && signal !== "") {
    const signalText = /^-?\d+(\.\d+)?$/.test(String(signal)) ? `${signal} dBm` : String(signal);
    rows.push(["Signal", signalText]);
  }
  return `
    <h3 class="popup-title">${escapeHtml(name)}</h3>
    <span class="popup-type" style="background:${meta.color}">${escapeHtml(meta.label)}</span>
    <dl class="popup-grid">
      ${rows.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join("")}
    </dl>`;
}

function pointMarker(feature) {
  const point = featurePoint(feature);
  if (!point) return null;
  const properties = feature.properties || {};
  const meta = TYPE_META[typeCode(properties)];
  const observations = Math.max(1, Number(pick(properties, ["observations", "observation_count", "count"], 1)));
  const radius = elements.densityMode.checked
    ? Math.max(6, Math.min(24, 4 + Math.log2(observations + 1) * 2.2))
    : 6;
  const marker = L.circleMarker(point, {
    radius,
    color: "#ffffff",
    weight: elements.densityMode.checked ? 1 : 1.5,
    fillColor: meta.color,
    fillOpacity: elements.densityMode.checked ? 0.42 : 0.82,
    bubblingMouseEvents: false
  });
  marker.bindPopup(popupHtml(properties), { maxWidth: 290 });
  return marker;
}

function renderNetworks(data) {
  networkLayer.clearLayers();
  const features = Array.isArray(data?.features) ? data.features : [];
  features.forEach(feature => {
    const properties = feature.properties || {};
    const isCluster = properties.cluster === true || properties.cluster === 1 || properties.cluster === "true";
    const marker = isCluster ? clusterMarker(feature) : pointMarker(feature);
    if (marker) networkLayer.addLayer(marker);
  });
  return features.length;
}

function routeDevice(feature, index) {
  return String(pick(feature.properties || {}, ["device", "device_name", "device_slug", "name"], `Route ${index + 1}`));
}

function routeColor(device) {
  if (!state.routeColors.has(device)) {
    state.routeColors.set(device, ROUTE_COLORS[state.routeColors.size % ROUTE_COLORS.length]);
  }
  return state.routeColors.get(device);
}

function renderRouteLegend(devices) {
  elements.routeLegend.replaceChildren();
  if (!devices.length || !elements.showRoutes.checked) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = elements.showRoutes.checked ? "No routes in this view." : "Route display is switched off.";
    elements.routeLegend.append(empty);
    return;
  }
  devices.forEach(device => {
    const row = document.createElement("div");
    row.className = "legend-item";
    const line = document.createElement("i");
    line.className = "legend-line";
    line.style.backgroundColor = routeColor(device);
    const label = document.createElement("span");
    label.textContent = device;
    row.append(line, label);
    elements.routeLegend.append(row);
  });
}

function renderRoutes(data) {
  routeLayer.clearLayers();
  if (!elements.showRoutes.checked) {
    renderRouteLegend([]);
    return 0;
  }
  const features = Array.isArray(data?.features) ? data.features : [];
  const devices = [];
  features.forEach((feature, index) => {
    const device = routeDevice(feature, index);
    if (!devices.includes(device)) devices.push(device);
    L.geoJSON(feature, {
      style: {
        color: routeColor(device),
        weight: 3.5,
        opacity: 0.82,
        lineCap: "round",
        lineJoin: "round"
      },
      onEachFeature: (_, layer) => {
        layer.bindTooltip(escapeHtml(device), { sticky: true, direction: "top" });
      }
    }).addTo(routeLayer);
  });
  renderRouteLegend(devices);
  return features.length;
}

async function refreshMap() {
  if (state.mapController) state.mapController.abort();
  const controller = new AbortController();
  const requestNumber = ++state.requestNumber;
  state.mapController = controller;

  showMapState("Loading networks and routes…");
  const hasNetworkTypes = activeTypes().length > 0;

  try {
    const networksRequest = hasNetworkTypes
      ? fetchJson(`/api/networks?${filterParams(true)}`, { signal: controller.signal })
      : Promise.resolve({ type: "FeatureCollection", features: [] });
    const routesRequest = elements.showRoutes.checked
      ? fetchJson(`/api/routes?${filterParams(false)}`, { signal: controller.signal })
      : Promise.resolve(state.routeData);
    const [networks, routes] = await Promise.all([networksRequest, routesRequest]);
    if (requestNumber !== state.requestNumber || controller.signal.aborted) return;

    state.networkData = networks || { type: "FeatureCollection", features: [] };
    if (elements.showRoutes.checked) {
      state.routeData = routes || { type: "FeatureCollection", features: [] };
    }
    const networkCount = renderNetworks(state.networkData);
    const routeCount = renderRoutes(state.routeData);
    clearApiAlert();
    if (!hasNetworkTypes && routeCount) {
      showMapState("Network types are off. Showing survey routes only.", "empty");
    } else if (!hasNetworkTypes) {
      showMapState("Choose at least one network type.", "empty");
    } else if (!networkCount && !routeCount) {
      showMapState("No networks match this view and filter.", "empty");
    } else {
      hideMapState();
    }
  } catch (error) {
    if (error.name === "AbortError" || requestNumber !== state.requestNumber) return;
    showMapState(`Map data unavailable: ${errorMessage(error, "Unknown API error")}`, "error");
    showApiAlert(`Map request failed: ${errorMessage(error, "Unknown API error")}`);
  }
}

function scheduleMapRefresh(delay = 300) {
  window.clearTimeout(state.debounceTimer);
  state.debounceTimer = window.setTimeout(refreshMap, delay);
}

function setSidebarCollapsed(collapsed) {
  elements.sidebar.classList.toggle("collapsed", collapsed);
  elements.sidebarToggle.classList.toggle("collapsed", collapsed);
  elements.sidebarToggle.setAttribute("aria-expanded", String(!collapsed));
  elements.toggleLabel.textContent = collapsed ? "Show controls" : "Hide controls";
  window.setTimeout(() => map.invalidateSize(), 230);
}

function resetFilters() {
  document.querySelectorAll('input[name="network-type"]').forEach(input => {
    input.checked = true;
  });
  Array.from(elements.deviceFilter.options).forEach(option => {
    option.selected = false;
  });
  elements.fromDate.value = "";
  elements.toDate.value = "";
  elements.showRoutes.checked = false;
  elements.densityMode.checked = false;
  scheduleMapRefresh(0);
}

function importRecords(body) {
  if (Array.isArray(body)) return body;
  return pick(body, ["imports", "items", "results", "files"], []);
}

function renderImports(body) {
  elements.importsList.replaceChildren();
  const records = importRecords(body);
  if (!Array.isArray(records) || !records.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No imports have been recorded yet.";
    elements.importsList.append(empty);
    return;
  }
  records.slice(0, 20).forEach(record => {
    const row = document.createElement("article");
    row.className = "import-row";

    const name = document.createElement("span");
    name.className = "import-name";
    name.title = String(pick(record, ["source_name", "filename", "file", "name"], "Unknown file"));
    name.textContent = name.title;

    const device = pick(record, ["device_label", "device", "device_name", "device_slug"], "Unknown device");
    const date = pick(record, ["imported_at", "created_at", "date", "scanned_at"]);
    const meta = document.createElement("span");
    meta.className = "import-meta";
    meta.textContent = `${device} · ${formatDate(date, true)}`;

    const outcomeText = String(pick(record, ["status", "outcome", "result"], "unknown"));
    const outcome = document.createElement("span");
    outcome.className = `import-outcome ${outcomeText.toLowerCase().replace(/[^a-z0-9_-]/g, "")}`;
    outcome.textContent = outcomeText;

    row.append(name, outcome, meta);
    elements.importsList.append(row);
  });
}

async function loadImports() {
  elements.refreshImports.disabled = true;
  try {
    renderImports(await fetchJson("/api/imports"));
  } catch (error) {
    elements.importsList.replaceChildren();
    const message = document.createElement("p");
    message.className = "inline-status error";
    message.textContent = `Imports unavailable: ${errorMessage(error, "Unknown API error")}`;
    elements.importsList.append(message);
  } finally {
    elements.refreshImports.disabled = false;
  }
}

async function waitForImportJob() {
  const deadline = Date.now() + 30 * 60 * 1000;
  while (Date.now() < deadline) {
    const body = await fetchJson("/api/imports");
    renderImports(body);
    if (!body?.job?.queued && !body?.job?.running) {
      if (body?.job?.error) throw new Error(body.job.error);
      return;
    }
    await new Promise(resolve => window.setTimeout(resolve, 1500));
  }
  throw new Error("The import is still running. Check Recent imports later.");
}

function acceptedFile(file) {
  return /\.(csv|csv\.gz|gz|kml|gpx|sqlite|db)$/i.test(file.name);
}

function setSelectedFiles(files) {
  const valid = Array.from(files).filter(acceptedFile);
  state.selectedFiles = valid;
  elements.fileList.replaceChildren();
  valid.forEach(file => {
    const item = document.createElement("li");
    item.textContent = `${file.name} (${formatNumber(file.size)} bytes)`;
    elements.fileList.append(item);
  });
  if (files.length && valid.length !== files.length) {
    elements.uploadStatus.className = "inline-status error";
    elements.uploadStatus.textContent = "Unsupported files were left out.";
  } else {
    elements.uploadStatus.textContent = "";
    elements.uploadStatus.className = "inline-status";
  }
}

function uploadFiles(event) {
  event.preventDefault();
  const device = elements.uploadDevice.value.trim();
  if (!device || !state.selectedFiles.length) {
    elements.uploadStatus.className = "inline-status error";
    elements.uploadStatus.textContent = !device ? "Enter a device name." : "Choose at least one supported file.";
    return;
  }

  const formData = new FormData();
  formData.append("device", device);
  state.selectedFiles.forEach(file => formData.append("files", file, file.name));

  const request = new XMLHttpRequest();
  request.open("POST", "/api/upload");
  request.setRequestHeader("Accept", "application/json");
  elements.uploadButton.disabled = true;
  elements.uploadProgressWrap.hidden = false;
  elements.uploadProgress.value = 0;
  elements.uploadProgressLabel.textContent = "Uploading…";
  elements.uploadStatus.textContent = "";
  elements.uploadStatus.className = "inline-status";

  request.upload.addEventListener("progress", progressEvent => {
    if (!progressEvent.lengthComputable) {
      elements.uploadProgress.removeAttribute("value");
      elements.uploadProgressLabel.textContent = "Uploading…";
      return;
    }
    const percent = Math.round((progressEvent.loaded / progressEvent.total) * 100);
    elements.uploadProgress.value = percent;
    elements.uploadProgressLabel.textContent = `${percent}%`;
  });

  request.addEventListener("load", async () => {
    elements.uploadButton.disabled = false;
    elements.uploadProgress.value = 100;
    let body = null;
    try {
      body = JSON.parse(request.responseText);
    } catch {
      body = null;
    }
    if (request.status >= 200 && request.status < 300) {
      elements.uploadStatus.className = "inline-status success";
      elements.uploadStatus.textContent = String(pick(body, ["message", "detail"], "Upload saved; import queued."));
      state.selectedFiles = [];
      elements.fileInput.value = "";
      elements.fileList.replaceChildren();
      try {
        await waitForImportJob();
        elements.uploadStatus.textContent = "Upload imported.";
        await loadSummary();
        scheduleMapRefresh(0);
      } catch (error) {
        elements.uploadStatus.className = "inline-status error";
        elements.uploadStatus.textContent = `Import status: ${errorMessage(error, "Unknown import error")}`;
      }
    } else {
      elements.uploadStatus.className = "inline-status error";
      elements.uploadStatus.textContent = `Upload failed: ${pick(body, ["detail", "error", "message"], `${request.status} ${request.statusText}`)}`;
    }
    window.setTimeout(() => {
      elements.uploadProgressWrap.hidden = true;
    }, 1200);
  });

  request.addEventListener("error", () => {
    elements.uploadButton.disabled = false;
    elements.uploadProgressWrap.hidden = true;
    elements.uploadStatus.className = "inline-status error";
    elements.uploadStatus.textContent = "Upload failed because the server could not be reached.";
  });

  request.addEventListener("abort", () => {
    elements.uploadButton.disabled = false;
    elements.uploadProgressWrap.hidden = true;
  });

  request.send(formData);
}

async function rescan() {
  elements.rescanButton.disabled = true;
  elements.uploadStatus.className = "inline-status";
  elements.uploadStatus.textContent = "Rescanning storage…";
  try {
    await fetchJson("/api/rescan", { method: "POST" });
    elements.uploadStatus.className = "inline-status success";
    elements.uploadStatus.textContent = "Rescan queued.";
    await waitForImportJob();
    elements.uploadStatus.textContent = "Rescan completed.";
    await loadSummary();
    scheduleMapRefresh(0);
  } catch (error) {
    elements.uploadStatus.className = "inline-status error";
    elements.uploadStatus.textContent = `Rescan failed: ${errorMessage(error, "Unknown API error")}`;
  } finally {
    elements.rescanButton.disabled = false;
  }
}

map.on("moveend zoomend", () => scheduleMapRefresh());
elements.sidebarToggle.addEventListener("click", () => {
  setSidebarCollapsed(!elements.sidebar.classList.contains("collapsed"));
});
document.querySelector(".brand").addEventListener("click", () => {
  if (window.matchMedia("(max-width: 760px)").matches) {
    setSidebarCollapsed(!elements.sidebar.classList.contains("collapsed"));
  }
});
document.querySelectorAll('input[name="network-type"]').forEach(input => {
  input.addEventListener("change", () => scheduleMapRefresh(0));
});
elements.deviceFilter.addEventListener("change", () => scheduleMapRefresh(0));
elements.fromDate.addEventListener("change", () => scheduleMapRefresh(0));
elements.toDate.addEventListener("change", () => scheduleMapRefresh(0));
elements.showRoutes.addEventListener("change", () => {
  if (!elements.showRoutes.checked) {
    routeLayer.clearLayers();
    renderRouteLegend([]);
  }
  scheduleMapRefresh(0);
});
elements.densityMode.addEventListener("change", () => {
  renderNetworks(state.networkData);
});
elements.resetFilters.addEventListener("click", resetFilters);
elements.refreshImports.addEventListener("click", loadImports);
elements.rescanButton.addEventListener("click", rescan);
elements.uploadForm.addEventListener("submit", uploadFiles);
elements.fileInput.addEventListener("change", event => setSelectedFiles(event.target.files));
elements.dropZone.addEventListener("click", () => elements.fileInput.click());
elements.dropZone.addEventListener("keydown", event => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    elements.fileInput.click();
  }
});
["dragenter", "dragover"].forEach(eventName => {
  elements.dropZone.addEventListener(eventName, event => {
    event.preventDefault();
    elements.dropZone.classList.add("dragging");
  });
});
["dragleave", "drop"].forEach(eventName => {
  elements.dropZone.addEventListener(eventName, event => {
    event.preventDefault();
    elements.dropZone.classList.remove("dragging");
  });
});
elements.dropZone.addEventListener("drop", event => setSelectedFiles(event.dataTransfer.files));

window.addEventListener("resize", () => map.invalidateSize());

Promise.allSettled([loadConfig(), loadSummary(), loadImports()])
  .finally(() => refreshMap());
