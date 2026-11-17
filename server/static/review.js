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
      if (run.parent_id) details.append(text("p", `接续检查 ${run.parent_id.slice(0, 8)}；旧报告及其材料快照已保留。`));
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
            if (source.support_status) item.append(text("small", "引文原文已匹配；它是否支持该判断仍需核对。"));
          }
          list.append(item);
        }
        details.append(list);
        if (report.unverified_hypotheses?.length) {
          const pending = document.createElement("details");
          pending.append(text("summary", `待人工判断的 AI 线索（${report.unverified_hypotheses.length} 条，未计为已发现问题）`));
          for (const hypothesis of report.unverified_hypotheses) {
            pending.append(text("p", `尚未验证的模型推测：${hypothesis.problem}`));
            pending.append(text("blockquote", `对应草稿：${hypothesis.draft_quote}`));
            for (const source of hypothesis.citations || []) {
              pending.append(text("blockquote", `原文匹配，支持关系未验证：${source.source_name} · ${source.source_ref}：${source.quote}`));
            }
          }
          details.append(pending);
        }
        if (report.follow_up?.length) {
          details.append(text("p", "待你补充或修改后继续："));
          for (const followUp of report.follow_up) {
            const row = document.createElement("p");
            row.append(text("span", followUp.request));
            if (followUp.section_id && host.focusSection) {
              const edit = text("button", "修改该章");
              edit.className = "button button-secondary button-small";
              edit.onclick = () => host.focusSection(followUp.section_id);
              row.append(edit);
            }
            const material = text("button", "补充并确认资料");
            material.className = "button button-secondary button-small";
            material.onclick = () => host.focusMaterials?.();
            row.append(material);
            details.append(row);
          }
        }
        if (!report.issues?.length) details.append(text("p", "本次未发现可报告的问题，仍请按学校要求人工核对。"));
      }
      if (terminal.has(run.status)) {
        const continuation = run.status === "completed" && (report?.follow_up?.length || report?.unverified_hypotheses?.length);
        const retry = text("button", continuation ? "已补充，检查新版本" : "重新检查当前草稿");
        retry.className = "button button-secondary button-small";
        retry.onclick = () => {
          try { sessionStorage.removeItem(`draft-review-request:${host.state.project.id}`); } catch {}
          return submit(host.state.project.id, generation, continuation ? run.id : null);
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

  async function submit(projectId, revision, parentId = null) {
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
      if (!pending || pending.fingerprint !== inputs.current_fingerprint || pending.mode !== mode || (pending.parent_id || null) !== parentId) {
        pending = { idempotency_key: crypto.randomUUID(), fingerprint: inputs.current_fingerprint, mode, parent_id: parentId };
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
