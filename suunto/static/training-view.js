(() => {
  const controls = document.querySelector("[data-view-scope]");
  const current = JSON.parse(controls.dataset.viewOptions);
  const save = controls.querySelector("[data-save-view]");
  const clear = controls.querySelector("[data-clear-view]");
  const status = controls.querySelector("[role=status]");
  const activity = controls.dataset.viewScope === "activity";
  const key = activity ? "suunto.activityView.v1" : "suunto.trainingView.v1";
  const allowed = activity ? ["sport", "q", "km", "minutes", "tss", "sort", "order", "start", "end", "range"]
    : ["sport", "period", "report", "days", "start", "end"];

  try {
    const storage = window.localStorage;
    const remembered = JSON.parse(storage.getItem(key) || "null");
    if (remembered && !location.search && !location.hash) {
      const destination = new URL(location.pathname, location.origin);
      for (const name of allowed) {
        if (typeof remembered[name] === "string") destination.searchParams.set(name, remembered[name]);
      }
      if (destination.search) {
        location.replace(destination);
        return;
      }
    }
    controls.hidden = false;
    clear.hidden = !remembered;

    save.addEventListener("click", () => {
      try {
        storage.setItem(key, JSON.stringify(current));
        clear.hidden = false;
        status.textContent = "已在此浏览器记住当前视图。";
      } catch {
        status.textContent = "此浏览器无法保存视图。";
      }
    });
    clear.addEventListener("click", () => {
      try {
        storage.removeItem(key);
        clear.hidden = true;
        status.textContent = "已清除记住的视图。";
        save.focus();
      } catch {
        status.textContent = "此浏览器无法清除视图。";
      }
    });
  } catch {
    // Preferences are optional when storage is unavailable.
  }
})();
