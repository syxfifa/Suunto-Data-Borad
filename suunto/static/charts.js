const colors = ["#14707c", "#b24c27", "#5e46a0", "#37722c"];

for (const element of document.querySelectorAll(".chart-data")) {
  const series = JSON.parse(element.textContent);
  const datasets = Object.entries(series.columns).map(([label, data], index) => ({
    label,
    data,
    borderColor: colors[index % colors.length],
    backgroundColor: colors[index % colors.length],
    borderWidth: 2,
    borderRadius: 3,
  }));
  const canvas = element.parentElement.querySelector("canvas");
  new Chart(canvas, {
    type: series.kind,
    data: { labels: series.labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: datasets.length > 1 } },
      scales: { y: { beginAtZero: true } },
    },
  });
}
