const routeMap = document.querySelector("#route-map");
const canvas = routeMap.querySelector("canvas");
const ctx = canvas.getContext("2d");
const pointStatus = document.querySelector("#route-point");
const link = document.querySelector("#route-map-link");

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
  const scale = Math.min((width - 88) / Math.max(maxEast - minEast, 0.00001),
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
    const x = width / 2 + (p.east - (minEast + maxEast) / 2) * scale;
    const y = height / 2 - (p.north - (minNorth + maxNorth) / 2) * scale;
    ctx.beginPath();
    ctx.arc(x, y, index === activeIndex ? 10 : 7, 0, Math.PI * 2);
    ctx.fillStyle = index === activeIndex ? "#b33922" : "#124b55";
    ctx.fill();
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 2;
    ctx.stroke();
  }
  ctx.lineWidth = 1;
}

for (const group of document.querySelectorAll(".route-points")) {
  const points = Array.from(group.querySelectorAll(".map-point"), (button) => ({
    button,
    lat: Number(button.dataset.lat),
    lon: Number(button.dataset.lon),
    label: button.textContent.trim(),
  }));
  for (const point of points) {
    point.button.addEventListener("click", () => {
      const {lat, lon, label} = point;
      pointStatus.textContent = [group.dataset.route, label, `${lat.toFixed(6)}, ${lon.toFixed(6)}`]
        .filter(Boolean).join(" · ");
      const map = new URL("https://www.openstreetmap.org/");
      map.search = new URLSearchParams({mlat: lat, mlon: lon});
      map.hash = `map=15/${lat}/${lon}`;
      link.href = map.href;
      drawPoints(points, point);
      routeMap.hidden = false;
      routeMap.scrollIntoView({block: "start"});
    });
  }
}
