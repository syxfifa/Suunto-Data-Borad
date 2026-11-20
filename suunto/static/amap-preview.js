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
            const error = new Error("Coordinate conversion failed");
            error.providerCode = String(result?.infocode || result?.info || "");
            reject(error);
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

  async function show(points, current, stage = () => {}) {
    stage("sdk");
    const api = await loadSdk();
    if (!current()) return;
    stage("conversion");
    const locations = await positions(api, points);
    if (!current()) return;
    stage("tiles");
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

const checkMap = document.querySelector("#amap-check-map");
if (checkMap) {
  const status = document.querySelector("#amap-check-status");
  const setStage = (stage) => {
    checkMap.dataset.stage = stage;
    status.textContent = {
      sdk: "正在加载高德 JS API…", conversion: "正在测试公开坐标转换…",
      tiles: "坐标转换成功，正在加载底图…", ready: "测试通过，坐标转换和底图均已加载。",
    }[stage];
  };
  // Use a public landmark, independently of any route on this device.
  createAmapPreview(checkMap, checkMap.dataset.key, "gps").show(
    [{lon: 116.3975, lat: 39.9087, label: "测试点"}], () => true, setStage,
  ).then(() => setStage("ready")).catch((error) => {
    const messages = {
      sdk: "高德 JS API 未加载。请检查 Web 端 Key、域名白名单和浏览器网络。",
      conversion: "坐标转换失败。请检查安全密钥或稍后重试。",
      tiles: "坐标转换成功，底图加载失败。请检查浏览器网络或稍后重试。",
    };
    const knownErrors = {
      "10001": "高德拒绝了 Key，请核对已保存的 Web 端 Key。",
      "10006": "高德拒绝了当前域名，请检查域名白名单。",
      "10009": "高德报告 Key 类型不匹配，请核对是否使用 Web 端（JS API）Key。",
    };
    const namedCodes = {INVALID_USER_KEY: "10001", INVALID_USER_DOMAIN: "10006", USERKEY_PLAT_NOMATCH: "10009"};
    const code = Object.hasOwn(namedCodes, error.providerCode) ? namedCodes[error.providerCode] : error.providerCode;
    status.textContent = Object.hasOwn(knownErrors, code) ? knownErrors[code] : messages[checkMap.dataset.stage];
    checkMap.dataset.stage = "failed";
  });
}
