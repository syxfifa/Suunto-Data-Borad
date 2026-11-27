(() => {
  const controls = document.querySelector("#training-view-preference");
  const current = JSON.parse(document.querySelector("#training-view-data").textContent);
  const save = document.querySelector("#remember-training-view");
  const clear = document.querySelector("#forget-training-view");
  const status = controls.querySelector("[role=status]");
  const key = "suunto.trainingView.v1";

  try {
    const storage = window.localStorage;
    const remembered = JSON.parse(storage.getItem(key) || "null");
    if (remembered && !location.search && !location.hash) {
      const destination = new URL("/training", location.origin);
      for (const name of ["sport", "period", "report", "days", "start", "end"]) {
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
