const chartTheme = getComputedStyle(document.documentElement);
const themeColor = (name) => chartTheme.getPropertyValue(name).trim();
const colors = ["--chart-cyan", "--chart-rose", "--chart-lilac", "--chart-gold"].map(themeColor);
const axis = themeColor("--muted");
const grid = themeColor("--line");

function auroraFill({chart, dataIndex}, selected) {
  const area = chart.chartArea;
  const top = dataIndex === selected ? colors[1] : colors[0];
  if (!area) return top;
  const fill = chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
  fill.addColorStop(0, top);
  fill.addColorStop(1, "#8e86e5");
  return fill;
}

function renderChart(canvas, series) {
  const datasets = Object.entries(series.columns).map(([label, data], index) => ({
    label,
    data,
    borderColor: colors[index % colors.length],
    backgroundColor: series.aurora ? (context) => auroraFill(context, series.selected) : colors[index % colors.length],
    borderWidth: series.aurora ? 0 : 2,
    borderRadius: series.aurora ? 8 : 3,
    maxBarThickness: 32,
    pointRadius: 2,
  }));
  return new Chart(canvas, {
    type: series.kind,
    data: { labels: series.labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: matchMedia("(prefers-reduced-motion: reduce)").matches ? false :
        series.aurora ? {duration: 420, easing: "easeOutQuart"} : undefined,
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
        x: {stacked: Boolean(series.stacked), grid: {display: false}, ticks: {color: axis, maxTicksLimit: 8, maxRotation: 0}},
        y: {
          beginAtZero: true, stacked: Boolean(series.stacked), grid: {color: grid}, ticks: {color: axis, maxTicksLimit: 5},
          title: {display: Boolean(series.aurora), text: Object.keys(series.columns)[0], color: axis},
          suggestedMax: series.aurora && Object.hasOwn(series.columns, "%") ? 100 : undefined,
        },
      },
    },
  });
}

for (const element of document.querySelectorAll(".chart-data")) {
  const data = JSON.parse(element.textContent);
  const panel = element.parentElement;
  const canvas = panel.querySelector("canvas");
  const selector = panel.querySelector(".chart-metric");
  let chart;

  function show(series) {
    chart?.destroy();
    chart = null;
    const available = Object.values(series.columns).some((values) => values.some(Number.isFinite));
    canvas.parentElement.hidden = !available;
    panel.querySelector(".chart-empty").hidden = available;
    canvas.setAttribute("aria-label", series.title);
    if (available) chart = renderChart(canvas, series);
  }

  if (selector) {
    selector.hidden = false;
    selector.addEventListener("change", () => show(data[Number(selector.value)]));
    show(data[Number(selector.value)]);
    continue;
  }
  const disclosure = element.closest("details");
  if (disclosure && !disclosure.open) {
    disclosure.addEventListener("toggle", function onOpen() {
      if (!disclosure.open) return;
      show(data);
      disclosure.removeEventListener("toggle", onOpen);
    });
  } else {
    show(data);
  }
}
