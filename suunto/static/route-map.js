const routeMap = document.querySelector("#route-map");
const canvas = routeMap.querySelector("canvas");
const ctx = canvas.getContext("2d");
const pointStatus = document.querySelector("#route-point");
const link = document.querySelector("#route-map-link");
const coordinates = document.querySelector("#route-coordinates");
let pointsShown = [];
let pointShown;
let zoom = 1;
let offsetX = 0;
let offsetY = 0;
let drag = null;

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
  ctx.fillStyle = "#edf5f4";
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = "#d1e1df";
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
  ctx.fillStyle = "#124b55";
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
    ctx.fillStyle = index === activeIndex ? "#b33922" : "#124b55";
    ctx.fill();
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 2;
    ctx.stroke();
  }
  ctx.lineWidth = 1;
}

function show(points, selected, label, individual) {
  const {lat, lon, alt} = selected;
  coordinates.value = individual ? `${lat.toFixed(6)}, ${lon.toFixed(6)}` : "";
  pointStatus.textContent = [label, coordinates.value, individual && alt !== "" ? alt + " m" : ""]
    .filter(Boolean).join(" · ");
  if (link) {
    const map = new URL("https://www.openstreetmap.org/");
    map.search = new URLSearchParams({mlat: lat, mlon: lon});
    map.hash = `map=15/${lat}/${lon}`;
    link.href = map.href;
    link.hidden = !individual;
  }
  pointsShown = points;
  pointShown = selected;
  zoom = 1;
  offsetX = offsetY = 0;
  drawPoints(pointsShown, pointShown);
  routeMap.hidden = false;
  routeMap.scrollIntoView({block: "start"});
}

const overviewPoints = new Map();
let routeCount = 0;
for (const group of document.querySelectorAll(".route-points")) {
  const points = Array.from(group.querySelectorAll(".map-point"), (button) => ({
    button,
    lat: Number(button.dataset.lat),
    lon: Number(button.dataset.lon),
    alt: button.dataset.alt,
    label: button.textContent.trim(),
  }));
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
  for (const point of points) {
    point.button.addEventListener("click", () => {
      show(points, point, [group.dataset.route, point.label].filter(Boolean).join(" · "), true);
    });
  }
}
document.querySelector("#route-overview").addEventListener("click", () => {
  const locations = [...overviewPoints.values()];
  show(locations, locations[0], `路线库位置 · ${routeCount} 条 · ${locations.length} 组邻近位置`, false);
});
for (const control of document.querySelectorAll("[data-map-zoom]")) {
  control.addEventListener("click", () => {
    const action = control.dataset.mapZoom;
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
