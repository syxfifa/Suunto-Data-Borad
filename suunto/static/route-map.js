const routeMap = document.querySelector("#route-map");
const routeFrame = routeMap.querySelector("iframe");

for (const button of document.querySelectorAll(".map-point")) {
  button.addEventListener("click", () => {
    const lat = Number(button.dataset.lat);
    const lon = Number(button.dataset.lon);
    const margin = 0.012;
    const bbox = [
      Math.max(-180, lon - margin), Math.max(-85.0511, lat - margin),
      Math.min(180, lon + margin), Math.min(85.0511, lat + margin),
    ];
    const map = new URL("https://www.openstreetmap.org/export/embed.html");
    map.search = new URLSearchParams({bbox: bbox.join(","), layer: "mapnik", marker: [lat, lon].join(",")});
    routeFrame.src = map.href;
    routeMap.hidden = false;
    routeMap.scrollIntoView({block: "start"});
  });
}
