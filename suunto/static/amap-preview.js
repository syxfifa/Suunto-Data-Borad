function createAmapPreview(container, key, coordinateSystem) {
  const converted = new Map();
  let sdk;
  let map;
  let mapReady;
  let markers = [];
  let fit;
  let attempt = 0;

  function loadSdk() {
    if (sdk) return sdk;
    window._AMapSecurityConfig = {serviceHost: `${location.origin}/_AMapService`};
    sdk = new Promise((resolve, reject) => {
      const callback = `suuntoAmapReady${++attempt}`;
      const script = document.createElement("script");
      const timeout = setTimeout(() => finish(), 8000);
      function finish(api) {
        clearTimeout(timeout);
        delete window[callback];
        if (api?.Map && api.Marker) {
          resolve(api);
        } else {
          script.remove();
          reject(new Error("AMap SDK unavailable"));
        }
      }
      window[callback] = () => finish(window.AMap);
      script.onerror = () => finish();
      script.referrerPolicy = "origin";
      script.src = `https://webapi.amap.com/maps?${new URLSearchParams({v: "2.0", key, callback})}`;
      document.head.append(script);
    }).catch((error) => {
      sdk = undefined;
      throw error;
    });
    return sdk;
  }

  function positions(api, points) {
    const coordinates = points.map(({lon, lat}) => [lon, lat]);
    if (coordinateSystem !== "gps") return coordinates;
    const id = JSON.stringify(coordinates);
    if (!converted.has(id)) {
      const pending = new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("Coordinate conversion timed out")), 6000);
        api.convertFrom(coordinates, "gps", (status, result) => {
          clearTimeout(timeout);
          if (status === "complete" && result?.locations?.length === points.length) {
            resolve(result.locations);
          } else {
            reject(new Error("Coordinate conversion failed"));
          }
        });
      }).catch((error) => {
        converted.delete(id);
        throw error;
      });
      converted.set(id, pending);
    }
    return converted.get(id);
  }

  async function show(points, current) {
    const api = await loadSdk();
    if (!current()) return;
    const locations = await positions(api, points);
    if (!current()) return;
    if (!map) {
      map = new api.Map(container, {viewMode: "2D", zoom: 15, center: locations[0], scrollWheel: false});
      mapReady = new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
          map.destroy();
          map = undefined;
          markers = [];
          reject(new Error("AMap tiles unavailable"));
        }, 8000);
        map.on("complete", () => {
          clearTimeout(timeout);
          resolve();
        });
      });
    }
    await mapReady;
    if (!current()) return;
    if (markers.length) map.remove(markers);
    markers = points.map((point, index) => new api.Marker({
      position: locations[index], title: point.label, label: {content: point.label, direction: "top"},
    }));
    map.add(markers);
    const multipleLocations = points.some(({lat, lon}) => lat !== points[0].lat || lon !== points[0].lon);
    fit = multipleLocations ? () => map.setFitView(markers, true, [38, 36, 50, 36], 17)
                            : () => map.setZoomAndCenter(15, locations[0]);
    fit();
  }

  function zoom(action) {
    if (action === "reset") fit();
    else if (action === "in") map.zoomIn();
    else map.zoomOut();
  }

  return {show, zoom};
}
