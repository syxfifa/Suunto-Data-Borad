const activityForm = document.querySelector(".activity-filters");
for (const picker of activityForm.querySelectorAll("[data-date-field]")) {
  const input = activityForm.elements.namedItem(picker.dataset.dateField);
  const syncPicker = () => { picker.value = input.value.trim().replaceAll("/", "-"); };
  syncPicker();
  input.addEventListener("input", syncPicker);
  picker.addEventListener("change", () => { input.value = picker.value; });
  picker.parentElement.hidden = false;
}

const selection = document.querySelector("#comparison-selection");
if (selection) {
  const storageKey = "suunto.activity-comparison";
  const inputs = [...document.querySelectorAll("[data-compare-id]")];
  const status = selection.querySelector("[role=status]");
  const picks = selection.querySelector(".comparison-picks");
  const link = selection.querySelector("a");
  let selected = [];
  function restore() {
    try {
      const stored = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
      if (Array.isArray(stored)) selected = stored.filter(item => item &&
        ["id", "sport", "date"].every(key => typeof item[key] === "string")).slice(0, 2).map(item => {
        const input = inputs.find(input => input.dataset.compareId === item.id);
        return input ? {...item, name: input.dataset.activityName, distance: input.dataset.distance} : item;
      });
    } catch { /* The current page still supports comparison when storage is unavailable. */ }
  }

  function render(message) {
    selection.hidden = !inputs.length && !selected.length;
    try { sessionStorage.setItem(storageKey, JSON.stringify(selected)); } catch { /* Keep the in-memory selection. */ }
    status.textContent = message || (selected.length
      ? `已选 ${selected.length} / 2 条 · ${selected[0].sport}`
      : "请选择两条同类型活动。");
    picks.replaceChildren();
    for (const [index, item] of selected.entries()) {
      const letter = index === 0 ? "A" : "B";
      const details = document.createElement("div");
      details.className = "comparison-pick";
      const name = document.createElement("strong");
      name.textContent = `${letter} · ${item.name || item.sport}`;
      const meta = document.createElement("small");
      meta.textContent = `${item.date} · ${item.distance ? item.distance + " km" : "距离未记录"}`;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = `移除 ${letter}`;
      button.setAttribute("aria-label", `移除 ${letter} ${item.name || item.sport}`);
      button.addEventListener("click", () => {
        selected = selected.filter(pick => pick.id !== item.id);
        render();
        (picks.querySelector("button") || inputs[0])?.focus();
      });
      details.append(name, meta, button);
      picks.append(details);
    }
    for (const input of inputs) input.checked = selected.some(item => item.id === input.dataset.compareId);
    link.hidden = selected.length !== 2;
    if (selected.length === 2) {
      const row = inputs.find(input => input.checked)?.closest("tr");
      const back = location.pathname + location.search + "#" + (row?.id || "activity-results");
      const query = new URLSearchParams({sport: selected[0].sport, first: selected[0].id,
        second: selected[1].id, return_to: back});
      link.href = "/compare?" + query;
    }
  }

  for (const input of inputs) {
    input.parentElement.hidden = false;
    input.addEventListener("change", () => {
      const {compareId: id, sport, date, activityName: name, distance} = input.dataset;
      if (!input.checked) selected = selected.filter(item => item.id !== id);
      else if (selected.some(item => item.sport !== sport)) return render("请选择与已选活动相同的运动类型。");
      else if (selected.length === 2) return render("已选两条活动。请先移除一条。");
      else selected.push({id, sport, date, name, distance});
      render();
    });
  }
  window.addEventListener("pageshow", event => {
    if (event.persisted) {
      restore();
      render();
    }
  });
  restore();
  render();
}
