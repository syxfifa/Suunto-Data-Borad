const syncProgress = document.querySelector('#sync-progress[data-watch="true"]');

if (syncProgress) {
  let timer;
  let checking = false;
  let watching = true;
  let errors = 0;
  const statuses = {
    ok: "同步完成", partial: "同步部分完成", failed: "同步失败",
    interrupted: "同步已中断", paused: "补采已暂停", yielded: "补采等待继续",
    authorization_required: "请重新授权",
  };

  async function checkProgress() {
    if (!watching || checking || document.hidden) return;
    checking = true;
    try {
      const response = await fetch("/api/sync/progress", { cache: "no-store", signal: AbortSignal.timeout(8000) });
      if (!response.ok) throw new Error("Status request failed");
      const progress = await response.json();
      const run = progress.latest;
      const activityLink = document.querySelector("#oauth-activities");
      if (activityLink) activityLink.hidden = !progress.has_workouts;
      errors = 0;
      watching = progress.authorized && (progress.running || progress.incremental_pending || progress.backfill_active);
      let message;
      if (!progress.authorized) message = "Suunto 需要重新授权。刷新设置后可重新连接。";
      else if (progress.retry_after && watching) message = `Suunto 正在限流，约 ${progress.retry_after} 秒后可重试。`;
      else if (progress.running) {
        const phase = activityLink && !progress.has_workouts && run?.trigger === "oauth" ? "正在首次同步" : "同步中";
        message = progress.source_label ? `${phase} · ${progress.source_label}` : `${phase}，正在准备。`;
      }
      else if (watching) message = "同步已排队。";
      else message = statuses[run?.status] || "当前没有同步任务。";
      if (run && (!watching || run.status === "running")) {
        message += ` 已处理 ${run.received} 条，新增或更新 ${run.changed} 条。`;
      }
      const button = document.querySelector('#sync form[action="/sync"] button');
      if (button) button.disabled = Boolean(progress.retry_after) || !progress.authorized;
      if (syncProgress.textContent.trim() !== message) syncProgress.textContent = message;
    } catch {
      errors += 1;
      if (errors >= 3) {
        watching = false;
        syncProgress.textContent = "暂时无法获取同步进度。刷新本页可重新查看。";
      }
    } finally {
      checking = false;
      if (watching && !document.hidden) timer = setTimeout(checkProgress, errors ? 5000 : 2500);
    }
  }

  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (!document.hidden) checkProgress();
  });
  checkProgress();
}
