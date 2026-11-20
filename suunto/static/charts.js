const colors = ["#15646a", "#a24928", "#654b8b", "#49713b"];
const axis = "#4a6265";
const grid = "#d5e0de";

function renderChart(canvas, series) {
  const datasets = Object.entries(series.columns).map(([label, data], index) => ({
    label,
    data,
    borderColor: colors[index % colors.length],
    backgroundColor: colors[index % colors.length],
    borderWidth: 2,
    borderRadius: 3,
    pointRadius: 2,
  }));
  new Chart(canvas, {
    type: series.kind,
    data: { labels: series.labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: matchMedia("(prefers-reduced-motion: reduce)").matches ? false : undefined,
      color: axis,
      onClick: (_, bars) => {
        const range = series.ranges?.[bars[0]?.index];
        if (range) {
          const params = new URLSearchParams({start: range[0], end: range[1], sport: series.sport || ""});
          location.assign("/activities?" + params);
        }
      },
      plugins: {legend: {display: datasets.length > 1, labels: {boxWidth: 12}}},
      scales: {
        x: {stacked: Boolean(series.stacked), grid: {display: false}, ticks: {maxTicksLimit: 8, maxRotation: 0}},
        y: {beginAtZero: true, stacked: Boolean(series.stacked), grid: {color: grid}, ticks: {maxTicksLimit: 5}},
      },
    },
  });
}

for (const element of document.querySelectorAll(".chart-data")) {
  const series = JSON.parse(element.textContent);
  const panel = element.parentElement;
  const canvas = panel.querySelector("canvas");
  if (!Object.values(series.columns).some((values) => values.some(Number.isFinite))) {
    canvas.parentElement.hidden = true;
    panel.querySelector(".chart-empty").hidden = false;
    continue;
  }
  const disclosure = element.closest("details");
  if (disclosure && !disclosure.open) {
    disclosure.addEventListener("toggle", function onOpen() {
      if (!disclosure.open) return;
      renderChart(canvas, series);
      disclosure.removeEventListener("toggle", onOpen);
    });
  } else {
    renderChart(canvas, series);
  }
}
