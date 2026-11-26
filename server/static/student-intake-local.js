(() => {
  "use strict";
  const byId = id => document.getElementById(id);
  const page = byId("intake"), oral = byId("oral"), load = byId("load"), extract = byId("extract");
  const stop = byId("stop"), status = byId("status"), panel = byId("review"), copy = byId("copy");
  let worker, state = "idle", reviewed = [];
  function update(next) {
    state = next;
    const busy = next === "loading" || next === "running";
    load.hidden = next !== "idle" && next !== "unsupported";
    load.disabled = next === "unsupported";
    extract.disabled = next !== "ready" || !oral.value.trim();
    oral.readOnly = next === "running";
    stop.hidden = !busy;
    panel.setAttribute("aria-busy", String(next === "running"));
  }
  function forgetResults() {
    reviewed = [];
    byId("facts").replaceChildren();
    byId("questions").replaceChildren();
    copy.disabled = true;
    panel.hidden = true;
  }
  function terminate() {
    if (worker) { worker.terminate(); worker = undefined; }
  }
  function el(tag, text) {
    const item = document.createElement(tag);
    item.textContent = text;
    return item;
  }
  function selected() {
    return reviewed.filter(item => item.check.checked).map(item => item.fact);
  }
  function show(result) {
    forgetResults();
    panel.hidden = false;
    const list = byId("facts");
    byId("review-note").textContent = result.decision === "needs_manual_review"
      ? "本次结果无法完整核对。请缩短口述或补充主体、时间后重试。"
      : !result.facts.length ? "本次没有找到可核对的条目。可以补充一项具体经历再试。"
      : result.decision === "partial_review" ? "部分候选缺少可靠依据。请先核对下列条目的字段、状态和原话。"
      : "请对照字段、状态和引文，勾选你确认无误的条目。";
    for (const item of result.facts) {
      const fact = item.fact, card = el("article", "");
      card.className = "fact";
      card.append(el("h3", item.label + " · " + item.status_label));
      if (fact.value !== null) card.append(el("p", fact.value));
      for (const source of fact.evidence) card.append(el("blockquote", source.turn_id + "　“" + source.quote + "”"));
      if (item.can_confirm) {
        const label = el("label", ""), check = document.createElement("input");
        check.type = "checkbox";
        label.append(check, document.createTextNode("我已核对这条原话和状态"));
        check.addEventListener("change", () => { copy.disabled = !selected().length; });
        reviewed.push({ check, fact });
        card.append(label);
      } else {
        card.append(el("small", fact.status === "withheld" ? "保留拒答状态，跳过追问。" : "这项需核对，暂不列入可复制记录。"));
      }
      list.append(card);
    }
    if (result.questions.length) {
      const questions = byId("questions"), ul = el("ul", "");
      questions.append(el("h3", "还需核对"), ul);
      for (const question of result.questions) ul.append(el("li", question));
    }
    byId("review-heading").focus();
  }
  function failed(message) {
    terminate();
    update("idle");
    status.textContent = message;
  }
  load.addEventListener("click", () => {
    if (state !== "idle") return;
    update("loading");
    status.textContent = "正在检查设备并下载模型。首次使用请保持页面打开。";
    try {
      const active = new Worker(page.dataset.worker);
      worker = active;
      active.onmessage = ({ data }) => {
        if (worker !== active) return;
        if (data.type === "status") status.textContent = data.text;
        if (data.type === "ready") {
          update("ready"); status.textContent = "模型已就绪，可以在此设备提取口述。";
        }
        if (data.type === "result") {
          update("ready"); status.textContent = "端侧提取完成。"; show(data.result);
        }
        if (data.type === "error") failed(data.text);
      };
      active.onerror = () => {
        if (worker === active) failed("端侧运行环境未能启动。请检查浏览器、网络和可用空间，再试一次。");
      };
      active.postMessage({ type: "load", manifest: page.dataset.model });
    } catch { failed("浏览器未能启动端侧运行环境。"); }
  });
  oral.addEventListener("input", () => {
    forgetResults(); update(state);
  });
  extract.addEventListener("click", () => {
    if (state !== "ready") return;
    const text = oral.value, lines = text.split(/\r?\n/).filter(line => line.trim());
    if (!lines.length || [...text].length > 600 || lines.length > 12 || /<\||\|>/.test(text)) {
      status.textContent = "请输入 1 至 600 字、最多 12 行的口述，并移除模型控制标记。";
      return;
    }
    forgetResults(); update("running");
    status.textContent = "正在此设备提取，并逐条核对原话。";
    worker.postMessage({ type: "extract", text });
  });
  stop.addEventListener("click", () => {
    terminate(); forgetResults(); update("idle");
    status.textContent = "已停止。可重新加载模型，已缓存的权重会复用。";
  });
  byId("clear").addEventListener("click", () => {
    oral.value = ""; forgetResults();
    if (state === "running" || state === "ready") { terminate(); update("idle"); }
    else update(state);
    status.textContent = "本页口述和候选已清空。"; oral.focus();
  });
  copy.addEventListener("click", async () => {
    const facts = selected();
    if (!facts.length) return;
    const payload = JSON.stringify({ model: "Qwen2.5-0.5B 学生口述 LoRA", confirmed: true, facts }, null, 2);
    try {
      await navigator.clipboard.writeText(payload);
      status.textContent = "已复制 " + facts.length + " 条核对过的记录，包含状态和原话。";
    } catch {
      status.textContent = "浏览器未允许复制。请允许剪贴板访问后重试。";
    }
  });
  window.addEventListener("pagehide", () => {
    terminate(); oral.value = ""; forgetResults(); update(navigator.gpu ? "idle" : "unsupported");
    status.textContent = "口述已清空。";
  });
  if (!window.isSecureContext || !navigator.gpu) {
    update("unsupported");
    status.textContent = "此浏览器未启用 WebGPU。请用支持 WebGPU 的桌面 Chrome 或 Edge 打开此页。";
  } else update("idle");
})();
