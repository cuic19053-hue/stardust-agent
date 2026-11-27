/* Material UI: all user/project text uses textContent, never injected HTML. */
(() => {
  "use strict";
  let host, data, root, projectId, urls = [], fillPlan, artifactEpoch = 0;
  const pendingJobs = new Map();
  let recoveryOwner = () => "local", recoveryStorage;
  const jobKey = (id, kind, target) => `${id}\0${kind}\0${target}`;

  function configureRecovery(owner) {
    recoveryOwner = owner;
    recoveryStorage = window.DraftRecovery.create("stardust-material-job-v1:", recoveryOwner);
  }

  function pendingJob(id, kind, target) {
    const owner = recoveryOwner();
    if (!owner) return null;
    const key = jobKey(id, kind, target);
    const inMemory = pendingJobs.get(owner + "\0" + key);
    if (inMemory) return inMemory;
    const saved = recoveryStorage?.read(key);
    const identifier = saved?.jobId ?? saved?.requestId;
    return typeof identifier === "string" && /^[a-f0-9]{32}$/.test(identifier) ? saved : null;
  }

  function clearRecovery(id) {
    const prefix = id ? `${id}\0` : "";
    for (const key of pendingJobs.keys()) {
      if (key.slice(key.indexOf("\0") + 1).startsWith(prefix)) pendingJobs.delete(key);
    }
    recoveryStorage?.clear(prefix);
  }

  function trackedAction(id, kind, target, isCurrent) {
    const owner = recoveryOwner();
    const key = jobKey(id, kind, target);
    const memoryKey = owner + "\0" + key;
    const current = () => isCurrent() && owner === recoveryOwner();
    function record(value) {
      if (!owner || !current()) return;
      if (value) pendingJobs.set(memoryKey, value);
      else pendingJobs.delete(memoryKey);
      recoveryStorage?.write(key, value);
    }
    return { id, kind, target, current, record, read: () => pendingJob(id, kind, target) };
  }

  const el = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  const configured = () => Boolean(host?.state.project?.material?.enabled);
  // Do not route generation away from the existing chain until the operator
  // has explicitly declared the Worker ready.
  const active = () => Boolean(configured() && projectId === host.state.project.id && data?.worker_ready);
  const path = suffix => `/api/projects/${encodeURIComponent(host.state.project.id)}/material${suffix}`;
  const request = (suffix, method = "GET", body) => host.api(path(suffix), {
    method, ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  async function waitFor(promise, timeoutMs, message) {
    // A timeout only ends observation; the server may still finish the task.
    let timer;
    try {
      return await Promise.race([promise, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(message)), timeoutMs);
      })]);
    } finally { clearTimeout(timer); }
  }
  const JOB_STATUS_NOTICE = "暂时无法获取任务状态，任务可能仍在后台运行；请稍后刷新任务列表，不要重复生成。";
  const JOB_SUBMIT_NOTICE = "暂未收到任务提交结果，任务可能已经开始；请先刷新任务列表及费用记录，不要重复提交。";
  function action(text, fn, parent) {
    const button = el("button", text, "button button-small button-secondary");
    button.type = "button";
    button.onclick = async () => {
      button.disabled = true;
      try { await fn(); } catch (e) { host.toast(e.message, true); }
      finally { button.disabled = false; }
    };
    parent.append(button);
    return button;
  }
  function input(parent, label, value = "", multiline = false) {
    const wrap = el("label", label, "material-field");
    const field = el(multiline ? "textarea" : "input");
    field.value = value;
    if (multiline) field.rows = 3;
    wrap.append(field); parent.append(wrap);
    return field;
  }
  function select(parent, label, values) {
    const wrap = el("label", label, "material-field");
    const node = el("select");
    values.forEach(([value, title]) => {
      const option = el("option", title); option.value = value; node.append(option);
    });
    wrap.append(node); parent.append(wrap); return node;
  }
  const terminalJobs = new Set(["completed", "failed", "cancelled", "needs_review"]);
  const jobStatus = { pending: "排队中", running: "执行中", completed: "已完成",
    failed: "失败", cancelled: "已取消", needs_review: "待核对结果与费用" };
  const jobKind = { section: "章节", outline: "候选目录", plan: "配图建议",
    figure: "图表", export: "Word 导出" };
  const jobStep = {
    queued: "排队中", preparing_inputs: "准备项目资料", planning_outline: "整理候选目录",
    planning_figure: "拟定候选配图方案", writing_and_existing_review: "生成本章并按原流程评审",
    model_request: "等待模型响应", draft_saved: "草稿已保存，继续本轮检查",
    drawing: "绘制图表", checking_figure_text: "检查图片文字",
    assembling_document: "组装 Word", rendering_pages: "渲染页面供检查",
    saving_artifacts: "保存下载文件",
  };
  async function blobUrl(url) {
    const epoch = artifactEpoch;
    const requestProject = host.state.project?.id;
    const token = host.state.authSession?.access_token;
    let blob;
    if (url.split("?")[0].endsWith("/files/proposal.docx")) {
      blob = await window.WordExport.fetchWordFile(url, { token });
    } else {
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const response = await fetch(url, { headers });
      if (!response.ok) throw new Error("私有产物读取失败，请检查登录状态");
      blob = await response.blob();
    }
    if (epoch !== artifactEpoch || host.state.project?.id !== requestProject || host.isProjectActive?.(requestProject) === false)
      throw new Error("项目内容已更新，请重新打开文件。");
    const objectUrl = URL.createObjectURL(blob);
    urls.push(objectUrl);
    return objectUrl;
  }
  async function refresh() {
    if (!configured()) return;
    const id = host.state.project.id;
    const next = await request("");
    if (host.state.project?.id !== id) return;
    data = next;
    host.state.project.material = data.context;
    host.state.project.material_revision = data.context.revision;
    draw();
  }
  async function mount(config) {
    host = config;
    root = document.querySelector("#materialPilot");
    if (!root) {
      root = el("section", undefined, "material-pilot");
      root.id = "materialPilot";
      document.querySelector("#templateStatus").after(root);
    }
    root.classList.toggle("hidden", !configured());
    if (!configured()) { projectId = null; data = null; return; }
    if (projectId !== host.state.project.id) data = null;
    projectId = host.state.project.id;
    try { await refresh(); } catch (e) {
      root.replaceChildren(el("p", `材料链路暂不可用：${e.message}`));
    }
  }
  function draw() {
    if (!data || !configured()) return;
    artifactEpoch++;
    urls.forEach(u => URL.revokeObjectURL(u)); urls = [];
    root.replaceChildren();
    const details = el("details"); details.open = true;
    details.append(el("summary", "材料工作流 · 信息、要求、配图与交付"));
    details.append(el("p", data.worker_ready
      ? "已启用持久任务。付费模型须明确确认；失败不自动重复调用。"
      : "Worker 尚未配置：可整理信息和计划，完整生成与页面检查尚未启用。"));
    action("刷新任务与关联提示", refresh, details);
    const stale = Object.entries(data.context.stale || {});
    for (const [node, reasons] of stale) {
      const row = el("div", `${node} 可能需要更新：${reasons.join("、")}`, "material-warning");
      action("已核对当前版本", async () => {
        await request("/acknowledge", "POST", { revision: data.context.revision, snapshot: data.context.snapshot, nodes: [node] });
        await refresh();
      }, row);
      details.append(row);
    }
    const sources = data.context.sources || [];
    const pending = sources.filter(s => !s.confirmed && s.kind !== "template");
    details.append(el("p", `资料 ${sources.length} 份，待核对 ${pending.length} 份。请在材料区核对 OCR 和来源。`));
    requirementEditor(details);
    planEditor(details);
    const tasks = el("details"); tasks.open = true; tasks.append(el("summary", "任务与交付结果"));
    for (const job of data.jobs || []) {
      const row = el("div", `${jobKind[job.kind] || "材料任务"} · ${job.target || "整份材料"} · ${jobStatus[job.status] || "处理中"} · ${jobStep[job.step] || "处理中"}`, "material-job");
      if (job.error) row.append(el("p", job.error));
      if (job.status !== "completed" && job.result?.partial_content) {
        const partial = el("details"); partial.append(el("summary", "查看已保存草稿（后续检查未完成）"));
        partial.append(el("pre", job.result.partial_content)); row.append(partial);
      }
      if (["pending", "running"].includes(job.status))
        action("取消后续执行", async () => { await request(`/jobs/${job.id}/cancel`, "POST"); await refresh(); }, row);
      if (job.status === "completed") {
        const reviewIncomplete = job.kind === "section" && (job.result?.review_incomplete || job.result?.status === "review_failed");
        if (reviewIncomplete) row.append(el("p", job.result?.grounding_issues?.length
          ? (job.result?.grounding_kind === "project"
            ? "草稿中的项目数据、成果或来源需要你核对，可在下方查看并决定是否使用。"
            : "草稿中的部分个人数字需要你核对，可在下方查看并决定是否使用。")
          : "本轮检查未完成，草稿可在下方查看并由你决定是否使用。"));
        if (!job.result?.applied && !["export", "outline"].includes(job.kind))
          action(reviewIncomplete ? "使用此草稿" : "保存结果／保留为备选", async () => {
            await flushEdits();
            const result = await request(`/jobs/${job.id}/apply`, "POST",
              reviewIncomplete ? { use_unreviewed_draft: true } : undefined);
            host.toast(reviewIncomplete && !result.conflict_status
              ? (job.result?.grounding_issues?.length
                ? (job.result?.grounding_kind === "project" ? "草稿已保存，请核对项目数据、成果和来源。" : "草稿已保存，请核对其中的个人数字。")
                : "草稿已保存，本轮检查尚未完成。")
              : (result.notice || "结果已保存"));
            await host.reload(projectId);
          }, row);
        if (job.result?.conflict_status && !reviewIncomplete) row.append(el("p", "输入已经变化：结果保留为备选，未覆盖当前内容。"));
        if (job.kind === "section" && job.result?.content) {
          const alternative = el("details"); alternative.append(el("summary", "查看生成正文"));
          alternative.append(el("pre", job.result.content)); row.append(alternative);
        }
        if (job.kind === "plan" && job.result?.plan) {
          const proposed = job.result.plan;
          row.append(el("p", `${proposed.title}：${proposed.nodes.join(" → ")}`));
          action("编辑此配图建议", () => fillPlan(proposed), row);
        }
        if (job.kind === "outline" && job.result?.outline) {
          row.append(el("p", job.result.outline.map(s => s.title).join("；")));
          const ownerProject = projectId;
          action("在目录编辑器中查看", () => {
            if (host.state.project?.id === ownerProject && host.isProjectActive?.(ownerProject) !== false)
              host.previewOutline?.(job.result);
          }, row);
        }
        if (job.kind === "figure") {
          action("查看生成图片", async () => {
            const image = el("img"); image.alt = job.target;
            image.src = await blobUrl(path(`/jobs/${job.id}/files/figure.png`)); row.append(image);
          }, row);
          if (job.result?.asset?.scene_text?.length) {
            row.append(el("p", `请逐字核对场景文字：${job.result.asset.scene_text.join("；")}`));
            action("已逐字核对并确认", async () => {
              if (!confirm("请先查看大图，并确认场景内所有关键文字准确。")) return;
              await request(`/figures/${job.target}/confirm-text`, "POST", {
                revision: data.context.revision, snapshot: data.context.snapshot, nodes: [],
              }); await refresh();
            }, row);
          }
        }
        if (job.kind === "export") {
          const report = job.result.report || {};
          const quality = { failed: "检查发现问题", not_checked: "尚未完成页面检查", needs_review: "需人工核对" };
          row.append(el("p", `排版检查：${quality[report.quality_status] || "未完成"}；模板：${report.template_status === "mapped" ? "已按规则填充，仍需核对" : "待核对"}`));
          row.append(el("p", `内容基础检查：${report.validation?.passed ? "通过基础检查，仍需人工核对" : "仍有缺项或一致性问题"}；图表和模板状态请分别查看，文件可生成不等于可直接提交。`));
          for (const warning of report.warnings || []) row.append(el("p", warning, "material-warning"));
          for (const check of report.checks || []) row.append(el("p", `${check.rule}：${({
            passed: "通过", failed: "失败", not_checked: "未检查", needs_review: "需人工核对",
          })[check.status] || "未完成"} · ${check.detail}`));
          action("下载此任务的 Word 草稿", async () => {
            const link = el("a"); link.href = await blobUrl(path(`/jobs/${job.id}/files/proposal.docx`));
            link.download = "申报材料草稿.docx"; link.click();
          }, row);
          action("查看逐页预览", async () => {
            const pages = el("div", undefined, "material-pages"); row.append(pages);
            for (const name of report.pages || []) {
              const image = el("img"); image.alt = name; image.loading = "lazy";
              image.src = await blobUrl(path(`/jobs/${job.id}/files/${name}`)); pages.append(image);
            }
          }, row);
        }
      }
      tasks.append(row);
    }
    details.append(tasks); root.append(details);
  }
  function requirementEditor(parent) {
    const box = el("details"); box.append(el("summary", "适用要求与优先级"));
    box.append(el("p", data.context.requirements.priority.join(" → ")));
    for (const conflict of data.context.requirements.conflicts || [])
      box.append(el("p", `${conflict.key}：${conflict.status === "needs_confirmation" ? "同级冲突，请确认后修改要求" : "按优先级采用：" + conflict.selected.value}`, "material-warning"));
    data.requirements.forEach((rule, index) => {
      const row = el("div", `${rule.key}：${rule.value}（${rule.source}；${rule.confirmed && rule.applicable ? "已确认适用" : "待确认"}）`);
      action("移除", async () => {
        await request("/requirements", "PUT", { revision: data.context.revision, items: data.requirements.filter((_, i) => i !== index) }); await refresh();
      }, row); box.append(row);
    });
    const kind = select(box, "来源类型", [["official", "正式通知／学校模板"], ["user", "我的明确要求"], ["material", "当前材料类型规则"], ["example", "参考示例"], ["default", "默认样式"]]);
    const key = select(box, "约束对象", [["word_count", "字数"], ["page_limit", "页数"], ["sections", "章节栏目"], ["font", "字体"], ["format", "版式"], ["content", "内容要求"]]);
    const value = input(box, "具体要求", "", true);
    const source = input(box, "出处／文件名");
    const scope = input(box, "适用学校、年份和范围");
    action("确认这条要求适用并保存", async () => {
      await request("/requirements", "PUT", { revision: data.context.revision, items: [...data.requirements, {
        key: key.value, value: value.value.trim(), source_kind: kind.value, source: source.value.trim(),
        scope: scope.value.trim(), confirmed: true, applicable: true,
      }] }); await refresh();
    }, box);
    parent.append(box);
  }
  function planEditor(parent) {
    const box = el("details"); box.append(el("summary", "图表计划与绘图"));
    box.append(el("p", "流程与架构用程序画；数据图只使用真实确认数据；GPT 生成概念示意。可编辑后确认，不强制每章配图。"));
    data.plans.forEach(plan => {
      const row = el("div", `${plan.title} → ${plan.section_id} · ${plan.renderer}`, "material-job");
      action("编辑", () => { fill(plan); box.open = true; }, row);
      const drawLabel = () => pendingJob(projectId, "figure", plan.id) ? "查看上次绘图任务" : "确认并生成此图";
      const drawFigure = action(drawLabel(), async () => {
        if (!plan.confirmed && !pendingJob(projectId, "figure", plan.id)) {
          fill(plan);
          box.open = true;
          host.toast("请先核对候选方案，点击下方保存并确认");
          return;
        }
        try { await listedJob("figure", plan.id, plan.renderer === "gpt"); }
        finally { drawFigure.textContent = drawLabel(); }
      }, row);
      action("移除计划", async () => {
        await request("/plans", "PUT", { revision: data.context.revision, items: data.plans.filter(p => p.id !== plan.id) }); await refresh();
      }, row);
      box.append(row);
    });
    let editingId = null;
    const title = input(box, "图题");
    const section = select(box, "所属章节", host.state.outline.map(s => [s.id, s.title]));
    const proposePlan = action("根据已确认资料拟定配图方案", async () => {
      if (!section.value) throw new Error("请先确认目录");
      try { await listedJob("plan", section.value, true); }
      finally { updatePlanLabel(); }
    }, box);
    const updatePlanLabel = () => {
      proposePlan.textContent = pendingJob(projectId, "plan", section.value)
        ? "查看上次配图建议任务" : "根据已确认资料拟定配图方案";
    };
    section.onchange = updatePlanLabel;
    updatePlanLabel();
    const renderer = select(box, "绘图方式", [["svg", "流程／架构（HTML/SVG）"], ["python", "数据图（Python）"], ["gpt", "概念／场景（GPT Image，付费）"], ["overlay", "已有图片追加准确文字（不调用生图模型）"], ["upload", "真实上传照片／素材（不调用模型）"]]);
    const chart = select(box, "数据图类型", [["bar", "柱状图"], ["line", "折线图"], ["gantt", "甘特图"]]);
    const nodes = input(box, "流程节点：每行一个", "", true);
    const edges = input(box, "连接关系：每行“1->2”；留空按节点顺序连接", "", true);
    const method = data.context.planned_items?.method || "";
    if (method) nodes.value = String(method).split(/[；;。\n]+/).filter(Boolean).join("\n");
    const numbers = input(box, "数据：每行“名称,数值”；甘特图为“名称,开始时间,持续时间”", "", true);
    const dataStatus = select(box, "数据性质", [["planned", "计划值"], ["measured", "实测值"]]);
    const source = input(box, "数据或图片来源说明");
    const prompt = input(box, "概念图描述（仅 GPT Image 使用）", "", true);
    const scene = input(box, "必须融入画面的文字：每行一条；普通科研标签请放下面", "", true);
    const overlay = input(box, "程序追加文字：每行一条，放在独立说明区域，不覆盖原图", "", true);
    const base = select(box, "局部排字的原图任务", [["", "不使用"], ...data.jobs.filter(j => j.kind === "figure" && j.status === "completed").map(j => [j.id, `${j.target} · ${j.id.slice(0, 8)}`])]);
    const documentId = select(box, "使用的真实上传图片", [["", "不使用"], ...(host.state.documents || [])
      .filter(d => (d.content_type || "").startsWith("image/")).map(d => [d.id, d.original_name])]);
    function fill(plan) {
      box.open = true;
      editingId = plan.id; title.value = plan.title; section.value = plan.section_id; renderer.value = plan.renderer;
      chart.value = plan.chart === "flow" ? "bar" : plan.chart; nodes.value = plan.nodes.join("\n");
      edges.value = (plan.edges || []).map(e => `${e.source+1}->${e.target+1}`).join("\n");
      numbers.value = plan.labels.map((l, i) => [l, ...(plan.chart === "gantt" ? [plan.starts[i]] : []), plan.values[i]].join(",")).join("\n");
      source.value = plan.source; prompt.value = plan.prompt; scene.value = plan.scene_text.join("\n");
      overlay.value = plan.overlays.map(o => o.text).join("\n"); base.value = plan.base_asset_job;
      dataStatus.value = plan.data_status;
      documentId.value = plan.document_id || "";
      title.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    fillPlan = fill;
    const lines = value => value.split(/\r?\n/).map(v => v.trim()).filter(Boolean);
    action("保存并确认图表计划（不调用模型）", async () => {
      const rows = lines(numbers.value).map(l => l.split(/[,，]/).map(s => s.trim()));
      const spec = {
        id: editingId || `fig_${crypto.randomUUID().replaceAll("-", "").slice(0, 16)}`,
        section_id: section.value, title: title.value.trim(), renderer: renderer.value,
        chart: renderer.value === "svg" ? "flow" : chart.value, nodes: lines(nodes.value),
        edges: lines(edges.value).map(value => {
          const match = value.match(/^(\d+)\s*->\s*(\d+)$/);
          if (!match) throw new Error("连接关系请填写 1->2 这样的编号");
          return { source: Number(match[1])-1, target: Number(match[2])-1 };
        }),
        labels: rows.map(r => r[0]), values: rows.map(r => Number(r.at(-1))),
        starts: chart.value === "gantt" ? rows.map(r => Number(r[1])) : [],
        source: source.value, data_status: dataStatus.value, prompt: prompt.value,
        scene_text: lines(scene.value), overlays: lines(overlay.value).map(text => ({ text })),
        base_asset_job: base.value, confirmed: true, text_confirmed: false,
        document_id: documentId.value,
      };
      await request("/plans", "PUT", { revision: data.context.revision, items: [...data.plans.filter(p => p.id !== spec.id), spec] }); await refresh();
    }, box);
    parent.append(box);
  }
  async function submit(kind, target, paid, extra = {}, { onSending, onAccepted, checking = false } = {}) {
    if (paid && !confirm((checking ? "暂未查到上次任务。继续会沿用原编号，只有任务尚未登记才会创建。\n" : "") +
        (kind === "section" ? "本章沿用现有撰写与评审流程，最多六次模型请求。" :
        kind === "figure" ? "本次生成一张图片。" : "本次调用一次文字模型拟定候选方案。") +
        "平台模型按现有积分规则结算，自定义模型由服务商收费；不自动付费重绘。是否确认？")) return null;
    const body = {
      revision: data?.context.revision ?? host.state.project.material_revision,
      kind, target, idempotency_key: extra.idempotency_key || crypto.randomUUID().replaceAll("-", ""),
      llm_config: paid ? { ...host.state.llmConfig } : null, confirm_paid: Boolean(paid),
      ...extra,
    };
    onSending?.(body.idempotency_key);
    let job;
    try {
      job = await waitFor(request("/jobs", "POST", body), 30000, JOB_SUBMIT_NOTICE);
    } catch (error) {
      if (!error.status || error.status >= 500) throw new Error(JOB_SUBMIT_NOTICE);
      throw error;
    }
    onAccepted?.(job);
    try {
      await waitFor(refresh(), 15000, "任务已提交，但暂时无法刷新任务列表；请稍后查看，不要重复提交。");
    } catch {
      throw new Error("任务已提交，但暂时无法刷新任务列表；请稍后查看，不要重复提交。");
    }
    host.toast("任务已保存，刷新页面后仍可查询。");
    return job;
  }
  async function getTrackedJob(action) {
    const { id, kind, target } = action;
    const previous = action.read();
    if (previous) {
      const suffix = previous.jobId ? `/jobs/${previous.jobId}` : `/jobs/by-request/${previous.requestId}`;
      try {
        const job = await waitFor(request(suffix), 15000, JOB_STATUS_NOTICE);
        if (!action.current()) throw new Error("项目已切换，请返回原项目查看任务。");
        if (job?.project_id !== id || job.kind !== kind || job.target !== target
            || !/^[a-f0-9]{32}$/.test(job.id) || (previous.jobId && job.id !== previous.jobId))
          throw new Error("暂时无法确认上次后台任务。");
        action.record({ ...previous, jobId: job.id });
        return job;
      } catch (error) {
        if (!action.current() || error.status !== 404) throw error;
        if (previous.jobId || !configured()) {
          action.record(null);
          throw new Error("任务暂不可查看。可检查任务区，再决定是否重新生成。");
        }
      }
      await waitFor(refresh(), 15000, "暂时无法读取项目版本，请稍后查看上次任务。");
      if (!action.current()) throw new Error("项目已切换，请返回原项目查看任务。");
    }
    const running = projectId === id && data?.jobs?.find(job =>
      job.kind === kind && job.target === target && ["pending", "running"].includes(job.status));
    if (running) {
      action.record({ jobId: running.id });
      return running;
    }
    try {
      return await submit(kind, target, true, previous ? { idempotency_key: previous.requestId } : {}, {
        checking: Boolean(previous),
        onSending: requestId => action.record({ requestId }),
        onAccepted: job => action.record({ jobId: job.id }),
      });
    } catch (error) {
      if ([400, 401, 402, 403, 404, 409, 422].includes(error.status) && !action.read()?.jobId && !previous)
        action.record(null);
      throw error;
    }
  }

  async function listedJob(kind, target, paid) {
    const id = host.state.project?.id;
    const view = host.captureView?.();
    const action = trackedAction(id, kind, target,
      () => host.state.project?.id === id && (view?.active() ?? host.isProjectActive?.(id) !== false));
    if (!action.read()) await refresh();
    if (!action.current()) return;
    if (!paid && !action.read()) return submit(kind, target, false);
    try {
      const job = await getTrackedJob(action);
      if (!job || !action.current()) return;
      if (terminalJobs.has(job.status)) {
        action.record(null);
        host.toast(job.error || "已找到上次任务，可在任务区查看结果。", job.status !== "completed");
      } else {
        host.toast("已找到后台任务，可在任务区查看进度。");
      }
    } finally {
      if (action.current()) void refresh().catch(() => {});
    }
  }

  async function section(sid, editor) {
    const id = host.state.project?.id;
    const view = host.captureView?.();
    const action = trackedAction(id, "section", sid, () => Boolean(id && host.state.project?.id === id
      && (view?.active() ?? host.isProjectActive?.(id) !== false)));
    const isCurrent = action.current;
    const loadedRevision = host.state.project.material_revision;
    const original = editor?.value;
    if (!action.read()) await waitFor(refresh(), 15000, "材料状态暂时无法读取，请检查网络后再生成。");
    if (!isCurrent()) throw new Error("项目已切换，未提交该章节任务。");
    if (!action.read() && editor && original !== (host.state.project.sections?.[sid] || "")) {
      if (loadedRevision !== data.context.revision)
        throw new Error("项目已在其他操作中变化，请先保存并核对当前章节，未覆盖您的编辑");
      await waitFor(host.api(`/api/projects/${projectId}/sections/${sid}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: original, expected_revision: loadedRevision,
          base_content: host.state.project.sections?.[sid] || "" }),
      }), 15000, "暂未收到编辑保存结果，请先刷新项目核对；尚未提交生成任务。");
      if (!isCurrent()) throw new Error("项目已切换，请返回原项目查看已保存的编辑。");
      host.state.project.sections[sid] = original;
      if (editor.value !== original) throw new Error("保存期间正文又有修改，请先保存最新编辑，再生成草稿。");
      await waitFor(refresh(), 15000, "材料状态暂时无法读取，请检查网络后再生成。");
      if (!isCurrent()) throw new Error("项目已切换，未提交该章节任务。");
    }
    const baseRevision = data?.context.revision ?? loadedRevision;
    const job = await getTrackedJob(action);
    if (!job) throw new Error("已取消模型调用");
    if (!isCurrent()) throw new Error("任务可能仍在原项目中执行，请返回原项目查询。");
    let current = job;
    for (let i = 0; i < 120; i++) {
      if (!terminalJobs.has(current.status)) {
        await new Promise(resolve => setTimeout(resolve, 2500));
        if (!isCurrent()) throw new Error("任务继续运行，可返回项目查询");
        current = await waitFor(request(`/jobs/${job.id}`), 15000, JOB_STATUS_NOTICE);
      }
      if (!isCurrent()) throw new Error("任务可能仍在原项目中执行，请返回原项目查询。");
      if (current.status === "completed") {
        if (current.result?.review_incomplete || current.result?.status === "review_failed") {
          // Keep the completed (and potentially billed) result in the task
          // list. Never silently apply or re-run an unreviewed draft.
          await refresh();
          if (!isCurrent()) throw new Error("项目已切换，请返回原项目查看草稿。");
          action.record(null);
          return { ...current.result, review_incomplete: true, conflict_status: "user_edit_preserved",
            notice: current.result?.grounding_issues?.length
              ? (current.result?.grounding_kind === "project"
                ? "草稿中的项目数据、成果或来源需要你核对，已保留在任务区。"
                : "草稿中的部分个人数字需要你核对，已保留在任务区。")
              : "本轮检查未完成，草稿已保留在任务区，可查看后决定是否使用。" };
        }
        if (editor && editor.value !== original && editor.value !== host.state.project.sections?.[sid]) {
          const latestEdit = editor.value;
          await waitFor(host.api(`/api/projects/${id}/sections/${sid}`, { method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content: latestEdit, expected_revision: baseRevision,
              base_content: host.state.project.sections?.[sid] || "" }) }), 15000,
            "暂未收到编辑保存结果，生成草稿仍在任务区；请刷新项目核对。");
          if (!isCurrent()) throw new Error("项目已切换，请返回原项目查看已保存的编辑。");
          host.state.project.sections[sid] = latestEdit;
          if (editor.value !== latestEdit) throw new Error("保存期间正文又有修改，请先保存最新编辑；生成结果仍在任务区。");
        }
        const result = await waitFor(request(`/jobs/${job.id}/apply`, "POST"), 15000,
          "暂未收到草稿保存结果，请刷新原项目查看，不要重复生成。");
        if (!isCurrent()) throw new Error("项目已切换，请返回原项目查看草稿结果。");
        await waitFor(refresh(), 15000, "草稿已处理，但暂时无法刷新项目，请稍后查看。");
        if (!isCurrent()) throw new Error("项目已切换，请返回原项目查看草稿。");
        action.record(null);
        return { ...result, material_applied: true };
      }
      if (["failed", "cancelled", "needs_review"].includes(current.status)) {
        action.record(null);
        throw new Error(current.error || "后台任务未完成，可在任务区查看。");
      }
    }
    throw new Error("任务仍在后台执行，请稍后刷新任务列表，不必重复提交");
  }
  async function flushEdits() {
    if (host.flushEdits) return host.flushEdits();
    // Save visible edits before freezing an export snapshot; never discard them by reloading.
    const id = host.state.project?.id;
    const userId = host.state.authSession?.user?.id || null;
    const isCurrent = () => Boolean(id && projectId === id && host.state.project?.id === id &&
      (host.state.authSession?.user?.id || null) === userId && host.isProjectActive?.(id) !== false);
    for (const area of document.querySelectorAll(".draft-section textarea")) {
      if (!isCurrent()) throw new Error("项目已切换，请回到原项目后继续操作。");
      const sid = area.closest(".draft-section").id.slice("draft-sec-".length);
      const content = area.value;
      if (content !== (host.state.project.sections?.[sid] || "")) {
        const result = await host.api(`/api/projects/${id}/sections/${sid}`, { method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content, expected_revision: host.state.project.material_revision,
            base_content: host.state.project.sections?.[sid] || "" }) });
        if (!isCurrent()) throw new Error("项目已切换，请回到原项目查看保存结果。");
        host.state.project.material_revision = result.material_revision;
        host.state.project.sections[sid] = content;
        if (area.value !== content) throw new Error("保存期间正文又有修改，请保存最新版本后继续。");
      }
    }
  }
  function invalidateArtifacts() {
    artifactEpoch++;
    urls.forEach(url => URL.revokeObjectURL(url));
    urls = [];
    if (data && configured()) draw();
  }

  async function exportDocument(options = {}, isCurrent = () => true, onWordReady, onTrack) {
    await waitFor(flushEdits(), 15000, "正文保存结果暂未确认，请先检查项目中的正文再导出。");
    if (!isCurrent()) return;
    await waitFor(refresh(), 15000, "材料状态暂时无法读取，请检查网络后再导出。");
    if (!isCurrent()) return;
    let job = data.jobs?.find(item => item.kind === "export" && ["pending", "running"].includes(item.status));
    if (job) {
      onTrack?.({ kind: "worker-job", id: job.id });
    } else {
      onTrack?.({ kind: "worker-request", id: options.idempotency_key });
      job = await submit("export", "", false, options, { onAccepted: accepted => {
        if (isCurrent()) onTrack?.({ kind: "worker-job", id: accepted.id });
      } });
    }
    if (!isCurrent()) return;
    const id = projectId;
    let lastStep;
    // Observe the existing durable job only. Never retry/resubmit model work.
    for (let attempt = 0; attempt < 80; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 2500));
      if (host.state.project?.id !== id) {
        host.toast("导出仍在后台执行，可返回原项目查看任务。");
        return;
      }
      if (!isCurrent()) return;
      const current = await waitFor(request(`/jobs/${job.id}`), 15000,
        "暂时无法获取导出状态，文件可能仍在后台生成；请查看任务区，不要重复提交。");
      if (host.state.project?.id !== id || !isCurrent()) return;
      if (current.status === "completed") {
        await waitFor(refresh(), 15000, "Word 已生成，但暂时无法刷新任务区，请稍后查看任务并下载。");
        if (host.state.project?.id !== id || !isCurrent()) return;
        const filePath = path(`/jobs/${job.id}/files/proposal.docx`);
        if (onWordReady) return onWordReady(filePath, current.result?.report?.warnings);
        const url = await blobUrl(filePath + "?current=true");
        if (host.state.project?.id !== id || !isCurrent()) {
          URL.revokeObjectURL(url);
          urls = urls.filter(item => item !== url);
          return;
        }
        const link = el("a");
        link.href = url;
        link.download = "申报材料草稿.docx";
        document.body.append(link); link.click(); link.remove();
        host.toast("Word 草稿已生成并下载；缺项与排版提示请在任务区核对。");
        return;
      }
      if (["failed", "cancelled", "needs_review"].includes(current.status)) {
        await waitFor(refresh(), 15000, "导出未完成，暂时无法刷新任务区，请稍后查看。");
        throw new Error(current.error || "导出未完成，请查看任务记录；不会自动重复生成或扣费。");
      }
      if (current.step !== lastStep) {
        host.toast(`Word 后台导出：${jobStep[current.step] || "处理中"}…`);
        lastStep = current.step;
      }
    }
    await waitFor(refresh(), 15000, "导出可能仍在后台执行，请稍后查看任务区。");
    host.toast("导出仍在后台执行，请稍后刷新任务区，不必重复生成正文。");
  }
  async function outline() {
    const id = host.state.project?.id;
    const view = host.captureView?.();
    const action = trackedAction(id, "outline", "",
      () => host.state.project?.id === id && (view?.active() ?? host.isProjectActive?.(id) !== false));
    if (!action.read()) await waitFor(refresh(), 15000, "暂时无法读取项目材料，请稍后再拟目录。");
    if (!action.current()) throw new Error("项目已切换");
    const job = await getTrackedJob(action);
    if (!job) throw new Error("已取消目录模型调用");
    let current = job;
    for (let i = 0; i < 120; i++) {
      if (!action.current()) throw new Error("任务继续执行，请返回项目查询");
      if (!terminalJobs.has(current.status)) {
        await new Promise(resolve => setTimeout(resolve, 2500));
        if (!action.current()) throw new Error("任务继续执行，请返回项目查询");
        current = await waitFor(request(`/jobs/${job.id}`), 15000, JOB_STATUS_NOTICE);
      }
      if (!action.current()) throw new Error("任务继续执行，请返回项目查询");
      if (current.status === "completed") {
        action.record(null);
        void refresh().catch(() => {});
        return { ...current.result, warning: current.result?.notice || "目录为候选方案，请核对后确认" };
      }
      if (["failed", "cancelled", "needs_review"].includes(current.status)) {
        action.record(null);
        throw new Error(current.error || "后台任务未完成，可在任务区查看。");
      }
    }
    throw new Error("目录仍在后台生成，请在任务区查询");
  }
  window.MaterialPilot = { mount, active, refresh, section, outline, exportDocument, flushEdits, blobUrl,
    invalidateArtifacts, configureRecovery, pendingJob, clearRecovery };
})();
