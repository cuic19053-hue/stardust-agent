(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const mobileLayout = window.matchMedia?.(
    "(max-width: 640px), (max-width: 1024px) and (max-height: 540px) and (hover: none) and (pointer: coarse)"
  );
  const llmConfigKey = "innovation-proposal-agent-llm-config";
  const MICRO_CNY_PER_CREDIT = 100000; // ¥1 = 10 workbench credits

  const activeProjectKey = "stardust-agent-active-project-id";

  // 只缓存公开目录；认证配置、余额和用户材料不能走这条路径。
  function readPublicCache(name) {
    try {
      const entry = JSON.parse(localStorage.getItem(`stardust-public-v1-${name}`));
      if (entry?.version === 1 && Date.now() - entry.savedAt < 7 * 86400000
          && Array.isArray(entry.data) && entry.data.length <= 200
          && entry.data.every(item => item && typeof item.id === "string"
            && typeof item.name === "string")) return entry.data;
    } catch {}
    return null;
  }

  function writePublicCache(name, data) {
    try {
      localStorage.setItem(`stardust-public-v1-${name}`,
        JSON.stringify({ version: 1, savedAt: Date.now(), data }));
    } catch {} // 禁用存储或空间不足时，仍然使用网络结果。
  }

  function sameOriginAuthFetch(supabaseUrl) {
    return (input, options) => {
      const upstream = new URL(supabaseUrl);
      const url = new URL(typeof input === "string" ? input : input.url);
      if (url.origin === upstream.origin) {
        if (url.pathname.startsWith("/auth/v1/")) {
          return fetch(`/api/auth/proxy/${url.pathname.slice(9)}${url.search}`, options);
        }
        if (url.pathname.startsWith("/storage/v1/")) {
          return fetch(`/api/storage/proxy/${url.pathname.slice(12)}${url.search}`, options);
        }
      }
      return fetch(input, options);
    };
  }

  function readSavedLlmConfig() {
    const fallback = {
      provider: "preset",
      model: "workbench-bundle",
      base_url: "",
      api_key: "",
      image_model: "",
      image_base_url: "",
      image_api_key: "",
      image_reuse_text: false,
    };
    try {
      localStorage.removeItem(llmConfigKey);
      const saved = JSON.parse(sessionStorage.getItem(llmConfigKey) || "null");
      if (saved && ["ollama", "openai_compatible", "preset"].includes(saved.provider)) return { ...fallback, ...saved };
    } catch {}
    return fallback;
  }

  const state = {
    project: null,
    projectsList: [],
    documents: [],
    messages: [],
    uploadKind: "source",
    uploading: false,
    outline: [],
    diagrams: [],
    editingDiagramId: "",
    llmConfig: readSavedLlmConfig(),
    chatCollapsed: false,
    cloudMode: false,
    privateRagConsentProjectId: null,
    ollamaLocal: null,
    userCredits: null,
    freeChatsRemaining: 0,
    cardRedemptionEnabled: false,
    cardShopUrl: "",
    presetModels: [],
    rechargePlans: [],
    authClient: null,
    authStatus: "loading",
    authBusy: false,
    authInitializing: false,
    authConfig: null,
    authSubscription: null,
    authSession: null,
    authMode: "signin",
    turnstileSiteKey: "",
    turnstileWidgetId: null,
    turnstileToken: "",
    appStarted: false,
    balanceRequestSeq: 0,
    skills: [],
    pendingProject: null,
    activeTab: "all",
    batchGenerating: false,
    batchAbort: false,
    chatSending: false,
  };

  // Project requests may finish out of order when the student switches chats.
  let projectLoadRevision = 0;
  let projectListRevision = 0;
  let loadingProjectId = null;
  // A sent message can finish after a disconnect. Keep it only in the current
  // tab, under its account and project, and never submit it automatically.
  const chatRecoveryStorageKey = "stardust-chat-recovery-v1";
  const interruptedChatMessages = new Map();
  const pendingChatRequests = new Map();
  const interruptedMessagesRendered = new Set();
  const recoveredChatInputs = new Map();
  let loadedRecoveryOwner = null;
  // A completed model call can outlive a failed history write. Keep its
  // returned text only in this page, scoped to the originating project.
  const uncertainSectionDrafts = new Map();
  const outlineDrafts = new Map();
  const outlinePreviews = new Map();
  const outlineRequests = new Map();
  const outlineRecovery = window.DraftRecovery.create("stardust-outline-recovery-v1:", chatRecoveryOwner);
  let outlineRecoveryWarning = false;
  const documentCorrections = new Map();
  const documentReviews = new Map();
  const documentRecovery = window.DraftRecovery.create("stardust-document-recovery-v1:", chatRecoveryOwner);
  let documentRecoveryWarning = false;
  const factEdits = window.FactEdits.create(chatRecoveryOwner, toast);
  window.MaterialPilot?.configureRecovery?.(chatRecoveryOwner);
  let factFormProjectId = null;
  const sectionEdits = window.SectionEdits.create({
    state, api: (...args) => api(...args), currentProjectView, recoveryOwner: chatRecoveryOwner,
    setDraftSaveStatus, updateDraftStats, toast,
  });
  const { flushDraftEdits, confirmGeneratedSection, applyGeneratedSection } = sectionEdits;
  const deletedProjectEpochs = new Map();
  let chatRecoveryEpoch = 0;

  function chatRecoveryOwner() {
    const userId = state.authSession?.user?.id;
    if (userId) return `user:${userId}`;
    return state.cloudMode ? null : "local";
  }

  function loadInterruptedChatMessages() {
    const owner = chatRecoveryOwner();
    if (!owner || loadedRecoveryOwner === owner) return;
    interruptedChatMessages.clear();
    interruptedMessagesRendered.clear();
    recoveredChatInputs.clear();
    loadedRecoveryOwner = owner;
    try {
      const raw = sessionStorage.getItem(chatRecoveryStorageKey);
      if (!raw || raw.length > 8_000_000) return;
      const saved = JSON.parse(raw);
      if (saved?.version !== 1 || saved.owner !== owner || !Array.isArray(saved.projects)) {
        sessionStorage.removeItem(chatRecoveryStorageKey);
        return;
      }
      for (const item of saved.projects) {
        if (!Array.isArray(item) || typeof item[0] !== "string" || item[0].length > 160 || !Array.isArray(item[1])) continue;
        const messages = [...new Set(item[1].filter(text => typeof text === "string" && text.trim() && text.length <= 12000))];
        if (messages.length) interruptedChatMessages.set(item[0], messages);
      }
    } catch {} // Storage may be blocked, full, or contain corrupt data.
  }

  function persistInterruptedChatMessages() {
    const owner = chatRecoveryOwner();
    if (!owner) return;
    try {
      if (!interruptedChatMessages.size) sessionStorage.removeItem(chatRecoveryStorageKey);
      else sessionStorage.setItem(chatRecoveryStorageKey, JSON.stringify({
        version: 1, owner, projects: [...interruptedChatMessages.entries()],
      }));
    } catch {} // In-memory recovery remains available if storage is blocked.
  }

  function rememberInterruptedChatMessage(projectId, message) {
    loadInterruptedChatMessages();
    const messages = interruptedChatMessages.get(projectId) || [];
    if (!messages.includes(message)) messages.push(message);
    interruptedChatMessages.set(projectId, messages);
    persistInterruptedChatMessages();
  }

  function forgetInterruptedChatMessage(projectId, message) {
    loadInterruptedChatMessages();
    const remaining = (interruptedChatMessages.get(projectId) || []).filter(item => item !== message);
    if (remaining.length) interruptedChatMessages.set(projectId, remaining);
    else interruptedChatMessages.delete(projectId);
    interruptedMessagesRendered.delete(`${projectId}\0${message}`);
    if (recoveredChatInputs.get(projectId) === message) recoveredChatInputs.delete(projectId);
    persistInterruptedChatMessages();
  }

  function updateRecoveredChatInput() {
    const projectId = state.project?.id;
    if (!projectId) return;
    loadInterruptedChatMessages();
    const previous = recoveredChatInputs.get(projectId);
    const text = ($("#chatInput")?.value || "").trim();
    if (previous === undefined) {
      if (text) {
        rememberInterruptedChatMessage(projectId, text);
        recoveredChatInputs.set(projectId, text);
      }
      return;
    }
    if (text === previous) return;
    const messages = interruptedChatMessages.get(projectId) || [];
    const index = messages.indexOf(previous);
    if (index < 0) { recoveredChatInputs.delete(projectId); return; }
    messages.splice(index, 1);
    if (text && !messages.includes(text)) messages.splice(index, 0, text);
    if (messages.length) interruptedChatMessages.set(projectId, messages);
    else interruptedChatMessages.delete(projectId);
    if (text) recoveredChatInputs.set(projectId, text);
    else recoveredChatInputs.delete(projectId);
    persistInterruptedChatMessages();
  }

  function clearInterruptedChatMessages() {
    chatRecoveryEpoch++;
    projectListRevision++;
    state.projectsList = [];
    const search = $("#projectSearchInput");
    if (search) search.value = "";
    renderProjectList([]);
    clearProjectResults();
    interruptedChatMessages.clear();
    pendingChatRequests.clear();
    try {
      for (let i = sessionStorage.length - 1; i >= 0; i--) {
        const key = sessionStorage.key(i);
        if (key?.startsWith("agent-chat-request:")) sessionStorage.removeItem(key);
      }
    } catch {}
    interruptedMessagesRendered.clear();
    recoveredChatInputs.clear();
    loadedRecoveryOwner = null;
    try { sessionStorage.removeItem(chatRecoveryStorageKey); } catch {}
    uncertainSectionDrafts.clear();
    sectionEdits.clear();
    outlineDrafts.clear();
    outlinePreviews.clear();
    outlineRequests.clear();
    outlineRecovery.clear();
    outlineRecoveryWarning = false;
    factEdits.clear();
    documentCorrections.clear();
    documentReviews.clear();
    documentRecovery.clear();
    clearWordExportRecovery();
    window.MaterialPilot?.clearRecovery?.();
    documentRecoveryWarning = false;
    factFormProjectId = null;
    deletedProjectEpochs.clear();
    const input = $("#chatInput");
    if (input) {
      input.value = "";
      if (input.style) input.style.height = "auto";
    }
    $("#chatMessages")?.replaceChildren();
  }

  function invalidateProjectLoad() {
    projectLoadRevision++;
    loadingProjectId = null;
  }

  function chatProjectIsActive(projectId) {
    return state.project?.id === projectId && (!loadingProjectId || loadingProjectId === projectId);
  }

  let projectViewVersion = 0;

  function currentProjectView() {
    const id = state.project?.id;
    const version = projectViewVersion;
    return { id, active: () => Boolean(id) && version === projectViewVersion && chatProjectIsActive(id) };
  }

  function clearProjectResults() {
    projectViewVersion++;
    if ($("#chatRunsDialog")?.open) $("#chatRunsDialog").close();
    $("#chatRunsList")?.replaceChildren();
    for (const selector of ["#sourceResults", "#knowledgeResults", "#factCandidates", "#intakeCandidates"]) {
      $(selector)?.replaceChildren();
    }
    for (const selector of ["#knowledgeExternalConsent", "#knowledgeIncludeExamples"]) {
      const checkbox = $(selector);
      if (checkbox) checkbox.checked = false;
    }
    if ($("#documentPreviewDialog")?.open) $("#documentPreviewDialog").close();
    clearLeaderPhoto();
  }

  function countWords(str) {
    if (!str) return 0;
    const cjk = (str.match(/[\u4e00-\u9fa5]/g) || []).length;
    const en = (str.replace(/[\u4e00-\u9fa5]/g, " ").match(/[a-zA-Z0-9_-]+/g) || []).length;
    return cjk + en;
  }

  const labels = {
    problem: "要解决的问题",
    project_name: "项目名称",
    application_year: "申报年度",
    education_level: "学历阶段",
    college: "所在学院",
    political_stage: "党组织考察阶段",
    target_user: "服务对象 / 场景",
    method: "拟采用的方法",
    foundation: "现有基础 / 资源",
    goal: "目标",
    expected_output: "预期产出",
    constraints: "项目限制",
    budget: "预算",
    team: "团队",
    duration: "项目周期",
    leader_name: "负责人",
    leader_photo: "负责人照片",
    advisor_name: "指导教师",
  };

  function toast(message, isError = false) {
    const node = $("#toast");
    if (!node) return;
    node.textContent = message;
    node.classList.toggle("error", isError);
    node.classList.remove("hidden");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => node.classList.add("hidden"), 3800);
  }

  // Lightweight safe Markdown renderer for Stardust Agent message bubbles
  function renderMarkdownSafe(text) {
    if (!text) return "";
    let escaped = text.replace(/<\/?WritingBlock\b[^<>]*>/gi, "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    // Code blocks ```code```
    escaped = escaped.replace(/```([\s\S]*?)```/g, (match, code) => {
      return `<pre><code>${code.trim()}</code></pre>`;
    });

    // Inline code `code`
    escaped = escaped.replace(/`([^`]+)`/g, "<code>$1</code>");

    // Headers
    escaped = escaped.replace(/^### (.*$)/gim, "<h4>$1</h4>");
    escaped = escaped.replace(/^## (.*$)/gim, "<h3>$1</h3>");
    escaped = escaped.replace(/^# (.*$)/gim, "<h2>$1</h2>");

    // Bold & Italic
    escaped = escaped.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    escaped = escaped.replace(/\*([^*]+)\*/g, "<em>$1</em>");

    // Blockquote
    escaped = escaped.replace(/^\> (.*$)/gim, "<blockquote>$1</blockquote>");

    // Lists
    escaped = escaped.replace(/^\s*-\s+(.*$)/gim, "<li>$1</li>");
    escaped = escaped.replace(/^\s*\d+\.\s+(.*$)/gim, "<li>$1</li>");
    escaped = escaped.replace(/(<li>[\s\S]*?<\/li>)/g, "<ul>$1</ul>");
    escaped = escaped.replace(/<\/ul>\s*<ul>/g, "");

    // Paragraphs & Line breaks
    const paragraphs = escaped.split(/\n{2,}/).map(p => {
      p = p.trim();
      if (!p) return "";
      if (p.startsWith("<h") || p.startsWith("<ul") || p.startsWith("<pre") || p.startsWith("<blockquote")) {
        return p;
      }
      return `<p>${p.replace(/\n/g, "<br>")}</p>`;
    }).filter(Boolean);

    return paragraphs.join("");
  }

  async function api(url, options = {}) {
    const requestOptions = { ...options };
    const headers = new Headers(requestOptions.headers || {});
    if (state.authSession?.access_token) {
      headers.set("Authorization", `Bearer ${state.authSession.access_token}`);
    }
    const method = (requestOptions.method || "GET").toUpperCase();
    const targetUrl = new URL(url, window.location.href);
    const path = targetUrl.pathname;
    const targetProject = path.match(/^\/api\/projects\/([^/]+)/)?.[1];
    if (targetUrl.origin === new URL(window.location.href).origin
        && privateRagConsented() && (!targetProject || targetProject === state.project.id)) {
      headers.set("X-Private-RAG-Consent", "cloudflare-bge-m3-v1");
      headers.set("X-Private-RAG-Project", state.project.id);
    }
    const usesLlm = method === "POST" && /\/api\/projects\/[^/]+\/(?:chat|outline\/generate|sections\/[^/]+\/generate|diagrams\/propose|facts\/propose|intake\/propose|images\/generate)$/.test(path);
    if (usesLlm) {
      if (state.cloudMode && state.modelConfigSyncStatus === "loading")
        throw new Error("正在同步你的云端模型配置，请稍候再试。");
      if (state.cloudMode && state.modelConfigSyncStatus === "error")
        throw new Error("云端模型配置尚未同步，请在模型设置中重试同步或明确保存新配置。");
      let body = {};
      if (typeof requestOptions.body === "string" && requestOptions.body) {
        try { body = JSON.parse(requestOptions.body); } catch {}
      }
      body.llm_config = { ...state.llmConfig };
      headers.set("Content-Type", "application/json");
      requestOptions.body = JSON.stringify(body);
    }
    requestOptions.headers = headers;
    const response = await fetch(url, requestOptions);
    const type = response.headers.get("content-type") || "";
    const body = type.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const detail = typeof body === "object" ? body.detail || body.error : body;
      const error = new Error(detail || `请求失败 (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return body;
  }
  const originalApi = api;

  function privateRagConsented() {
    return Boolean(state.authConfig?.private_embedding_configured && state.project?.id
      && state.privateRagConsentProjectId === state.project.id
      && $("#privateRagConsent")?.checked);
  }

  function button(label, cls = "button button-small button-secondary") {
    const node = document.createElement("button");
    node.type = "button";
    node.className = cls;
    node.textContent = label;
    return node;
  }

  function updateTemplateExplain() {
    const has = $('input[name="hasTemplate"]:checked')?.value === "yes";
    $("#templateExplain").textContent = has
      ? "上传 Word 模板时会尽量参考原文件栏目；PDF 模板会参考栏目生成 Word，并提醒你核对。"
      : "没有模板时，Agent 会按所选赛道拟定通用结构，并提醒你提交前按学校要求核对。";
  }

  function renderPrivacyNotice(project = state.project) {
  }

  function showModelStatus(localStatus) {
    updateIntakeModelNote();
    if (typeof localStatus?.ollama_local === "boolean") {
      state.ollamaLocal = localStatus.ollama_local;
    }
    renderPrivacyNotice();
    const status = $("#modelStatus");
    if (state.llmConfig.provider === "preset") {
      status.textContent = "平台融合模型 · DeepSeek Flash + GPT Image";
      status.classList.remove("offline");
      status.classList.add("remote-warning");
      return;
    }
    if (state.llmConfig.provider === "openai_compatible") {
      const imgTag = state.llmConfig.image_model ? ` + ${state.llmConfig.image_model}` : "";
      status.textContent = `自定义 API · ${state.llmConfig.model || "未设置模型"}${imgTag}`;
      status.classList.remove("offline");
      status.classList.add("remote-warning");
      return;
    }
    if (!localStatus) {
      status.textContent = `本地模型 · ${state.llmConfig.model || "qwen2.5:7b"}（请测试连接）`;
      status.classList.remove("offline", "remote-warning");
      return;
    }
    const ready = localStatus.ollama_available && localStatus.ollama_model_installed;
    const model = state.llmConfig.model || localStatus.ollama_model;
    const selectedIsHealthModel = model === localStatus.ollama_model;
    status.textContent = ready && selectedIsHealthModel
      ? `本地引擎已连接 · ${model}`
      : localStatus.ollama_available && selectedIsHealthModel
        ? `缺少模型 · 请先 ollama pull ${model}`
        : localStatus.ollama_available
          ? `本地 Ollama · ${model}（请测试连接）`
      : !localStatus.ollama_available
        ? `Ollama 未启动 · ${model}`
        : `缺少模型 · 请先 ollama pull ${model}`;
    status.classList.toggle("offline", !localStatus.ollama_available || (selectedIsHealthModel && !ready));
    status.classList.remove("remote-warning");
  }

  function selectedLlmConfig() {
    const provider = $('input[name="llmProvider"]:checked')?.value || "preset";
    if (provider === "preset") {
      return {
        provider,
        model: "workbench-bundle",
        base_url: "",
        api_key: "",
      };
    }
    if (provider === "ollama") {
      return {
        provider,
        model: $("#localModelName").value.trim() || "qwen2.5:7b",
        base_url: "",
        api_key: "",
      };
    }
    return {
      provider,
      base_url: $("#customModelBase").value.trim(),
      model: $("#customModelName").value.trim(),
      api_key: $("#customModelKey").value.trim(),
      image_model: $("#customImageModel") ? $("#customImageModel").value.trim() : "",
      image_base_url: $("#customImageBase") ? $("#customImageBase").value.trim() : "",
      image_api_key: $("#customImageKey") ? $("#customImageKey").value.trim() : "",
      image_reuse_text: $("#customImageReuseText") ? Boolean($("#customImageReuseText").checked) : false,
    };
  }

  function syncModelSettingsForm() {
    const config = state.llmConfig;
    if ($("#providerPreset")) $("#providerPreset").checked = config.provider === "preset";
    $("#providerOllama").checked = config.provider === "ollama";
    $("#providerCustom").checked = config.provider === "openai_compatible";
    $("#localModelName").value = config.provider === "ollama" ? config.model : "qwen2.5:7b";
    $("#customModelBase").value = config.provider === "openai_compatible" ? config.base_url : "";
    $("#customModelName").value = config.provider === "openai_compatible" ? config.model : "";
    $("#customModelKey").value = config.provider === "openai_compatible" ? config.api_key : "";
    if ($("#customImageModel")) $("#customImageModel").value = config.image_model || "";
    if ($("#customImageBase")) $("#customImageBase").value = config.image_base_url || "";
    if ($("#customImageKey")) $("#customImageKey").value = config.image_api_key || "";
    if ($("#customImageReuseText")) {
      $("#customImageReuseText").checked = Boolean(config.image_reuse_text);
      $("#customImageSeparateFields")?.classList.toggle("hidden", Boolean(config.image_reuse_text));
    }
    updateModelFields();
    $("#modelConfigFeedback").textContent = "";
    $("#modelConfigFeedback").className = "model-config-feedback";
  }

  function updateModelFields() {
    const provider = $('input[name="llmProvider"]:checked')?.value || "preset";
    const isPreset = provider === "preset";
    const isOllama = provider === "ollama";
    const custom = provider === "openai_compatible";
    if ($("#presetModelFields")) $("#presetModelFields").classList.toggle("hidden", !isPreset);
    $("#localModelFields").classList.toggle("hidden", !isOllama);
    $("#customModelFields").classList.toggle("hidden", !custom);
    const canImage = isPreset || custom;
    $("#workbenchImageControls")?.classList.toggle("hidden", !canImage);
  }

  function storeLlmConfig(config) {
    state.llmConfig = config;
    try {
      if (state.cloudMode) sessionStorage.removeItem(llmConfigKey);
      else sessionStorage.setItem(llmConfigKey, JSON.stringify(config));
    } catch {
      toast("浏览器无法保存本次模型配置；本次页面仍可继续使用。", true);
    }
    showModelStatus(null);
  }

  async function testSelectedLlmConfig() {
    const config = selectedLlmConfig();
    const feedback = $("#modelConfigFeedback");
    const testButton = $("#testModelConfigBtn");
    feedback.textContent = "正在测试连接…";
    feedback.className = "model-config-feedback";
    testButton.disabled = true;
    try {
      const result = await api("/api/model-config/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      feedback.textContent = `连接成功：${result.model}`;
      feedback.className = "model-config-feedback success";
    } catch (error) {
      feedback.textContent = error.message;
      feedback.className = "model-config-feedback error";
    } finally {
      testButton.disabled = false;
    }
  }

  let modelConfigRevision = 0;

  async function loadCloudModelConfig() {
    const account = state.authSession?.user?.id;
    if (!state.cloudMode || !account) return;
    const revision = ++modelConfigRevision;
    state.modelConfigSyncStatus = "loading";
    try {
      const result = await api("/api/user/llm-config");
      if (revision !== modelConfigRevision || account !== state.authSession?.user?.id) return;
      storeLlmConfig(result.config || {
        provider: "preset", model: "workbench-bundle", base_url: "", api_key: "",
      });
      state.modelConfigSyncStatus = "ready";
    } catch {
      if (revision === modelConfigRevision && account === state.authSession?.user?.id) {
        state.modelConfigSyncStatus = "error";
        toast("云端模型配置读取失败，可在模型设置中点击“同步云端配置”重试。", true);
      }
    }
  }

  async function saveSelectedLlmConfig() {
    const config = selectedLlmConfig();
    if (config.provider === "preset" && !state.presetModels.some((model) => model.id === "workbench-bundle" && model.available)) {
      $("#modelConfigFeedback").textContent = "平台融合模型尚未接通，请先使用自带 Key 或本地模型。";
      $("#modelConfigFeedback").className = "model-config-feedback error";
      return;
    }
    if (config.provider === "openai_compatible" && (!config.base_url || !config.model || !config.api_key)) {
      $("#modelConfigFeedback").textContent = "请填写文本 API 地址、模型名称和 API Key。";
      $("#modelConfigFeedback").className = "model-config-feedback error";
      return;
    }
    const account = state.authSession?.user?.id;
    const revision = ++modelConfigRevision;
    const saveButton = $("#saveModelConfigBtn");
    saveButton.disabled = true;
    try {
      if (state.cloudMode) {
        if (!account) throw new Error("请先登录。");
        await api("/api/user/llm-config", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(config),
        });
      }
      if (revision !== modelConfigRevision || account !== state.authSession?.user?.id) return;
      storeLlmConfig(config);
      state.modelConfigSyncStatus = "ready";
    } catch {
      $("#modelConfigFeedback").textContent = "保存失败，原配置未更改，请重试。";
      $("#modelConfigFeedback").className = "model-config-feedback error";
      return;
    } finally {
      saveButton.disabled = false;
    }
    $("#modelSettingsDialog").close();
    const provName = config.provider === "preset" ? "平台融合模型包" : config.provider === "ollama" ? "本地模型" : "自定义 API";
    const imgInfo = config.image_model ? ` · 生图: ${config.image_model}` : "";
    toast(`已切换为${provName}：${config.model}${imgInfo}`);
  }

  async function deleteCloudModelConfig() {
    const account = state.authSession?.user?.id;
    if (!state.cloudMode || !account) return;
    if (!confirm("删除此账号云端保存的模型配置？其他设备下次同步后也会清除。")) return;
    const revision = ++modelConfigRevision;
    try {
      await api("/api/user/llm-config", { method: "DELETE" });
      if (revision !== modelConfigRevision || account !== state.authSession?.user?.id) return;
      storeLlmConfig({ provider: "preset", model: "workbench-bundle", api_key: "", base_url: "" });
      state.modelConfigSyncStatus = "ready";
      syncModelSettingsForm();
      toast("云端配置已删除；其他设备下次同步时生效。");
    } catch {
      $("#modelConfigFeedback").textContent = "删除失败，云端原配置未更改，请重试。";
    }
  }

  async function loadUserBalance() {
    const requestSeq = ++state.balanceRequestSeq;
    const accountId = state.authSession?.user?.id || null;
    if (state.cloudMode && !accountId) return;
    try {
      const data = await api("/api/user/balance");
      if (requestSeq !== state.balanceRequestSeq) return;
      if (state.cloudMode && (accountId !== state.authSession?.user?.id || data.user_id !== accountId)) return;
      state.userCredits = data.credits;
      state.freeChatsRemaining = Number(data.free_chats_remaining || 0);
      if ($("#userCreditsValue")) $("#userCreditsValue").textContent = data.credits;
      if ($("#rechargeModalCredits")) $("#rechargeModalCredits").textContent = data.credits;
      if ($("#presetDialogCredits")) $("#presetDialogCredits").textContent = data.credits;
      renderFreeChatsIndicator();
      renderCreditTransactions(data.transactions || []);
    } catch (e) {
      console.warn("加载用户积分失败", e);
      if (requestSeq === state.balanceRequestSeq && state.userCredits === null) {
        if ($("#userCreditsValue")) $("#userCreditsValue").textContent = "加载失败";
        if ($("#rechargeModalCredits")) $("#rechargeModalCredits").textContent = "加载失败";
      }
    }
  }

  function openRechargeDialog() {
    $("#rechargeDialog")?.showModal();
    void loadUserBalance();
  }

  function renderFreeChatsIndicator() {
    const badge = $("#freeChatsTopbarBadge");
    const hint = $("#freeChatHint");
    const countNum = $("#freeChatCountNum");
    const remaining = Number(state.freeChatsRemaining || 0);
    if (remaining > 0) {
      if (badge) {
        badge.textContent = `🎁 免费对话剩 ${remaining} 次`;
        badge.classList.remove("hidden");
      }
      if (hint) {
        if (countNum) countNum.textContent = String(remaining);
        hint.classList.remove("hidden");
      }
    } else {
      if (badge) badge.classList.add("hidden");
      if (hint) hint.classList.add("hidden");
    }
  }

  function renderCreditTransactions(transactions) {
    const container = $("#creditTransactions");
    if (!container) return;
    container.replaceChildren();
    if (!transactions.length) {
      const empty = document.createElement("p");
      empty.className = "balance-sub";
      empty.textContent = "暂无积分流水";
      container.appendChild(empty);
      return;
    }
    transactions.slice(0, 10).forEach((tx) => {
      const row = document.createElement("div");
      row.className = "credit-transaction-row";
      const label = document.createElement("span");
      label.textContent = tx.kind === "card_redeem" ? "卡密充值"
        : tx.kind === "usage_reserve" ? `模型调用 · ${tx.model || "平台模型"}`
          : tx.kind === "admin_adjustment" ? "异常补偿 / 人工调整"
          : tx.kind === "usage_refund" || tx.kind === "usage_settle" ? "用量结算"
            : "积分变动";
      const amount = document.createElement("strong");
      const delta = Number(tx.delta_micro_cny || 0) / MICRO_CNY_PER_CREDIT;
      amount.textContent = `${delta > 0 ? "+" : ""}${delta.toFixed(4)} 点`;
      amount.classList.toggle("credit-positive", delta > 0);
      const time = document.createElement("time");
      time.textContent = (tx.created_at || "").replace("T", " ").slice(0, 16);
      row.append(label, amount, time);
      container.appendChild(row);
    });
  }

  async function openUsageDialog() {
    try {
      const data = await api("/api/admin/usage-details");
      const stats = data.stats || {};
      const logs = data.logs || [];

      if ($("#statCallCount")) $("#statCallCount").textContent = (stats.call_count || 0).toLocaleString();
      if ($("#statGrandTokens")) $("#statGrandTokens").textContent = (stats.grand_total_tokens || 0).toLocaleString();
      if ($("#statAvgLatency")) $("#statAvgLatency").textContent = `${Math.round(stats.avg_latency_ms || 0)} ms`;
      if ($("#statEstimatedCost")) {
        $("#statEstimatedCost").textContent = data.billing_status === "provider_cost_reconciled"
          ? `¥${Number(data.actual_provider_cost || 0).toFixed(2)}`
          : "待核对";
      }

      // 模型分布标签
      const tagsContainer = $("#usageModelTags");
      if (tagsContainer) {
        tagsContainer.innerHTML = "";
        const byModel = stats.by_model || [];
        if (byModel.length === 0) {
          tagsContainer.innerHTML = '<span class="text-muted" style="font-size:12px;">暂无模型调用数据</span>';
        } else {
          byModel.forEach((bm) => {
            const tag = document.createElement("span");
            tag.className = "usage-tag";
            tag.textContent = `${bm.model || "未知"} · ${bm.count} 次 (${Number(bm.tokens || 0).toLocaleString()} tokens)`;
            tagsContainer.appendChild(tag);
          });
        }
      }

      // 流水表格
      const tbody = $("#usageTableBody");
      if (tbody) {
        tbody.innerHTML = "";
        if (logs.length === 0) {
          tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted">暂无调用明细</td></tr>';
        } else {
          logs.forEach((log) => {
            const tr = document.createElement("tr");
            const timeStr = (log.created_at || "").replace("T", " ").slice(5, 19);
            for (const value of [timeStr, log.project_title || log.project_id || "全局",
              log.task_type || "chat", log.model || "未知", Number(log.prompt_tokens || 0).toLocaleString(),
              Number(log.completion_tokens || 0).toLocaleString(), Number(log.total_tokens || 0).toLocaleString(),
              `${log.latency_ms || 0} ms`]) {
              const cell = document.createElement("td");
              cell.textContent = value;
              tr.append(cell);
            }
            tbody.appendChild(tr);
          });
        }
      }

      $("#usageDialog")?.showModal();
    } catch (e) {
      toast(e.message || "加载用量明细失败", true);
    }
  }

  let currentOnboardingStep = 1;

  function setOnboardingStep(step) {
    currentOnboardingStep = Math.max(1, Math.min(4, step));
    $$(".onboarding-step-tab").forEach((tab) => {
      const tabStep = parseInt(tab.dataset.step, 10);
      tab.classList.toggle("active", tabStep === currentOnboardingStep);
    });
    $$(".onboarding-step-content").forEach((content) => {
      const contentStep = parseInt(content.dataset.step, 10);
      content.classList.toggle("hidden", contentStep !== currentOnboardingStep);
    });

    const prevBtn = $("#prevOnboardingBtn");
    const nextBtn = $("#nextOnboardingBtn");
    const finishBtn = $("#finishOnboardingBtn");

    if (prevBtn) prevBtn.classList.toggle("hidden", currentOnboardingStep === 1);
    if (nextBtn) {
      nextBtn.classList.toggle("hidden", currentOnboardingStep === 4);
      nextBtn.textContent = `下一步 (${currentOnboardingStep}/4)`;
    }
    if (finishBtn) {
      finishBtn.classList.toggle("hidden", currentOnboardingStep !== 4);
    }
  }

  async function showChatRuns(offset = 0) {
    const view = currentProjectView();
    if (!view.active()) return;
    $("#chatActionMenu")?.classList.add("hidden");
    const dialog = $("#chatRunsDialog");
    if (!dialog.open) dialog.showModal();
    const page = document.createElement("div");
    $("#chatRunsList").replaceChildren(page);
    const active = () => view.active() && dialog.open && page.isConnected;
    const url = `/api/projects/${view.id}/chat-runs`;
    const labels = { running: "执行中", suspended: "已暂停", interrupted: "执行中断",
      completed: "已完成", needs_review: "调用结果待核对" };
    page.textContent = "正在读取任务…";
    try {
      const { runs } = await api(`${url}?offset=${offset}`);
      if (!active()) return;
      page.textContent = runs.length ? "" : "本页没有对话任务。";
      for (const run of runs) {
        const entry = document.createElement("details");
        const label = document.createElement("summary");
        label.textContent = `${labels[run.status] || run.status} · ${new Date(run.created_at * 1000).toLocaleString()} · ${run.preview}`;
        const body = document.createElement("div");
        entry.append(label, body);
        page.append(entry);
        entry.addEventListener("toggle", async () => {
          if (!entry.open || !active()) return;
          body.textContent = "读取中…";
          try {
            const detail = await api(`${url}/${encodeURIComponent(run.id)}`);
            if (!active()) return;
            body.textContent = [detail.message, detail.answer ?? labels[detail.status]].join("\n\n");
            if (["suspended", "interrupted"].includes(detail.status)) {
              const resume = document.createElement("button");
              resume.className = "button button-small";
              resume.textContent = "继续原任务";
              resume.onclick = () => {
                if (!active()) return;
                dialog.close();
                sendChat(null, detail.message, detail.id);
              };
              body.append(document.createElement("br"), resume);
            }
          } catch (error) {
            if (active()) body.textContent = error.message;
          }
        });
      }
      for (const [label, next] of [["上一页", offset - 20], ["下一页", offset + 20]]) {
        if (next < 0 || (next > offset && runs.length < 20)) continue;
        const button = document.createElement("button");
        button.className = "button button-small";
        button.textContent = label;
        button.onclick = () => showChatRuns(next);
        page.append(button);
      }
    } catch (error) {
      if (active()) page.textContent = error.message;
    }
  }

  function openOnboardingDialog() {
    setOnboardingStep(1);
    $("#onboardingDialog")?.showModal();
  }

  function closeOnboardingDialog() {
    if ($("#dontShowOnboardingAgain")?.checked) {
      try {
        localStorage.setItem("stardust_onboarding_dismissed", "true");
      } catch {}
    }
    $("#onboardingDialog")?.close();
  }

  function checkInitialOnboarding() {
    try {
      if (!localStorage.getItem("stardust_onboarding_dismissed")) {
        openOnboardingDialog();
      }
    } catch {}
  }

  async function loadPresetModelsAndPlans() {
    const cached = readPublicCache("presets");
    if (cached) { state.presetModels = cached; renderPresetModelCards(); }
    try {
      const data = await api("/api/models/preset");
      state.presetModels = data.models || [];
      writePublicCache("presets", state.presetModels);
      renderPresetModelCards();
    } catch (e) {
      console.warn("加载模型及套餐失败", e);
    }
  }

  function renderPresetModelCards() {
    const container = $("#presetModelCards");
    if (!container) return;
    container.replaceChildren();
    const bundle = state.presetModels.find((model) => model.id === "workbench-bundle");
    const card = document.createElement("div");
    card.className = `preset-model-card active${bundle?.available ? "" : " unavailable"}`;
    const top = document.createElement("div");
    top.className = "preset-card-top";
    const title = document.createElement("span");
    title.className = "preset-card-title";
    title.textContent = "DeepSeek Flash 写作 + GPT Image 生图";
    const cost = document.createElement("span");
    cost.className = "preset-card-cost";
    cost.textContent = bundle?.available ? "按实际用量计费" : "尚未开通";
    top.append(title, cost);
    const description = document.createElement("div");
    description.className = "preset-card-desc";
    description.textContent = bundle?.available
      ? "Agent 会按任务自动路由两种能力，统一从当前账户积分扣费。"
      : "服务账户、卡密核验与成本结算尚未全部接通；当前不会调用或扣费。";
    card.append(top, description);
    container.appendChild(card);
  }

  async function handleRedeemCode() {
    const input = $("#redeemCodeInput");
    const code = input ? input.value.trim() : "";
    if (!code) {
      toast("请输入有效的兑换卡密", true);
      return;
    }
    const btn = $("#submitRedeemBtn");
    btn.disabled = true;
    try {
      const res = await api("/api/pay/redeem", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      if (res.pending) {
        toast(res.message || "兑换正在核对，请勿重复提交。", true);
        return;
      }
      toast(res.message || "兑换成功！");
      if (input) input.value = "";
      await loadUserBalance();
    } catch (e) {
      toast(e.message || "兑换失败", true);
    } finally {
      btn.disabled = false;
    }
  }

  async function makePilotFingerprint() {
    const code = $("#redeemCodeInput").value.trim();
    if (!code) return toast("请先粘贴已购买的卡密", true);
    try {
      const result = await api("/api/pay/card-fingerprint", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      $("#pilotFingerprintOutput").value = result.fingerprint;
      toast("已生成核对编号；这一步未兑换、未消耗卡密。");
    } catch (error) {
      toast(error.message, true);
    }
  }

  function updateTurnstileState() {
    const slot = $("#turnstileWidget");
    if (!slot) return;
    const isSignup = state.authMode === "signup";
    if (!isSignup || !state.turnstileSiteKey) {
      slot.classList.add("hidden");
      return;
    }
    slot.classList.remove("hidden");
    const renderWidget = () => {
      if (!window.turnstile || state.turnstileWidgetId !== null) return;
      try {
        state.turnstileWidgetId = window.turnstile.render("#turnstileWidget", {
          sitekey: state.turnstileSiteKey,
          callback: (token) => {
            state.turnstileToken = token;
            const feedback = $("#authFeedback");
            if (feedback && feedback.textContent === "请先完成人机安全验证。") {
              feedback.textContent = "";
            }
          },
          "expired-callback": () => {
            state.turnstileToken = "";
          },
          "error-callback": () => {
            state.turnstileToken = "";
          },
          theme: "auto",
          ...(mobileLayout?.matches && window.innerWidth < 360 ? { size: "compact" } : {}),
        });
      } catch (err) {
        console.warn("Turnstile render error:", err);
      }
    };
    if (window.turnstile) {
      renderWidget();
    } else {
      let attempts = 0;
      const timer = setInterval(() => {
        attempts++;
        if (window.turnstile) {
          clearInterval(timer);
          renderWidget();
        } else if (attempts > 20) {
          clearInterval(timer);
        }
      }, 250);
    }
  }

  function syncAuthControls() {
    const ready = state.authStatus === "ready" && Boolean(state.authClient?.auth);
    const submit = $("#authSubmitBtn");
    submit.disabled = !ready || state.authBusy;
    submit.textContent = state.authBusy
      ? state.authMode === "signup" ? "正在创建账号…" : "正在登录…"
      : ready ? state.authMode === "signup" ? "创建账号" : "登录"
      : state.authStatus === "loading" ? "正在加载登录组件…" : "登录服务暂不可用";
    $("#authModeToggle").disabled = !ready || state.authBusy;
    $("#retryAuthBtn").classList.toggle("hidden", state.authStatus !== "error");
  }

  function showAuthGate(message = "") {
    $("#intakeCandidates")?.replaceChildren();
    $("#intakeArchive")?.replaceChildren();
    state.appStarted = false;
    $("#appShell").classList.add("hidden");
    $("#authGate").classList.remove("hidden");
    $("#signOutBtn").classList.add("hidden");
    $("#authFeedback").textContent = message;
    syncAuthControls();
    updateTurnstileState();
  }

  async function startApplication(session = null) {
    if (state.appStarted) return;
    state.authSession = session;
    state.appStarted = true;
    $("#authGate").classList.add("hidden");
    $("#appShell").classList.remove("hidden");
    $("#signOutBtn").classList.toggle("hidden", !state.cloudMode);
    const accountEpoch = chatRecoveryEpoch;
    const initialNavigation = projectLoadRevision;
    const activeSession = () => state.appStarted && chatRecoveryEpoch === accountEpoch
      && session?.user?.id === state.authSession?.user?.id;
    await loadCloudModelConfig();
    if (!activeSession()) return;
    await loadUserBalance();
    if (!activeSession()) return;
    let [, projects] = await Promise.all([checkHealth(), refreshProjects()]);
    if (projects === null && activeSession() && projectLoadRevision === initialNavigation) {
      projects = await refreshProjects();
      if (projects === null && activeSession()) toast("项目列表暂时无法加载，请刷新后重试。", true);
    }
    if (!activeSession()) return;
    if (projects && projectLoadRevision === initialNavigation) {
      if (projects.length) {
        let lastId;
        try { lastId = localStorage.getItem(activeProjectKey); } catch {}
        const target = projects.find(project => project.id === lastId) || projects[0];
        await loadProject(target.id, { refreshList: false });
      } else {
        showWelcomeView();
      }
    }
    if (activeSession()) checkInitialOnboarding();
  }

  async function initializeCloudAuth(config) {
    if (state.authInitializing) return;
    state.authConfig = config;
    state.cloudMode = Boolean(config.cloud_mode);
    renderPrivacyNotice();
    if (!state.cloudMode) {
      await startApplication();
      return;
    }
    // 未验证用户前不使用标签页遗留的凭据，避免换账号继承。
    state.llmConfig = { provider: "preset", model: "workbench-bundle", base_url: "", api_key: "" };
    try { sessionStorage.removeItem(llmConfigKey); } catch {}
    const localBadge = $(".sidebar-footer .local-badge");
    if (localBadge) localBadge.textContent = "云端同步 · 安全存储";
    const sidebarNote = $(".sidebar-footer p");
    if (sidebarNote) sidebarNote.textContent = "项目与材料已安全同步，可根据需要随时导出为 Word。";
    const modelIntro = $(".model-settings-intro");
    if (modelIntro) {
      modelIntro.textContent = config.preset_enabled
        ? "默认使用平台融合模型，按当前账号积分结算；也可选择自带 Key。"
        : "通过用户配置的 OpenAI 兼容 API 推理；模型服务费用由你与服务商直接结算。";
    }
    if (!window.supabase?.createClient) {
      state.authClient = null;
      state.authStatus = "error";
      showAuthGate("登录组件加载失败，请点击“重新连接登录服务”；无需更换邮箱或密码。");
      return;
    }
    if (config.preset_enabled) {
      // Preserve an intentional BYOK choice for this tab; otherwise the
      // platform bundle is the zero-configuration default.
      if (state.llmConfig.provider !== "openai_compatible" || !state.llmConfig.api_key) {
        storeLlmConfig({ provider: "preset", model: "workbench-bundle", base_url: "", api_key: "" });
      }
    } else {
      state.llmConfig = { provider: "openai_compatible", model: "", base_url: "", api_key: "" };
    }
    $("#providerOllama").closest(".model-provider-option")?.classList.add("hidden");
    state.authInitializing = true;
    state.authStatus = "loading";
    state.authClient = null;
    state.authSubscription?.unsubscribe();
    state.authSubscription = null;
    syncAuthControls();
    let data;
    try {
      const client = window.supabase.createClient(
        config.supabase_url,
        config.supabase_publishable_key,
        {
          auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
          global: { fetch: sameOriginAuthFetch(config.supabase_url) },
        }
      );
      const result = await client.auth.getSession();
      if (result.error) throw result.error;
      data = result.data;
      state.authClient = client;
      state.authStatus = "ready";
    } catch {
      state.authStatus = "error";
      showAuthGate("无法初始化登录服务，请点击“重新连接登录服务”或稍后重试。");
      return;
    } finally {
      state.authInitializing = false;
      syncAuthControls();
    }
    const subscription = state.authClient.auth.onAuthStateChange((event, session) => {
      if (state.authSession?.user?.id && session?.user?.id
          && state.authSession.user.id !== session.user.id) {
        invalidateProjectLoad();
        clearInterruptedChatMessages();
        state.privateRagConsentProjectId = null;
        if ($("#privateRagConsent")) $("#privateRagConsent").checked = false;
        clearExportArtifacts();
        state.project = null;
        state.messages = [];
        state.documents = [];
        showWelcomeView();
        modelConfigRevision++;
        state.appStarted = false;
        state.llmConfig = { provider: "preset", model: "workbench-bundle", api_key: "", base_url: "" };
      }
      state.authSession = session;
      if (session && !state.appStarted) {
        // 不在 Auth 回调锁内调用 SDK，避免 setSession/getSession 死锁。
        const epoch = chatRecoveryEpoch;
        setTimeout(() => {
          if (epoch === chatRecoveryEpoch && state.authSession?.user?.id === session.user.id)
            void startApplication(state.authSession);
        }, 0);
      } else if (!session && event === "SIGNED_OUT") {
        invalidateProjectLoad();
        clearInterruptedChatMessages();
        state.privateRagConsentProjectId = null;
        if ($("#privateRagConsent")) $("#privateRagConsent").checked = false;
        state.appStarted = false;
        state.userCredits = null;
        state.balanceRequestSeq++;
        if ($("#userCreditsValue")) $("#userCreditsValue").textContent = "—";
        if ($("#rechargeModalCredits")) $("#rechargeModalCredits").textContent = "—";
        clearExportArtifacts();
        state.project = null;
        state.messages = [];
        state.documents = [];
        modelConfigRevision++;
        try { sessionStorage.removeItem(llmConfigKey); } catch {}
        state.llmConfig = { provider: "openai_compatible", model: "", base_url: "", api_key: "" };
        $("#customModelKey").value = "";
        $("#customImageKey").value = "";
        $("#modelSettingsDialog").close();
        showAuthGate("已退出登录。");
      }
    });
    state.authSubscription = subscription.data.subscription;
    if (data.session) {
      await startApplication(data.session);
    } else {
      showAuthGate();
    }
  }

  async function submitAuthForm(event) {
    event.preventDefault();
    const feedback = $("#authFeedback");
    if (state.authBusy) return;
    if (state.authStatus !== "ready" || !state.authClient?.auth) {
      feedback.className = "auth-feedback error";
      feedback.textContent = state.authStatus === "loading"
        ? "登录组件正在初始化，请稍候；当前尚未发送注册或登录请求。"
        : "登录服务尚未就绪，请点击“重新连接登录服务”；无需更换邮箱或密码。";
      syncAuthControls();
      return;
    }
    const authClient = state.authClient;
    const email = $("#authEmail").value.trim().toLowerCase();
    const password = $("#authPassword").value;

    if (state.authMode === "signup") {
      const domain = (email.split("@")[1] || "").toLowerCase();
      const allowedDomains = ["qq.com", "foxmail.com", "163.com", "126.com", "sina.com", "gmail.com", "outlook.com", "hotmail.com"];
      const isEdu = domain === "edu.cn" || domain.endsWith(".edu.cn");
      if (!allowedDomains.includes(domain) && !isEdu) {
        feedback.className = "auth-feedback error";
        feedback.textContent = "为保障服务稳定，注册仅支持 QQ邮箱、163/126、高校教育网 (edu.cn)、Gmail、Outlook 等正规邮箱。";
        return;
      }
    }

    if (state.authMode === "signup" && state.turnstileSiteKey) {
      if (!state.turnstileToken) {
        feedback.className = "auth-feedback error";
        feedback.textContent = "请先完成人机安全验证。";
        return;
      }
    }

    state.authBusy = true;
    syncAuthControls();
    feedback.className = "auth-feedback";
    feedback.textContent = state.authMode === "signup" ? "正在创建账号…" : "正在登录…";
    try {
      const signupOptions = {
        emailRedirectTo: window.location.origin,
      };
      if (state.turnstileToken) {
        signupOptions.captchaToken = state.turnstileToken;
      }

      // SDK 的全部 Auth 请求已由 global.fetch 定向到同源代理，保留官方会话管理。
      const result = state.authMode === "signup"
        ? await authClient.auth.signUp({ email, password, options: signupOptions })
        : await authClient.auth.signInWithPassword({ email, password });
      if (result.error) throw result.error;
      if (result.data.session) {
        await startApplication(result.data.session);
        await loadUserBalance();
      } else if (state.authMode === "signup") {
        feedback.textContent = "账号已创建。请查收邮箱并完成验证后登录。";
        feedback.classList.add("success");
      } else {
        feedback.textContent = "登录成功，正在载入你的项目…";
      }
    } catch (error) {
      const message = error.message || "";
      feedback.textContent = /Cannot read properties|undefined|Failed to fetch|NetworkError|fetch failed/i.test(message)
        ? "无法连接登录服务，请稍后重试或检查网络。"
        : /invalid login credentials/i.test(message) ? "邮箱或密码错误，请重新核对。"
        : /email not confirmed/i.test(message) ? "请先完成邮箱验证后登录。"
        : message || "登录失败，请检查邮箱和密码。";
      feedback.classList.add("error");
    } finally {
      if (window.turnstile && state.turnstileWidgetId !== null && state.authMode === "signup") {
        try {
          window.turnstile.reset(state.turnstileWidgetId);
        } catch (_) {}
        state.turnstileToken = "";
      }
      state.authBusy = false;
      syncAuthControls();
    }
  }

  async function signOutCloudUser() {
    if (!state.authClient) return;
    const { error } = await state.authClient.auth.signOut();
    if (error) toast("退出失败：" + error.message, true);
    else {
      state.privateRagConsentProjectId = null;
      if ($("#privateRagConsent")) $("#privateRagConsent").checked = false;
    }
  }

  function renderProjectList(projects, filterKeyword = "") {
    const list = $("#projectList");
    const countBadge = $("#projectCountBadge");
    if (countBadge) countBadge.textContent = state.projectsList.length;
    list.replaceChildren();

    const query = filterKeyword.trim().toLowerCase();
    const filtered = query
      ? projects.filter(p => {
          const title = (p.title || "").toLowerCase();
          const skill = (p.state?.skill_name || "").toLowerCase();
          return title.includes(query) || skill.includes(query);
        })
      : projects;

    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "project-list-empty";
      empty.textContent = query ? "未找到匹配的项目" : "暂无归档项目";
      list.append(empty);
      return;
    }

    for (const project of filtered) {
      const item = document.createElement("button");
      item.className = `project-item ${state.project?.id === project.id ? "active" : ""}`;

      const body = document.createElement("div");
      body.className = "proj-body";
      const titleRow = document.createElement("div");
      titleRow.className = "proj-title-row";
      const icon = document.createElement("span");
      icon.className = "proj-icon";
      const category = project.state?.skill_category || "";
      icon.textContent = category === "scholarship" ? "🎖️" : category === "practice" ? "🌾" : category === "competition" ? "🏆" : "📄";
      const name = document.createElement("span");
      name.className = "proj-name";
      name.textContent = project.title || "未命名项目";
      titleRow.append(icon, name);

      const tag = document.createElement("span");
      tag.className = `proj-skill-tag cat-${category || "other"}`;
      tag.textContent = project.state?.skill_name || "申报材料";
      body.append(titleRow, tag);

      const delBtn = document.createElement("span");
      delBtn.className = "proj-del-icon";
      delBtn.innerHTML = "×";
      delBtn.title = "删除此项目";
      delBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`确定删除项目“${project.title}”吗？${state.cloudMode ? "历史加密备份可能长期保留已删除的数据。" : ""}`)) return;
        try {
          await api(`/api/projects/${project.id}`, { method: "DELETE" });
          closeDeletedProject(project.id);
          await refreshProjects();
          toast("项目已删除。");
        } catch (err) {
          toast(err.message, true);
        }
      });

      item.title = `${project.title || "项目"} (${project.state?.skill_name || "申报材料"})`;
      item.append(body, delBtn);
      item.dataset.id = project.id;
      item.addEventListener("click", () => {
        $$(".project-item").forEach(el => el.classList.remove("active"));
        item.classList.add("active");
        loadProject(project.id);
      });
      list.append(item);
    }
  }

  async function refreshProjects() {
    const revision = ++projectListRevision;
    try {
      const response = await api("/api/projects");
      if (revision !== projectListRevision) return;
      state.projectsList = response.projects || [];
      renderProjectList(state.projectsList, $("#projectSearchInput")?.value || "");
      return state.projectsList;
    } catch (e) {
      if (revision !== projectListRevision) return;
      console.warn("刷新项目列表失败", e);
      return null;
    }
  }
  async function checkHealth() {
    try {
      showModelStatus(await api("/api/health"));
    } catch {
      if (state.llmConfig.provider === "openai_compatible") showModelStatus(null);
      else {
        $("#modelStatus").textContent = "Agent 服务暂不可用";
        $("#modelStatus").classList.add("offline");
      }
    }
  }

  function showProject() {
    $("#welcome").classList.add("hidden");
    $("#createPanel").classList.add("hidden");
    $("#workspace").classList.remove("hidden");
    $("#crumbProject").textContent = state.project?.title || "项目";
    const skillBadge = $("#crumbSkillBadge");
    if (skillBadge) {
      skillBadge.textContent = state.project?.state?.skill_name || "通用材料";
      skillBadge.classList.remove("hidden");
    }
    $("#quickExportTopBtn")?.classList.remove("hidden");
  }

  function showWelcomeView() {
    $("#workspace").classList.add("hidden");
    $("#createPanel").classList.add("hidden");
    $("#welcome").classList.remove("hidden");
    $("#crumbProject").textContent = "欢迎";
    $("#crumbSkillBadge")?.classList.add("hidden");
    $("#quickExportTopBtn")?.classList.add("hidden");
    $$(".project-item").forEach(item => item.classList.remove("active"));
    if (mobileLayout?.matches) window.scrollTo(0, 0);
  }

  function closeDeletedProject(projectId) {
    loadInterruptedChatMessages();
    interruptedChatMessages.delete(projectId);
    recoveredChatInputs.delete(projectId);
    persistInterruptedChatMessages();
    deletedProjectEpochs.set(projectId, (deletedProjectEpochs.get(projectId) || 0) + 1);
    sectionEdits.removeProject(projectId);
    factEdits.removeProject(projectId);
    const keyPrefix = `${projectId}\0`;
    for (const key of documentCorrections.keys()) if (key.startsWith(keyPrefix)) documentCorrections.delete(key);
    for (const key of documentReviews.keys()) if (key.startsWith(keyPrefix)) documentReviews.delete(key);
    documentRecovery.clear(keyPrefix);
    clearWordExportRecovery(projectId);
    window.MaterialPilot?.clearRecovery?.(projectId);
    if (factFormProjectId === projectId) factFormProjectId = null;
    outlineDrafts.delete(projectId);
    outlinePreviews.delete(projectId);
    outlineRequests.delete(projectId);
    outlineRecovery.clear(`${projectId}\0`);
    for (const key of uncertainSectionDrafts.keys()) {
      if (key.startsWith(`${projectId}\0`)) uncertainSectionDrafts.delete(key);
    }
    for (const key of interruptedMessagesRendered) {
      if (key.startsWith(`${projectId}\0`)) interruptedMessagesRendered.delete(key);
    }
    // Even an unopened project can be deleted from the sidebar during its first load.
    if (loadingProjectId === projectId || (!loadingProjectId && state.project?.id === projectId)) invalidateProjectLoad();
    if (state.project?.id !== projectId) return;
    clearProjectResults();
    const input = $("#chatInput");
    if (input) {
      input.value = "";
      if (input.style) input.style.height = "auto";
    }
    clearExportArtifacts();
    state.project = null;
    showWelcomeView();
  }

  async function loadProject(projectId, { refreshList = true } = {}) {
    const revision = ++projectLoadRevision;
    loadingProjectId = projectId;
    try {
      $$(".project-item").forEach(el => {
        el.classList.toggle("active", el.dataset.id === projectId);
      });
      const project = await api(`/api/projects/${projectId}`);
      if (revision !== projectLoadRevision) return;
      if (state.project?.id === projectId && project.material_revision < state.project.material_revision) return;
      if (state.project?.id !== projectId) {
        clearProjectResults();
        const input = $("#chatInput");
        const previousProjectId = state.project?.id;
        if (previousProjectId && input?.value?.trim()) {
          updateRecoveredChatInput();
          rememberInterruptedChatMessage(previousProjectId, input.value.trim());
          recoveredChatInputs.delete(previousProjectId);
          input.value = "";
          if (input.style) input.style.height = "auto";
        }
        state.privateRagConsentProjectId = null;
        if ($("#privateRagConsent")) $("#privateRagConsent").checked = false;
        $("#privateRagResults")?.replaceChildren();
        $("#uploadResult").classList.add("hidden");
        clearExportArtifacts({ preserveRecovery: true });
        if (mobileLayout?.matches) showMobileWorkspaceView("chat");
      }
      state.project = project;
      syncWordExportButtons();
      try { localStorage.setItem(activeProjectKey, projectId); } catch {}
      state.documents = project.documents || [];
      state.messages = project.messages || [];
      restoreOutlineEditor(project);
      state.diagrams = project.diagrams || [];
      showProject();
      configureSkillWorkspace(project);
      $("#projectHeading").textContent = project.title;
      $("#projectSkillName").textContent = project.state?.skill_name || "申报材料工作区";
      $("#projectMetaLevel").textContent = project.level || "级别暂未确定";
      $("#projectMetaDiscipline").textContent = project.discipline || "学科待补充";
      const schoolEl = $("#projectMetaSchool");
      if (schoolEl) schoolEl.textContent = project.school || "高校待补充";
      renderTemplateStatus(project);
      renderDocuments();
      renderFacts();
      renderConfirmedSources();
      renderOutline();
      renderSections();
      if (window.MaterialPilot) await window.MaterialPilot.mount({ api, state, toast, reload: loadProject, flushEdits: flushDraftEdits, previewOutline: showOutlineFromTask, isProjectActive: chatProjectIsActive,
        captureView: currentProjectView });
      if (revision !== projectLoadRevision) return;
      if (window.DraftReview) void window.DraftReview.mount({ api, state, toast,
        focusSection: focusDraftSection,
        focusMaterials: () => {
          handleTabNavigation("panel-materials");
          document.getElementById("panel-materials")?.scrollIntoView({ behavior: "smooth" });
        },
        flushEdits: flushDraftEdits });
      renderDiagrams();
      renderMessages();
      restoreInterruptedChatMessage(projectId);
      updateTemplateQuickActions(project);
      if (refreshList) void refreshProjects();
      if (state.llmConfig.provider === "preset") void loadUserBalance();
    } catch (error) {
      if (revision === projectLoadRevision) toast(error.message, true);
    } finally {
      if (revision === projectLoadRevision) loadingProjectId = null;
    }
  }

  function isPersonalApplication(project) {
    const skillId = project?.state?.skill_id || "";
    const category = project?.state?.skill_category || state.skills.find((skill) => skill.id === skillId)?.category || "";
    return (["scholarship", "honor"].includes(category) || ["national-scholarship", "major-transfer", "graduate-recommendation"].includes(skillId)) && skillId !== "class-collective";
  }

  function configureSkillWorkspace(project) {
    const skillId = project?.state?.skill_id === "" ? "" : (project?.state?.skill_id || "innovation-research");
    const picker = $("#conversationSkillPicker");
    const selector = $("#conversationSkillSelect");
    if (picker && selector) {
      picker.classList.toggle("hidden", project?.state?.skill_id !== "");
      selector.replaceChildren();
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "选择材料类型";
      selector.append(empty);
      for (const skill of state.skills) {
        const option = document.createElement("option");
        option.value = skill.id;
        option.textContent = skill.name;
        selector.append(option);
      }
    }
    const skillMeta = state.skills.find((skill) => skill.id === skillId);
    const category = project?.state?.skill_category || skillMeta?.category || "";
    const personalApplication = isPersonalApplication(project);
    const researchOrCompetition = ["research", "competition"].includes(category);
    const practical = category === "practice";
    const diagramsAvailable = researchOrCompetition || practical;
    const paperSearch = $("#paperQuery")?.closest(".source-search-row");
    if (paperSearch) paperSearch.classList.toggle("hidden", !researchOrCompetition);
    const diagramTab = $('.workspace-tab-btn[data-target="diagrams-panel"]');
    if (diagramTab) diagramTab.classList.toggle("hidden", !diagramsAvailable);
    const mobileDiagramOption = $('#mobileMoreStages option[value="diagrams-panel"]');
    if (mobileDiagramOption) {
      mobileDiagramOption.disabled = !diagramsAvailable;
      if (!diagramsAvailable && $("#workspaceGrid").dataset.mobileView === "diagrams-panel") {
        showMobileWorkspaceView("chat");
      }
    }
    const diagramPanel = $("#diagrams-panel");
    if (diagramPanel) diagramPanel.classList.toggle("hidden", !diagramsAvailable);
    const photoTab = $('.upload-tab[data-kind="photo"]');
    if (photoTab) photoTab.classList.toggle("hidden", !(researchOrCompetition || practical));
    $("#innovationReviewRow")?.classList.toggle("hidden", skillId !== "innovation-research");
    if (skillId !== "innovation-research") $("#reviewResult")?.classList.add("hidden");
    if ($("#exportHint")) $("#exportHint").textContent = researchOrCompetition || practical
      ? "缺失内容保留“待补充”，无实验图保留标准化占位线框。"
      : "请补充草稿中的空缺内容后导出。";
    $("#officialQuery").placeholder = `输入「${project?.state?.skill_name || "申报材料"}」相关的学校或当年通知关键词`;
    $("#factsSummary").dataset.skillId = skillId;
    const materialHeading = $("#panel-materials .panel-heading h2");
    const materialDescription = $("#panel-materials .panel-heading p");
    const sourceHeading = $("#sources-panel .panel-heading h2");
    const sourceDescription = $("#sources-panel .panel-heading p");
    const sourceTab = $('.workspace-tab-btn[data-target="sources-panel"]');
    const sourceUploadTab = $('.upload-tab[data-kind="source"]');
    if (materialHeading) materialHeading.textContent = personalApplication ? "个人参考材料与知识库" : "项目参考材料与知识库";
    if (materialDescription) materialDescription.textContent = personalApplication
      ? "上传个人资料、成绩证明或学校模板，帮助整理申请材料。"
      : "上传你的实验记录、已有阶段报告或学校模板，作为申报书撰写的参考依据。";
    if (sourceHeading) sourceHeading.textContent = personalApplication ? "学校通知与申报依据" : "学术文献检索与官方立项依据";
    if (sourceDescription) sourceDescription.textContent = personalApplication
      ? "查找学校和官方发布的评审通知，了解申请条件和材料要求。"
      : "通过 Crossref 检索真实前沿文献；通过白名单检索 edu.cn / gov.cn 官方立项文件。";
    if (sourceTab) sourceTab.textContent = personalApplication ? "🔍 学校通知与依据" : "🔍 学术文献与通知";
    if (sourceUploadTab) sourceUploadTab.textContent = personalApplication ? "个人资料" : "项目资料";

    // 动态调整右侧对话快捷提示词
    const toolsRoot = $("#chatTools");
    if (toolsRoot) {
      toolsRoot.replaceChildren();
      let customPrompts = [];
      if (skillId === "national-scholarship") {
        customPrompts = [
          { label: "🎖️ 突出学业与排名", prompt: "请根据我提供的成绩和排名，帮我整理学习表现；没有的信息请留空。" },
          { label: "🏆 梳理竞赛与科研", prompt: "请根据我提供的竞赛和科研经历整理成果，没有的经历不要添加。" },
          { label: "✍️ 起草申请书", prompt: "请根据我提供的资料写一份国家奖学金申请书，缺少的信息留空。" },
          { label: "📋 检查申请书格式", prompt: "请检查申请书的称呼、段落和落款格式。" },
          { label: "🔍 查学校评审通知", prompt: "请查找我所在学校本年度的国家奖学金评审通知。" },
        ];
      } else if (skillId === "grant-application" || skillId === "motivation-scholarship") {
        customPrompts = [
          { label: "🤝 整理家庭情况", prompt: "请根据我提供的家庭情况整理申请理由，没有提供的信息请留空。" },
          { label: "💼 梳理勤工助学经历", prompt: "请根据我提供的勤工助学经历整理申请内容；没有提供的经历不要添加。" },
          { label: "✍️ 起草申请书", prompt: `请根据我提供的资料写一份${project?.state?.skill_name || "奖助学金"}申请书，缺少的信息留空。` },
          { label: "📋 检查书信体规范与称呼", prompt: "请帮我检查当前申请书的抬头、称呼、此致敬礼与落款格式是否符合标准公文规范。" },
          { label: "🔍 查高校奖助学金评审细则", prompt: "请帮我检索本年度高校奖学金评定的一般门槛和打分偏向。" },
        ];
      } else if (category === "scholarship") {
        customPrompts = [
          { label: "🎖️ 整理学习表现", prompt: "请根据我提供的成绩和学习经历整理申请内容，没有提供的信息请留空。" },
          { label: "🏆 梳理已有成果", prompt: "请根据我提供的获奖和实践经历整理成果，没有提供的经历不要添加。" },
          { label: "✍️ 起草申请书", prompt: `请根据我提供的资料写一份${project?.state?.skill_name || "奖学金"}申请书，缺少的信息留空。` },
          { label: "📋 检查申请书格式", prompt: "请检查申请书的称呼、段落和落款格式。" },
          { label: "🔍 查学校评审通知", prompt: "请查找我所在学校本年度的奖学金评审通知。" },
        ];
      } else if (category === "honor" && personalApplication) {
        customPrompts = [
          { label: "⭐ 整理已有表现", prompt: "请根据我提供的学习、工作和实践情况整理个人表现，未提供的信息留空。" },
          { label: "🏆 梳理已有成果", prompt: "请根据我提供的获奖和活动经历整理成果，没有提供的经历不要添加。" },
          { label: "✍️ 起草申请书", prompt: `请根据我提供的资料写一份${project?.state?.skill_name || "评优"}申请书，缺少的信息留空。` },
          { label: "📋 检查申请书格式", prompt: "请检查申请书的称呼、段落和落款格式。" },
          { label: "🔍 查学校评审通知", prompt: "请查找我所在学校本年度的评优评审通知。" },
        ];
      } else if (skillId === "graduate-recommendation") {
        customPrompts = [
          { label: "🎓 整理学习与排名", prompt: "请根据我提供的成绩和排名整理保研申请材料，没有提供的信息留空。" },
          { label: "🔬 梳理已有科研经历", prompt: "请根据我提供的科研或实践经历整理材料，没有提供的经历不要添加。" },
          { label: "✍️ 起草个人陈述", prompt: "请根据我提供的资料起草保研个人陈述，缺少的信息留空。" },
          { label: "📋 检查材料完整性", prompt: "请检查我已有的保研申请材料还有哪些内容需要补充。" },
          { label: "🔍 查院校推免通知", prompt: "请查找目标院校本年度的推免通知与材料要求。" },
        ];
      } else if (skillId === "major-transfer") {
        customPrompts = [
          { label: "🎓 整理转专业原因", prompt: "请根据我提供的经历整理转专业原因，没有提供的信息请留空。" },
          { label: "📚 梳理目标专业了解", prompt: "请根据我提供的资料整理对目标专业课程和培养方向的了解，不要编造学校信息。" },
          { label: "✍️ 起草申请书", prompt: "请根据我提供的资料起草转专业申请书，缺少的信息留空。" },
          { label: "📋 检查申请书格式", prompt: "请检查申请书的称呼、段落和落款格式。" },
          { label: "🔍 查学校转专业通知", prompt: "请查找我所在学校本年度的转专业通知和申请条件。" },
        ];
      } else if (category === "practice") {
        customPrompts = [
          { label: "🌾 规划三下乡调研路线", prompt: "请帮我设计为期 7 天的社会实践调研日程、点位安排与走访对象分类。" },
          { label: "📊 设计问卷调查 5 维度", prompt: "请针对本次实践主题设计 5 个维度的问卷结构框架与抽样测算依据。" },
          { label: "🛡️ 完善安全应急防范预案", prompt: "请帮我拟定实践团队行前准备、防汛防暑与突发事件的安全防范预案。" },
          { label: "📰 梳理成果输出与宣传方案", prompt: "请帮我规划调研报告、纪实短片、主流媒体发稿等预期产出清单。" },
          { label: "💰 核算实践经费预算细目", prompt: "请按交通、食宿、物资、印刷等科目，帮我核算详细且合理的实践预算表。" },
        ];
      } else if (researchOrCompetition) {
        customPrompts = [
          { label: "⌕ 查项目材料依据", prompt: "请根据我已确认的项目材料，找出与当前项目相关的依据，并标明文件和页码。" },
          { label: "⌕ 查相关前沿论文", prompt: "请搜索与当前项目方向相关的真实论文，给出标题、作者、年份、DOI/链接和摘要；不要编造。" },
          { label: "⌕ 查官方申报通知", prompt: "请查找当前赛道、当前学校或官方机构发布的申报通知，给出年份和来源链接；没有结果就明确说查不到。" },
          { label: "💡 提炼核心科学难点", prompt: "请根据当前研究构思，帮我提炼项目的核心科学挑战与关键技术路线。" },
          { label: "⏰ 规划甘特图里程碑", prompt: "请帮我拟订一个为期一年的实施进度安排与甘特图里程碑节点。" },
        ];
      } else {
        customPrompts = [
          { label: "⭐ 提炼个人综合亮点", prompt: "请根据我的基本情况，帮我提炼出最核心的竞争优势与思想学业闪光点。" },
          { label: "🏛️ 阐明思想认识与成长", prompt: "请帮我撰写思想品德与政治素养成长的心路历程，表述庄重大方、积极向上。" },
          { label: "👥 总结学生干部履职成效", prompt: "请帮我把学生干部与服务同学的琐碎工作，量化转化为有成效的履职总结。" },
          { label: "🎯 规划下一阶段发展目标", prompt: "请帮我结合未来学业与职业规划，撰写切实可行、态度明确的承诺与展望。" },
          { label: "📋 检查材料完整性与格式", prompt: "请核对本申报材料的各个段落是否齐全、格式是否规范无错漏。" },
        ];
      }
      for (const item of customPrompts) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = item.label;
        btn.dataset.prompt = item.prompt;
        btn.addEventListener("click", () => {
          $("#chatInput").value = item.prompt;
          $("#chatInput").focus();
        });
        toolsRoot.append(btn);
      }
    }
  }

  async function renameCurrentProject() {
    if (!state.project) return;
    const currentTitle = state.project.title || "";
    const newTitle = window.prompt("请输入新的项目 / 课题名称：", currentTitle);
    if (!newTitle || newTitle.trim() === currentTitle) return;
    try {
      const updated = await api(`/api/projects/${state.project.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: newTitle.trim() }),
      });
      state.project = updated;
      $("#projectHeading").textContent = updated.title;
      $("#crumbProject").textContent = updated.title;
      await refreshProjects();
      toast("课题名称已更新。");
    } catch (err) {
      toast(err.message, true);
    }
  }

  function createPanel() {
    $("#welcome").classList.add("hidden");
    $("#workspace").classList.add("hidden");
    $("#createPanel").classList.remove("hidden");
    if (mobileLayout?.matches) window.scrollTo(0, 0);
  }

  async function startConversation() {
    $("#projectRequest").value = "";
    $("#projectSkill").value = "";
    $("#routeCandidates").replaceChildren();
    $("#routeCandidates").classList.add("hidden");
    createPanel();
    $("#projectRequest").focus();
  }

  async function startGeneralChat() {
    if (projectCreationInProgress) return;
    projectCreationInProgress = true;
    const button = $("#newChatBtn");
    button.disabled = true;
    try {
      const project = await api("/api/projects/conversation", { method: "POST" });
      await refreshProjects();
      await loadProject(project.id);
      $("#chatInput").focus();
    } catch (error) {
      toast(error.message, true);
    } finally {
      projectCreationInProgress = false;
      button.disabled = false;
    }
  }

  async function selectConversationSkill() {
    const projectId = state.project?.id;
    const selected = $("#conversationSkillSelect").value;
    if (!projectId || !selected) return toast("请选择要准备的材料类型。", true);
    const submit = $("#conversationSkillApply");
    submit.disabled = true;
    try {
      await api(`/api/projects/${projectId}/skill`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill_id: selected }),
      });
      if (state.project?.id === projectId) await loadProject(projectId);
    } catch (error) {
      toast(error.message, true);
    } finally {
      submit.disabled = false;
    }
  }

  async function loadSkills() {
    const cached = readPublicCache("skills");
    if (cached) renderSkills(cached);
    const result = await api("/api/skills");
    const skills = result.skills || [];
    writePublicCache("skills", skills);
    renderSkills(skills);
  }

  function renderSkills(skills) {
    state.skills = skills;
    const select = $("#projectSkill");
    const selected = select.value;
    const first = select.options[0];
    select.replaceChildren(first);
    const groups = new Map();
    for (const skill of state.skills) {
      const category = skill.category || "other";
      if (!groups.has(category)) {
        const group = document.createElement("optgroup");
        group.label = ({
          scholarship: "奖学金", honor: "评优", political: "政治材料",
          research: "科研立项", competition: "竞赛", practice: "实践",
          military: "征兵入伍", study_abroad: "留学交流", other: "其他",
        })[category] || "其他";
        groups.set(category, group);
        select.append(group);
      }
      const option = document.createElement("option");
      option.value = skill.id;
      option.textContent = skill.name;
      groups.get(category).append(option);
    }
    if (skills.some((skill) => skill.id === selected)) select.value = selected;
    if (state.project?.state?.skill_id === "") configureSkillWorkspace(state.project);
  }

  function showRouteCandidates(candidates, message) {
    const box = $("#routeCandidates");
    box.replaceChildren();
    const intro = document.createElement("p");
    intro.textContent = message || "请确认最符合你需求的申报赛道：";
    box.append(intro);
    for (const candidate of candidates) {
      const choice = button(candidate.name, "button button-small button-secondary");
      choice.addEventListener("click", () => {
        if (!state.pendingProject) return;
        state.pendingProject.skill_id = candidate.id;
        completeProjectCreation(state.pendingProject);
      });
      box.append(choice);
    }
    box.classList.remove("hidden");
  }

  let projectCreationInProgress = false;
  async function completeProjectCreation(payload) {
    if (projectCreationInProgress) return;
    projectCreationInProgress = true;
    const submit = $('#createForm button[type="submit"]');
    submit.disabled = true;
    const choices = [...document.querySelectorAll("#routeCandidates button, #heroRouteCandidates button")];
    choices.forEach(choice => { choice.disabled = true; });
    try {
      const project = await api("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.pendingProject = null;
      $("#routeCandidates").classList.add("hidden");
      $("#createForm").reset();
      updateTemplateExplain();
      await refreshProjects();
      await loadProject(project.id);
      if (payload.initial_request) {
        await sendChat(null, payload.initial_request);
      }
      toast(`已进入「${project.state?.skill_name || "申报材料"}」工作区。`);
      if (payload.has_template) {
        setUploadKind("template");
        if (mobileLayout?.matches) showMobileWorkspaceView("panel-materials");
        $("#documentsList").scrollIntoView({ behavior: "smooth", block: "center" });
      } else {
        $("#chatInput").focus();
      }
    } catch (error) {
      toast(error.message, true);
      if ((error.message || "").includes("积分不足") || (error.message || "").includes("402")) {
        openRechargeDialog();
      }
    } finally {
      projectCreationInProgress = false;
      submit.disabled = false;
      choices.forEach(choice => { choice.disabled = false; });
    }
  }

  function updateTemplateQuickActions(project) {
    const bar = $("#templateQuickActions");
    const needsAnswer = project?.state?.skill_id !== "" && project?.state?.template_preference === "not_asked";
    bar.classList.toggle("hidden", !needsAnswer);
    renderPrivacyNotice(project);
  }

  function renderTemplateStatus(project) {
    const box = $("#templateStatus");
    const template = (project.documents || []).find((document) => document.kind === "template");
    box.classList.remove("hidden", "warning", "ready");
    if (project.state?.skill_id === "" && !template) {
      box.textContent = "你可以先对话，确定要准备的材料后再选择或上传模板。";
    } else if (template) {
      box.classList.add("ready");
      box.textContent = template.original_name.toLowerCase().endsWith(".docx")
        ? "已上传 Word 模板：导出时会尽量在原模板字段内填写；无法识别的位置会保留并提示核对。"
        : "已上传 PDF 模板：会参考识别到的栏目生成可编辑 Word，但版式需要人工核对。";
    } else if (project.state?.template_preference === "not_asked") {
      box.textContent = "还没确认是否有学校模板；先在右侧对话回答，Agent 会根据你的选择继续。";
    } else if (project.state?.template_preference === "has_template") {
      box.classList.add("warning");
      box.textContent = "你选择了有学校模板，但还没有上传。请先在“项目材料 → 学校模板”中上传，否则只能按通用模板生成草稿。";
    } else {
      box.textContent = "当前使用通用模板。提交前请对照学校通知和官方申报表核对栏目与排版。";
    }
  }

  async function createProject(event) {
    event.preventDefault();
    const request = $("#projectRequest").value.trim();
    const manuallySelected = $("#projectSkill").value;
    if (!request && !manuallySelected) {
      toast("请先描述你要准备的材料，或手动选择一个赛道。", true);
      $("#projectRequest").focus();
      return;
    }
    const payload = {
      title: $("#projectTitle").value.trim(),
      level: $("#projectLevel").value,
      discipline: $("#projectDiscipline").value.trim(),
      school: $("#projectSchool").value.trim(),
      has_template: $('input[name="hasTemplate"]:checked')?.value === "yes",
      skill_id: manuallySelected,
      initial_request: request,
    };
    if (!manuallySelected && request) {
      const submit = $('#createForm button[type="submit"]');
      submit.disabled = true;
      try {
        const route = await api("/api/route", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: request }),
        });
        if (route.selected) {
          payload.skill_id = route.selected.id;
        } else {
          state.pendingProject = payload;
          const candidates = route.candidates?.length
            ? route.candidates
            : state.skills.slice(0, 35).map((skill) => ({ id: skill.id, name: skill.name }));
          showRouteCandidates(candidates, route.message);
          return;
        }
      } catch (error) {
        toast(`自动识别失败：${error.message}。你可以手动选择赛道。`, true);
        return;
      } finally {
        submit.disabled = false;
      }
    }
    await completeProjectCreation(payload);
  }

  function setUploadKind(kind) {
    state.uploadKind = kind;
    $$(".upload-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.kind === kind));
    const copy = {
      source: ["把项目信息材料拖到这里", "Word、PDF、扫描件或照片"],
      template: ["上传学校下发的申报模板", "Word 模板尽量原位填写；PDF 模板会参考重建"],
      official: ["上传学校或主办方通知", "确认文字后可检索；来源真实性及适用范围仍需你核验"],
      photo: ["上传真实实验 / 调研照片", "没有照片时 Word 会放“待补充”占位，不会生成假图；负责人证件照可在下方事实面板上传"],
    }[kind];
    $("#uploadTitle").textContent = copy[0];
    $("#uploadHint").textContent = `${copy[1]} · 单个文件最大 ${uploadLimitMB()} MB`;
    $("#fileInput").accept = kind === "photo" ? ".png,.jpg,.jpeg,.webp" : ".docx,.pdf,.txt,.md,.png,.jpg,.jpeg,.webp";
  }

  const {
    uploadLimitMB, oversizedUploadError, uploadFilesToCloud, cloudUploadIntent, uploadResumable,
  } = window.MaterialUpload.create({ state, api: (...args) => api(...args) });

  async function uploadFiles(fileList, kind = state.uploadKind, fromChat = false) {
    const view = currentProjectView();
    if (!view.active() || !fileList.length) return;
    if (state.uploading) return toast("材料正在上传，请稍候。", true);
    if (fileList.length > 12) return toast("一次最多上传 12 个文件。", true);
    const sizeError = oversizedUploadError(fileList);
    if (sizeError) {
      renderUploadResult(Array.from(fileList).filter(file => file.size > uploadLimitMB() * 1_000_000)
        .map(file => ({ document: { original_name: file.name }, error: sizeError })));
      $("#fileInput").value = "";
      $("#chatFileInput").value = "";
      return toast(sizeError, true);
    }
    const projectId = view.id;
    const form = new FormData();
    for (const file of fileList) form.append("files", file);
    const names = fileList.map((file) => file.name);
    const query = new URLSearchParams({ kind });
    state.uploading = true;
    const cameraButton = $("#mobileCameraBtn");
    if (cameraButton) cameraButton.disabled = true;
    $("#chooseFileBtn").disabled = true;
    $("#chatAttachBtn").disabled = true;
    const status = $("#uploadResult");
    status.replaceChildren();
    status.textContent = "正在上传并识别材料，请稍候…";
    status.classList.remove("hidden", "error");
    try {
      toast(state.cloudMode ? "正在安全上传并识别材料…" : "正在本机识别文件…");
      const reportProgress = message => { if (view.active()) status.textContent = message; };
      const result = state.cloudMode
        ? await uploadFilesToCloud(fileList, kind, projectId, false, reportProgress)
        : await api(`/api/projects/${projectId}/documents?${query}`, { method: "POST", body: form });
      const failed = (result.documents || []).filter((d) => d.error);
      const uploaded = (result.documents || []).filter((d) => !d.error);
      if (!view.active()) return;
      await loadProject(projectId);
      if (!view.active()) return;
      renderUploadResult(result.documents || []);
      toast(failed.length ? "有文件处理失败，请查看上传结果。" : `上传成功：${uploaded.length} 个文件，可预览并核对文字。`, failed.length > 0);
      const firstDocument = (result.documents || []).find((item) => item.document?.id);
      if (firstDocument) void previewDocument(firstDocument.document, true);
      if (fromChat && uploaded.length) {
        const labels = { source: "项目信息材料", template: "学校申报模板", official: "学校通知（待核验）", photo: "实验 / 调研照片" };
        const successfulNames = uploaded.map((item) => item.document?.original_name).filter(Boolean);
        await sendChat(
          undefined,
          `我刚上传了${labels[kind] || "材料"}：${(successfulNames.length ? successfulNames : names).join("、")}。请告诉我下一步怎么处理。`
        );
      }
      if (fromChat && failed.length && view.active()) {
        appendMessage("assistant", `以下文件未能完成处理：\n${failed.map((item) =>
          `${item.document?.original_name || "文件"}：${item.error}`
        ).join("\n")}\n请查看项目材料区的上传结果后重试。`);
      }
    } catch (error) {
      if (!view.active()) return;
      renderUploadResult(names.map((name) => ({ document: { original_name: name }, error: error.message })));
      toast(error.message, true);
      if (fromChat) appendMessage("assistant", `上传没有完成：${error.message}`);
    } finally {
      state.uploading = false;
      if (cameraButton) cameraButton.disabled = false;
      $("#chooseFileBtn").disabled = false;
      $("#chatAttachBtn").disabled = false;
      $("#fileInput").value = "";
      $("#chatFileInput").value = "";
    }
  }

  function chatUploadAccept(kind) {
    if (kind === "photo") return ".png,.jpg,.jpeg,.webp";
    if (kind === "template") return ".docx,.pdf";
    return ".docx,.pdf,.txt,.md,.png,.jpg,.jpeg,.webp";
  }

  function renderUploadResult(items) {
    const box = $("#uploadResult");
    box.replaceChildren();
    const failed = items.filter((item) => item.error);
    const title = document.createElement("strong");
    title.textContent = items.length
      ? `上传结果：${items.length - failed.length} 个成功，${failed.length} 个处理失败`
      : "未收到上传结果，请重试。";
    box.append(title);
    box.classList.remove("hidden");
    box.classList.toggle("error", failed.length > 0 || !items.length);
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "upload-result-item";
      const label = document.createElement("span");
      const saved = Boolean(item.document?.id);
      label.textContent = `${item.document?.original_name || "文件"} — ${
        item.error ? `${saved ? "已保存，识别失败" : "上传失败"}：${item.error}` : "上传成功"
      }`;
      row.append(label);
      if (saved) {
        const preview = button("预览", "button button-small button-secondary");
        preview.addEventListener("click", () => void previewDocument(item.document));
        row.append(preview);
      }
      box.append(row);
    }
  }

  let documentPreviewVersion = 0;
  let documentPreviewUrl = "";

  function clearDocumentPreview() {
    documentPreviewVersion += 1;
    $("#documentPreviewBody").replaceChildren();
    if (documentPreviewUrl) URL.revokeObjectURL(documentPreviewUrl);
    documentPreviewUrl = "";
  }

  async function fetchDocumentBlob(doc) {
    const headers = new Headers();
    if (state.authSession?.access_token) {
      headers.set("Authorization", `Bearer ${state.authSession.access_token}`);
    }
    // Private files must be fetched with the session JWT, not used as bare img/iframe URLs.
    const path = state.cloudMode && state.authConfig?.storage_upload_endpoint && doc.storage_path
      ? `/uploads/object/${doc.storage_path}`
      : `/api/documents/${encodeURIComponent(doc.id)}/file`;
    const response = await fetch(path, { headers });
    if (!response.ok) {
      throw new Error(response.status === 401
        ? "登录已过期，请重新登录后预览。"
        : `原文件读取失败（HTTP ${response.status}），可查看提取文字。`);
    }
    return response.blob();
  }

  async function previewDocument(doc, justUploaded = false) {
    const dialog = $("#documentPreviewDialog");
    clearDocumentPreview();
    const version = documentPreviewVersion;
    const body = $("#documentPreviewBody");
    const status = $("#documentPreviewStatus");
    const extension = doc.original_name.split(".").pop().toLowerCase();
    $("#documentPreviewTitle").textContent = doc.original_name;
    const failed = doc.extraction_status === "error";
    status.textContent = failed
      ? `原文件已保存，但文字识别失败：${doc.extraction_warning || "请重新上传可复制文字的版本，或手动补充文字。"}`
      : `${justUploaded ? "上传成功 · " : ""}${doc.kind === "template" ? "学校模板" : doc.confirmed ? "材料已入库并建立检索" : "材料已保存，识别文字待核对"}。`;
    $("#reviewDocumentBtn").onclick = () => {
      dialog.close();
      handleTabNavigation("panel-materials");
      const card = $(`[data-document-id="${doc.id}"]`);
      card?.scrollIntoView({ behavior: "smooth", block: "center" });
      $("textarea", card || $("#documentsList"))?.focus();
    };
    $("#downloadDocumentBtn").onclick = async () => {
      try {
        const blob = await fetchDocumentBlob(doc);
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = doc.original_name;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) {
        toast(error.message, true);
      }
    };
    if (!dialog.open) dialog.showModal();
    const text = document.createElement("pre");
    text.className = "document-preview-text";
    text.textContent = doc.extracted_text || "暂无识别文字；可下载原文件查看，或在材料卡片中手动补充。";
    if (!["pdf", "png", "jpg", "jpeg", "webp"].includes(extension)) {
      const note = document.createElement("p");
      note.textContent = extension === "docx"
        ? "Word 文字预览（不保留原版式）。查看完整排版请下载原文件。"
        : "识别文字预览";
      body.append(note, text);
      body.setAttribute("aria-busy", "false");
      return;
    }
    body.textContent = "正在加载原文件预览…";
    body.setAttribute("aria-busy", "true");
    try {
      const blob = await fetchDocumentBlob(doc);
      if (version !== documentPreviewVersion || !dialog.open) return;
      // Use the supported extension's MIME so PDF/image previews work even if the upload MIME was generic.
      const mime = extension === "pdf" ? "application/pdf"
        : `image/${extension === "jpg" ? "jpeg" : extension}`;
      documentPreviewUrl = URL.createObjectURL(new Blob([blob], { type: mime }));
      const media = document.createElement(extension === "pdf" ? "iframe" : "img");
      media.src = documentPreviewUrl;
      if (extension === "pdf") media.title = `PDF 原文件预览：${doc.original_name}`;
      else media.alt = doc.original_name;
      const fallback = document.createElement("p");
      fallback.textContent = "若浏览器无法显示原文件，请使用上方“下载原文件”。下方为识别文字。";
      body.replaceChildren(media, fallback, text);
    } catch (error) {
      if (version !== documentPreviewVersion || !dialog.open) return;
      const warning = document.createElement("p");
      warning.textContent = error.message;
      body.replaceChildren(warning, text);
    } finally {
      if (version === documentPreviewVersion) body.setAttribute("aria-busy", "false");
    }
  }

  function persistDocumentCorrection(key, correction) {
    if (correction) documentCorrections.set(key, correction);
    else documentCorrections.delete(key);
    if (documentRecovery.write(key, correction) || !correction || documentRecoveryWarning) return;
    documentRecoveryWarning = true;
    toast("浏览器暂时无法保留材料校对稿，请先确认保存再刷新。", true);
  }

  function restoreDocumentCorrection(key, doc) {
    let correction = documentCorrections.get(key);
    if (!correction) {
      const cached = documentRecovery.read(key);
      if (typeof cached?.text !== "string" || !Number.isSafeInteger(cached.revision)) return null;
      correction = cached;
    }
    const latest = doc.extracted_text || "";
    if (doc.review_revision > correction.revision && latest === correction.submitted) {
      correction.revision = doc.review_revision;
      delete correction.submitted;
      persistDocumentCorrection(key, correction);
    }
    if (doc.confirmed && latest === correction.text && !documentReviews.has(key)) {
      persistDocumentCorrection(key, null);
      return null;
    }
    documentCorrections.set(key, correction);
    return correction;
  }

  function renderDocuments() {
    const view = currentProjectView();
    const list = $("#documentsList");
    list.replaceChildren();
    if (!state.documents.length) return;
    for (const doc of state.documents) {
      const correctionKey = `${view.id}\0${doc.id}`;
      let correction = doc.kind === "template" ? null : restoreDocumentCorrection(correctionKey, doc);
      const card = document.createElement("div");
      card.className = "document-card";
      card.dataset.documentId = doc.id;
      const top = document.createElement("div");
      top.className = "doc-top";
      const type = document.createElement("div");
      type.className = "doc-type";
      type.textContent = (doc.original_name.split(".").pop() || "FILE").slice(0, 4).toUpperCase();
      const name = document.createElement("div");
      name.className = "doc-name";
      name.textContent = doc.original_name;
      const badge = document.createElement("span");
      badge.className = `doc-state ${doc.confirmed ? "confirmed" : ""}`;
      badge.textContent = doc.extraction_status === "error" ? "已保存 · 识别失败"
        : doc.kind === "template" ? "已上传 · 模板" : doc.confirmed ? "已上传 · 已入库" : "已上传 · 待核对";
      top.append(type, name, badge);
      card.append(top);
      if (doc.extraction_warning) {
        const warning = document.createElement("div");
        warning.className = "doc-warning";
        warning.textContent = doc.extraction_warning;
        card.append(warning);
      }
      const text = document.createElement("textarea");
      text.className = "doc-text";
      text.rows = Math.min(7, Math.max(3, Math.ceil((doc.extracted_text || "").length / 95)));
      text.value = correction?.text ?? doc.extracted_text ?? "";
      text.placeholder = doc.extraction_status === "error" ? "识别失败，请重新上传或手动补充内容。" : "暂无识别文字";
      text.readOnly = doc.kind === "template" || (doc.confirmed && !correction);
      text.setAttribute("aria-label", `识别文字：${doc.original_name}`);
      card.append(text);
      const actions = document.createElement("div");
      actions.className = "doc-actions";
      const preview = button("预览文件", "button button-small button-secondary");
      preview.addEventListener("click", () => { if (view.active()) void previewDocument(doc); });
      actions.append(preview);
      const conflict = document.createElement("div");
      conflict.className = "section-versions";
      conflict.hidden = true;
      const hasConflict = () => Boolean(correction && correction.revision !== doc.review_revision);
      const syncConflict = () => {
        conflict.hidden = !hasConflict();
        conflict.replaceChildren();
        if (!hasConflict()) return;
        const label = document.createElement("p");
        label.textContent = "材料文字已更新，请核对服务器版本后再确认。";
        const latest = document.createElement("pre");
        latest.textContent = doc.extracted_text || "（正文为空）";
        const keep = button("保留本页文字");
        const use = button("使用服务器文字", "button button-small button-quiet");
        keep.addEventListener("click", () => {
          if (!view.active()) return;
          correction = { text: text.value, revision: doc.review_revision };
          persistDocumentCorrection(correctionKey, correction);
          syncConflict();
        });
        use.addEventListener("click", () => {
          if (!view.active()) return;
          correction = null;
          persistDocumentCorrection(correctionKey, null);
          text.value = doc.extracted_text || "";
          syncConflict();
        });
        conflict.append(label, latest, keep, use);
      };
      if (doc.kind !== "template") {
        text.addEventListener("input", () => {
          if (state.project?.id !== view.id) return;
          if (!correction) correction = { revision: doc.review_revision };
          correction.text = text.value;
          if (!documentReviews.has(correctionKey) && !correction.submitted && !hasConflict()
              && text.value === (doc.extracted_text || "")) correction = null;
          persistDocumentCorrection(correctionKey, correction);
        });
        card.append(conflict);
        syncConflict();
      }

      if (state.cloudMode && !doc.confirmed && doc.extraction_status !== "ready") {
        const retry = button("重试文字识别", "button button-small button-secondary");
        retry.addEventListener("click", async () => {
          if (!view.active()) return;
          retry.disabled = true;
          try {
            const result = await api(`/api/documents/${doc.id}/process`, { method: "POST" });
            if (!view.active()) return;
            renderUploadResult(result.documents || []);
            await loadProject(view.id);
          } catch (error) {
            if (view.active()) toast(`原文件仍保留，识别未完成：${error.message}`, true);
          } finally { retry.disabled = false; }
        });
        actions.append(retry);
      }
      if (doc.kind !== "template") {
        const confirmText = async event => {
          if (!view.active() || documentReviews.has(correctionKey)) return;
          if (hasConflict()) {
            toast("请先选择要保留的文字。", true);
            return;
          }
          if (!text.value.trim()) {
            toast("请先补充或核对识别文字。", true);
            return;
          }
          const submitted = text.value;
          const revision = correction?.revision ?? doc.review_revision;
          const request = {};
          documentReviews.set(correctionKey, request);
          const control = event?.currentTarget;
          if (control) control.disabled = true;
          const previousSubmission = correction?.submitted;
          correction = { text: submitted, revision, submitted };
          persistDocumentCorrection(correctionKey, correction);
          try {
            const result = await observeMaterialRequest(`/api/documents/${doc.id}/confirm`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ confirmed: true, corrected_text: submitted, expected_revision: revision }),
            }, 90000, "材料确认结果待核对，已保留本页文字。");
            if (documentReviews.get(correctionKey) !== request) return;
            const latest = documentCorrections.get(correctionKey);
            if (latest?.text === submitted) persistDocumentCorrection(correctionKey, null);
            else if (latest && Number.isSafeInteger(result.document?.review_revision))
              persistDocumentCorrection(correctionKey, { text: latest.text, revision: result.document.review_revision });
            if (!view.active() && !chatProjectIsActive(view.id)) return;
            clearExportArtifacts();
            window.MaterialPilot?.invalidateArtifacts?.();
            await loadProject(view.id, { refreshList: false });
            if (!view.active()) return;
            toast(result.index_mode === "keyword_fallback" && privateRagConsented()
              ? "材料已确认，可用关键词检索；向量索引暂未建立。"
              : "已确认材料并建立检索索引。");
          } catch (error) {
            if (documentReviews.get(correctionKey) !== request) return;
            const latest = documentCorrections.get(correctionKey);
            if (latest && error.status >= 400 && error.status < 500) {
              latest.submitted = previousSubmission;
              persistDocumentCorrection(correctionKey, latest);
            }
            if (!view.active()) return;
            if (error.status === 409 || error.status >= 500) await loadProject(view.id, { refreshList: false });
            if (view.active()) toast(error.message, true);
          } finally {
            if (documentReviews.get(correctionKey) === request) documentReviews.delete(correctionKey);
            if (control) control.disabled = false;
          }
        };
        if (!doc.confirmed) {
          const confirmBtn = button("核对并确认入库", "button button-small button-primary");
          confirmBtn.addEventListener("click", confirmText);
          actions.append(confirmBtn);
        } else {
          if (state.authConfig?.private_embedding_configured) {
            const reindex = button("重新建立向量索引", "button button-small button-secondary");
            reindex.addEventListener("click", event => {
              if (!privateRagConsented()) {
                toast("请先阅读并勾选本项目的 Cloudflare 私有检索授权。", true);
                return;
              }
              void confirmText(event);
            });
            actions.append(reindex);
          }
          const save = button("确认修改并重新索引", "button button-small button-primary");
          save.hidden = !correction;
          save.addEventListener("click", confirmText);
          const edit = button("修改识别文字", "button button-small button-secondary");
          edit.hidden = Boolean(correction);
          edit.addEventListener("click", () => {
            text.readOnly = false;
            text.focus();
            save.hidden = false;
            edit.hidden = true;
          });
          actions.append(edit, save);
        }
      }
      const remove = button("移除", "button button-small button-quiet");
      remove.addEventListener("click", async () => {
        if (!view.active() || !confirm(`确定从本项目移除“${doc.original_name}”吗？${state.cloudMode ? "历史加密备份可能长期保留已删除的材料。" : ""}`)) return;
        const epoch = chatRecoveryEpoch;
        try {
          await api(`/api/documents/${doc.id}`, { method: "DELETE" });
          if (epoch !== chatRecoveryEpoch) return;
          documentReviews.delete(correctionKey);
          persistDocumentCorrection(correctionKey, null);
          if (!view.active()) return;
          await loadProject(view.id);
          if (view.active()) toast("材料和对应索引已删除。");
        } catch (error) {
          if (view.active()) toast(error.message, true);
        }
      });
      actions.append(remove);
      card.append(actions);
      list.append(card);
    }
  }

  function renderFacts() {
    const project = state.project;
    if (!project) return;
    const draft = factEdits.open(project);
    if (factFormProjectId !== project.id) {
      factFormProjectId = project.id;
      $("#factsEditor").classList.toggle("hidden", !draft.entries.size);
    }
    renderIntakeArchive();
    updateIntakeModelNote();
    const facts = { ...(state.project?.state?.confirmed_facts || {}) };
    $("#politicalStageField").hidden = !["summary-report", "youth-league-conversion"].includes(state.project?.state?.skill_id);
    const factSources = state.project?.state?.fact_sources || {};
    const summary = $("#factsSummary");
    summary.replaceChildren();
    const skillId = state.project?.state?.skill_id;
    const personalApplication = isPersonalApplication(state.project);
    const commonKeys = personalApplication ? ["application_year", "education_level", "college"] : [
      "project_name", "problem", "target_user", "method", "foundation",
      "goal", "expected_output", "constraints", "duration", "budget",
      "leader_name", "advisor_name", "team", "application_year", "education_level", "college"
    ];
    if (!$("#politicalStageField").hidden) commonKeys.push("political_stage");
    const keys = [...new Set([...commonKeys, ...Object.keys(facts)])];
    for (const key of keys) {
      if (key.startsWith("skill_") || key.startsWith("template_") || key.startsWith("fact_") || key === "leader_photo") continue;
      const val = facts[key];
      const box = document.createElement("div");
      box.className = `fact-chip ${val ? "" : "missing"} clickable`;
      box.title = val ? "点击展开编辑该字段" : "点击展开编辑并补充该字段";
      const label = document.createElement("small");
      label.textContent = labels[key] || key.replace(/_/g, " ");
      const value = document.createElement("span");
      value.textContent = val || "待补充 ＋";
      box.append(label, value);
      if (factSources[key]) {
        const source = document.createElement("small");
        source.className = "fact-source";
        source.textContent = `依据：“${factSources[key]}”`;
        box.append(source);
      }
      box.addEventListener("click", () => {
        $("#factsEditor").classList.remove("hidden");
        const targetInput = $(`[data-fact="${key}"]`);
        if (targetInput) {
          targetInput.focus();
          targetInput.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
      summary.append(box);
    }
    const photoId = state.project?.state?.leader_photo || facts.leader_photo;
    const photoBox = document.createElement("div");
    photoBox.className = `fact-chip ${photoId ? "" : "missing"} clickable`;
    photoBox.title = photoId ? "负责人照片已上传，点击展开编辑" : "点击上传负责人照片";
    const photoLabel = document.createElement("small");
    photoLabel.textContent = "负责人照片";
    const photoValue = document.createElement("span");
    photoValue.textContent = photoId ? "已上传 ✓" : "待补充 ＋";
    photoBox.append(photoLabel, photoValue);
    photoBox.addEventListener("click", () => {
      $("#factsEditor").classList.remove("hidden");
      $("#chooseLeaderPhotoBtn")?.focus();
    });
    if (!personalApplication || photoId) summary.append(photoBox);
    renderLeaderPhoto();
    const dynamic = $("#dynamicFactsGrid");
    if (dynamic) {
      dynamic.replaceChildren();
      const dynamicKeys = new Set(Object.keys(facts));
      for (const entry of draft.entries.values()) if (entry.group === "facts") dynamicKeys.add(entry.key);
      for (const key of dynamicKeys) {
        if (Object.hasOwn(labels, key) || key.startsWith("skill_") || key.startsWith("template_")
            || key.startsWith("fact_") || key === "leader_photo") continue;
        appendDynamicFact(key, factEdits.value(project, draft, "facts", key));
      }
    }
    // The editor survives project switches. Always refresh every input,
    // including fields hidden in the current application type, so saving a
    // new project cannot carry over another project's data.
    const projectOnlyFacts = new Set(["project_name", "problem", "target_user", "method", "foundation", "goal",
      "expected_output", "constraints", "duration", "budget", "leader_name", "advisor_name", "team"]);
    $$("[data-fact]").forEach((field) => {
      const key = field.dataset.fact;
      const value = factEdits.value(project, draft, "facts", key);
      field.value = value;
      const parent = field.closest(".field");
      if (parent && projectOnlyFacts.has(key)) {
        // Existing values remain visible/editable even after a skill change.
        parent.classList.toggle("hidden", personalApplication && !String(value ?? "").trim());
      }
    });
    const photoField = $(".leader-photo-field");
    if (photoField) photoField.classList.toggle("hidden", personalApplication && !photoId);
    $$("[data-project]").forEach((field) => {
      field.value = factEdits.value(project, draft, "project", field.dataset.project);
    });
    renderFactDraftStatus(project, draft);
  }

  const projectFieldLabels = { title: "项目名称", level: "申报级别", discipline: "专业 / 学科", school: "学校" };

  function renderFactDraftStatus(project, draft) {
    const notice = $("#factFormStatus");
    const list = $("#factConflictList");
    if (!notice || !list) return;
    const conflicts = factEdits.conflicts(project, draft);
    const awaitingReceipt = [...draft.entries.values()].some(entry => entry.submitted !== undefined);
    notice.textContent = draft.saving ? "正在保存，可以继续编辑。" : conflicts.length
      ? "其他页面已修改下列字段，选择要保留的值后再保存。"
      : awaitingReceipt ? "上一次保存待确认，请核对后保存。"
      : draft.entries.size ? (draft.recovered ? "已恢复未保存的信息，请核对后保存。" : "有修改待保存。") : "";
    notice.hidden = !notice.textContent;
    list.hidden = !conflicts.length;
    const view = currentProjectView();
    list.replaceChildren();
    for (const entry of conflicts) {
      const row = document.createElement("div");
      row.className = "section-versions";
      const label = document.createElement("strong");
      label.textContent = entry.group === "project" ? projectFieldLabels[entry.key] || entry.key
        : labels[entry.key] || entry.key.replace(/_/g, " ");
      const current = document.createElement("p");
      current.textContent = `已保存：${factEdits.readValue(project, entry.group, entry.key) || "（空）"}`
        + (entry.group === "facts" && entry.key === "project_name" ? `，当前项目标题「${project.title || "（空）"}」` : "");
      const keep = button("保留本页输入");
      const accept = button("使用已保存值", "button button-small button-quiet");
      const resolve = keepLocal => {
        if (!view.active() || state.project !== project) return;
        factEdits.resolve(project, entry, keepLocal);
        renderFacts();
      };
      keep.disabled = accept.disabled = draft.saving;
      keep.addEventListener("click", () => resolve(true));
      accept.addEventListener("click", () => resolve(false));
      row.append(label, current, keep, accept);
      list.append(row);
    }
    $("#saveFactsBtn").disabled = draft.saving || conflicts.length > 0;
  }

  function recordFactInput(field, project = state.project) {
    if (!project || typeof field?.value !== "string") return;
    const key = field.dataset?.fact || field.dataset?.project;
    const group = field.dataset?.fact ? "facts" : "project";
    if (!/^[a-z][a-z0-9_]{1,63}$/.test(key || "") || (group === "project" && !Object.hasOwn(projectFieldLabels, key))
        || (key === "political_stage" && $("#politicalStageField").hidden)) return;
    factEdits.edit(project, group, key, field.value);
  }

  let leaderPhotoBlobUrl = "";
  let leaderPhotoId = null;
  let leaderPhotoRequest = 0;

  function clearLeaderPhoto() {
    leaderPhotoRequest++;
    if (leaderPhotoBlobUrl) URL.revokeObjectURL(leaderPhotoBlobUrl);
    leaderPhotoBlobUrl = "";
    leaderPhotoId = null;
    $("#leaderPhotoPreview")?.replaceChildren();
    $("#removeLeaderPhotoBtn")?.classList.add("hidden");
  }

  async function renderLeaderPhoto() {
    const preview = $("#leaderPhotoPreview");
    if (!preview) return;
    const view = currentProjectView();
    const photoId = state.project?.state?.leader_photo;
    if (photoId && photoId === leaderPhotoId && leaderPhotoBlobUrl) return;
    clearLeaderPhoto();
    const request = leaderPhotoRequest;
    const choose = $("#chooseLeaderPhotoBtn");
    if (choose) choose.textContent = photoId ? "更换照片" : "上传照片";
    const placeholder = document.createElement("span");
    placeholder.className = "photo-placeholder-text";
    placeholder.textContent = photoId ? "正在加载照片…" : "暂无照片";
    preview.append(placeholder);
    if (!photoId) return;
    $("#removeLeaderPhotoBtn")?.classList.remove("hidden");
    const current = () => request === leaderPhotoRequest && view.active() && state.project?.state?.leader_photo === photoId;
    const doc = (state.documents || []).find(d => d.id === photoId) || { id: photoId };
    try {
      const blob = await fetchDocumentBlob(doc);
      if (!current()) return;
      leaderPhotoId = photoId;
      leaderPhotoBlobUrl = URL.createObjectURL(blob);
      const img = document.createElement("img");
      img.src = leaderPhotoBlobUrl;
      img.alt = "负责人照片";
      img.onerror = () => {
        if (current()) {
          clearLeaderPhoto();
          preview.textContent = "照片无法预览";
          $("#removeLeaderPhotoBtn")?.classList.remove("hidden");
        }
      };
      preview.replaceChildren(img);
    } catch {
      if (current()) placeholder.textContent = "照片已登记，预览读取失败";
    }
  }

  async function handleLeaderPhotoUpload(file) {
    const view = currentProjectView();
    if (!file || !view.active()) return;
    const sizeError = oversizedUploadError([file]);
    if (sizeError) return toast(sizeError, true);
    const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
    if (![".png", ".jpg", ".jpeg", ".webp"].includes(ext)) {
      return toast("照片只支持 PNG、JPG 或 WebP 格式。", true);
    }
    const btn = $("#chooseLeaderPhotoBtn");
    if (btn) btn.disabled = true;
    try {
      toast("正在上传负责人照片…");
      if (state.cloudMode) {
        const result = await uploadFilesToCloud([file], "photo", view.id, true);
        const failed = result.documents.find(item => item.error);
        if (failed) throw new Error(failed.error);
      } else {
        const form = new FormData();
        form.append("file", file);
        await api(`/api/projects/${view.id}/leader-photo`, {
          method: "POST",
          body: form,
        });
      }
      if (!view.active()) return;
      await loadProject(view.id);
      if (view.active()) toast("负责人照片上传成功！");
    } catch (err) {
      if (view.active()) toast(`上传失败：${err.message}`, true);
    } finally {
      if (btn) btn.disabled = false;
      const input = $("#leaderPhotoInput");
      if (input) input.value = "";
    }
  }

  async function handleLeaderPhotoDelete() {
    const view = currentProjectView();
    if (!view.active()) return;
    if (typeof confirm === "function" && !confirm("确定删除已上传的负责人照片吗？")) return;
    const btn = $("#removeLeaderPhotoBtn");
    if (btn) btn.disabled = true;
    try {
      await api(`/api/projects/${view.id}/leader-photo`, { method: "DELETE" });
      if (!view.active()) return;
      await loadProject(view.id);
      if (view.active()) toast("已删除负责人照片。");
    } catch (err) {
      if (view.active()) toast(`删除失败：${err.message}`, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function appendDynamicFact(key, value = "") {
    const grid = $("#dynamicFactsGrid");
    if (!grid) return;
    const field = document.createElement("label");
    field.className = "field";
    const caption = document.createElement("span");
    caption.textContent = `赛道信息 · ${key.replace(/_/g, " ")}`;
    const input = document.createElement("textarea");
    input.dataset.fact = key;
    input.rows = 2;
    input.value = value || "";
    field.append(caption, input);
    grid.append(field);
  }

  function addCustomFact() {
    const facts = state.project?.state?.confirmed_facts || {};
    let index = 1;
    while (`custom_field_${index}` in facts || $(`[data-fact="custom_field_${index}"]`)) index += 1;
    appendDynamicFact(`custom_field_${index}`);
    $("#dynamicFactsGrid").lastElementChild?.querySelector("textarea")?.focus();
  }

  function renderConfirmedSources() {
    const target = $("#confirmedSources");
    target.replaceChildren();
    const stateData = state.project?.state || {};
    const all = [
      ...(stateData.references || []).map((item) => ({ ...item, _kind: "paper" })),
      ...(stateData.official_sources || []).map((item) => ({ ...item, _kind: "official" })),
    ];
    if (!all.length) return;
    const label = document.createElement("div");
    label.className = "inline-hint";
    label.style.marginTop = "12px";
    label.style.fontWeight = "600";
    label.textContent = "✓ 已由你核验确认的立项依据";
    target.append(label);
    for (const source of all) {
      const line = document.createElement("div");
      line.className = "source-card";
      const title = document.createElement("h3");
      title.textContent = `${source._kind === "paper" ? "学术文献" : "官方立项通知"}：${source.title || "未命名来源"}`;
      const link = document.createElement("a");
      link.href = source.url || (source.doi ? `https://doi.org/${source.doi}` : "#");
      link.textContent = link.href === "#" ? "缺少链接" : source.url || `DOI: ${source.doi}`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      line.append(title, link);
      target.append(line);
    }
  }

  function renderSourceCards(items, kind, view) {
    const target = $("#sourceResults");
    target.replaceChildren();
    if (!items?.length) {
      const note = document.createElement("div");
      note.className = "empty-note";
      note.textContent = "没有找到匹配结果。可以更换检索关键词，或直接粘贴官网链接。";
      target.append(note);
      return;
    }
    for (const source of items) {
      const card = document.createElement("div");
      card.className = "source-card";
      const title = document.createElement("h3");
      title.textContent = source.title || "未命名来源";
      const details = document.createElement("p");
      details.textContent = kind === "paper"
        ? `${(source.authors || []).join(", ") || "作者待核"} · ${source.year || "年份待核"} · ${source.venue || "出版信息待核"}${source.doi ? ` · DOI ${source.doi}` : ""}`
        : `${source.source_domain || ""} · ${source.published_at || "发布日期待核"} · ${source.snippet || ""}`;
      card.append(title, details);
      if (source.url) {
        const link = document.createElement("a");
        link.href = source.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = source.url;
        card.append(link);
      }
      const warning = document.createElement("p");
      warning.className = "source-warning";
      warning.textContent = kind === "paper"
        ? (source.content_checked
          ? "检索到摘要/元数据，不代表已读全文；请先核对内容。"
          : "只有题名/元数据，不能据此写研究结论。")
        : "搜索摘要不是通知全文；请打开原始页面核对年份和适用对象。";
      card.append(warning);
      const add = button("我已核对，加入来源", "button button-small button-primary");
      add.addEventListener("click", () => confirmSource(source, kind, add, view));
      card.append(add);
      target.append(card);
    }
  }

  async function searchPublicKnowledge() {
    const view = currentProjectView();
    if (!view.active()) {
      toast("请先选择项目。", true);
      return;
    }
    const query = $("#knowledgeQuery").value.trim();
    if (query.length < 2 || query.length > 300) {
      toast("请输入 2–300 字的检索问题。", true);
      return;
    }
    const btn = $("#knowledgeSearchBtn");
    const target = $("#knowledgeResults");
    btn.disabled = true;
    try {
      const result = await api(`/api/projects/${view.id}/search/knowledge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          external_query_consent: $("#knowledgeExternalConsent").checked === true,
          include_examples: $("#knowledgeIncludeExamples").checked === true,
        }),
      });
      if (!view.active()) return;
      target.replaceChildren();
      const notice = document.createElement("p");
      notice.className = "source-warning";
      notice.textContent = `${result.message || ""} 检索模式：${result.retrieval_mode || "未知"}。${result.fallback_reason || ""}`;
      target.append(notice);
      for (const item of result.results || []) {
        const card = document.createElement("div");
        card.className = "source-card";
        const title = document.createElement("h3");
        title.textContent = `${item.kind === "official" ? "官方资料摘要" : item.kind === "institution_report" ? "校内活动报道（非评审规则）" : item.kind === "example" ? "范例" : "非官方写作指南"}：${item.title || ""}`;
        const scope = document.createElement("p");
        scope.textContent = [item.scope, item.source_ref, item.school, item.application_year,
          item.verified_at ? `核验时间：${item.verified_at}` : ""].filter(Boolean).join(" · ");
        const excerpt = document.createElement("p");
        excerpt.textContent = item.content || "";
        card.append(title, scope, excerpt);
        if (item.kind === "example") {
          const license = document.createElement("p");
          license.className = "source-warning";
          license.textContent = `授权信息：${item.license || "待核验"}；署名：${item.attribution || "待核验"}；${item.example_match_type === "cross_track" ? "跨赛道参考" : "同赛道参考"}。范文仅供结构参考，不得将示例事实写成你的事实。`;
          card.append(license);
          try {
            const rights = new URL(item.rights_evidence_url);
            if (rights.protocol === "https:") {
              const evidenceLink = document.createElement("a");
              evidenceLink.href = rights.href;
              evidenceLink.target = "_blank";
              evidenceLink.rel = "noopener noreferrer";
              evidenceLink.textContent = "查看授权依据";
              card.append(evidenceLink);
            }
          } catch {}
        }
        try {
          const url = new URL(item.source_url);
          if (url.protocol === "https:") {
            const link = document.createElement("a");
            link.href = url.href;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = "查看原始来源（请核对版本）";
            card.append(link);
          }
        } catch {}
        target.append(card);
      }
    } catch (error) {
      if (view.active()) toast(error.message || "赛道知识检索失败。", true);
    } finally {
      if (view.active()) {
        $("#knowledgeExternalConsent").checked = false;
        $("#knowledgeIncludeExamples").checked = false;
      }
      btn.disabled = false;
    }
  }

  async function confirmSource(source, kind, btn, view) {
    if (!view.active()) return;
    btn.disabled = true;
    try {
      const yearValue = typeof source.year === "number"
        ? source.year
        : (/^\d{4}$/.test(String(source.year || "")) ? Number(source.year) : null);
      await api(`/api/projects/${view.id}/references/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: source.title || "未命名来源",
          authors: source.authors || [],
          year: yearValue,
          venue: source.venue || "",
          doi: source.doi || "",
          url: source.url || "",
          abstract: source.abstract || source.snippet || "",
          source_type: kind === "paper" ? "paper" : "official",
          published_at: source.published_at || "",
        }),
      });
      if (!view.active()) return;
      await loadProject(view.id);
      if (view.active()) toast("来源已加入项目；仍请核对其是否支持正文中的具体论断。");
    } catch (error) {
      if (!view.active()) return;
      toast(error.message, true);
      btn.disabled = false;
    }
  }

  async function searchSources(kind) {
    const view = currentProjectView();
    if (!view.active()) return;
    const input = kind === "paper" ? $("#paperQuery") : $("#officialQuery");
    const query = input.value.trim();
    if (query.length < 2) return toast("请输入至少两个字的关键词。", true);
    const btn = kind === "paper" ? $("#paperSearchBtn") : $("#officialSearchBtn");
    btn.disabled = true;
    btn.textContent = "搜索中…";
    $("#sourceResults").replaceChildren();
    const loading = document.createElement("div");
    loading.className = "source-loading";
    loading.textContent = kind === "paper" ? "正在查 Crossref 论文元数据…" : "正在检索官方域名…";
    $("#sourceResults").append(loading);
    try {
      const endpoint = kind === "paper" ? "papers" : "official";
      const result = await api(`/api/projects/${view.id}/search/${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      if (!view.active()) return;
      renderSourceCards(result.results, kind, view);
      if (result.error) toast(result.error, true);
    } catch (error) {
      if (!view.active()) return;
      toast(error.message, true);
      $("#sourceResults").replaceChildren();
    } finally {
      btn.disabled = false;
      btn.textContent = kind === "paper" ? "检索学术论文" : "查官方指南";
    }
  }

  async function previewOfficialUrl() {
    const view = currentProjectView();
    if (!view.active()) return;
    const url = $("#officialUrl").value.trim();
    if (!url) return toast("请先粘贴学校或政府官网的 HTTPS 通知链接。", true);
    const btn = $("#officialUrlBtn");
    btn.disabled = true;
    btn.textContent = "读取中…";
    try {
      const result = await api(`/api/projects/${view.id}/official/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      if (!view.active()) return;
      const target = $("#sourceResults");
      target.replaceChildren();
      const card = document.createElement("div");
      card.className = "source-card";
      const title = document.createElement("h3");
      title.textContent = result.title || "官方通知";
      const link = document.createElement("a");
      link.href = result.url;
      link.textContent = result.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      const excerpt = document.createElement("p");
      excerpt.textContent = (result.content || "").slice(0, 4000);
      const date = document.createElement("p");
      date.textContent = result.published_at
        ? `页面发布日期：${result.published_at} · 抓取时间 UTC：${result.retrieved_at_utc}`
        : `页面未标明发布日期 · 抓取时间 UTC：${result.retrieved_at_utc}`;
      const warning = document.createElement("p");
      warning.className = "source-warning";
      warning.textContent = result.warning || "请核对页面年份和适用范围。";
      const add = button("我已核对，将全文加入当前项目材料", "button button-small button-primary");
      add.addEventListener("click", async () => {
        if (!view.active()) return;
        add.disabled = true;
        try {
          const imported = await api(`/api/projects/${view.id}/official/import`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: result.url }),
          });
          if (!view.active()) return;
          await loadProject(view.id);
          if (view.active()) toast(imported.note || "官方通知已读取；确认文字后才能进入 RAG。");
        } catch (error) {
          if (!view.active()) return;
          toast(error.message, true);
          add.disabled = false;
        }
      });
      card.append(title, link, date, excerpt, warning, add);
      target.append(card);
    } catch (error) {
      if (!view.active()) return;
      toast(error.message, true);
      if ((error.message || "").includes("积分不足") || (error.message || "").includes("402")) {
        openRechargeDialog();
      }
    } finally {
      btn.disabled = false;
      btn.textContent = "直接抓取原文";
    }
  }

  async function saveFacts() {
    const view = currentProjectView();
    if (!view.active()) return;
    const draft = factEdits.open(state.project);
    if (draft.saving) return;
    for (const field of [...$$("[data-fact]"), ...$$("[data-project]")]) recordFactInput(field);
    if (factEdits.conflicts(state.project, draft).length) {
      renderFactDraftStatus(state.project, draft);
      return;
    }
    const initial = factEdits.prepare(state.project, draft);
    if (!initial.changes.length) return;
    draft.saving = true;
    clearExportArtifacts();
    window.MaterialPilot?.invalidateArtifacts?.();
    renderFactDraftStatus(state.project, draft);
    let plan = initial;
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        factEdits.start(view.id, draft, plan.changes);
        try {
          const receipt = await observeMaterialRequest(`/api/projects/${view.id}/facts/form`, {
            method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(plan.payload),
          }, 30000, "保存结果待确认，已保留本页输入。");
          factEdits.finish(view.id, draft, plan.changes);
          if (view.active() && Number.isSafeInteger(receipt?.material_revision)
              && receipt.material_revision >= state.project.material_revision) {
            Object.assign(state.project, receipt.project);
            state.project.state ||= {};
            state.project.state.confirmed_facts = receipt.confirmed_facts;
            state.project.state.fact_sources = receipt.fact_sources;
            state.project.material_revision = receipt.material_revision;
          }
          break;
        } catch (error) {
          if (error.status >= 400 && error.status < 500) factEdits.finish(view.id, draft, plan.changes, true);
          if (error.status !== 409 || !view.active()) throw error;
          const revision = state.project.material_revision;
          await loadProject(view.id, { refreshList: false });
          if (!view.active()) return;
          factEdits.open(state.project, true);
          if (factEdits.conflicts(state.project, draft).length) {
            toast("这些字段同时被修改，请核对后保存。", true);
            return;
          }
          const next = factEdits.prepare(state.project, draft);
          if (!next.changes.length) break;
          if (attempt || state.project.material_revision === revision) throw error;
          const sameInputs = next.changes.every(({ entry, value }) =>
            initial.changes.some(change => change.entry === entry && change.value === value));
          if (!sameInputs) {
            toast("已载入最新项目信息，本页还有修改待保存。");
            return;
          }
          plan = next;
        }
      }
      if (view.active()) {
        clearExportArtifacts();
        window.MaterialPilot?.invalidateArtifacts?.();
        await loadProject(view.id);
        if (!view.active()) return;
        if (!draft.entries.size) $("#factsEditor").classList.add("hidden");
        toast(draft.entries.size ? "已保存提交时的信息，本页还有修改待保存。" : "已保存为你确认的项目事实。");
      } else if (factEdits.tracked(view.id, draft) && chatProjectIsActive(view.id)) {
        draft.saving = false;
        await loadProject(view.id, { refreshList: false });
      }
    } catch (error) {
      if (!view.active()) return;
      toast(error.status ? error.message : "保存结果待确认，已保留本页输入。", true);
    } finally {
      draft.saving = false;
      if (state.project?.id === view.id && factEdits.tracked(view.id, draft)) renderFactDraftStatus(state.project, draft);
    }
  }

  function intakeCard(record) {
    const card = document.createElement("article");
    card.className = "fact-candidate";
    const fact = record.fact;
    const title = document.createElement("strong");
    title.textContent = record.label + " · " + record.status_label;
    if (fact.record_id !== "self") title.textContent += " · " + fact.record_id;
    card.append(title);
    if (fact.value !== null) {
      const value = document.createElement("p");
      value.textContent = fact.value;
      card.append(value);
    }
    for (const source of fact.evidence) {
      const line = document.createElement("small");
      line.textContent = source.turn_id + " 原话　“" + source.quote + "”";
      card.append(line);
    }
    return card;
  }

  function renderIntakeArchive() {
    const root = $("#intakeArchive");
    if (!root) return;
    root.replaceChildren();
    const records = state.project?.state?._student_intake_confirmed || [];
    if (!records.length) return;
    const heading = document.createElement("h4");
    heading.textContent = "已核对的口述档案";
    root.append(heading);
    for (const record of records) root.append(intakeCard(record));
  }

  function updateIntakeModelNote() {
    const node = $("#intakeModelNote");
    if (!node) return;
    const config = state.llmConfig;
    const model = config.provider === "preset" ? "DeepSeek V4 Flash（按工作台现有积分计费）"
      : (config.model || "尚未配置的模型") + (config.provider === "ollama" ? "（本机）" : "（自带 API）");
    node.textContent = "点击后，本项目中的学生发言将交给 " + model + " 提取。请逐项核对状态和原话。";
  }

  async function proposeIntake() {
    const projectId = state.project?.id;
    if (!projectId) return;
    const owner = state.authSession?.user?.id;
    const active = () => chatProjectIsActive(projectId) && owner === state.authSession?.user?.id;
    const root = $("#intakeCandidates");
    const btn = $("#proposeIntakeBtn");
    root.replaceChildren();
    btn.disabled = true;
    btn.textContent = "正在核对口述…";
    try {
      const result = await api("/api/projects/" + projectId + "/intake/propose", { method: "POST" });
      if (!active()) return;
      const note = document.createElement("p");
      note.className = "inline-hint";
      const prefix = result.model ? "本次模型 " + result.model + "。" : "";
      note.textContent = prefix + (result.decision === "needs_manual_review"
        ? "这次输出未通过校验，请在对话中补充原话或手动填写。"
        : result.decision === "partial_review" ? "部分信息无法核对。请逐项查看下方候选，其余信息可在对话中补充。"
        : result.facts.length ? "请选择已核对的条目。" : "没有可核对的口述候选。");
      root.append(note);
      for (const record of result.facts) {
        const card = intakeCard(record);
        if (record.can_confirm) {
          const save = button("我已核对，按此状态入档", "button button-small button-primary");
          save.addEventListener("click", async () => {
            if (!active()) return;
            save.disabled = true;
            try {
              await api("/api/projects/" + projectId + "/intake/confirm", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ confirmed: true, source_revision: result.source_revision,
                  fact: record.fact, model: result.model, ticket: record.ticket }),
              });
              if (!active()) return;
              card.remove();
              await loadProject(projectId);
              if (active()) toast("已保存这项口述、状态和原话。");
            } catch (error) {
              if (active()) toast(error.message, true);
              save.disabled = false;
            }
          });
          card.append(save);
        }
        root.append(card);
      }
      if (result.questions.length) {
        const title = document.createElement("strong");
        title.textContent = "请在对话里补充";
        const list = document.createElement("ul");
        for (const question of result.questions) {
          const li = document.createElement("li");
          li.textContent = question;
          list.append(li);
        }
        root.append(title, list);
      }
    } catch (error) {
      if (active()) toast(error.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "提取本项目口述";
    }
  }

  async function proposeFacts() {
    const view = currentProjectView();
    if (!view.active()) return;
    const btn = $("#proposeFactsBtn");
    btn.disabled = true;
    btn.textContent = "提取中…";
    const root = $("#factCandidates");
    root.replaceChildren();
    try {
      const result = await api(`/api/projects/${view.id}/facts/propose`, { method: "POST" });
      if (!view.active()) return;
      if (!result.candidates?.length) {
        const note = document.createElement("div");
        note.className = "empty-note";
        note.textContent = "没有找到带有明确原文依据的新事实。你可以在“编辑”中手动补充并确认。";
        root.append(note);
        return;
      }
      for (const candidate of result.candidates) {
        const card = document.createElement("div");
        card.className = "fact-candidate";
        const title = document.createElement("strong");
        title.textContent = labels[candidate.key] || candidate.key;
        const value = document.createElement("p");
        value.textContent = candidate.value;
        const evidence = document.createElement("small");
        evidence.textContent = `原文依据：“${candidate.source_quote}”`;
        const confirm = button("我已核对，确认保存", "button button-small button-primary");
        confirm.addEventListener("click", async () => {
          if (!view.active()) return;
          confirm.disabled = true;
          try {
            await api(`/api/projects/${view.id}/facts/confirm`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(candidate),
            });
            if (!view.active()) return;
            card.remove();
            await loadProject(view.id);
            if (view.active()) toast("已保存你确认的项目事实。");
          } catch (error) {
            if (view.active()) toast(error.message, true);
            confirm.disabled = false;
          }
        });
        card.append(title, value, evidence, confirm);
        root.append(card);
      }
    } catch (error) {
      if (!view.active()) return;
      toast(error.message, true);
      if ((error.message || "").includes("积分不足") || (error.message || "").includes("402")) {
        openRechargeDialog();
      }
    } finally {
      btn.disabled = false;
      btn.textContent = "从对话提取候选事实";
    }
  }

  function copyOutline(rows) {
    return (rows || []).map(({ id, title }) => ({ id, title }));
  }

  function sameOutline(a, b) {
    return a.length === b.length && a.every((row, index) => row.id === b[index].id && row.title === b[index].title);
  }

  function savedOutlineRows(rows) {
    if (!Array.isArray(rows) || !rows.every(row => typeof row?.id === "string" && typeof row.title === "string")) return null;
    return copyOutline(rows);
  }

  function persistOutlineEditor(projectId) {
    const draft = outlineDrafts.get(projectId);
    const preview = outlinePreviews.get(projectId);
    const value = draft || preview ? {
      draft: draft && { base: draft.base, rows: draft.rows, warning: draft.warning, submitted: draft.submitted },
      preview: preview && { outline: preview.outline, warning: preview.warning },
    } : null;
    if (outlineRecovery.write(projectId, value) || !value || outlineRecoveryWarning) return;
    outlineRecoveryWarning = true;
    toast("浏览器暂时无法保留目录草稿，请先保存目录再刷新。", true);
  }

  function restoreOutlineEditor(project) {
    const id = project.id;
    if (!outlineDrafts.has(id) && !outlinePreviews.has(id)) {
      const recovery = outlineRecovery.read(id);
      const base = savedOutlineRows(recovery?.draft?.base);
      const rows = savedOutlineRows(recovery?.draft?.rows);
      if (base && rows) outlineDrafts.set(id, {
        base, rows, warning: typeof recovery.draft.warning === "string" ? recovery.draft.warning : "",
        submitted: savedOutlineRows(recovery.draft.submitted) || undefined, recovered: true,
      });
      const outline = savedOutlineRows(recovery?.preview?.outline);
      if (outline?.length) outlinePreviews.set(id, {
        outline, warning: typeof recovery.preview.warning === "string" ? recovery.preview.warning : "",
      });
    }
    const draft = outlineDrafts.get(id);
    const saved = copyOutline(project.outline);
    if (draft && !draft.saving && project.state?.outline_confirmed) {
      if (draft.submitted && sameOutline(saved, draft.submitted)) {
        draft.base = saved;
        draft.submitted = undefined;
        if (sameOutline(saved, normalizedOutline(draft.rows))) draft.rows = saved;
      }
      if (!draft.submitted && sameOutline(saved, draft.rows)) outlineDrafts.delete(id);
    }
    state.outline = outlineDrafts.get(id)?.rows || saved;
    persistOutlineEditor(id);
  }

  function normalizedOutline(rows) {
    return rows.map(({ id, title }, index) => ({
      id: id.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 80) || `section_${index + 1}`,
      title: Array.from(title.trim()).slice(0, 100).join(""),
    }));
  }

  function editOutline(rows, warning = "") {
    const project = state.project;
    // Keep a stable copy of the directory on which this edit started.
    const draft = outlineDrafts.get(project.id) || { base: copyOutline(project.outline) };
    draft.rows = rows;
    draft.warning = warning;
    draft.recovered = false;
    if (!draft.saving && !draft.submitted && project.state?.outline_confirmed
        && sameOutline(draft.base, rows) && sameOutline(rows, copyOutline(project.outline))) outlineDrafts.delete(project.id);
    else outlineDrafts.set(project.id, draft);
    state.outline = rows;
    persistOutlineEditor(project.id);
    clearExportArtifacts();
  }

  function applyOutlinePreview(result) {
    const project = state.project;
    const current = copyOutline(project.outline);
    const remaining = new Map(current.map(row => [row.id, row]));
    const reserved = new Set([...remaining.keys(), ...Object.keys(project.sections || {})]);
    const used = new Set();
    const next = result.outline.map((item, index) => {
      const title = String(item.title || "").trim().slice(0, 100);
      if (!title) return null;
      const requested = String(item.id || "").replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 80) || `section_${index + 1}`;
      const sameId = remaining.get(requested);
      const matched = sameId?.title.trim() === title ? sameId
        : [...remaining.values()].find(row => row.title.trim() === title);
      let id = matched?.id || requested;
      if (matched) remaining.delete(matched.id);
      else if (reserved.has(id) || used.has(id)) {
        const prefix = id.slice(0, 65) + "_suggested";
        id = prefix;
        for (let number = 2; reserved.has(id) || used.has(id); number++) id = `${prefix}_${number}`;
      }
      used.add(id);
      return { id, title };
    }).filter(Boolean);
    if (!next.length) {
      toast(result.warning || "没有可用的候选章节。", true);
      return;
    }
    const preserved = [...remaining.values()].filter(row => String(project.sections?.[row.id] || "").trim()
      || sectionEdits.hasLocalContent(project.id, row.id));
    if (next.length + preserved.length > 20) {
      toast("已有正文的章节和候选章节合计超过 20 个，请先调整目录。", true);
      return;
    }
    outlinePreviews.delete(project.id);
    editOutline([...next, ...preserved], (result.warning || "请核对候选目录后保存。")
      + (preserved.length ? " 已保留含正文的原章节。" : ""));
    renderOutline();
    toast("候选目录已就绪，请核对后保存。");
  }

  function showOutlineFromTask(result) {
    if (!state.project || !result.outline?.length) return;
    outlinePreviews.set(state.project.id, result);
    persistOutlineEditor(state.project.id);
    if (!outlineDrafts.has(state.project.id)) applyOutlinePreview(result);
    else renderOutline();
    handleTabNavigation("outline-panel");
  }

  function hasPendingMaterialJob(projectId, kind, target = "") {
    return Boolean(window.MaterialPilot?.pendingJob?.(projectId, kind, target));
  }

  function shouldUseMaterialJob(projectId, kind, target) {
    if (hasPendingMaterialJob(projectId, kind, target)) return true;
    return window.MaterialPilot?.usesWorker() ?? false;
  }

  function syncOutlineGenerateLabel() {
    const btn = $("#generateOutlineBtn");
    const projectId = state.project?.id;
    if (btn && !outlineRequests.has(projectId))
      btn.textContent = hasPendingMaterialJob(projectId, "outline") ? "查看上次目录任务" : "拟一份目录";
  }

  async function suggestOutline(generate) {
    const view = currentProjectView();
    const btn = $(generate ? "#generateOutlineBtn" : "#resetOutlineBtn");
    if (!view.active() || !btn || outlineRequests.has(view.id)) return;
    const current = copyOutline(state.outline);
    const request = {};
    outlineRequests.set(view.id, request);
    btn.disabled = true;
    if (generate) btn.textContent = "正在整理…";
    try {
      const worker = generate && await shouldUseMaterialJob(view.id, "outline");
      if (!view.active()) return;
      const result = generate
        ? (worker
          ? await window.MaterialPilot.outline()
          : await observeMaterialRequest(`/api/projects/${view.id}/outline/generate`, { method: "POST" }, 180000,
            "目录生成结果暂未收到，请稍后查看，避免重复提交。"))
        : await api(`/api/projects/${view.id}/outline/recommend`);
      if (outlineRequests.get(view.id) !== request) return;
      if (!result.outline?.length) {
        if (view.active()) toast(result.warning || "暂未取得候选目录。", true);
        return;
      }
      outlinePreviews.set(view.id, result);
      persistOutlineEditor(view.id);
      if (view.active() && sameOutline(current, state.outline) && !outlineDrafts.has(view.id)) {
        applyOutlinePreview(result);
      } else {
        if (view.active()) {
          renderOutline();
          toast("候选目录已就绪，可在目录编辑器中查看。");
        }
      }
    } catch (error) {
      if (!view.active()) return;
      toast(error.message, true);
      if ((error.message || "").includes("积分不足") || (error.message || "").includes("402")) openRechargeDialog();
    } finally {
      if (outlineRequests.get(view.id) === request) outlineRequests.delete(view.id);
      btn.disabled = false;
      if (generate) syncOutlineGenerateLabel();
    }
  }

  const generateOutline = () => suggestOutline(true);
  const recommendOutline = () => suggestOutline(false);

  function renderOutline() {
    syncOutlineGenerateLabel();
    const list = $("#outlineList");
    list.replaceChildren();
    const view = currentProjectView();
    const saved = copyOutline(state.project?.outline);
    const draft = outlineDrafts.get(view.id);
    const conflict = draft && !sameOutline(draft.base, saved);
    const warning = $("#outlineWarning");
    warning.textContent = conflict ? "其他页面已修改目录，请核对后选择要保留的版本。"
      : draft?.submitted && !draft.saving ? "上一次目录保存待确认，请核对后保存。"
      : draft?.recovered ? "目录草稿已恢复，请核对后保存。"
      : draft?.warning || (draft ? "目录有待保存的修改。" : "");
    list.className = "outline-list";
    if (!state.outline.length) {
      const note = document.createElement("p");
      note.className = "empty-note";
      note.textContent = "还没有目录。可手动添加章节、使用赛道推荐目录或请 Agent 拟一份。";
      list.append(note);
    }
    const change = rows => {
      if (!view.active()) return;
      editOutline(rows);
      renderOutline();
    };
    state.outline.forEach((section, index) => {
      const row = document.createElement("div");
      row.className = "outline-row";
      const mark = document.createElement("span");
      mark.className = "drag-mark";
      mark.textContent = String(index + 1).padStart(2, "0");
      const input = document.createElement("input");
      input.value = section.title;
      input.maxLength = 100;
      input.addEventListener("input", () => {
        if (!view.active()) return;
        editOutline(state.outline.map((item, i) => i === index ? { ...item, title: input.value } : item));
        const hasDraft = outlineDrafts.has(view.id);
        if (!conflict || !hasDraft) warning.textContent = hasDraft ? "目录有待保存的修改。" : "";
        discard.classList.toggle("hidden", !hasDraft);
      });
      const move = (offset, label, title) => {
        const control = button(label, offset > 0 ? "remove-outline down-outline" : "remove-outline");
        control.title = title;
        const next = index + offset;
        control.disabled = next < 0 || next >= state.outline.length;
        control.addEventListener("click", () => {
          if (control.disabled || !view.active()) return;
          const rows = [...state.outline];
          [rows[index], rows[next]] = [rows[next], rows[index]];
          change(rows);
        });
        return control;
      };
      const remove = button("×", "remove-outline");
      remove.title = "移出目录";
      remove.addEventListener("click", () => change(state.outline.filter((_, i) => i !== index)));
      row.append(mark, input, move(-1, "↑", "上移"), move(1, "↓", "下移"), remove);
      list.append(row);
    });
    const full = state.outline.length >= 20;
    const add = button("＋ 添加章节", "button button-small button-quiet");
    add.disabled = full;
    add.addEventListener("click", () => {
      if (add.disabled || !view.active()) return;
      const suffix = globalThis.crypto?.randomUUID?.() || `${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
      change([...state.outline, { id: `section_${suffix}`, title: "新章节" }]);
      const allInputs = $$("#outlineList .outline-row input");
      allInputs.at(-1)?.focus();
      allInputs.at(-1)?.select();
    });
    list.append(add);
    const discard = button("恢复已保存目录", "button button-small button-quiet");
    discard.classList.toggle("hidden", !draft);
    discard.addEventListener("click", () => {
      if (!view.active() || outlineDrafts.get(view.id)?.saving) return;
      if (!confirm("放弃本页目录修改，恢复已保存的目录吗？")) return;
      outlineDrafts.delete(view.id);
      persistOutlineEditor(view.id);
      restoreOutlineEditor(state.project);
      renderOutline();
    });
    list.append(discard);
    const preview = outlinePreviews.get(view.id);
    if (preview) {
      const use = button("查看已生成的候选目录", "button button-small button-secondary");
      use.addEventListener("click", () => {
        if (!view.active()) return;
        if (!outlineDrafts.has(view.id) || confirm("用候选目录替换本页正在编辑的目录吗？")) applyOutlinePreview(preview);
      });
      list.append(use);
    }
    if (conflict) {
      const comparison = document.createElement("div");
      const caption = document.createElement("p");
      caption.textContent = saved.length ? `服务器目录：${saved.map(item => item.title).join("、")}` : "服务器当前目录为空。";
      const keep = button("保留本页目录", "button button-small button-secondary");
      keep.addEventListener("click", () => {
        if (!view.active() || outlineDrafts.get(view.id) !== draft || draft.saving) return;
        draft.base = saved;
        draft.submitted = undefined;
        persistOutlineEditor(view.id);
        renderOutline();
      });
      comparison.append(caption, keep);
      list.append(comparison);
    }
    const inEditor = new Set(state.outline.map(row => row.id));
    const archived = (state.project?.state?._archived_outline || []).filter(row => !inEditor.has(row.id));
    if (archived.length) {
      const history = document.createElement("details");
      const label = document.createElement("summary");
      label.textContent = "已移出的章节";
      history.append(label);
      for (const row of archived) {
        const restore = button(`恢复「${row.title}」`, "button button-small button-secondary");
        restore.disabled = full;
        restore.addEventListener("click", () => {
          if (!restore.disabled && view.active()) change([...state.outline, { ...row }]);
        });
        history.append(restore);
      }
      list.append(history);
    }
    $("#outlineActions").classList.remove("hidden");
    $("#saveOutlineBtn").disabled = !state.outline.length || Boolean(conflict || draft?.saving);
  }

  async function saveOutline() {
    const view = currentProjectView();
    if (!view.active() || !state.outline.length) return;
    const draft = outlineDrafts.get(view.id) || { base: copyOutline(state.project.outline), rows: state.outline };
    if (draft.saving || !sameOutline(draft.base, copyOutline(state.project.outline))) return;
    const rows = draft.rows;
    const removed = draft.base.filter(row => !rows.some(item => item.id === row.id)
      && (String(state.project.sections?.[row.id] || "").trim() || sectionEdits.hasLocalContent(view.id, row.id)));
    if (removed.length && !confirm("移出的章节将从后续 Word 导出中排除，正文可从「已移出的章节」恢复。继续保存吗？")) return;
    outlineDrafts.set(view.id, draft);
    draft.saving = true;
    const previousSubmission = draft.submitted;
    let submitted;
    $("#saveOutlineBtn").disabled = true;
    clearExportArtifacts();
    window.MaterialPilot?.invalidateArtifacts?.();
    try {
      await flushDraftEdits();
      if (!view.active()) return;
      submitted = normalizedOutline(rows);
      draft.submitted = submitted;
      persistOutlineEditor(view.id);
      const fencePreviousWrite = previousSubmission && sameOutline(submitted, draft.base);
      const result = await api(`/api/projects/${view.id}/outline`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ outline: rows, base_outline: draft.base,
          expected_revision: fencePreviousWrite ? state.project.material_revision : undefined }),
      });
      const stillTracked = outlineDrafts.get(view.id) === draft;
      if (stillTracked) {
        draft.base = copyOutline(result.outline);
        draft.submitted = undefined;
        if (draft.rows === rows) outlineDrafts.delete(view.id);
        persistOutlineEditor(view.id);
      }
      if (!view.active()) {
        if (stillTracked && chatProjectIsActive(view.id)) {
          draft.saving = false;
          await loadProject(view.id, { refreshList: false });
        }
        return;
      }
      if (result.material_revision < state.project.material_revision) {
        await loadProject(view.id, { refreshList: false });
        return;
      }
      state.project.outline = copyOutline(result.outline);
      const emptySections = Object.fromEntries(result.outline.map(item => [item.id, ""]));
      state.project.sections = { ...emptySections, ...state.project.sections };
      state.project.state ||= {};
      state.project.state.outline_confirmed = true;
      if (result.archived_outline) state.project.state._archived_outline = result.archived_outline;
      if (result.material_revision !== undefined) state.project.material_revision = result.material_revision;
      restoreOutlineEditor(state.project);
      renderSections();
      if (window.MaterialPilot?.active()) void window.MaterialPilot.refresh().catch(() => {});
      toast(draft.rows === rows ? "目录已确认。现在可以逐章生成或编辑。" : "已保存提交时的目录，本页还有新修改待保存。");
    } catch (error) {
      if (submitted && outlineDrafts.get(view.id) === draft && error.status >= 400 && error.status < 500) {
        draft.submitted = previousSubmission;
        persistOutlineEditor(view.id);
      }
      if (!view.active()) return;
      if (error.status === 409) {
        draft.saving = false;
        await loadProject(view.id, { refreshList: false });
      }
      if (view.active()) toast(error.message, true);
    } finally {
      draft.saving = false;
      if (view.active() || (chatProjectIsActive(view.id) && outlineDrafts.get(view.id) === draft)) renderOutline();
    }
  }

  function updateDraftStats() {
    const badge = $("#draftStatsBadge");
    if (!badge) return;
    const outline = state.project?.outline || [];
    const sections = state.project?.sections || {};
    const totalWords = outline.reduce((sum, s) => sum + countWords(sections[s.id] || ""), 0);
    const completed = outline.filter(s => (sections[s.id] || "").trim().length > 0).length;
    badge.textContent = `${completed}/${outline.length} 章节 · 共 ${totalWords} 字`;
  }

  function setDraftSaveStatus(statusPill, saved) {
    statusPill.className = `section-status-pill ${saved ? "saved" : "unsaved"}`;
    statusPill.textContent = saved ? "✓ 已保存" : "● 未保存";
  }

  let allSectionsFolded = false;
  async function observeMaterialRequest(url, options, timeoutMs, timeoutMessage) {
    // Timing out the browser's wait does not establish whether the server or
    // model finished. Never automatically resubmit a potentially paid request.
    const controller = new AbortController();
    let timer;
    let timedOut = false;
    try {
      return await Promise.race([
        api(url, { ...options, signal: controller.signal }),
        new Promise((_, reject) => { timer = setTimeout(() => {
          timedOut = true;
          controller.abort();
          reject(new Error(timeoutMessage));
        }, timeoutMs); }),
      ]);
    } catch (error) {
      if (timedOut) throw new Error(timeoutMessage);
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  const SECTION_WAIT_NOTICE = "等待时间过长，当前章节可能仍在后台处理，结果和费用状态尚未确认。请先刷新项目、查看任务和费用，再决定是否重新生成；系统不会自动重复提交。";
  const SECTION_STATUS_NOTICE = "暂时无法获取后台任务状态，任务可能仍在进行；请先查看任务和费用，不要重复生成。";

  function toggleAllSections() {
    allSectionsFolded = !allSectionsFolded;
    $$(".draft-section-body").forEach(body => {
      body.classList.toggle("folded", allSectionsFolded);
    });
    $$(".fold-toggle-btn").forEach(btn => {
      btn.textContent = allSectionsFolded ? "▶" : "▼";
    });
    const btn = $("#toggleAllSectionsBtn");
    if (btn) btn.textContent = allSectionsFolded ? "展开全部" : "折叠全部";
  }

  function rememberSectionCandidate(key, content) {
    const drafts = uncertainSectionDrafts.get(key) || [];
    if (!drafts.includes(content)) drafts.push(content);
    uncertainSectionDrafts.set(key, drafts);
  }

  function syncBatchGenerateLabel() {
    const button = $("#batchGenerateSectionsBtn");
    if (!button || state.batchGenerating) return;
    const project = state.project;
    const pending = project?.outline?.some(section => hasPendingMaterialJob(project.id, "section", section.id));
    button.textContent = pending ? "查看待确认的章节任务" : "⚡ 批量生成所有章节";
  }

  async function startBatchGeneration() {
    if (!state.project?.outline?.length) {
      toast("请先拟定并确认目录", true);
      return;
    }
    if (state.batchGenerating) return;

    state.batchGenerating = true;
    state.batchAbort = false;
    const projectId = state.project.id;
    const view = currentProjectView();
    const accountId = state.authSession?.user?.id || null;
    const generationEpoch = chatRecoveryEpoch;
    const deletionEpoch = deletedProjectEpochs.get(projectId) || 0;
    const isCurrent = () => chatRecoveryEpoch === generationEpoch &&
      (state.authSession?.user?.id || null) === accountId &&
      (deletedProjectEpochs.get(projectId) || 0) === deletionEpoch && view.active();
    try {
      if ([...document.querySelectorAll(".draft-section textarea")].some(area => {
        const sid = area.closest(".draft-section")?.id?.slice("draft-sec-".length);
        return sid && area.value !== (state.project.sections?.[sid] || "");
      })) await flushDraftEdits();
    } catch (error) {
      state.batchGenerating = false;
      state.batchAbort = false;
      if (isCurrent()) toast(error.message || "请先保存章节编辑，再开始批量生成。", true);
      return;
    }
    if (!isCurrent()) { state.batchGenerating = false; state.batchAbort = false; return; }
    const chapterId = area => area.closest(".draft-section").id.slice("draft-sec-".length);
    const editorBaselines = new Map([...document.querySelectorAll(".draft-section textarea")]
      .map(area => [chapterId(area), area.value]));
    const editorsChanged = () => [...document.querySelectorAll(".draft-section textarea")]
      .some(area => editorBaselines.get(chapterId(area)) !== area.value);

    const wrapper = $("#batchProgressWrapper");
    const fill = $("#batchProgressFill");
    const text = $("#batchProgressText");
    const batchBtn = $("#batchGenerateSectionsBtn");
    const outline = copyOutline(state.project.outline);
    const pending = outline.filter(section => hasPendingMaterialJob(projectId, "section", section.id));
    const recoveryOnly = pending.length > 0;
    const chapters = recoveryOnly ? pending : outline;

    if (wrapper) wrapper.classList.remove("hidden");
    if (batchBtn) {
      batchBtn.disabled = true;
      batchBtn.textContent = recoveryOnly ? "正在查询任务…" : "批量生成中…";
    }

    let completedCount = 0;
    const total = chapters.length;
    let stopped = "";

    for (let i = 0; i < total; i++) {
      if (state.batchAbort || !isCurrent()) {
        stopped = state.batchAbort ? "已停止后续章节" : "已切换项目，停止后续章节";
        break;
      }
      const section = chapters[i];
      // Another control may have finished this recovery since batch started.
      if (recoveryOnly && !hasPendingMaterialJob(projectId, "section", section.id)) continue;
      const base = state.project.sections?.[section.id] || "";
      if (fill) fill.style.width = `${Math.round(((i + 0.1) / total) * 100)}%`;
      if (text) text.textContent = `${recoveryOnly ? "正在查询" : "正在生成"} [${i + 1}/${total}]：${section.title}…`;

      try {
        const worker = await shouldUseMaterialJob(projectId, "section", section.id);
        if (state.batchAbort || !isCurrent()) {
          stopped = state.batchAbort ? "已停止后续章节" : "已切换项目，停止后续章节";
          break;
        }
        const result = worker ? await window.MaterialPilot.section(section.id, null)
          : await observeMaterialRequest(`/api/projects/${projectId}/sections/${section.id}/generate`,
              { method: "POST" }, 180000, SECTION_WAIT_NOTICE);
        if (!isCurrent()) { stopped = "已切换项目，停止后续章节"; break; }
        if (result?.status === "processing") {
          stopped = "当前章节仍在后台处理，请先查看任务状态；已停止后续章节，未重复提交";
          break;
        }
        if (!result || (typeof result.content !== "string" && result.conflict_status !== "user_edit_preserved")) {
          stopped = "未收到完整结果，请先查看当前章节；已停止后续章节，未重复提交";
          break;
        }
        if (typeof result.content === "string" && result.conflict_status !== "user_edit_preserved") {
          clearExportArtifacts();
          window.MaterialPilot?.invalidateArtifacts?.();
        }
        if (editorsChanged()) {
          // Generation may already be saved on the server. Keep it as the
          // baseline, but never replace text typed while the request ran.
          if (result.conflict_status !== "user_edit_preserved" && typeof result.content === "string") {
            if (result.material_applied) applyGeneratedSection(section.id, result.content, result.material_revision);
            else rememberSectionCandidate(`${projectId}\0${section.id}`, result.content);
          }
          stopped = "生成期间的手写编辑已保留，请保存后再继续；已停止后续章节";
          break;
        }
        if (state.batchAbort) { stopped = "已停止后续章节；当前章节结果请在原项目中查看"; break; }
        if (result.conflict_status === "user_edit_preserved") {
          stopped = (result.notice || "当前章节需要核对，结果已保留为备选") + "；已停止后续章节";
          break;
        }
        if (result.requires_student_input) {
          stopped = (result.notice || "当前章节需要补充信息") + "；已停止后续章节";
          break;
        }
        let revision;
        try {
          revision = await confirmGeneratedSection(view, section.id, base, result);
        } catch (error) {
          if (isCurrent()) rememberSectionCandidate(`${projectId}\0${section.id}`, result.content);
          throw error;
        }
        if (!isCurrent()) break;
        const changed = editorsChanged();
        if (!applyGeneratedSection(section.id, result.content, revision) || changed) {
          stopped = "章节在生成期间发生变化，请核对正文后继续";
          break;
        }
        if (editorBaselines.has(section.id)) editorBaselines.set(section.id, result.content);
        completedCount++;
      } catch (err) {
        console.warn(`生成章节 ${section.title} 失败:`, err);
        stopped = "当前章节结果或费用状态尚未确认，请先查看；已停止后续章节，未自动重试";
        break;
      }
    }

    if (fill && !stopped) fill.style.width = "100%";
    const done = recoveryOnly ? "章节任务查询完成" : "批量生成结束";
    if (text) text.textContent = `${stopped || done}，共完成 ${completedCount}/${total} 章节。`;
    if (isCurrent()) {
      if (!editorsChanged()) renderSections();
      updateDraftStats();
      toast(stopped || `${done}，共更新 ${completedCount} 个章节。`, Boolean(stopped));
    }

    setTimeout(() => {
      if (wrapper) wrapper.classList.add("hidden");
    }, 2800);

    state.batchGenerating = false;
    state.batchAbort = false;
    if (batchBtn) batchBtn.disabled = false;
    syncBatchGenerateLabel();
  }

  function cancelBatchGeneration() {
    state.batchAbort = true;
    const progressText = $("#batchProgressText");
    if (progressText) progressText.textContent = "已发起取消，正在等待当前章节收尾…";
  }


  function renderReviewCard(wrapper, card) {
    if (!wrapper || !card) return;
    let cardEl = wrapper.querySelector(".agentic-review-card");
    if (!cardEl) {
      cardEl = document.createElement("div");
      cardEl.className = "agentic-review-card";
      wrapper.append(cardEl);
    }
    cardEl.replaceChildren();

    const header = document.createElement("div");
    header.className = "agentic-review-header";

    const scoreTitle = document.createElement("div");
    scoreTitle.className = "agentic-review-score";
    const incomplete = card.review_status === "failed" || !Number.isFinite(card.score);
    scoreTitle.textContent = incomplete
      ? "AI 辅助检查：评审未完成（无分数）"
      : `AI 辅助检查：${card.score} 分 · ${card.grade || ""}（非官方评审）`;

    const badge = document.createElement("span");
    badge.className = `agentic-review-badge ${card.passed && !incomplete ? "" : "warning"}`;
    badge.textContent = incomplete ? "草稿保留 · 待重试评审"
      : `${card.iterations} 轮检查 · ${card.passed ? "通过辅助检查" : "待完善"}`;

    header.append(scoreTitle, badge);
    cardEl.append(header);

    if (Array.isArray(card.user_hints) && card.user_hints.length > 0) {
      for (const hint of card.user_hints) {
        const hintEl = document.createElement("div");
        hintEl.className = "agentic-review-hint";
        hintEl.textContent = hint;
        cardEl.append(hintEl);
      }
    }
  }

  const sectionVersionLabels = new Map([
    ["before_restore", "恢复前的正文"], ["restore", "已恢复的正文"],
    ["before_chat_replacement", "聊天草稿替换前的正文"], ["ai_generated", "生成草稿"],
    ["ai_generated_alternative", "备选草稿"], ["user_edit", "手写正文"],
  ]);

  function renderSections() {
    syncBatchGenerateLabel();
    const root = $("#draftSections");
    sectionEdits.detachEditors();
    root.replaceChildren();
    if (mobileLayout?.matches) {
      allSectionsFolded = false;
      $("#toggleAllSectionsBtn").textContent = "折叠全部";
    }
    updateDraftStats();
    if (!state.project?.outline?.length) {
      const note = document.createElement("div");
      note.className = "empty-note";
      note.textContent = "确认目录后，这里会出现章节编辑器。";
      root.append(note);
      return;
    }
    for (const section of state.project.outline) {
      const boundProjectId = state.project?.id;
      const uncertainKey = `${boundProjectId}\0${section.id}`;
      const wrapper = document.createElement("div");
      wrapper.className = "draft-section";
      wrapper.id = `draft-sec-${section.id}`;
      const head = document.createElement("div");
      head.className = "draft-section-head";

      const titleGroup = document.createElement("div");
      titleGroup.className = "draft-title-group";
      const foldToggle = document.createElement("button");
      foldToggle.type = "button";
      foldToggle.className = "fold-toggle-btn";
      foldToggle.textContent = "▼";
      foldToggle.title = "展开/折叠章节内容";

      const title = document.createElement("strong");
      title.textContent = section.title;

      const remoteContent = state.project.sections?.[section.id] || "";
      const draft = sectionEdits.open(uncertainKey, remoteContent);
      const initialContent = draft.content;
      const wordBadge = document.createElement("span");
      wordBadge.className = "section-word-badge";
      wordBadge.textContent = `${countWords(initialContent)} 字`;

      const statusPill = document.createElement("span");
      statusPill.className = `section-status-pill ${initialContent.trim() ? "saved" : "unsaved"}`;
      statusPill.textContent = initialContent.trim() ? "✓ 已有草稿" : "● 待撰写";

      titleGroup.append(foldToggle, title, wordBadge, statusPill);

      const actions = document.createElement("div");
      actions.className = "draft-section-actions";

      const copy = button("📋 复制", "button button-small button-quiet");
      copy.title = "复制本章节草稿正文到剪贴板";

      const generationLabel = () => hasPendingMaterialJob(boundProjectId, "section", section.id)
        ? "查看上次生成任务" : "生成草稿";
      const generate = button(generationLabel(), "button button-small button-secondary");
      const save = button("保存", "button button-small button-primary");
      const history = button("历史版本", "button button-small button-quiet");
      actions.append(copy, generate, save, history);
      head.append(titleGroup, actions);

      const bodyWrapper = document.createElement("div");
      bodyWrapper.className = "draft-section-body";

      // Toolbar for mode switch and quick placeholders
      const toolbar = document.createElement("div");
      toolbar.className = "draft-section-toolbar";

      const leftTools = document.createElement("div");
      leftTools.className = "toolbar-group";
      const editModeBtn = document.createElement("button");
      editModeBtn.type = "button";
      editModeBtn.className = "mode-switch-btn active";
      editModeBtn.textContent = "✏️ 编辑";

      const previewModeBtn = document.createElement("button");
      previewModeBtn.type = "button";
      previewModeBtn.className = "mode-switch-btn";
      previewModeBtn.textContent = "👁️ 预览排版";

      leftTools.append(editModeBtn, previewModeBtn);

      const rightTools = document.createElement("div");
      rightTools.className = "toolbar-group";
      const placeholderBtn = document.createElement("button");
      placeholderBtn.type = "button";
      placeholderBtn.className = "placeholder-btn";
      placeholderBtn.textContent = "＋ 插入【待补充】";
      placeholderBtn.title = "在光标处插入待补充标记";

      rightTools.append(placeholderBtn);
      toolbar.append(leftTools, rightTools);

      const editor = document.createElement("textarea");
      editor.value = initialContent;
      editor.placeholder = "点击“生成草稿”，或直接在这里撰写。缺失具体数据或依据时标注【待补充】。";

      const previewBox = document.createElement("div");
      previewBox.className = "section-preview-render hidden";

      const versionsBox = document.createElement("div");
      versionsBox.className = "section-versions hidden";
      const uncertainBox = document.createElement("div");
      uncertainBox.className = "section-versions hidden";

      const conflictBox = document.createElement("div");
      conflictBox.className = "section-versions hidden";
      function paintDraft() {
        wordBadge.textContent = `${countWords(draft.content)} 字`;
        setDraftSaveStatus(statusPill, draft.content === draft.base);
        if (draft.recovered && draft.content !== draft.base) statusPill.textContent = "● 已恢复，未保存";
        if (draft.submitted !== undefined && draft.content === draft.base) {
          statusPill.className = "section-status-pill unsaved";
          statusPill.textContent = "● 保存待确认";
        }
        conflictBox.replaceChildren();
        const conflict = draft.base !== draft.remote;
        conflictBox.classList.toggle("hidden", !conflict);
        if (!conflict) return;
        statusPill.className = "section-status-pill unsaved";
        statusPill.textContent = "● 两处编辑待核对";
        const label = document.createElement("p");
        label.textContent = "本章节在其他页面有修改。请对照下方最新正文，选择要保留的内容。";
        const latest = document.createElement("pre");
        latest.textContent = draft.remote || "（正文为空）";
        const keep = button("保留我的编辑", "button button-small button-secondary");
        const useLatest = button("使用最新正文", "button button-small button-quiet");
        keep.addEventListener("click", () => resolveConflict(false));
        useLatest.addEventListener("click", () => resolveConflict(true));
        conflictBox.append(label, latest, keep, useLatest);
      }
      function resolveConflict(useRemote) {
        if (!currentEditor()) return;
        if (useRemote) editor.value = draft.remote;
        recordInput(true);
      }
      function currentEditor() {
        return chatProjectIsActive(boundProjectId) && sectionEdits.isAttached(uncertainKey, editor);
      }
      function recordInput(resolveConflict = false) {
        if (!sectionEdits.isAttached(uncertainKey, editor) || state.project?.id !== boundProjectId) return;
        clearExportArtifacts();
        window.MaterialPilot?.invalidateArtifacts?.();
        sectionEdits.record(uncertainKey, editor.value, resolveConflict);
        paintDraft();
      }
      draft.display = { editor, paint: paintDraft };
      if (draft.base !== draft.content || draft.base !== draft.remote || draft.submitted !== undefined) paintDraft();

      function renderUncertainDrafts() {
        uncertainBox.replaceChildren();
        const drafts = uncertainSectionDrafts.get(uncertainKey) || [];
        for (const content of drafts) {
          const row = document.createElement("div");
          const note = document.createElement("p");
          note.textContent = "草稿保存状态不明。请先复制，或放入编辑器后保存；不必重复生成。";
          const text = document.createElement("pre");
          text.textContent = content;
          const copyDraft = button("复制草稿", "button button-small button-quiet");
          copyDraft.addEventListener("click", async () => {
            try { await navigator.clipboard.writeText(content); toast("已复制草稿。"); }
            catch { toast("复制失败，请手动选取草稿内容。", true); }
          });
          const use = button("放入编辑器", "button button-small button-secondary");
          use.addEventListener("click", () => {
            if (!chatProjectIsActive(boundProjectId)) return;
            if (editor.value !== (state.project?.sections?.[section.id] || "") &&
                !confirm("当前编辑内容还未保存，确定用这份草稿替换编辑框内容吗？")) return;
            editor.value = content;
            recordInput();
            toast("已放入编辑器，保存后生效。");
          });
          row.append(note, text, copyDraft, use);
          uncertainBox.append(row);
        }
        uncertainBox.classList.toggle("hidden", !drafts.length);
      }
      renderUncertainDrafts();
      history.addEventListener("click", async () => {
        if (!versionsBox.classList.contains("hidden")) {
          versionsBox.classList.add("hidden");
          return;
        }
        if (!currentEditor()) return;
        const view = currentProjectView();
        const versionsUrl = `/api/projects/${encodeURIComponent(view.id)}/sections/${encodeURIComponent(section.id)}/versions`;
        history.disabled = true;
        try {
          const data = await api(versionsUrl);
          if (!currentEditor()) return;
          versionsBox.replaceChildren();
          for (const version of data.versions || []) {
            if (typeof version.content !== "string") continue;
            const row = document.createElement("div");
            const label = document.createElement("strong");
            const savedAt = new Date(version.created_at);
            const source = sectionVersionLabels.get(version.source) || "历史版本";
            label.textContent = Number.isNaN(savedAt.getTime()) ? source : `${source} · ${savedAt.toLocaleString("zh-CN")}`;
            const text = document.createElement("pre");
            text.textContent = version.content || "（正文为空）";
            const use = button("放入编辑器", "button button-small button-secondary");
            use.addEventListener("click", () => {
              if (!currentEditor()) return;
              if (editor.value !== (state.project?.sections?.[section.id] || "") &&
                  !confirm("当前编辑内容还未保存，确定用历史版本替换编辑框内容吗？")) return;
              editor.value = version.content;
              recordInput();
              versionsBox.classList.add("hidden");
              updateDraftStats();
              toast("已放入编辑器，保存后生效。");
            });
            row.append(label, text, use);
            if (version.id != null) {
              const restore = button("恢复并保存", "button button-small button-secondary");
              restore.addEventListener("click", async () => {
                if (!currentEditor() || restore.disabled) return;
                const dirty = editor.value !== (state.project.sections?.[section.id] || "");
                if (dirty && !confirm("恢复前会先保存你正在编辑的正文，可在历史版本中找回。继续恢复吗？")) return;
                restore.disabled = true;
                try {
                  await flushDraftEdits([{ area: editor, section: wrapper }]);
                  if (!currentEditor()) return;
                  clearExportArtifacts();
                  window.MaterialPilot?.invalidateArtifacts?.();
                  const restored = await api(`${versionsUrl}/${encodeURIComponent(version.id)}/restore`, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ expected_revision: state.project.material_revision,
                      base_content: state.project.sections?.[section.id] || "" }),
                  });
                  if (!view.active()) return;
                  clearExportArtifacts();
                  window.MaterialPilot?.invalidateArtifacts?.();
                  await loadProject(view.id, { refreshList: false });
                  if (view.active()) toast(restored.notice || "已恢复所选版本。");
                } catch (error) {
                  if (view.active()) toast(`恢复未确认：${error.message}`, true);
                } finally {
                  restore.disabled = false;
                }
              });
              row.append(restore);
            }
            versionsBox.append(row);
          }
          if (!versionsBox.children.length) versionsBox.textContent = "暂无历史版本。";
          versionsBox.classList.remove("hidden");
        } catch (error) {
          if (currentEditor()) toast(`读取历史版本失败：${error.message}`, true);
        } finally {
          history.disabled = false;
        }
      });

      function updatePreview() {
        previewBox.innerHTML = editor.value.trim()
          ? renderMarkdownSafe(editor.value)
          : '<p style="color:var(--text-subtle);font-style:italic;">暂无草稿内容，点击上方“生成草稿”或直接输入。</p>';
      }

      editModeBtn.addEventListener("click", () => {
        editModeBtn.classList.add("active");
        previewModeBtn.classList.remove("active");
        editor.classList.remove("hidden");
        previewBox.classList.add("hidden");
      });

      previewModeBtn.addEventListener("click", () => {
        previewModeBtn.classList.add("active");
        editModeBtn.classList.remove("active");
        updatePreview();
        editor.classList.add("hidden");
        previewBox.classList.remove("hidden");
      });

      placeholderBtn.addEventListener("click", () => {
        const start = editor.selectionStart || 0;
        const end = editor.selectionEnd || 0;
        const text = editor.value;
        const insert = "【待补充：请核实具体数据/依据】";
        editor.value = text.slice(0, start) + insert + text.slice(end);
        editor.focus();
        editor.selectionStart = editor.selectionEnd = start + insert.length;
        editor.dispatchEvent(new Event("input"));
      });

      editor.addEventListener("input", () => recordInput());

      editor.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "s") {
          e.preventDefault();
          save.click();
        }
      });

      foldToggle.addEventListener("click", () => {
        const isFolded = bodyWrapper.classList.toggle("folded");
        foldToggle.textContent = isFolded ? "▶" : "▼";
      });

      const sources = document.createElement("div");
      sources.className = "source-hints";
      sources.textContent = "尚无本章节材料来源标记";

      copy.addEventListener("click", async () => {
        if (!editor.value) {
          toast("当前章节内容为空，暂无可复制文本", true);
          return;
        }
        try {
          await navigator.clipboard.writeText(editor.value);
          toast(`已复制“${section.title}”正文`);
        } catch {
          editor.select();
          document.execCommand("copy");
          toast(`已复制“${section.title}”正文`);
        }
      });

      generate.addEventListener("click", async () => {
        const generationProjectId = state.project?.id;
        if (!currentEditor()) return;
        clearExportArtifacts();
        window.MaterialPilot?.invalidateArtifacts?.();
        const view = currentProjectView();
        const generationEpoch = chatRecoveryEpoch;
        const generationUserId = state.authSession?.user?.id || null;
        const deletionEpoch = deletedProjectEpochs.get(generationProjectId) || 0;
        const mayRetainResult = () => generationEpoch === chatRecoveryEpoch &&
          generationUserId === (state.authSession?.user?.id || null) &&
          deletionEpoch === (deletedProjectEpochs.get(generationProjectId) || 0);
        generate.disabled = true;
        generate.textContent = "生成中…";
        statusPill.className = "section-status-pill generating";
        statusPill.textContent = "✦ 生成中…";
        wrapper.classList.add("generating");
        try {
          if (editor.value !== (state.project?.sections?.[section.id] || "")) {
            await flushDraftEdits();
            if (!mayRetainResult() || !currentEditor()) return;
          }
          const editorAtStart = editor.value;
          const worker = await shouldUseMaterialJob(generationProjectId, "section", section.id);
          if (!mayRetainResult() || !currentEditor()) return;
          let result = worker ? await window.MaterialPilot.section(section.id, editor)
            : await observeMaterialRequest(`/api/projects/${generationProjectId}/sections/${section.id}/generate`,
                { method: "POST" }, 180000, SECTION_WAIT_NOTICE);
          if (!mayRetainResult()) return;
          if (result && result.status === "processing" && result.job_id) {
            statusPill.textContent = "✦ 后台生成中…";
            let completed = false;
            for (let i = 0; i < 45; i++) {
              await new Promise(r => setTimeout(r, 2000));
              if (!mayRetainResult()) return;
              const jobSt = await observeMaterialRequest(`/api/projects/${generationProjectId}/sections/${section.id}/jobs/${result.job_id}`,
                {}, 15000, SECTION_STATUS_NOTICE);
              if (jobSt && jobSt.status === "completed") {
                result = jobSt.result || {};
                completed = true;
                break;
              }
              if (jobSt && ["failed", "cancelled", "needs_review"].includes(jobSt.status)) {
                throw new Error(jobSt.error_message || "后台生成失败");
              }
              statusPill.textContent = `✦ 生成中(${jobSt.step || "执行中"})…`;
            }
            if (!completed) {
              if (chatProjectIsActive(generationProjectId)) {
                statusPill.className = "section-status-pill unsaved";
                statusPill.textContent = "● 后台处理中";
                toast("当前章节仍在后台处理，请先查看任务状态和费用，再决定是否重新生成。", true);
              }
              return;
            }
          }

          if (!mayRetainResult()) return;
          if (result?.result_save_uncertain && typeof result.content === "string" && result.content.trim())
            rememberSectionCandidate(uncertainKey, result.content);
          if (!view.active()) return;

          if (result?.requires_student_input && editor.value.trim()) {
            const changed = editor.value !== (state.project?.sections?.[section.id] || "");
            statusPill.className = `section-status-pill ${changed ? "unsaved" : "saved"}`;
            statusPill.textContent = changed ? "● 未保存" : "✓ 已保存";
            toast(result.notice || "请先补充个人情况或相关材料。");
            return;
          }

          if (result.conflict_status === "user_edit_preserved") {
            if (result.citation_check?.warnings?.length)
              sources.textContent = result.citation_check.warnings.join(" ");
            if (result.result_save_uncertain) {
              if (currentEditor()) renderUncertainDrafts();
              else renderSections();
            }
            toast(result.notice || "检测到您已编辑该章节，AI最新草稿已安全存入历史版本，未覆盖您的正文。", false);
            statusPill.className = `section-status-pill ${result.result_save_uncertain ? "unsaved" : "saved"}`;
            statusPill.textContent = result.result_save_uncertain ? "● 草稿待保存" : "● 有新备选版本";
            if (result.review_card) {
              renderReviewCard(wrapper, result.review_card);
            }
          } else if (result.content) {
            clearExportArtifacts();
            window.MaterialPilot?.invalidateArtifacts?.();
            if (!result.material_applied && (draft.display?.editor.value ?? draft.content) !== editorAtStart) {
              rememberSectionCandidate(uncertainKey, result.content);
              renderSections();
              toast("生成期间的新编辑已保留，生成结果可在候选草稿中查看。");
              return;
            }
            let revision;
            try {
              revision = await confirmGeneratedSection(view, section.id, editorAtStart, result);
            } catch (error) {
              if (mayRetainResult()) rememberSectionCandidate(uncertainKey, result.content);
              if (view.active()) {
                renderSections();
                toast(`生成结果已保留。请核对正文后保存：${error.message}`, true);
              }
              return;
            }
            if (!view.active()) return;
            if (!applyGeneratedSection(section.id, result.content, revision)) {
              await loadProject(generationProjectId, { refreshList: false });
              if (view.active()) toast("已有更新的章节版本，请核对最新正文。");
              return;
            }
            if (!currentEditor()) return;
            if (editor.value !== result.content) {
              toast("生成期间的新编辑已保留，请检查并保存。", true);
              return;
            }
            const evidence = (result.evidence || []).map((e) => `${e.source_name} · ${e.source_ref}`).join("；");
            const publicRefs = (result.public_knowledge?.results || [])
              .map((e) => `${e.kind === "official" ? "官方摘要" : "非官方指南"}：${e.title} · ${e.source_ref}`).join("；");
            sources.textContent = (evidence ? `项目材料：${evidence}` : "未检索到项目材料；请补充事实或保留“待补充”。")
              + (publicRefs ? ` 公共参考：${publicRefs}。请核对来源和适用范围。` : "");
            if (result.review_card) {
              renderReviewCard(wrapper, result.review_card);
            }
            statusPill.className = "section-status-pill saved";
            statusPill.textContent = "✓ 已保存";
            updateDraftStats();
            toast(result.notice || "章节草稿已生成并自动保存。");
          }
        } catch (error) {
          if (!currentEditor()) return;
          statusPill.className = "section-status-pill unsaved";
          statusPill.textContent = ([SECTION_WAIT_NOTICE, SECTION_STATUS_NOTICE].includes(error.message)
            || /可能.*(?:后台|开始)|任务已提交|暂未收到草稿保存结果|草稿已处理/.test(error.message || ""))
            ? "● 结果待确认" : "● 生成失败";
          toast(error.message, true);
          if ((error.message || "").includes("积分不足") || (error.message || "").includes("402")) {
            openRechargeDialog();
          }
        } finally {
          generate.disabled = false;
          generate.textContent = generationLabel();
          syncBatchGenerateLabel();
          wrapper.classList.remove("generating");
        }
      });
      save.addEventListener("click", async () => {
        if (!currentEditor()) return;
        clearExportArtifacts();
        window.MaterialPilot?.invalidateArtifacts?.();
        const currentVal = editor.value;
        save.disabled = true;
        save.textContent = "保存中…";
        try {
          await flushDraftEdits([{ area: editor, section: wrapper }]);
          if (!currentEditor()) return;
          const drafts = uncertainSectionDrafts.get(uncertainKey) || [];
          const remaining = drafts.filter(content => content !== currentVal);
          if (remaining.length) uncertainSectionDrafts.set(uncertainKey, remaining);
          else uncertainSectionDrafts.delete(uncertainKey);
          renderUncertainDrafts();
          paintDraft();
          if (state.project?.material?.enabled) void window.MaterialPilot?.refresh();
          toast(`已保存“${section.title}”。`);
        } catch (error) {
          if (!currentEditor()) return;
          paintDraft();
          toast(`保存失败：${error.message}`, true);
        } finally {
          save.disabled = false;
          save.textContent = "保存";
        }
      });

      bodyWrapper.append(toolbar, editor, previewBox, versionsBox, uncertainBox, sources, conflictBox);
      wrapper.append(head, bodyWrapper);
      root.append(wrapper);
    }
  }

  async function proposeDiagram() {
    const btn = $("#proposeDiagramBtn");
    btn.disabled = true;
    btn.textContent = "请稍候…";
    try {
      const result = await api(`/api/projects/${state.project.id}/diagrams/propose`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: $("#diagramType").value }),
      });
      $("#diagramTitle").value = result.title;
      $("#diagramNodes").value = result.nodes.join("\n");
      $("#diagramEditor").classList.remove("hidden");
      toast("图表节点已拟好，请修改和确认后再生成。");
    } catch (error) {
      toast(error.message, true);
      if ((error.message || "").includes("积分不足") || (error.message || "").includes("402")) {
        openRechargeDialog();
      }
    } finally {
      btn.disabled = false;
      btn.textContent = "请 Agent 提建议";
    }
  }

  async function saveDiagram() {
    const nodes = $("#diagramNodes").value.split(/\r?\n/).map((n) => n.trim()).filter(Boolean);
    try {
      const result = await api(`/api/projects/${state.project.id}/diagrams`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: $("#diagramTitle").value || $("#diagramType").value,
          nodes,
          diagram_id: state.editingDiagramId,
        }),
      });
      state.project.diagrams = state.project.diagrams || [];
      const diagram = result.diagram;
      if (diagram.preview) diagram.preview_url = `/api/projects/${state.project.id}/diagrams/${diagram.id}/preview`;
      state.project.diagrams = state.project.diagrams.filter((d) => d.id !== diagram.id).concat(diagram);
      state.diagrams = state.project.diagrams;
      renderDiagrams();
      $("#diagramEditor").classList.add("hidden");
      state.editingDiagramId = "";
      $("#saveDiagramBtn").textContent = "确认并生成图";
      toast(result.warning || "图表已保存。");
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function generateWorkbenchImage() {
    if (!state.project) return toast("请先打开项目", true);
    const prompt = $("#workbenchImagePrompt").value.trim();
    if (prompt.length < 5) return toast("请补充具体的配图描述", true);
    const btn = $("#generateWorkbenchImageBtn");
    btn.disabled = true;
    try {
      const result = await api(`/api/projects/${state.project.id}/images/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt }),
      });
      state.project.diagrams = [...(state.project.diagrams || []), result.diagram];
      state.diagrams = state.project.diagrams;
      renderDiagrams();
      await loadUserBalance();
      toast("配图已生成并保存到项目。");
    } catch (error) {
      toast(error.message, true);
      await loadUserBalance();
      if ((error.message || "").includes("积分不足") || (error.message || "").includes("402")) {
        openRechargeDialog();
      }
    } finally {
      btn.disabled = false;
    }
  }

  function showImageLightbox(src, title = "图表预览") {
    const dialog = $("#imageLightboxDialog");
    const img = $("#lightboxImage");
    const titleEl = $("#lightboxTitle");
    const dl = $("#lightboxDownloadBtn");
    if (!dialog || !img) return;
    img.src = src;
    if (titleEl) titleEl.textContent = title;
    if (dl) {
      dl.href = src;
      dl.download = `${title}.png`;
    }
    dialog.showModal();
  }

  function renderDiagrams() {
    const gallery = $("#diagramGallery");
    gallery.replaceChildren();
    const diagrams = state.diagrams || [];
    if (!diagrams.length) {
      const empty = document.createElement("div");
      empty.className = "empty-diagram-box";
      empty.innerHTML = `<strong>暂未生成路线图或项目配图</strong><p style="margin:4px 0 0;color:var(--text-subtle);">点击上方「请 Agent 提建议」梳理关键节点，或输入提示词生成项目配图，生成后将自动嵌入最终 Word 文档中。</p>`;
      gallery.append(empty);
      return;
    }
    for (const diagram of diagrams) {
      const card = document.createElement("div");
      card.className = "diagram-card";
      const title = document.createElement("h3");
      title.textContent = diagram.title;
      card.append(title);
      const imgUrl = diagram.material_job_id
        ? `/api/projects/${state.project.id}/material/jobs/${diagram.material_job_id}/files/figure.png`
        : diagram.preview_url || (diagram.preview ? `/api/projects/${state.project.id}/diagrams/${diagram.id}/preview` : null);
      if (imgUrl) {
        const image = document.createElement("img");
        if (diagram.material_job_id && window.MaterialPilot) {
          window.MaterialPilot.blobUrl(imgUrl).then(url => {
            image.src = url;
            image.onclick = () => showImageLightbox(url, diagram.title);
          }).catch(() => { image.alt = "图片暂不可用，请在任务区重试预览"; });
        } else image.src = imgUrl;
        image.alt = diagram.title;
        image.title = "点击查看高清大图";
        image.addEventListener("click", () => showImageLightbox(imgUrl, diagram.title));
        card.append(image);
      }
      const steps = document.createElement("p");
      steps.textContent = (diagram.nodes || []).join(" → ");
      card.append(steps);

      const actionsBar = document.createElement("div");
      actionsBar.className = "diagram-card-actions";

      const edit = button("✏️ 编辑步骤", "button button-small button-secondary");
      if (diagram.material_job_id) {
        edit.disabled = true;
        edit.textContent = "在上方图表计划中编辑";
      }
      edit.addEventListener("click", () => {
        state.editingDiagramId = diagram.id;
        $("#diagramTitle").value = diagram.title;
        $("#diagramNodes").value = (diagram.nodes || []).join("\n");
        $("#diagramEditor").classList.remove("hidden");
        $("#saveDiagramBtn").textContent = "保存图表修改";
        $("#diagramEditor").scrollIntoView({ behavior: "smooth", block: "center" });
      });
      actionsBar.append(edit);

      if (imgUrl) {
        const viewBig = button("🔍 放大", "button button-small button-quiet");
        viewBig.addEventListener("click", () => showImageLightbox(imgUrl, diagram.title));
        actionsBar.append(viewBig);
      }

      const del = document.createElement("button");
      del.type = "button";
      del.className = "diagram-del-btn";
      del.textContent = "🗑️ 删除";
      del.title = "从本项目中移除此图表";
      del.addEventListener("click", async () => {
        if (!confirm(`确定删除图表“${diagram.title}”吗？`)) return;
        try {
          await api(`/api/projects/${state.project.id}/diagrams/${diagram.id}`, { method: "DELETE" });
          state.diagrams = (state.diagrams || []).filter(d => d.id !== diagram.id);
          state.project.diagrams = state.diagrams;
          renderDiagrams();
          toast("图表已删除。");
        } catch (err) {
          toast(err.message, true);
        }
      });
      actionsBar.append(del);

      card.append(actionsBar);
      gallery.append(card);
    }
  }

  function toggleChatMenu() {
    const menu = $("#chatActionMenu");
    menu?.classList.toggle("hidden");
  }

  async function copyFullChatHistory() {
    const messages = state.messages || [];
    if (!messages.length) return toast("当前暂无对话记录", true);
    const text = messages.map(m => `### ${m.role === "user" ? "用户" : "Stardust Agent"}\n\n${m.content}`).join("\n\n---\n\n");
    try {
      await navigator.clipboard.writeText(text);
      toast("已复制完整对话记录到剪贴板");
    } catch {
      toast("复制失败，请重试", true);
    }
  }

  function exportChatAsMarkdown() {
    const messages = state.messages || [];
    if (!messages.length) return toast("当前暂无对话记录", true);
    const text = `# Stardust Agent 对话记录 · ${state.project?.title || "未命名课题"}\n\n` +
      messages.map(m => `### ${m.role === "user" ? "用户" : "Stardust Agent"}\n\n${m.content}`).join("\n\n---\n\n");
    const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${state.project?.title || "课题"}对话记录.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast("已导出 Markdown 对话记录文件");
  }

  function clearChatViewDisplay() {
    if (!confirm("确定清空当前对话窗口的显示内容吗？（不会删除服务器上的项目历史）")) return;
    const root = $("#chatMessages");
    root.replaceChildren();
    appendMessage(
      "assistant",
      `窗口已清空。我是 **Stardust Agent**，针对「${state.project?.state?.skill_name || "当前赛道"}」，有什么我可以继续协助你的？`
    );
    toast("窗口已重置");
  }

  function autoResizeChatInput() {
    const input = $("#chatInput");
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(180, Math.max(44, input.scrollHeight))}px`;
  }

  function restoreInterruptedChatMessage(projectId) {
    loadInterruptedChatMessages();
    const messages = interruptedChatMessages.get(projectId) || [];
    if (!messages.length || state.project?.id !== projectId) return;
    const input = $("#chatInput");
    if (input && !input.value) {
      const message = messages[0];
      input.value = message;
      autoResizeChatInput();
      recoveredChatInputs.set(projectId, message);
      toast("已保留上次输入；若发送曾中断，请先查看对话结果。");
    }
    // The student has already started another message. Do not overwrite it;
    // display each previous input with a dedicated copy action instead.
    for (const message of [...(interruptedChatMessages.get(projectId) || [])]) {
      if (input?.value === message) {
        recoveredChatInputs.set(projectId, message);
        continue;
      }
      const key = `${projectId}\0${message}`;
      if (interruptedMessagesRendered.has(key)) continue;
      interruptedMessagesRendered.add(key);
      appendMessage("user", message);
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "button button-secondary button-small";
      copy.textContent = "复制上次输入";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(message);
          forgetInterruptedChatMessage(projectId, message);
          toast("已复制上次输入");
        } catch {
          toast("复制失败，请手动选取上方文字", true);
        }
      });
      appendMessage("assistant", "刚才的输入已保留在上方。请先查看对话及积分状态，再决定是否重新发送。", copy);
    }
  }

  function syncChatControls() {
    $("#sendBtn").disabled = state.chatSending;
    $$("#chatTools button").forEach((button) => { button.disabled = state.chatSending; });
  }

  function appendThinkingMessage() {
    const root = $("#chatMessages");
    const row = document.createElement("div");
    row.className = "message assistant thinking";
    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.textContent = "✦";
    const bubble = document.createElement("div");
    bubble.className = "thinking-bubble";
    const status = document.createElement("span");
    status.textContent = "正在等待模型回复，请勿重复发送…";
    const dots = document.createElement("span");
    dots.className = "thinking-dots";
    dots.append(document.createElement("span"), document.createElement("span"), document.createElement("span"));
    bubble.append(status, dots);
    row.append(avatar, bubble);
    root.append(row);
    root.scrollTop = root.scrollHeight;
    const started = Date.now();
    const timer = setInterval(() => {
      const seconds = Math.floor((Date.now() - started) / 1000);
      status.textContent = seconds >= 30
        ? `已等待 ${seconds} 秒，模型响应较慢，请勿重复发送。`
        : `正在等待模型回复，已等待 ${seconds} 秒…`;
    }, 1000);
    return {
      row,
      remove() { clearInterval(timer); row.remove(); },
      setStatus(msg) { clearInterval(timer); status.textContent = msg; },
    };
  }

  function renderMessages() {
    const root = $("#chatMessages");
    root.replaceChildren();
    interruptedMessagesRendered.clear();
    if (!(state.messages || []).length) {
      appendMessage(
        "assistant",
        state.project?.state?.template_preference === "not_asked"
          ? `开始准备「${state.project?.state?.skill_name || "申报材料"}」前，想先确认你是否有学校或单位下发的模板？`
          : `你好！我是 **Stardust Agent** 申报材料助手，将按「${state.project?.state?.skill_name || "当前赛道"}」要求协助你。你可以先描述目标，或上传已有材料。`
      );
      return;
    }
    for (const message of state.messages || []) appendMessage(message.role, message.content);
  }

  function appendMessage(role, text, actionNode = null) {
    const root = $("#chatMessages");
    const row = document.createElement("div");
    row.className = `message ${role === "user" ? "user" : "assistant"}`;
    if (role !== "user") {
      const avatar = document.createElement("div");
      avatar.className = "message-avatar";
      avatar.textContent = "✦";
      row.append(avatar);
    }
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    if (role === "assistant") {
      bubble.innerHTML = renderMarkdownSafe(text);
      if (actionNode) {
        bubble.append(actionNode);
      }
      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "bubble-copy-btn";
      copyBtn.textContent = "📋 复制";
      copyBtn.title = "复制此条回答";
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(text);
          toast("已复制回答内容");
        } catch {
          toast("复制失败，请手动选取", true);
        }
      });
      row.append(copyBtn);
    } else {
      bubble.textContent = text;
    }
    row.append(bubble);
    root.append(row);
    root.scrollTop = root.scrollHeight;
    return row;
  }

  // Read SSE frames incrementally: proxies may use CRLF and UTF-8 characters
  // or frame separators may be split across network chunks. A terminal frame
  // is authoritative, so do not wait for EOF after receiving it.
  async function readChatEvents(reader, onEvent, isActive = () => true) {
    const decoder = new TextDecoder();
    let buffer = "";
    let eventType = "message";
    let dataLines = [];
    let terminal = false;

    function processBuffer(final = false) {
      while (!terminal) {
        const match = /\r\n|\r|\n/.exec(buffer);
        if (!match) break;
        // A trailing CR may be the first byte of a CRLF separator.
        if (!final && match[0] === "\r" && match.index === buffer.length - 1) break;
        const line = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        if (!line) {
          if (dataLines.length) {
            const parsed = JSON.parse(dataLines.join("\n"));
            const type = eventType;
            eventType = "message";
            dataLines = [];
            onEvent(type, parsed);
            terminal = type === "done" || type === "error";
          } else {
            eventType = "message";
          }
          continue;
        }
        if (line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        let value = colon < 0 ? "" : line.slice(colon + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "event") eventType = value;
        else if (field === "data") dataLines.push(value);
      }
    }

    while (!terminal) {
      const { done, value } = await reader.read();
      if (!isActive()) return;
      if (done) {
        buffer += decoder.decode();
        processBuffer(true);
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      processBuffer();
    }
  }

  async function refreshChatAfterError(projectId) {
    // Refresh is helpful, but the same network outage may also hang it. Do not
    // hold the editor or erase recoverable input while waiting indefinitely.
    let timer;
    const previousRevision = projectLoadRevision;
    let expired = false;
    try {
      await Promise.race([
        Promise.resolve().then(() => loadProject(projectId)).catch(() => {}),
        new Promise(resolve => { timer = setTimeout(() => { expired = true; resolve(); }, 5000); }),
      ]);
    } finally {
      clearTimeout(timer);
      if (expired && loadingProjectId === projectId && projectLoadRevision === previousRevision + 1)
        invalidateProjectLoad();
    }
  }

  async function sendChat(event, messageOverride = null, resumeTurnId = null) {
    event?.preventDefault();
    const input = $("#chatInput");
    const message = messageOverride === null ? input.value.trim() : messageOverride.trim();
    if (!message || !state.project) return;
    if (state.chatSending) return toast("上一条请求还未完成，请勿重复发送。", true);
    if (state.cloudMode && ["loading", "error"].includes(state.modelConfigSyncStatus))
      return toast("云端模型配置尚未同步完成，请稍候或在模型设置中重试同步。", true);
    // A new instruction may change what belongs in the document. Revoke old
    // local download links immediately, even before the request completes.
    clearExportArtifacts();
    window.MaterialPilot?.invalidateArtifacts?.();
    const projectId = state.project.id;
    const accountId = state.authSession?.user?.id || null;
    const requestEpoch = chatRecoveryEpoch;
    // Keep the request id with the same recoverable input; never store API keys.
    const requestStorageKey = "agent-chat-request:" + JSON.stringify([
      accountId, projectId, message, state.llmConfig.provider, state.llmConfig.model, state.llmConfig.base_url,
      state.llmConfig.api_key?.slice(-4),
    ]);
    let idempotencyKey = pendingChatRequests.get(requestStorageKey);
    try { idempotencyKey ||= sessionStorage.getItem(requestStorageKey); } catch {}
    if (!resumeTurnId && !idempotencyKey) {
      idempotencyKey = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
      try { sessionStorage.setItem(requestStorageKey, idempotencyKey); } catch {}
    }
    if (!resumeTurnId) pendingChatRequests.set(requestStorageKey, idempotencyKey);
    const sameAccount = () => chatRecoveryEpoch === requestEpoch && (state.authSession?.user?.id || null) === accountId;
    if (messageOverride === null) {
      updateRecoveredChatInput();
      // Persist before the request: a refresh can happen while the provider is
      // still working, and must not trigger an automatic second request.
      rememberInterruptedChatMessage(projectId, message);
      recoveredChatInputs.delete(projectId);
    }
    state.chatSending = true;
    syncChatControls();
    if (messageOverride === null) {
      input.value = "";
      input.style.height = "auto";
    }
    if (!resumeTurnId) appendMessage("user", message);
    let pending = appendThinkingMessage();

    // Check if api was replaced by a mock (e.g. in headless unit tests)
    const isMockApi = typeof api === "function" && api !== originalApi;
    if (isMockApi) {
      try {
        const result = await api(`/api/projects/${projectId}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message }),
        });
        pending.remove();
        if (!sameAccount() || !chatProjectIsActive(projectId)) return;
        if (typeof result.message !== "string" || !result.message.trim()) {
          throw new Error("模型未返回可用正文，请勿连续重试；请先查看积分流水并联系管理员核对。");
        }
        if (messageOverride === null) forgetInterruptedChatMessage(projectId, message);
        try { sessionStorage.removeItem(requestStorageKey); } catch {}
        pendingChatRequests.delete(requestStorageKey);
        appendMessage("assistant", result.message);
        state.messages.push({ role: "user", content: message }, { role: "assistant", content: result.message });
        if (result.free_chat) {
          state.freeChatsRemaining = Number(result.free_chats_remaining || 0);
          renderFreeChatsIndicator();
          if (state.freeChatsRemaining > 0) {
            toast(`本轮为免费体验对话（剩余 ${state.freeChatsRemaining} 次免费对话）`);
          } else {
            toast("本轮为最后一次免费体验对话，后续调用将使用积分");
          }
        } else if (result.free_chats_remaining !== undefined) {
          state.freeChatsRemaining = Number(result.free_chats_remaining || 0);
          renderFreeChatsIndicator();
        }
        if (result.replayed || result.setup_updated || (result.tool_calls || []).some((name) => ["save_project_fact", "save_section"].includes(name))) {
          await loadProject(state.project.id);
        }
        void refreshProjects().catch(() => {});
        if (state.llmConfig.provider === "preset") void loadUserBalance();
      } catch (error) {
        pending.remove();
        if (messageOverride === null && sameAccount())
          rememberInterruptedChatMessage(projectId, message);
        if (!sameAccount() || !chatProjectIsActive(projectId)) return;
        // Refresh is best-effort: an unavailable server must not prevent us
        // restoring the student's unsent text and unlocking the input.
        await refreshChatAfterError(projectId);
        if (!sameAccount() || !chatProjectIsActive(projectId)) return;
        restoreInterruptedChatMessage(projectId);
        const isCreditsErr = (error.message || "").includes("积分不足") || (error.message || "").includes("402");
        if (isCreditsErr) {
          const ctaWrap = document.createElement("div");
          ctaWrap.className = "chat-recharge-cta";
          if (state.cardShopUrl) {
            const shopLink = document.createElement("a");
            shopLink.className = "button button-primary button-small";
            shopLink.href = state.cardShopUrl;
            shopLink.target = "_blank";
            shopLink.rel = "noopener noreferrer";
            shopLink.textContent = "获取卡密 ↗";
            ctaWrap.append(shopLink);
          }
          const openBtn = document.createElement("button");
          openBtn.type = "button";
          openBtn.className = "button button-secondary button-small";
          openBtn.textContent = "输入卡密";
          openBtn.addEventListener("click", () => openRechargeDialog());
          ctaWrap.append(openBtn);
          appendMessage("assistant", `暂时无法完成：${error.message}`, ctaWrap);
          openRechargeDialog();
        } else {
          appendMessage("assistant", `暂时无法完成：${error.message}`);
        }
        if (state.llmConfig.provider === "preset") void loadUserBalance();
      } finally {
        state.chatSending = false;
        syncChatControls();
      }
      return;
    }

    // Real SSE streaming path
    let assistantRow = null;
    let bubble = null;
    let streamedText = "";
    let finalResult = null;
    let streamReader = null;
    let serverErrorEvent = false;
    const uncertainReply = "连接已中断，回复未完成，结果和计费状态尚未确认。请先刷新对话并查看积分流水，再决定是否重新发送；系统不会自动重复发送。";
    const waitingTimeoutReply = "等待时间过长，已停止等待；任务可能仍在处理，结果和计费状态尚未确认。请先刷新对话并查看积分流水，再决定是否重新发送；系统不会自动重复发送。";
    const chatAbort = new AbortController();
    let chatTimedOut = false;
    let inactivityTimer;
    // Network intermediaries can leave an SSE connection open indefinitely.
    // Abort only this observation after 3 minutes with no response event;
    // the provider may still finish and may still charge for the request.
    function resetChatInactivityTimer() {
      clearTimeout(inactivityTimer);
      inactivityTimer = setTimeout(() => {
        chatTimedOut = true;
        chatAbort.abort();
      }, 180000);
    }
    resetChatInactivityTimer();

    try {
      const url = `/api/projects/${projectId}/chat?stream=true`;
      const headers = new Headers({
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
      });
      if (state.authSession?.access_token) {
        headers.set("Authorization", `Bearer ${state.authSession.access_token}`);
      }
      if (privateRagConsented() && state.project?.id === projectId) {
        headers.set("X-Private-RAG-Consent", "cloudflare-bge-m3-v1");
        headers.set("X-Private-RAG-Project", projectId);
      }
      const body = {
        message,
        ...(resumeTurnId ? { resume_turn_id: resumeTurnId } : { idempotency_key: idempotencyKey }),
        stream: true,
        llm_config: { ...state.llmConfig },
      };

      let response;
      try {
        response = await fetch(url, {
          method: "POST",
          headers,
          body: JSON.stringify(body),
          signal: chatAbort.signal,
        });
      } catch {
        throw new Error(chatTimedOut ? waitingTimeoutReply : uncertainReply);
      }

      if (!response.ok) {
        let errText = "";
        try {
          const errJson = await response.json();
          errText = errJson.detail || errJson.error;
        } catch {
          errText = await response.text();
        }
        throw new Error(errText || `请求失败 (${response.status})`);
      }

      if (!response.body) throw new Error(uncertainReply);
      const reader = streamReader = response.body.getReader();
      try {
        await readChatEvents(reader, (eventType, parsed) => {
          resetChatInactivityTimer();
          if (!sameAccount() || !chatProjectIsActive(projectId)) return;
          if (eventType === "status") {
            if (pending.setStatus && parsed.message) {
              pending.setStatus(parsed.message);
            }
          } else if (eventType === "reset") {
            assistantRow?.remove();
            assistantRow = null;
            bubble = null;
            streamedText = "";
            if (!pending.row.isConnected) pending = appendThinkingMessage();
          } else if (eventType === "delta") {
            if (!assistantRow) {
              pending.remove();
              assistantRow = document.createElement("div");
              assistantRow.className = "message assistant";
              const avatar = document.createElement("div");
              avatar.className = "message-avatar";
              avatar.textContent = "✦";
              assistantRow.append(avatar);
              bubble = document.createElement("div");
              bubble.className = "message-bubble";
              assistantRow.append(bubble);
              const root = $("#chatMessages");
              root.append(assistantRow);
            }
            streamedText += parsed.text || "";
            bubble.textContent = streamedText;
            const root = $("#chatMessages");
            root.scrollTop = root.scrollHeight;
          } else if (eventType === "done") {
            finalResult = parsed;
          } else if (eventType === "error") {
            serverErrorEvent = true;
            throw new Error(parsed.error || "请求发生错误");
          }
        }, () => sameAccount() && chatProjectIsActive(projectId));
      } catch (error) {
        if (serverErrorEvent) throw error;
        throw new Error(chatTimedOut ? waitingTimeoutReply : uncertainReply);
      }

      pending.remove();
      if (!sameAccount() || !chatProjectIsActive(projectId)) return;

      if (!finalResult || typeof finalResult.message !== "string") {
        throw new Error(uncertainReply);
      }
      const finalText = finalResult.message;
      if (!finalText.trim()) {
        throw new Error("模型未返回可用正文，请勿连续重试；请先查看积分流水并联系管理员核对。");
      }
      if (messageOverride === null) forgetInterruptedChatMessage(projectId, message);
      if (!resumeTurnId) {
        try { sessionStorage.removeItem(requestStorageKey); } catch {}
        pendingChatRequests.delete(requestStorageKey);
      }

      if (assistantRow && bubble) {
        bubble.innerHTML = renderMarkdownSafe(finalText);
        const copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.className = "bubble-copy-btn";
        copyBtn.textContent = "📋 复制";
        copyBtn.title = "复制此条回答";
        copyBtn.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(finalText);
            toast("已复制回答内容");
          } catch {
            toast("复制失败，请手动选取", true);
          }
        });
        assistantRow.append(copyBtn);
      } else {
        appendMessage("assistant", finalText);
      }

      state.messages.push({ role: "user", content: message }, { role: "assistant", content: finalText });

      if (finalResult?.free_chat) {
        state.freeChatsRemaining = Number(finalResult.free_chats_remaining || 0);
        renderFreeChatsIndicator();
        if (state.freeChatsRemaining > 0) {
          toast(`本轮为免费体验对话（剩余 ${state.freeChatsRemaining} 次免费对话）`);
        } else {
          toast("本轮为最后一次免费体验对话，后续调用将使用积分");
        }
      } else if (finalResult?.free_chats_remaining !== undefined) {
        state.freeChatsRemaining = Number(finalResult.free_chats_remaining || 0);
        renderFreeChatsIndicator();
      }
      if (resumeTurnId || finalResult?.replayed || finalResult?.setup_updated || (finalResult?.tool_calls || []).some((name) => ["save_project_fact", "save_section"].includes(name))) {
        await refreshChatAfterError(projectId);
      }
      void refreshProjects().catch(() => {});
      if (state.llmConfig.provider === "preset") void loadUserBalance();
    } catch (error) {
      pending.remove();
      if (assistantRow && !streamedText.trim()) assistantRow.remove();
      if (messageOverride === null && sameAccount())
        rememberInterruptedChatMessage(projectId, message);
      if (!sameAccount() || !chatProjectIsActive(projectId)) return;
      // An unsuccessful refresh must not strand the input in recovery storage.
      await refreshChatAfterError(projectId);
      if (!sameAccount() || !chatProjectIsActive(projectId)) return;
      restoreInterruptedChatMessage(projectId);
      const isCreditsErr = (error.message || "").includes("积分不足") || (error.message || "").includes("402");
      if (isCreditsErr) {
        const ctaWrap = document.createElement("div");
        ctaWrap.className = "chat-recharge-cta";
        if (state.cardShopUrl) {
          const shopLink = document.createElement("a");
          shopLink.className = "button button-primary button-small";
          shopLink.href = state.cardShopUrl;
          shopLink.target = "_blank";
          shopLink.rel = "noopener noreferrer";
          shopLink.textContent = "获取卡密 ↗";
          ctaWrap.append(shopLink);
        }
        const openBtn = document.createElement("button");
        openBtn.type = "button";
        openBtn.className = "button button-secondary button-small";
        openBtn.textContent = "输入卡密";
        openBtn.addEventListener("click", () => openRechargeDialog());
        ctaWrap.append(openBtn);
        appendMessage("assistant", `暂时无法完成：${error.message}`, ctaWrap);
        openRechargeDialog();
      } else {
        appendMessage("assistant", `暂时无法完成：${error.message}`);
      }
      if (state.llmConfig.provider === "preset") void loadUserBalance();
    } finally {
      clearTimeout(inactivityTimer);
      state.chatSending = false;
      syncChatControls();
      if (streamReader) {
        pending.remove();
        try { await streamReader.cancel(); } catch {}
        try { streamReader.releaseLock(); } catch {}
      }
    }
  }

  function claimEvidenceText(row) {
    const evidence = row.evidence || row.cited_passage;
    if (!evidence) return "";
    const version = evidence.review_revision != null ? `确认版本 ${evidence.review_revision}` :
      `原件 ${evidence.source_sha256.slice(0, 12)}`;
    const scope = evidence.scope ? `（${evidence.scope}）` : "";
    return `${evidence.source_name} · ${evidence.source_ref} · ${version}${scope}『${evidence.quote}』`;
  }

  async function validate() {
    const btn = $("#validateBtn");
    const oldText = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "检查中…"; }
    try {
      const result = await api(`/api/projects/${state.project.id}/validate`, { method: "POST" });
      const box = $("#validationResult");
      box.replaceChildren();
      box.classList.remove("hidden", "pass", "warn");
      box.classList.add(result.passed ? "pass" : "warn");
      const title = document.createElement("strong");
      title.textContent = result.passed ? "基础检查通过" : "草稿仍有待补充项";
      box.append(title);
      const list = document.createElement("ul");
      for (const missing of result.missing_facts || []) {
        const item = document.createElement("li");
        item.textContent = missing.label;
        list.append(item);
      }
      for (const name of result.unconfirmed_documents || []) {
        const item = document.createElement("li");
        item.textContent = `未确认材料：${name}`;
        list.append(item);
      }
      for (const section of result.missing_sections || []) {
        const item = document.createElement("li");
        item.textContent = `未填写章节：${section}`;
        list.append(item);
      }
      for (const warning of result.consistency_warnings || []) {
        const item = document.createElement("li");
        item.textContent = warning;
        list.append(item);
      }
      for (const row of result.claim_audit?.claims || []) {
        const citation = claimEvidenceText(row);
        if (!citation) continue;
        const item = document.createElement("li");
        item.textContent = `${row.evidence ? "已定位原文" : "待对照原文"}『${row.claim}』 · ${citation}`;
        list.append(item);
      }
      for (const item of result.low_confidence_ocr || []) {
        const li = document.createElement("li");
        li.textContent = `OCR 低置信度：${item.name}（${item.count} 行）`;
        list.append(li);
      }
      const photo = document.createElement("li");
      photo.textContent = result.photo_notice;
      list.append(photo);
      box.append(list);
      if (result.validation_scope) {
        const scope = document.createElement("div");
        scope.textContent = result.validation_scope;
        box.append(scope);
      }
      if (result.draft_notice) {
        const note = document.createElement("div");
        note.textContent = result.draft_notice;
        box.append(note);
      }
    } catch (error) {
      toast(error.message, true);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = oldText || "基础检查"; }
    }
  }

  async function runReview() {
    const buttonNode = $("#reviewBtn");
    buttonNode.disabled = true;
    buttonNode.textContent = "评审中…";
    try {
      const result = await api(`/api/projects/${state.project.id}/review`, { method: "POST" });
      if (result.error) throw new Error(result.error);
      const report = result.report || {};
      const box = $("#reviewResult");
      box.replaceChildren();
      box.classList.remove("hidden");
      const summary = document.createElement("div");
      const total = report.total_score ?? "—";
      const grade = report.grade ?? "—";
      const summaryText = document.createElement("strong");
      summaryText.textContent = `模拟结果：${String(total)} / ${String(report.max_score ?? 100)} · ${String(grade)}`;
      summary.append(summaryText);
      box.append(summary);
      for (const dimension of report.dimensions || []) {
        const row = document.createElement("div");
        row.className = "review-dimension";
        const label = document.createElement("b");
        label.textContent = `${dimension.name || "评审维度"}：${dimension.weighted_score ?? dimension.score ?? "—"}`;
        const detail = document.createElement("div");
        detail.textContent = dimension.comment || "";
        row.append(label, detail);
        box.append(row);
      }
      const note = document.createElement("div");
      note.className = "inline-hint";
      note.textContent = result.notice || "结果仅供修改参考。";
      box.append(note);
    } catch (error) {
      toast(error.message, true);
      if ((error.message || "").includes("积分不足") || (error.message || "").includes("402")) {
        openRechargeDialog();
      }
    } finally {
      buttonNode.disabled = false;
      buttonNode.textContent = "开启模拟评审";
    }
  }

  const { exportDoc, fetchExportBlob, invalidate: invalidateWordExport,
    syncButtons: syncWordExportButtons, clearRecovery: clearWordExportRecovery } = window.WordExport.create({
    state, $, api: (...args) => api(...args), chatProjectIsActive, recoveryOwner: chatRecoveryOwner,
    flushDraftEdits, showChatDrafts, toast, openRechargeDialog,
  });

  function clearExportArtifacts(options) {
    invalidateWordExport(options);
    $("#chatDraftDialog")?.close();
    $("#chatDraftItems")?.replaceChildren();
    const save = $("#confirmChatDraftsBtn");
    if (save) save.onclick = null;
  }

  async function showChatDrafts(preview = null, afterSave = null) {
    const projectId = state.project?.id;
    if (!projectId) return;
    await flushDraftEdits();
    preview ||= await api(`/api/projects/${projectId}/chat-drafts`);
    if (state.project?.id !== projectId) return;
    const dialog = $("#chatDraftDialog");
    const root = $("#chatDraftItems");
    const save = $("#confirmChatDraftsBtn");
    root.replaceChildren();
    $("#chatDraftNotice").textContent = preview.notice;
    const choices = [];
    for (const item of preview.items) {
      const row = document.createElement("section");
      row.className = "chat-draft-item";
      const label = document.createElement("label");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = !item.already_saved && !item.conflict && !item.requires_review;
      check.disabled = item.already_saved;
      const title = document.createElement("strong");
      title.textContent = item.title;
      label.append(check, title);
      const note = document.createElement("p");
      note.textContent = item.already_saved ? "已保存过此版本，保留当前章节及手动修改。"
        : item.conflict ? "已有不同正文；勾选后用这份草稿替换，原正文保留为历史版本。"
        : item.requires_review ? "请先核对并修改这份草稿，再勾选保存。"
        : "勾选后保存，也可以先在下方修改内容。";
      if (item.warnings?.length) note.textContent += ` ${item.warnings.join(" ")}`;
      for (const row of item.fact_checks || []) {
        const citation = claimEvidenceText(row);
        if (citation) note.textContent += ` 『${row.claim}』可对照 ${citation}`;
      }
      const editor = document.createElement("textarea");
      editor.value = item.content;
      editor.readOnly = check.disabled;
      row.append(label, note, editor);
      root.append(row);
      choices.push({ item, check, editor });
    }
    if (!choices.length) {
      const note = document.createElement("p");
      note.textContent = "尚未发现完整的章节草稿。请先在对话中起草，或在章节编辑器中保存正文。";
      root.append(note);
    }
    if (afterSave && (state.project.outline || []).some(s => (state.project.sections?.[s.id] || "").trim())) {
      const useSaved = document.createElement("button");
      useSaved.type = "button";
      useSaved.className = "button button-secondary";
      useSaved.textContent = "保持已保存正文并继续导出";
      useSaved.addEventListener("click", async () => {
        if (state.project?.id !== projectId) return;
        dialog.close();
        root.replaceChildren();
        save.onclick = null;
        await afterSave({ skipChatDrafts: true });
      });
      root.append(useSaved);
    }
    save.textContent = afterSave ? "确认保存并继续导出" : "确认保存所选章节";
    save.disabled = !choices.some(c => !c.check.disabled);
    save.onclick = async () => {
      if (state.project?.id !== projectId) return;
      const items = choices.filter(c => c.check.checked && !c.check.disabled)
        .map(c => ({ id: c.item.id, content: c.editor.value, ...(c.item.conflict ? { replace_existing: true } : {}) }));
      if (!items.length) { toast("请至少选择一个待保存章节", true); return; }
      save.disabled = true;
      try {
        const result = await api(`/api/projects/${projectId}/chat-drafts/confirm`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ snapshot: preview.snapshot, items }),
        });
        if (state.project?.id !== projectId) return;
        await loadProject(projectId);
        dialog.close();
        root.replaceChildren();
        save.onclick = null;
        toast(result.notice);
        if (afterSave) await afterSave({ skipChatDrafts: true });
      } catch (error) {
        $("#chatDraftNotice").textContent = error.message;
        toast(error.message, true);
      } finally { save.disabled = false; }
    };
    if (!dialog.open) dialog.showModal();
  }

  async function deleteCurrentProject() {
    if (!state.project || !confirm(`确定删除“${state.project.title}”及其材料吗？${state.cloudMode ? "历史加密备份可能长期保留已删除的数据。" : "删除后无法恢复。"}`)) return;
    const projectId = state.project.id;
    try {
      await api(`/api/projects/${projectId}`, { method: "DELETE" });
      closeDeletedProject(projectId);
      await refreshProjects();
      toast("项目和关联材料已从本机删除。");
    } catch (error) {
      toast(error.message, true);
    }
  }

  function toggleChatColumn() {
    const grid = $("#workspaceGrid");
    if (!grid) return;
    state.chatCollapsed = !state.chatCollapsed;
    grid.classList.toggle("chat-collapsed", state.chatCollapsed);
    const btn = $("#toggleChatBtn");
    if (btn) {
      btn.textContent = state.chatCollapsed ? "💬 展开助理" : "💬 对话助理";
    }
  }

  function focusDraftSection(sectionId) {
    const section = document.getElementById(`draft-sec-${sectionId}`);
    const panel = section?.closest(".panel");
    if (!panel?.id) {
      toast("该章节已不在当前目录中。", true);
      return;
    }
    handleTabNavigation(panel.id);
    if (section.querySelector(".draft-section-body.folded")) {
      section.querySelector(".fold-toggle-btn")?.click();
    }
    const editor = section.querySelector("textarea");
    if (editor?.classList.contains("hidden")) {
      section.querySelector(".mode-switch-btn")?.click();
    }
    editor?.focus({ preventScroll: true });
    section.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function handleTabNavigation(targetId) {
    if (mobileLayout?.matches) {
      showMobileWorkspaceView(targetId);
      return;
    }
    state.activeTab = targetId;
    $$(".workspace-tab-btn").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.target === targetId);
    });
    const panels = $$(".workspace-main .panel");
    if (targetId === "all") {
      panels.forEach(panel => panel.classList.remove("hidden"));
    } else {
      panels.forEach(panel => {
        const isMatch = panel.id === targetId || panel.classList.contains(targetId);
        panel.classList.toggle("hidden", !isMatch);
      });
      const targetPanel = $(`#${targetId}`);
      if (targetPanel) {
        targetPanel.scrollIntoView({ block: "start" });
      }
    }
  }

  function showMobileWorkspaceView(targetId) {
    const grid = $("#workspaceGrid");
    grid.dataset.mobileView = targetId;
    $(".mobile-active-panel", grid)?.classList.remove("mobile-active-panel");
    if (targetId !== "chat") $(`#${targetId}`, grid)?.classList.add("mobile-active-panel");

    const tabs = $$("[data-mobile-target]");
    for (const tab of tabs) {
      tab.setAttribute("aria-pressed", String(tab.dataset.mobileTarget === targetId));
    }
    const more = $("#mobileMoreStages");
    const extraStageSelected = !tabs.some(tab => tab.dataset.mobileTarget === targetId);
    more.value = extraStageSelected ? targetId : "";
    more.parentElement.classList.toggle("active", extraStageSelected);

    if (mobileLayout?.matches) window.scrollTo(0, 0);
  }

  function bindMobileControls() {
    if (!mobileLayout) return;
    const menu = $("#mobileMenuDialog");
    const sidebar = $("#sidebar");
    const actions = $(".topbar-actions");
    const actionsHome = actions.parentElement;
    const restoreMenuContent = () => {
      $("#appShell").prepend(sidebar);
      actionsHome.append(actions);
    };
    const closeMenu = () => {
      menu.close();
      restoreMenuContent();
    };
    $("#mobileMenuBtn").addEventListener("click", () => {
      if (!mobileLayout?.matches) return;
      $("#mobileMenuProjects").append(sidebar);
      $("#mobileMenuActions").append(actions);
      menu.showModal();
    });
    $("#closeMobileMenuBtn").addEventListener("click", closeMenu);
    menu.addEventListener("close", () => {
      if (!menu.open && sidebar.parentElement !== $("#appShell")) restoreMenuContent();
    });
    menu.addEventListener("click", (event) => {
      if (event.target.closest(".project-item, #newProjectBtn, #newChatBtn, .topbar-actions button")) {
        closeMenu();
      }
    }, { capture: true });
    const updateMobileViewport = () => {
      const viewport = window.visualViewport;
      if (mobileLayout.matches && viewport?.scale === 1) {
        document.documentElement.style.setProperty("--mobile-viewport-height", `${viewport.height}px`);
      } else {
        document.documentElement.style.removeProperty("--mobile-viewport-height");
      }
    };
    mobileLayout.addEventListener("change", () => {
      if (!mobileLayout.matches && menu.open) closeMenu();
      updateMobileViewport();
    });
    window.visualViewport?.addEventListener("resize", updateMobileViewport);
    updateMobileViewport();

    for (const tab of $$("[data-mobile-target]")) {
      tab.addEventListener("click", () => showMobileWorkspaceView(tab.dataset.mobileTarget));
    }
    $("#mobileMoreStages").addEventListener("change", (event) => {
      showMobileWorkspaceView(event.target.value);
    });
    const cameraInput = $("#mobileCameraInput");
    $("#mobileCameraBtn").addEventListener("click", () => cameraInput.click());
    cameraInput.addEventListener("change", () => {
      void uploadFiles([...cameraInput.files]);
      cameraInput.value = "";
    });
  }

  function setupResizeHandles() {
    const navHandle = $("#navResizeHandle");
    const workspaceHandle = $("#workspaceResizeHandle");

    if (navHandle) {
      let isDraggingNav = false;
      navHandle.addEventListener("mousedown", (e) => {
        isDraggingNav = true;
        navHandle.classList.add("active");
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
      });

      window.addEventListener("mousemove", (e) => {
        if (!isDraggingNav) return;
        const newWidth = Math.max(180, Math.min(480, e.clientX));
        document.documentElement.style.setProperty("--sidebar-width", `${newWidth}px`);
      });

      window.addEventListener("mouseup", () => {
        if (isDraggingNav) {
          isDraggingNav = false;
          navHandle.classList.remove("active");
          document.body.style.cursor = "";
          document.body.style.userSelect = "";
        }
      });
    }

    if (workspaceHandle) {
      let isDraggingWorkspace = false;
      workspaceHandle.addEventListener("mousedown", (e) => {
        isDraggingWorkspace = true;
        workspaceHandle.classList.add("active");
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
      });

      window.addEventListener("mousemove", (e) => {
        if (!isDraggingWorkspace) return;
        const windowWidth = window.innerWidth;
        const chatWidth = Math.max(280, Math.min(650, windowWidth - e.clientX - 32));
        const grid = $("#workspaceGrid");
        if (grid && !state.chatCollapsed) {
          grid.style.gridTemplateColumns = `minmax(0, 1.25fr) 4px ${chatWidth}px`;
        }
      });

      window.addEventListener("mouseup", () => {
        if (isDraggingWorkspace) {
          isDraggingWorkspace = false;
          workspaceHandle.classList.remove("active");
          document.body.style.cursor = "";
          document.body.style.userSelect = "";
        }
      });
    }
  }

  function bindEvents() {
    $("#newProjectBtn").addEventListener("click", startConversation);
    $("#newChatBtn").addEventListener("click", startGeneralChat);
    $("#conversationSkillApply").addEventListener("click", selectConversationSkill);
    $("#welcomeCreateBtn").addEventListener("click", startConversation);
    $("#welcomeSetupBtn").addEventListener("click", createPanel);
    $("#heroStartBtn")?.addEventListener("click", triggerHeroStart);
    $("#heroProjectRequest")?.addEventListener("keydown", (e) => {
      if (mobileLayout?.matches) return;
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        triggerHeroStart();
      }
    });
    $$(".quick-tag-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        const query = btn.dataset.query || btn.textContent.trim();
        const heroInput = $("#heroProjectRequest");
        if (heroInput) {
          heroInput.value = query;
          triggerHeroStart();
        }
      });
    });
    $("#projectSearchInput")?.addEventListener("input", (e) => {
      renderProjectList(state.projectsList, e.target.value);
    });
    $("#homeBreadcrumbBtn")?.addEventListener("click", showWelcomeView);
    $("#quickExportTopBtn")?.addEventListener("click", exportDoc);
    $("#headingExportBtn")?.addEventListener("click", exportDoc);
    $("#recoverChatDraftsBtn")?.addEventListener("click", () => {
      showChatDrafts().catch(error => toast(error.message, true));
    });
    $("#closeChatDraftsBtn")?.addEventListener("click", () => $("#chatDraftDialog").close());
    $("#renameProjectBtn")?.addEventListener("click", renameCurrentProject);
    $("#projectHeading")?.addEventListener("click", renameCurrentProject);
    $("#closeCreateBtn").addEventListener("click", () => {
      $("#createPanel").classList.add("hidden");
      $("#welcome").classList.remove("hidden");
    });
    $("#createForm").addEventListener("submit", createProject);
    $$('input[name="hasTemplate"]').forEach((radio) => radio.addEventListener("change", updateTemplateExplain));
    $$(".upload-tab").forEach((tab) => tab.addEventListener("click", () => setUploadKind(tab.dataset.kind)));
    $("#chooseFileBtn").addEventListener("click", () => $("#fileInput").click());
    $("#privateRagConsent")?.addEventListener("change", (event) => {
      state.privateRagConsentProjectId = event.target.checked ? state.project?.id || null : null;
    });
    $("#privateRagSearch")?.addEventListener("click", async () => {
      if (!state.project?.id) return;
      const targetId = state.project.id;
      const output = $("#privateRagResults");
      try {
        const result = await api(`/api/projects/${targetId}/search/materials`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: $("#privateRagQuery").value }),
        });
        if (state.project?.id !== targetId) return;
        output.replaceChildren();
        const status = document.createElement("p");
        status.textContent = `检索模式：${result.retrieval_mode}。${result.fallback_reason || result.message || ""}`;
        output.append(status);
        for (const item of result.results || []) {
          const paragraph = document.createElement("p");
          paragraph.textContent = `${item.source_name} · ${item.source_ref}：${item.text}`;
          output.append(paragraph);
        }
      } catch (error) { toast(error.message, true); }
    });
    $("#fileInput").addEventListener("change", (event) => uploadFiles([...event.target.files]));
    $("#chatAttachBtn").addEventListener("click", () => $("#chatFileInput").click());
    $("#chatUploadKind").addEventListener("change", (event) => {
      $("#chatFileInput").accept = chatUploadAccept(event.target.value);
    });
    $("#chatFileInput").addEventListener("change", (event) => {
      uploadFiles([...event.target.files], $("#chatUploadKind").value, true);
    });
    $("#dropZone").addEventListener("dragover", (event) => { event.preventDefault(); event.currentTarget.classList.add("dragover"); });
    $("#dropZone").addEventListener("dragleave", (event) => event.currentTarget.classList.remove("dragover"));
    $("#dropZone").addEventListener("drop", (event) => {
      event.preventDefault();
      event.currentTarget.classList.remove("dragover");
      uploadFiles([...event.dataTransfer.files]);
    });
    $("#editFactsBtn").addEventListener("click", () => {
      $("#factsEditor").classList.toggle("hidden");
    });
    const factsEditor = $("#factsEditor");
    const onFactInput = event => {
      if (factFormProjectId !== state.project?.id) return;
      recordFactInput(event.target);
      renderFactDraftStatus(state.project, factEdits.open(state.project));
    };
    factsEditor.addEventListener("input", onFactInput);
    factsEditor.addEventListener("change", onFactInput);
    $("#addFactBtn").addEventListener("click", addCustomFact);
    $("#saveFactsBtn").addEventListener("click", saveFacts);
    $("#chooseLeaderPhotoBtn")?.addEventListener("click", () => $("#leaderPhotoInput")?.click());
    $("#leaderPhotoInput")?.addEventListener("change", (e) => {
      if (e.target.files?.[0]) handleLeaderPhotoUpload(e.target.files[0]);
    });
    $("#removeLeaderPhotoBtn")?.addEventListener("click", handleLeaderPhotoDelete);
    $("#proposeIntakeBtn").addEventListener("click", proposeIntake);
    $("#proposeFactsBtn").addEventListener("click", proposeFacts);
    $("#paperSearchBtn").addEventListener("click", () => searchSources("paper"));
    $("#officialSearchBtn").addEventListener("click", () => searchSources("official"));
    $("#knowledgeSearchBtn").addEventListener("click", searchPublicKnowledge);
    $("#officialUrlBtn").addEventListener("click", previewOfficialUrl);
    $("#generateOutlineBtn").addEventListener("click", generateOutline);
    $("#resetOutlineBtn")?.addEventListener("click", recommendOutline);
    $("#saveOutlineBtn").addEventListener("click", saveOutline);
    $("#batchGenerateSectionsBtn")?.addEventListener("click", startBatchGeneration);
    $("#cancelBatchBtn")?.addEventListener("click", cancelBatchGeneration);
    $("#toggleAllSectionsBtn")?.addEventListener("click", toggleAllSections);
    $("#proposeDiagramBtn").addEventListener("click", proposeDiagram);
    $("#saveDiagramBtn").addEventListener("click", saveDiagram);
    $("#generateWorkbenchImageBtn").addEventListener("click", generateWorkbenchImage);
    $("#pilotFingerprintBtn").addEventListener("click", makePilotFingerprint);
    $("#chatForm").addEventListener("submit", sendChat);
    $("#chatInput").addEventListener("keydown", (event) => {
      if (mobileLayout?.matches) return;
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendChat();
      }
    });
    $("#chatInput").addEventListener("input", () => {
      autoResizeChatInput();
      updateRecoveredChatInput();
    });
    $$(".chat-tools button").forEach((button) => button.addEventListener("click", () => {
      $("#chatInput").value = button.dataset.prompt;
      autoResizeChatInput();
      $("#chatInput").focus();
    }));
    $$("#templateQuickActions button").forEach((button) => button.addEventListener("click", () => {
      $("#chatInput").value = button.dataset.templateAnswer;
      sendChat();
    }));
    $("#validateBtn").addEventListener("click", validate);
    $("#reviewBtn").addEventListener("click", runReview);
    $("#exportBtn").addEventListener("click", exportDoc);
    $("#deleteProjectBtn").addEventListener("click", deleteCurrentProject);
    $("#clearChatBtn")?.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleChatMenu();
    });
    $("#copyFullChatBtn")?.addEventListener("click", () => {
      toggleChatMenu();
      copyFullChatHistory();
    });
    $("#exportChatMdBtn")?.addEventListener("click", () => {
      toggleChatMenu();
      exportChatAsMarkdown();
    });
    $("#explainChatStorageBtn")?.addEventListener("click", () => {
      toggleChatMenu();
      toast(state.cloudMode ? "对话记录随项目保存在云端；加密备份可能长期保留历史数据。" : "对话记录保存在当前本机项目库中。");
    });
    $("#clearChatViewBtn")?.addEventListener("click", () => {
      toggleChatMenu();
      clearChatViewDisplay();
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".chat-menu-container")) {
        $("#chatActionMenu")?.classList.add("hidden");
      }
    });
    $("#closeLightboxBtn")?.addEventListener("click", () => $("#imageLightboxDialog")?.close());
    $("#closeDocumentPreviewBtn").addEventListener("click", () => $("#documentPreviewDialog").close());
    $("#documentPreviewDialog").addEventListener("close", clearDocumentPreview);
    $("#toggleChatBtn")?.addEventListener("click", toggleChatColumn);

    $$(".workspace-tab-btn").forEach(btn => {
      btn.addEventListener("click", () => handleTabNavigation(btn.dataset.target));
    });

    // Dialog backdrop click to close
    $$("dialog").forEach((dlg) => {
      dlg.addEventListener("click", (e) => {
        const rect = dlg.getBoundingClientRect();
        const inDialog = (
          rect.top <= e.clientY && e.clientY <= rect.top + rect.height &&
          rect.left <= e.clientX && e.clientX <= rect.left + rect.width
        );
        if (!inDialog) dlg.close();
      });
    });

    $("#modelSettingsBtn").addEventListener("click", () => {
      syncModelSettingsForm();
      $("#modelSettingsDialog").showModal();
    });
    $("#closeModelSettingsBtn").addEventListener("click", () => $("#modelSettingsDialog").close());
    $$('input[name="llmProvider"]').forEach((input) => input.addEventListener("change", updateModelFields));
    $("#testModelConfigBtn").addEventListener("click", testSelectedLlmConfig);
    $("#saveModelConfigBtn").addEventListener("click", saveSelectedLlmConfig);
    $("#deleteCloudModelConfigBtn")?.addEventListener("click", deleteCloudModelConfig);
    $("#syncCloudModelConfigBtn")?.addEventListener("click", async () => {
      await loadCloudModelConfig();
      syncModelSettingsForm();
    });
    $("#customImageReuseText")?.addEventListener("change", (e) => {
      $("#customImageSeparateFields")?.classList.toggle("hidden", e.target.checked);
    });
    $("#creditsBadgeBtn")?.addEventListener("click", openRechargeDialog);
    $("#openRechargeBtn")?.addEventListener("click", openRechargeDialog);
    $("#sidebarRechargeBtn")?.addEventListener("click", openRechargeDialog);
    $("#closeRechargeBtn")?.addEventListener("click", () => $("#rechargeDialog")?.close());
    $("#chatRunsBtn")?.addEventListener("click", () => showChatRuns());
    $("#chatRunsRefresh")?.addEventListener("click", () => showChatRuns());
    $("#usageLogsBtn")?.addEventListener("click", openUsageDialog);
    $("#closeUsageBtn")?.addEventListener("click", () => $("#usageDialog")?.close());
    $("#refreshUsageBtn")?.addEventListener("click", openUsageDialog);
    $("#submitRedeemBtn")?.addEventListener("click", handleRedeemCode);
    const openContactDialog = () => $("#contactDevDialog")?.showModal();
    $("#contactDevBtn")?.addEventListener("click", openContactDialog);
    $("#sidebarContactDevBtn")?.addEventListener("click", openContactDialog);
    $("#rechargeContactDevBtn")?.addEventListener("click", () => {
      $("#rechargeDialog")?.close();
      openContactDialog();
    });
    $("#closeContactDevBtn")?.addEventListener("click", () => $("#contactDevDialog")?.close());
    $("#helpGuideBtn")?.addEventListener("click", openOnboardingDialog);
    $("#closeOnboardingBtn")?.addEventListener("click", closeOnboardingDialog);
    $("#prevOnboardingBtn")?.addEventListener("click", () => setOnboardingStep(currentOnboardingStep - 1));
    $("#nextOnboardingBtn")?.addEventListener("click", () => setOnboardingStep(currentOnboardingStep + 1));
    $("#finishOnboardingBtn")?.addEventListener("click", closeOnboardingDialog);
    $$(".onboarding-step-tab").forEach((tab) => {
      tab.addEventListener("click", () => setOnboardingStep(parseInt(tab.dataset.step, 10)));
    });
    $("#authForm").addEventListener("submit", submitAuthForm);
    $("#retryAuthBtn").addEventListener("click", () => {
      // No credential resubmission or automatic signup retries.
      if (state.authInitializing || state.authBusy) return;
      if (!window.supabase?.createClient || !state.authConfig) {
        window.location.reload();
      } else {
        void initializeCloudAuth(state.authConfig);
      }
    });
    $("#authModeToggle").addEventListener("click", () => {
      if (state.authStatus !== "ready" || state.authBusy) return;
      state.authMode = state.authMode === "signin" ? "signup" : "signin";
      $("#authTitle").textContent = state.authMode === "signup" ? "创建你的项目空间" : "登录你的项目空间";
      syncAuthControls();
      $("#authModeToggle").textContent = state.authMode === "signup"
        ? "已有账号？返回登录"
        : "还没有账号？创建账号";
      $("#authPassword").autocomplete = state.authMode === "signup" ? "new-password" : "current-password";
      $("#authFeedback").textContent = "";
      updateTurnstileState();
    });
    $("#signOutBtn").addEventListener("click", signOutCloudUser);
  }

  async function triggerHeroStart() {
    const input = $("#heroProjectRequest");
    const query = input?.value?.trim();
    if (!query) {
      toast("请先输入你的申报需求，例如：我要申请国家励志奖学金", true);
      input?.focus();
      return;
    }
    $("#projectRequest").value = query;
    const startBtn = $("#heroStartBtn");
    if (startBtn) {
      startBtn.disabled = true;
      startBtn.innerHTML = '<span>匹配赛道中…</span>';
    }
    try {
      const route = await api("/api/route", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      if (route.selected) {
        await completeProjectCreation({
          title: "",
          level: "",
          discipline: "",
          school: "",
          has_template: false,
          skill_id: route.selected.id,
          initial_request: query,
        });
      } else {
        const box = $("#heroRouteCandidates");
        if (box) {
          box.replaceChildren();
          const intro = document.createElement("p");
          intro.textContent = route.message || "为你匹配到以下赛道，请确认：";
          box.append(intro);
          const candidates = route.candidates?.length ? route.candidates : state.skills.slice(0, 6);
          for (const cand of candidates) {
            const chip = button(cand.name, "button button-small button-secondary");
            chip.addEventListener("click", () => {
              completeProjectCreation({
                title: "",
                level: "",
                discipline: "",
                school: "",
                has_template: false,
                skill_id: cand.id,
                initial_request: query,
              });
            });
            box.append(chip);
          }
          box.classList.remove("hidden");
        }
      }
    } catch (err) {
      toast(`赛道匹配失败：${err.message}`, true);
    } finally {
      if (startBtn) {
        startBtn.disabled = false;
        startBtn.innerHTML = '<span>立即开启</span><span class="arrow">→</span>';
      }
    }
  }

  async function init() {
    setupResizeHandles();
    bindEvents();
    bindMobileControls();
    updateTemplateExplain();
    $("#chatFileInput").accept = chatUploadAccept($("#chatUploadKind").value);
    showAuthGate("正在初始化登录服务…");
    // 目录和套餐不再是登录的前置依赖，缓存命中与否都不会阻塞认证。
    void loadSkills().catch(() => console.warn("赛道目录暂不可用，可稍后重试"));
    void loadPresetModelsAndPlans();
    const config = await api("/api/config");
    $("#privateRagSettings")?.classList.toggle("hidden", !config.private_embedding_configured);
    $("#deleteCloudModelConfigBtn")?.classList.toggle("hidden", !config.cloud_mode);
    $("#syncCloudModelConfigBtn")?.classList.toggle("hidden", !config.cloud_mode);
    state.turnstileSiteKey = config.turnstile_site_key || "";
    updateTurnstileState();
    $("#pilotFingerprintBox")?.classList.toggle(
      "hidden", new URLSearchParams(window.location.search).get("pilot") !== "1"
    );
    $("#workbenchImageControls")?.classList.toggle("hidden", !config.preset_enabled);
    state.cardRedemptionEnabled = Boolean(config.card_redemption_enabled);
    state.cardShopUrl = config.card_shop_url || "";
    const redeemButton = $("#submitRedeemBtn");
    if (redeemButton) {
      redeemButton.disabled = !state.cardRedemptionEnabled;
      redeemButton.textContent = state.cardRedemptionEnabled ? "立即兑换" : "代兑暂未开通";
    }
    const shopLink = $("#cardShopLink");
    const shopUnavailable = $("#cardShopUnavailable");
    if (shopLink && state.cardRedemptionEnabled && config.card_shop_url) {
      shopLink.href = config.card_shop_url;
      shopLink.classList.remove("hidden");
      shopUnavailable?.classList.add("hidden");
    }
    if (!config.preset_enabled && state.llmConfig.provider === "preset") {
      storeLlmConfig({ provider: config.cloud_mode ? "openai_compatible" : "ollama",
        model: config.cloud_mode ? "" : "qwen2.5:7b", base_url: "", api_key: "" });
    }
    if (!config.preset_enabled) {
      $("#providerPreset")?.closest(".model-provider-option")?.classList.add("hidden");
      if (state.llmConfig.provider === "preset") updateModelFields();
    }
    await initializeCloudAuth(config);
    // In cloud mode the auth gate initializes the Supabase session first; only
    // then can the server bind the balance request to the verified user ID.
    if (!config.cloud_mode || state.authSession?.access_token) await loadUserBalance();
  }

  document.addEventListener("DOMContentLoaded", () => {
    window.addEventListener("beforeunload", event => {
      if (!sectionEdits.hasUnsaved() && !factEdits.hasUnsaved() && !documentCorrections.size
          && !documentRecovery.hasEntries() && !outlineDrafts.size && !outlinePreviews.size && !outlineRecovery.hasEntries()) return;
      event.preventDefault();
      event.returnValue = "";
    });
    init().catch((error) => {
      if (state.authStatus !== "ready" && !state.appStarted) {
        state.authStatus = "error";
        showAuthGate("工作台初始化失败，请点击“重新连接登录服务”或刷新页面重试。");
      } else {
        toast(error.message, true);
      }
    });
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.authSession?.access_token) void loadUserBalance();
  });
})();
