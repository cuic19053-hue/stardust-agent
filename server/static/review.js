/* Version-bound local draft review. Results are private and rendered as text. */
(() => {
  "use strict";
  let host = null, generation = 0, timer = null, submitting = false;
  let mountedProject = null;
  const terminal = new Set(["completed", "failed", "needs_review"]);
  const labels = { pending: "等待检查服务", running: "检查中", completed: "检查完成",
    failed: "检查未完成", needs_review: "调用结果待核对" };

  function current(projectId, revision) {
    return revision === generation && host?.state.project?.id === projectId;
  }

  function text(tag, value) {
    const element = document.createElement(tag);
    element.textContent = value;
    return element;
  }

  function render(items, fingerprint) {
    const box = document.querySelector("#draftReviewResults");
    box.replaceChildren();
    for (const run of items) {
      const details = document.createElement("details");
      details.open = items[0]?.id === run.id;
      const stale = fingerprint && fingerprint !== run.fingerprint;
      const title = `${labels[run.status] || "状态待确认"} · 草稿 ${run.fingerprint.slice(0, 8)}${stale ? "（历史版本）" : ""}`;
      details.append(text("summary", title));
      if (run.error) details.append(text("p", run.error));
      if (run.billing) {
        const billing = run.billing;
        const points = value => (Number(value || 0) / 100000).toFixed(5).replace(/\.?0+$/, "");
        if (billing.status === "settled") details.append(text("p", `本次 AI 预审费用：${points(billing.actual_micro_cny)} 积分（已结算）。`));
        if (billing.status === "reserved") details.append(text("p", `本次已预留 ${points(billing.reserved_micro_cny)} 积分，用量确认后结算；不会自动重复调用。`));
        if (billing.status === "refunded") details.append(text("p", "本次模型调用未完成，预留积分已退回。"));
        if (billing.status === "needs_review") details.append(text("p", "本次用量账单待核对，请查看积分记录。"));
      }
      const report = run.report;
      if (report) {
        details.append(text("p", report.notice));
        if (!report.complete) details.append(text("p", "AI 检查未完成，以下仅含已完成的检查结果。"));
        for (const warning of report.warnings || []) details.append(text("p", warning));
        const list = document.createElement("ol");
        for (const issue of report.issues || []) {
          const item = document.createElement("li");
          const section = report.section_titles?.[issue.section_id] || "项目整体";
          item.append(text("strong", `${section}：${issue.problem}`));
          item.append(text("p", issue.suggestion));
          if (issue.draft_quote) item.append(text("blockquote", `草稿原文：${issue.draft_quote}`));
          if (issue.origin === "model") item.append(text("small", "模型意见待核对；来源匹配不代表结论已验证。"));
          for (const source of issue.citations || []) {
            item.append(text("blockquote", `${source.source_name} · ${source.source_ref}：${source.quote}`));
          }
          list.append(item);
        }
        details.append(list);
        if (!report.issues?.length) details.append(text("p", "本次未发现可报告的问题，仍请按学校要求人工核对。"));
      }
      if (terminal.has(run.status)) {
        const retry = text("button", "重新检查当前草稿");
        retry.className = "button button-secondary button-small";
        retry.onclick = () => {
          try { sessionStorage.removeItem(`draft-review-request:${host.state.project.id}`); } catch {}
          return submit(host.state.project.id, generation);
        };
        details.append(retry);
      }
      box.append(details);
    }
  }

  async function refresh(projectId, revision) {
    const result = await host.api(`/api/projects/${projectId}/reviews`);
    if (!current(projectId, revision)) return;
    render(result.items, result.current_fingerprint);
    const button = document.querySelector("#draftReviewBtn");
    button.disabled = submitting || !result.current_fingerprint;
    document.querySelector("#draftReviewHint").textContent = result.unavailable_reason ||
      "检查时保存当前正文快照。结果用于修改参考，不会替换正文。";
    const active = result.items.some(run => !terminal.has(run.status));
    const settling = result.items.some(run => run.status === "completed" && run.billing?.status === "reserved");
    if (active || settling) {
      clearTimeout(timer);
      timer = setTimeout(() => { void refresh(projectId, revision).catch(() => {}); }, active ? 2000 : 10000);
    }
    return result;
  }

  async function submit(projectId, revision) {
    if (submitting || !current(projectId, revision)) return;
    submitting = true;
    document.querySelector("#draftReviewBtn").disabled = true;
    clearTimeout(timer);
    try {
      await host.flushEdits();
      if (!current(projectId, revision)) return;
      const inputs = await host.api(`/api/projects/${projectId}/reviews`);
      if (!current(projectId, revision)) return;
      if (!inputs.current_fingerprint) throw new Error(inputs.unavailable_reason || "请先保存草稿");
      const mode = document.querySelector("#draftReviewModel").checked ? "model" : "rules";
      // Retain an uncertain POST's exact key/input, even across a page reload.
      const storageKey = `draft-review-request:${projectId}`;
      let pending;
      try { pending = JSON.parse(sessionStorage.getItem(storageKey) || "null"); } catch {}
      if (!pending || pending.fingerprint !== inputs.current_fingerprint || pending.mode !== mode) {
        pending = { idempotency_key: crypto.randomUUID(), fingerprint: inputs.current_fingerprint, mode };
      }
      try { sessionStorage.setItem(storageKey, JSON.stringify(pending)); } catch {}
      const run = await host.api(`/api/projects/${projectId}/reviews`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pending),
      });
      if (!current(projectId, revision)) return;
      // Keep the key until this input changes; double-clicks cannot enqueue duplicates.
      host.toast(`已提交草稿 ${run.fingerprint.slice(0, 8)} 的检查`);
    } catch (error) {
      if (current(projectId, revision)) host.toast(error.message, true);
    } finally {
      submitting = false;
      if (current(projectId, revision)) {
        await refresh(projectId, revision).catch(() => {
          document.querySelector("#draftReviewBtn").disabled = false;
        });
      }
    }
  }

  async function mount(config) {
    host = config;
    const revision = ++generation;
    const projectId = host.state.project?.id;
    if (mountedProject !== projectId) document.querySelector("#draftReviewModel").checked = false;
    mountedProject = projectId;
    clearTimeout(timer);
    const panel = document.querySelector("#draftReviewPanel");
    panel.classList.add("hidden");
    document.querySelector("#draftReviewResults").replaceChildren();
    if (!projectId || (host.state.project.state?.skill_id || "innovation-research") !== "innovation-research") return;
    try {
      const capabilities = await host.api("/api/reviews/capabilities");
      if (!current(projectId, revision) || !capabilities.enabled) return;
      const model = document.querySelector("#draftReviewModel");
      model.disabled = capabilities.ai_available === false;
      if (model.disabled) model.checked = false;
      document.querySelector("#draftReviewModelLabel").textContent = capabilities.ai_label || "同时检查论证与事实依据（使用本地模型）";
      panel.classList.remove("hidden");
      document.querySelector("#draftReviewBtn").onclick = () => submit(projectId, revision);
      await refresh(projectId, revision);
    } catch (error) {
      if (current(projectId, revision)) panel.classList.add("hidden");
    }
  }

  window.DraftReview = { mount };
})();
