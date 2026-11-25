for (const form of document.querySelectorAll("[data-report-date-range]")) {
  const { start, end } = form.elements;
  const error = form.querySelector(".report-date-error");
  form.addEventListener("submit", event => {
    if (start.type !== "date" || end.type !== "date" || start.value <= end.value) return;
    event.preventDefault();
    error.textContent = "结束日期不能早于开始日期。";
    error.hidden = false;
    end.setAttribute("aria-invalid", "true");
    end.setAttribute("aria-describedby", error.id);
    end.focus();
  });
  form.addEventListener("input", event => {
    if (event.target !== start && event.target !== end) return;
    error.hidden = true;
    for (const input of [start, end]) {
      input.removeAttribute("aria-invalid");
      input.removeAttribute("aria-describedby");
    }
  });
}
