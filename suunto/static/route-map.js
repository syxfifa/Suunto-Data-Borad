const routeMap = document.querySelector("#route-map");
const mapHome = document.querySelector("#route-map-home");
const overviewSlot = document.querySelector("#route-map-overview-slot");
const compact = matchMedia("(max-width: 59.99rem)");
const inlineRow = document.createElement("tr");
inlineRow.className = "route-inline-map";
inlineRow.setAttribute("role", "row");
const inlineCell = inlineRow.insertCell();
inlineCell.colSpan = document.querySelector(".route-results thead tr").cells.length;
inlineCell.setAttribute("role", "cell");

function placeMap(trigger) {
  const row = trigger?.closest("tr");
  if (compact.matches && row) {
    if (row.nextElementSibling !== inlineRow) row.after(inlineRow);
    if (routeMap.parentElement !== inlineCell) inlineCell.append(routeMap);
  } else {
    const slot = compact.matches ? overviewSlot : mapHome;
    if (routeMap.parentElement !== slot) slot.append(routeMap);
    inlineRow.remove();
  }
}

compact.addEventListener("change", () => {
  if (!routeMap.hidden) placeMap(mapTrigger);
});
const canvas = routeMap.querySelector("canvas");
const ctx = canvas.getContext("2d");
const mapTheme = getComputedStyle(document.documentElement);
const mapColor = (name) => mapTheme.getPropertyValue(name).trim();
const pointStatus = document.querySelector("#route-point");
const link = document.querySelector("#route-map-link");
const coordinates = document.querySelector("#route-coordinates");
const amapContainer = document.querySelector("#amap-preview");
const amapStatus = document.querySelector("#amap-status");
const amapRetry = document.querySelector("#amap-retry");
const pointChoice = document.querySelector("#route-point-choice");
const pointSelect = document.querySelector("#route-point-select");
const amap = amapContainer && createAmapPreview(amapContainer, routeMap.dataset.amapKey, routeMap.dataset.amapCoordinates);
let pointsShown = [];
let pointShown;
let zoom = 1;
let offsetX = 0;
let offsetY = 0;
let drag = null;
let mapTrigger;
let routeSelection;
let previewId = 0;

function drawPoints(points, selected) {
  const {width, height} = canvas;
  const radians = Math.PI / 180;
  // Center the longitude offsets on the selected point, including at the date line.
  const positions = points.map((point) => ({
    east: ((point.lon - selected.lon + 540) % 360 - 180) * Math.cos(selected.lat * radians),
    north: point.lat - selected.lat,
  }));
  const east = positions.map((p) => p.east);
  const north = positions.map((p) => p.north);
  const minEast = Math.min(...east);
  const maxEast = Math.max(...east);
  const minNorth = Math.min(...north);
  const maxNorth = Math.max(...north);
  const scale = zoom * Math.min((width - 88) / Math.max(maxEast - minEast, 0.00001),
                               (height - 88) / Math.max(maxNorth - minNorth, 0.00001));
  ctx.fillStyle = mapColor("--canvas");
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = mapColor("--line");
  ctx.beginPath();
  for (let x = 20; x < width; x += 32) {
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
  }
  for (let y = 20; y < height; y += 32) {
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
  }
  ctx.stroke();
  ctx.font = "bold 17px system-ui";
  ctx.fillStyle = mapColor("--ink");
  ctx.fillText("↑ 北", 14, 25);

  // Draw the selected marker last so it stays visible at overlapping coordinates.
  const activeIndex = points.indexOf(selected);
  const order = positions.map((_, index) => index).filter((index) => index !== activeIndex);
  order.push(activeIndex);
  for (const index of order) {
    const p = positions[index];
    const x = width / 2 + offsetX + (p.east - (minEast + maxEast) / 2) * scale;
    const y = height / 2 + offsetY - (p.north - (minNorth + maxNorth) / 2) * scale;
    ctx.beginPath();
    const radius = (index === activeIndex ? 10 : 7) + Math.min(8, (points[index].count || 1) - 1);
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fillStyle = mapColor(index === activeIndex ? "--chart-rose" : "--chart-cyan");
    ctx.fill();
    ctx.strokeStyle = mapColor("--surface");
    ctx.lineWidth = 2;
    ctx.stroke();
  }
  ctx.lineWidth = 1;
}

function showCoordinates(selected, label, individual) {
  const {lat, lon, alt} = selected;
  coordinates.value = individual ? `${lat.toFixed(6)}, ${lon.toFixed(6)}` : "";
  pointStatus.textContent = [label, individual && alt != null && alt !== "" ? alt + " m" : ""]
    .filter(Boolean).join(" · ");
  if (link) {
    const map = new URL("https://www.openstreetmap.org/");
    map.search = new URLSearchParams({mlat: lat, mlon: lon});
    map.hash = `map=15/${lat}/${lon}`;
    link.href = map.href;
    link.hidden = !individual;
  }
}

function show(points, selected, label, individual, trigger, navigate = true) {
  showCoordinates(selected, label, individual);
  pointsShown = points;
  pointShown = selected;
  zoom = 1;
  offsetX = offsetY = 0;
  drawPoints(pointsShown, pointShown);
  mapTrigger = trigger;
  placeMap(trigger);
  routeMap.hidden = false;
  if (navigate) {
    routeMap.focus({preventScroll: true});
    if (compact.matches || !trigger?.closest("tr")) routeMap.scrollIntoView({block: "nearest"});
  }
}

const overviewPoints = new Map();
const selectableRoutes = [];
let routeCount = 0;
for (const group of document.querySelectorAll(".route-points")) {
  const data = group.querySelector(".route-point-data");
  const points = data ? JSON.parse(data.textContent) : Array.from(group.querySelectorAll(".map-point"), (button) => ({
    button,
    lat: Number(button.dataset.lat),
    lon: Number(button.dataset.lon),
    alt: button.dataset.alt,
    label: button.textContent.trim(),
  }));
  if (points.length) {
    const button = group.closest("tr").querySelector(".route-select");
    const route = {points, button, label: button.textContent.trim()};
    selectableRoutes.push(route);
    for (const point of points) {
      point.button?.addEventListener("click", () => selectRoute(route, true, point, point.button));
    }
    button.addEventListener("click", () => selectRoute(route));
  }
  if (points.length) {
    const point = points.find((item) => item.label === "参考中心") || points[0];
    const cell = `${point.lat.toFixed(3)},${point.lon.toFixed(3)}`;
    const existing = overviewPoints.get(cell);
    if (existing) {
      existing.count += 1;
    } else {
      overviewPoints.set(cell, {...point, count: 1});
    }
    routeCount += 1;
  }
}

function showBasemap(visible) {
  if (!amapContainer) return;
  amapContainer.inert = !visible;
  routeMap.classList.toggle("amap-visible", visible);
}

async function loadBasemap() {
  const pending = ++previewId;
  showBasemap(false);
  amapRetry.hidden = true;
  amapStatus.textContent = "正在加载高德地图…";
  try {
    await amap.show(routeSelection.points, () => previewId === pending);
    if (previewId !== pending) return;
    showBasemap(true);
    amapStatus.textContent = "";
  } catch {
    if (previewId !== pending) return;
    amapStatus.textContent = "高德地图暂时无法加载。仍可查看和复制坐标。";
    amapRetry.hidden = false;
  }
}

function clearSelection() {
  previewId += 1;
  showBasemap(false);
  routeSelection?.button.setAttribute("aria-pressed", "false");
  routeSelection = undefined;
  pointChoice.hidden = true;
  if (amap) {
    amapStatus.textContent = "";
    amapRetry.hidden = true;
  }
}

function selectRoute(route, navigate = true, point = route.points[0], trigger = route.button) {
  const changed = routeSelection !== route;
  routeSelection?.button.setAttribute("aria-pressed", "false");
  routeSelection = route;
  route.button.setAttribute("aria-pressed", "true");
  pointChoice.hidden = false;
  pointSelect.replaceChildren(...route.points.map((item, index) => new Option(item.label, index)));
  pointSelect.value = route.points.indexOf(point);
  show(route.points, point, `${route.label} · ${point.label}`, true, trigger, navigate);
  if (amap && changed) loadBasemap();
}

document.querySelector("#route-overview").addEventListener("click", (event) => {
  clearSelection();
  const locations = [...overviewPoints.values()];
  show(locations, locations[0], `本页路线位置 · ${routeCount} 条 · ${locations.length} 组邻近位置`,
       false, event.currentTarget);
});
document.querySelector("#route-map-close").addEventListener("click", () => {
  clearSelection();
  routeMap.hidden = true;
  placeMap();
  mapTrigger?.focus();
});
pointSelect.addEventListener("change", () => {
  const point = routeSelection.points[Number(pointSelect.value)];
  showCoordinates(point, `${routeSelection.label} · ${point.label}`, true);
  pointShown = point;
  drawPoints(pointsShown, pointShown);
});
if (amap && selectableRoutes.length) {
  amapRetry.addEventListener("click", loadBasemap);
  selectRoute(selectableRoutes[0], false);
}
for (const control of document.querySelectorAll("[data-map-zoom]")) {
  control.addEventListener("click", () => {
    const action = control.dataset.mapZoom;
    if (routeMap.classList.contains("amap-visible")) {
      amap.zoom(action);
      return;
    }
    if (action === "reset") {
      zoom = 1;
      offsetX = offsetY = 0;
    } else {
      zoom = Math.min(8, Math.max(0.25, zoom * (action === "in" ? 1.5 : 1 / 1.5)));
    }
    drawPoints(pointsShown, pointShown);
  });
}
canvas.addEventListener("pointerdown", (event) => {
  drag = {x: event.clientX, y: event.clientY};
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener("pointermove", (event) => {
  if (!drag) return;
  offsetX += (event.clientX - drag.x) * canvas.width / canvas.clientWidth;
  offsetY += (event.clientY - drag.y) * canvas.height / canvas.clientHeight;
  drag = {x: event.clientX, y: event.clientY};
  drawPoints(pointsShown, pointShown);
});
canvas.addEventListener("lostpointercapture", () => { drag = null; });
document.querySelector("#copy-coordinates").addEventListener("click", async () => {
  if (!coordinates.value) return;
  coordinates.select();
  if (navigator.clipboard) {
    try { await navigator.clipboard.writeText(coordinates.value); } catch { /* Keep the text selected. */ }
  } else {
    document.execCommand("copy");
  }
});
