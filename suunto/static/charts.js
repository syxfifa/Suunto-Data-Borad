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

const coverageLabels = ["已确认无活动", "同步未确认完整", "活动缺少指标"];
const coverageColors = [axis, colors[1], colors[3]];

function seriesStates(series) {
  if (!series.covered) return [];
  const columns = Object.values(series.columns);
  return series.covered.map((covered, index) => [
    covered && !series.counts[index],
    !covered,
    series.missing?.[index] ?? (Boolean(series.counts[index]) && columns.some(values => values[index] == null)),
  ]);
}

function coverageMarkers(series) {
  const states = seriesStates(series);
  return {
    id: "suuntoCoverage",
    afterDatasetsDraw({ctx, scales, chartArea}) {
      if (!states.length) return;
      const space = chartArea.width / states.length;
      ctx.save();
      states.forEach((flags, index) => {
        const x = scales.x.getPixelForValue(index);
        flags.forEach((flag, kind) => {
          if (!flag) return;
          ctx.fillStyle = coverageColors[kind];
          ctx.fillRect(x - space * .4, chartArea.bottom - 3 * (kind + 1), Math.max(1, space * .8), 2);
        });
      });
      ctx.restore();
    },
  };
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
    plugins: series.covered ? [coverageMarkers(series)] : [],
    data: { labels: series.labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: matchMedia("(prefers-reduced-motion: reduce)").matches ? false :
        series.aurora ? {duration: 420, easing: "easeOutQuart"} : undefined,
      color: axis,
      onClick: (_, bars) => {
        const link = series.links?.[bars[0]?.index];
        if (link) location.assign(link);
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

function drawValues(container, series) {
  const table = document.createElement("table");
  const header = table.createTHead().insertRow();
  const fields = Object.entries(series.columns);
  const heading = (row, label, scope) => {
    const cell = document.createElement("th");
    cell.scope = scope;
    cell.textContent = label;
    row.append(cell);
    return cell;
  };
  container.setAttribute("aria-label", series.title);
  for (const label of ["日期或类别", ...fields.map(([name]) => name)]) heading(header, label, "col");
  if (series.covered) heading(header, "数据状态", "col");
  const rows = table.createTBody();
  for (const [index, label] of series.labels.entries()) {
    const row = rows.insertRow();
    const name = heading(row, label, "row");
    const href = series.links?.[index];
    if (href) {
      const link = document.createElement("a");
      link.href = href;
      link.textContent = label;
      name.replaceChildren(link);
    }
    let missing = false;
    for (const [, values] of fields) {
      const value = values[index];
      missing ||= value == null;
      row.insertCell().textContent = value ?? "—";
    }
    if (series.covered) {
      const status = [];
      if (!series.covered[index]) status.push(href ? "同步未确认完整" : "日期未到");
      if (series.covered[index] && !series.counts[index]) status.push("已确认无活动");
      if (series.missing?.[index] ?? (series.counts[index] && missing)) status.push("部分活动缺少指标");
      if (!status.length) status.push("已确认覆盖");
      row.insertCell().textContent = status.join(" · ");
    }
  }
  container.replaceChildren(table);
}

for (const element of document.querySelectorAll(".chart-data")) {
  const data = JSON.parse(element.textContent);
  const panel = element.parentElement;
  const canvas = panel.querySelector("canvas");
  const selector = panel.querySelector(".chart-metric");
  const values = panel.querySelector(".chart-values");
  const tableRegion = values.querySelector(".table-scroll");
  let visibleSeries = selector ? data[Number(selector.value)] : data;
  let chart;

  values.hidden = false;
  values.addEventListener("toggle", () => {
    if (values.open) drawValues(tableRegion, visibleSeries);
  });

  function show(series) {
    visibleSeries = series;
    if (values.open) drawValues(tableRegion, series);
    chart?.destroy();
    chart = null;
    const states = seriesStates(series);
    const legend = panel.querySelector(".chart-coverage");
    if (legend) {
      legend.replaceChildren();
      coverageLabels.forEach((label, kind) => {
        const count = states.filter(flags => flags[kind]).length;
        if (!count) return;
        const item = document.createElement("li");
        item.style.setProperty("--coverage-color", coverageColors[kind]);
        item.textContent = `${label} ${count} 段`;
        legend.append(item);
      });
      legend.hidden = !legend.childElementCount;
    }
    const empty = panel.querySelector(".chart-empty");
    const failure = panel.querySelector(".chart-failure");
    const hasData = Object.values(series.columns).some(column => column.some(Number.isFinite));
    canvas.parentElement.hidden = true;
    canvas.setAttribute("aria-label", series.title);
    empty.hidden = hasData;
    failure.hidden = true;
    if (!hasData) return;
    if (typeof Chart === "undefined") {
      failure.hidden = false;
      values.open = true;
      return;
    }
    canvas.parentElement.hidden = false;
    try {
      chart = renderChart(canvas, series);
    } catch (error) {
      canvas.parentElement.hidden = true;
      failure.hidden = false;
      values.open = true;
      console.error("Could not render Suunto chart", error);
    }
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
