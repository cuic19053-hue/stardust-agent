const assert = require("node:assert/strict");
const { Buffer } = require("node:buffer");
const { createHash, webcrypto } = require("node:crypto");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const wordType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const wordBytes = Buffer.concat([Buffer.from("PK\x03\x04"), Buffer.alloc(24)]);
const wordDigest = `sha-256=:${createHash("sha256").update(wordBytes).digest("base64")}:`;
const wordResponse = () => ({ ok: true, headers: new Headers({ "Repr-Digest": wordDigest }),
  blob: async () => new Blob([wordBytes], { type: wordType }) });

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    this.open = false;
    this.textContent = "";
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      toggle(name, force) {
        const active = force === undefined ? !classes.has(name) : force;
        if (active) classes.add(name);
        else classes.delete(name);
      },
    };
  }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  removeChild(child) { this.children = this.children.filter(c => c !== child); }
  click() { this.clicked = true; return this.listeners.click?.(); }
  replaceChildren(...children) { this.children = children; this.textContent = ""; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  closest() { return null; }
  remove() { this.removed = true; }
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.close?.(); }
}

function harness(sharedSession = new Map()) {
  const elements = new Map();
  const revoked = [];
  const created = [];
  const requests = [];
  const timers = new Map();
  let nextTimer = 1;
  let draftAreas = [];
  let factFields = [];
  let workspacePanels = [];
  const document = {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    },
    getElementById: (id) => elements.get(`#${id}`) || null,
    querySelectorAll(selector) {
      if (selector === ".draft-section textarea") return draftAreas;
      if (selector === "[data-fact]") return factFields;
      if (selector === ".workspace-main .panel") return workspacePanels;
      return [];
    },
    createElement: (tag) => new Element(tag),
    addEventListener() {},
    body: new Element("body"),
  };
  const storage = new Map();
  const session = sharedSession;
  const context = vm.createContext({
    document,
    window: { location: { origin: "https://workbench.example.test" } },
    localStorage: { getItem: (k) => storage.get(k) || null, setItem: (k,v) => storage.set(k,v), removeItem: (k) => storage.delete(k) },
    sessionStorage: {
      get length() { return session.size; },
      key: index => [...session.keys()][index] ?? null,
      getItem: key => session.get(key) ?? null,
      setItem: (key, value) => session.set(key, value),
      removeItem: key => session.delete(key),
    },
    Headers,
    AbortController,
    Blob,
    crypto: webcrypto,
    btoa: value => Buffer.from(value, "binary").toString("base64"),
    TextEncoder,
    TextDecoder,
    URL: {
      createObjectURL(blob) {
        created.push(blob);
        return `blob:preview-${created.length}`;
      },
      revokeObjectURL: (url) => revoked.push(url),
    },
    fetch: async (url, options) => {
      requests.push({ url, options });
      return url.endsWith("/download") ? wordResponse() : { ok: true, blob: async () => new Blob(["test original"]) };
    },
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval(callback) { const id = nextTimer++; timers.set(id, callback); return id; },
    clearInterval(id) { timers.delete(id); },
  });
  for (const name of ["material-upload.js", "draft-recovery.js", "section-edits.js", "fact-edits.js", "word-export.js"]) {
    const moduleFile = resolve(__dirname, "../../server/static", name);
    vm.runInContext(readFileSync(moduleFile, "utf8"), context, { filename: moduleFile });
  }
  const file = resolve(__dirname, "../../server/static/app.js");
  const source = readFileSync(file, "utf8").replace(/\}\)\(\);\s*$/, `
    globalThis.ui = {
      state, renderUploadResult, renderDocuments, previewDocument, renderLeaderPhoto, proposeFacts, searchSources, previewOfficialUrl,
      clearDocumentPreview, fetchDocumentBlob, uploadFiles,
      uploadFilesToCloud, cloudUploadIntent, uploadResumable, setUploadKind, oversizedUploadError,
      renderPrivacyNotice, showModelStatus, updateTemplateQuickActions,
      syncAuthControls, initializeCloudAuth, submitAuthForm,
      readPublicCache, writePublicCache, sameOriginAuthFetch, storeLlmConfig, loadCloudModelConfig, saveSelectedLlmConfig,
      sendChat, syncChatControls, clearInterruptedChatMessages, loadProject, refreshProjects, closeDeletedProject, startApplication, deleteCurrentProject, startGeneralChat, selectConversationSkill, configureSkillWorkspace, renderFacts, recordFactInput, saveFacts, renderSections, restoreOutlineEditor, renderOutline, generateOutline, recommendOutline, saveOutline, validate, restoreInterruptedChatMessage, updateRecoveredChatInput, startBatchGeneration, cancelBatchGeneration,
      exportDoc, fetchExportBlob, flushDraftEdits, showChatDrafts, clearExportArtifacts, focusDraftSection,
      setApi(fn) { api = fn; },
      setLoadProject(fn) { loadProject = fn; },
      setChatRefresh(fn) { refreshProjects = fn; loadUserBalance = fn; },
    };
  })();`);
  vm.runInContext(source, context, { filename: file });
  return {
    ui: context.ui, context, requests, revoked, created, timers, session,
    element: (selector) => document.querySelector(selector),
    setDraftAreas: (areas) => { draftAreas = areas; },
    setFactFields: (fields) => { factFields = fields; },
    setWorkspacePanels: (panels) => { workspacePanels = panels; },
  };
}

const doc = {
  id: "a".repeat(32),
  original_name: "学校材料.pdf",
  extracted_text: "待学生核对的材料文字",
  extraction_status: "ready",
  kind: "source",
  confirmed: false,
};

test("a review issue opens the chapter panel and focuses an editor in a folded preview", () => {
  const h = harness();
  const draft = h.element("#draft-panel");
  const review = h.element("#finish-panel");
  draft.id = "draft-panel";
  review.id = "finish-panel";
  draft.classList.add("hidden");
  draft.scrollIntoView = () => {};
  h.setWorkspacePanels([draft, review]);

  const section = h.element("#draft-sec-method");
  const body = new Element();
  const editor = new Element("textarea");
  const fold = new Element("button");
  const edit = new Element("button");
  body.classList.add("folded");
  editor.classList.add("hidden");
  fold.addEventListener("click", () => body.classList.remove("folded"));
  edit.addEventListener("click", () => editor.classList.remove("hidden"));
  section.closest = selector => selector === ".panel" ? draft : null;
  section.querySelector = selector => ({
    ".draft-section-body.folded": body.classList.contains("folded") ? body : null,
    ".fold-toggle-btn": fold,
    ".mode-switch-btn": edit,
    "textarea": editor,
  })[selector];
  editor.focus = () => {
    assert.equal(body.classList.contains("folded"), false);
    assert.equal(editor.classList.contains("hidden"), false);
    editor.focused = true;
  };
  section.scrollIntoView = () => { section.scrolled = true; };

  h.ui.focusDraftSection("method");
  assert.equal(h.ui.state.activeTab, draft.id);
  assert.equal(draft.classList.contains("hidden"), false);
  assert.equal(review.classList.contains("hidden"), true);
  assert.ok(editor.focused && section.scrolled);
});

test("recommended outline previews without a model and retains saved chapters", async () => {
  const h = harness();
  h.ui.state.project = { id: "project-one", outline: [{ id: "old-summary", title: "项目摘要" },
    { id: "custom", title: "学生自写章节" }], sections: { "old-summary": "原有摘要", custom: "学生原文" } };
  const calls = [];
  h.ui.setApi(async (url, options) => {
    calls.push({ url, options });
    return { outline: [{ id: "abstract", title: "项目摘要" }, { id: "background", title: "选题背景" }], warning: "请核对学校模板。" };
  });
  await h.ui.recommendOutline();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/projects/project-one/outline/recommend");
  assert.equal(calls[0].options, undefined);
  assert.deepEqual(Array.from(h.ui.state.outline, item => item.id), ["old-summary", "background", "custom"]);
  assert.equal(h.ui.state.project.outline.length, 2, "the saved outline is unchanged until confirmation");
  assert.match(h.element("#outlineWarning").textContent, /已保留含正文的原章节/);
});

test("a recommendation arriving after a project switch cannot replace the new outline", async () => {
  const h = harness();
  h.ui.state.project = { id: "old", outline: [], sections: {} };
  let finish;
  h.ui.setApi(() => new Promise(resolve => { finish = resolve; }));
  const pending = h.ui.recommendOutline();
  await Promise.resolve();
  h.ui.state.project = { id: "new", outline: [], sections: {} };
  h.ui.state.outline = [{ id: "new-section", title: "新项目章节" }];
  finish({ outline: [{ id: "old-section", title: "旧项目推荐" }] });
  await pending;
  assert.equal(h.ui.state.outline[0].id, "new-section");
});

test("confirming an outline updates the current project for subsequent edits", async () => {
  const h = harness();
  h.ui.state.project = { id: "project-one", outline: [], sections: {}, state: {} };
  h.ui.state.outline = [{ id: "abstract", title: "项目摘要" }];
  const calls = [];
  h.ui.setApi(async (url, options) => { calls.push({ url, options }); return { outline: [{ id: "abstract", title: "项目摘要" }] }; });
  await h.ui.saveOutline();
  assert.equal(calls[0].url, "/api/projects/project-one/outline");
  assert.equal(h.ui.state.project.outline[0].id, "abstract");
  assert.equal(h.ui.state.project.sections.abstract, "");
  assert.equal(h.ui.state.project.state.outline_confirmed, true);
});

test("valid directory keys that share object property names create normal blank editors", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.project.outline = [];
  h.ui.state.outline = [{ id: "constructor", title: "第一节" }, { id: "__proto__", title: "第二节" }];
  h.ui.setApi(async () => ({ outline: h.ui.state.outline, material_revision: 3 }));
  await h.ui.saveOutline();
  assert.equal(h.ui.state.project.sections.constructor, "");
  assert.equal(h.ui.state.project.sections["__proto__"], "");
  assert.equal(h.element("#draftSections").children.length, 2);
});

test("a generated preview never reuses an existing chapter ID for another topic", async () => {
  const h = harness(); exportingProject(h);
  h.ui.restoreOutlineEditor(h.ui.state.project);
  h.ui.setApi(async () => ({ outline: [
    { id: "purpose", title: "不同的研究方法" }, { id: "purpose", title: "重复标识的新栏目" },
  ] }));
  await h.ui.generateOutline();
  const draft = h.ui.state.outline;
  assert.deepEqual(Array.from(draft, row => row.title), ["不同的研究方法", "重复标识的新栏目", "项目目的"]);
  assert.equal(new Set(Array.from(draft, row => row.id)).size, 3);
  assert.equal(draft[2].id, "purpose");
  h.ui.renderSections();
  assert.equal(h.element("#draftSections").children.length, 1);
  assert.equal(h.element("#draftSections").children[0].id, "draft-sec-purpose");
});

test("typing while a candidate is being generated keeps both the editor text and the preview", async () => {
  const h = harness(); exportingProject(h);
  h.ui.restoreOutlineEditor(h.ui.state.project);
  h.ui.renderOutline();
  let finish;
  h.ui.setApi(() => new Promise(resolve => { finish = resolve; }));
  const pending = h.ui.generateOutline();
  await new Promise(resolve => setImmediate(resolve));
  const input = h.element("#outlineList").children[0].children[1];
  input.value = "继续写的栏目";
  input.listeners.input();
  finish({ outline: [{ id: "candidate", title: "模型候选" }] });
  await pending;
  assert.equal(h.ui.state.outline[0].title, "继续写的栏目");
  assert.ok(h.element("#outlineList").children.some(child => child.textContent === "查看已生成的候选目录"));
});

test("a refreshed outline keeps local edits and exposes the competing saved version", () => {
  const h = harness(); exportingProject(h);
  h.ui.restoreOutlineEditor(h.ui.state.project);
  h.ui.renderOutline();
  const input = h.element("#outlineList").children[0].children[1];
  input.value = "本页准备确认的标题";
  input.listeners.input();
  assert.equal(h.ui.state.project.outline[0].title, "项目目的");
  h.ui.state.project.outline = [{ id: "purpose", title: "服务器上的新标题" }];
  h.ui.restoreOutlineEditor(h.ui.state.project);
  h.ui.renderOutline();
  assert.equal(h.ui.state.outline[0].title, "本页准备确认的标题");
  assert.match(h.element("#outlineWarning").textContent, /其他页面/);
  assert.equal(h.element("#saveOutlineBtn").disabled, true);
  const comparison = h.element("#outlineList").children.at(-1);
  assert.match(comparison.children[0].textContent, /服务器上的新标题/);
  comparison.children[1].click();
  assert.equal(h.element("#saveOutlineBtn").disabled, false);
});

test("reopening the page restores an edited directory against the original baseline", () => {
  const session = new Map();
  const initial = harness(session); exportingProject(initial);
  const edit = openOutlineEditor(initial);
  edit.rename("刷新前的目录");
  const reloaded = harness(session); exportingProject(reloaded);
  reloaded.ui.state.project.outline[0].title = "另一页先保存的目录";
  openOutlineEditor(reloaded);
  assert.equal(reloaded.ui.state.outline[0].title, "刷新前的目录");
  assert.match(reloaded.element("#outlineWarning").textContent, /其他页面/);
  assert.equal(reloaded.element("#saveOutlineBtn").disabled, true);
  assert.equal(reloaded.requests.length, 0);
});

test("a committed outline save with a lost receipt preserves typing after submission", async () => {
  const session = new Map();
  const first = harness(session); exportingProject(first);
  const edit = openOutlineEditor(first);
  edit.rename(" 已提交的目录 ");
  let launched;
  const started = new Promise(resolve => { launched = resolve; });
  first.ui.setApi(async (_url, options) => {
    launched(JSON.parse(options.body));
    return new Promise(() => {});
  });
  void first.ui.saveOutline();
  assert.equal((await started).outline[0].title.trim(), "已提交的目录");
  edit.rename("提交后继续改的目录");

  const reloaded = harness(session); exportingProject(reloaded);
  reloaded.ui.state.project.state.outline_confirmed = true;
  reloaded.ui.state.project.outline[0].title = "已提交的目录";
  openOutlineEditor(reloaded);
  assert.equal(reloaded.ui.state.outline[0].title, "提交后继续改的目录");
  assert.equal(reloaded.element("#saveOutlineBtn").disabled, false);
  let saving;
  reloaded.ui.setApi(async (_url, options) => {
    saving = JSON.parse(options.body);
    return { outline: saving.outline, material_revision: 4 };
  });
  await reloaded.ui.saveOutline();
  assert.equal(saving.base_outline[0].title, "已提交的目录");
  assert.equal(saving.outline[0].title, "提交后继续改的目录");
  assert.equal(session.size, 0);
});

test("refresh keeps a completed candidate alongside the outline still being edited", async () => {
  const session = new Map();
  const first = harness(session); exportingProject(first);
  const edit = openOutlineEditor(first);
  edit.rename("正在整理的目录");
  first.ui.setApi(async () => ({ outline: [{ id: "candidate", title: "已完成的候选目录" }] }));
  await first.ui.generateOutline();
  const reloaded = harness(session); exportingProject(reloaded);
  openOutlineEditor(reloaded);
  assert.equal(reloaded.ui.state.outline[0].title, "正在整理的目录");
  assert.ok(reloaded.element("#outlineList").children.some(child => child.textContent === "查看已生成的候选目录"));
});

test("undoing a local directory edit removes the pending browser draft", () => {
  const session = new Map();
  const h = harness(session); exportingProject(h);
  h.ui.state.project.state.outline_confirmed = true;
  const editing = openOutlineEditor(h);
  editing.rename("瞬时改动");
  assert.equal(session.size, 1);
  editing.rename("项目目的");
  assert.equal(session.size, 0);
  assert.equal(h.element("#outlineWarning").textContent, "");
});

test("directory recovery remains bound to the owning account and clears when its project is deleted", () => {
  const session = new Map([["unrelated", "keep"]]);
  const owner = harness(session); exportingProject(owner);
  owner.ui.state.cloudMode = true;
  owner.ui.state.authSession.user = { id: "owner" };
  openOutlineEditor(owner).rename("项目的私有目录草稿");
  const other = harness(session); exportingProject(other);
  other.ui.state.cloudMode = true;
  other.ui.state.authSession.user = { id: "different-account" };
  openOutlineEditor(other);
  assert.equal(other.ui.state.outline[0].title, "项目目的");
  assert.ok([...session.values()].some(value => value.includes("项目的私有目录草稿")));
  owner.ui.closeDeletedProject(owner.ui.state.project.id);
  assert.deepEqual([...session], [["unrelated", "keep"]]);
});

test("a candidate returned after logout cannot recreate directory recovery", async () => {
  const session = new Map();
  const h = harness(session); exportingProject(h);
  h.ui.state.authSession.user = { id: "previous" };
  openOutlineEditor(h).rename("退出前的目录");
  let finish;
  h.ui.setApi(() => new Promise(resolve => { finish = resolve; }));
  const pending = h.ui.generateOutline();
  await new Promise(resolve => setImmediate(resolve));
  h.ui.clearInterruptedChatMessages();
  h.ui.state.authSession = { user: { id: "next" } };
  finish({ outline: [{ id: "candidate", title: "旧账号的候选" }] });
  await pending;
  assert.equal(session.size, 0);
});

test("blocked browser storage leaves the working directory usable and warns about refresh", () => {
  const h = harness(); exportingProject(h);
  const editing = openOutlineEditor(h);
  h.context.sessionStorage.setItem = () => { throw new Error("storage blocked"); };
  editing.rename("仅保存在当前页的目录");
  assert.match(h.element("#toast").textContent, /先保存目录再刷新/);
  openOutlineEditor(h);
  assert.equal(h.ui.state.outline[0].title, "仅保存在当前页的目录");
});

test("returning to a project during a directory save preserves later typing and clears the loading state", async () => {
  const h = harness(); exportingProject(h);
  h.ui.setChatRefresh(async () => {});
  const project = h.ui.state.project;
  const source = { ...project, title: "编辑中的项目", outline: project.outline.map(row => ({ ...row })),
    state: { ...project.state, outline_confirmed: true } };
  let uploaded;
  let responded;
  let start;
  const sent = new Promise(resolve => { start = resolve; });
  h.ui.setApi(async (url, options = {}) => {
    if (options.method === "PUT") {
      const body = JSON.parse(options.body);
      uploaded = body.outline;
      start();
      return new Promise(resolve => { responded = resolve; });
    }
    if (url.endsWith("/other")) return { ...source, id: "other", title: "另一项目" };
    return { ...source };
  });
  openOutlineEditor(h).rename("服务器将收到的目录");
  const saving = h.ui.saveOutline();
  await sent;
  openOutlineEditor(h).rename("保存期间继续编辑的目录");
  await h.ui.loadProject("other", { refreshList: false });
  await h.ui.loadProject(source.id, { refreshList: false });
  assert.equal(h.element("#saveOutlineBtn").disabled, true);
  source.outline = uploaded;
  source.material_revision++;
  responded({ outline: uploaded, material_revision: source.material_revision });
  await saving;
  assert.equal(h.ui.state.project.outline[0].title, "服务器将收到的目录");
  assert.equal(h.ui.state.outline[0].title, "保存期间继续编辑的目录");
  assert.equal(h.element("#saveOutlineBtn").disabled, false);
});

function openOutlineEditor(h) {
  h.ui.restoreOutlineEditor(h.ui.state.project);
  h.ui.renderOutline();
  return { rename(title) {
    const input = h.element("#outlineList").children[0].children[1];
    input.value = title;
    input.listeners.input();
  } };
}

test("removing the only directory row still leaves manual addition available", () => {
  const h = harness(); exportingProject(h);
  h.ui.restoreOutlineEditor(h.ui.state.project);
  h.ui.renderOutline();
  h.element("#outlineList").children[0].children[4].click();
  const add = h.element("#outlineList").children.find(child => child.textContent === "＋ 添加章节");
  assert.ok(add);
  add.click();
  assert.equal(h.ui.state.outline.length, 1);
  assert.equal(h.ui.state.project.outline[0].id, "purpose");
});

test("student validation displays the limited scope of the check", async () => {
  const h = harness();
  h.ui.state.project = { id: "project-one" };
  h.ui.setApi(async () => ({ passed: true, validation_scope: "通用内容检查，具体以学校通知和模板为准。", photo_notice: "" }));
  await h.ui.validate();
  const box = h.element("#validationResult");
  assert.ok(box.children.some(child => child.textContent.includes("具体以学校通知和模板为准")));
});

test("national scholarship workspace offers relevant prompts and hides innovation review", () => {
  const h = harness();
  const project = { state: { skill_id: "national-scholarship", skill_category: "scholarship", skill_name: "国家奖学金" } };
  h.ui.configureSkillWorkspace(project);
  const prompts = h.element("#chatTools").children.map(child => child.textContent);
  assert.ok(prompts.some(text => text.includes("学业")));
  assert.ok(prompts.some(text => text.includes("起草申请书")));
  assert.ok(prompts.every(text => !text.includes("家庭经济") && !text.includes("勤工助学")));
  assert.equal(h.element("#innovationReviewRow").classList.contains("hidden"), true);
  assert.ok(!h.element("#exportHint").textContent.includes("实验图"));
  h.ui.state.project = project;
  h.ui.renderFacts();
  const factLabels = h.element("#factsSummary").children.map(child => child.children[0]?.textContent);
  assert.ok(factLabels.includes("所在学院"));
  assert.ok(!factLabels.includes("预算") && !factLabels.includes("负责人照片"));
  h.ui.configureSkillWorkspace({ state: { skill_id: "innovation-research", skill_category: "research" } });
  assert.equal(h.element("#innovationReviewRow").classList.contains("hidden"), false);
});

test("national scholarship keeps personal layout before skill metadata is available", () => {
  const h = harness();
  const project = { state: { skill_id: "national-scholarship" } };
  h.ui.state.project = project;
  h.ui.renderFacts();
  const labels = h.element("#factsSummary").children.map(child => child.children[0]?.textContent);
  assert.ok(labels.includes("所在学院") && !labels.includes("预算"));
});

test("personal award workspaces use student-specific fields and guidance", () => {
  const h = harness();
  for (const [id, category] of [["outstanding-student", "honor"], ["grant-application", "scholarship"], ["university-scholarship", "scholarship"], ["major-transfer", "other"], ["graduate-recommendation", "other"]]) {
    const project = { state: { skill_id: id, skill_category: category } };
    h.ui.state.project = project;
    h.ui.configureSkillWorkspace(project);
    h.ui.renderFacts();
    const labels = h.element("#factsSummary").children.map(child => child.children[0]?.textContent);
    assert.ok(labels.includes("所在学院"));
    assert.ok(!labels.includes("预算") && !labels.includes("项目名称") && !labels.includes("负责人照片"));
    assert.equal(h.element("#panel-materials .panel-heading h2").textContent, "个人参考材料与知识库");
    assert.equal(h.element("#sources-panel .panel-heading h2").textContent, "学校通知与申报依据");
    const prompts = h.element("#chatTools").children.map(child => child.textContent);
    assert.equal(prompts.some(text => text.includes("家庭")), id === "grant-application");
    if (id === "major-transfer") assert.ok(prompts.some(text => text.includes("转专业")));
    if (id === "graduate-recommendation") assert.ok(prompts.some(text => text.includes("个人陈述")));
  }
  h.ui.configureSkillWorkspace({ state: { skill_id: "innovation-research", skill_category: "research" } });
  assert.equal(h.element("#panel-materials .panel-heading h2").textContent, "项目参考材料与知识库");
  assert.equal(h.element("#sources-panel .panel-heading h2").textContent, "学术文献检索与官方立项依据");
});

test("switching from a research project clears hidden editor fields before saving a student application", async () => {
  const h = harness();
  const budget = h.element('[data-fact="budget"]');
  const budgetParent = new Element("label");
  budget.dataset.fact = "budget";
  budget.closest = () => budgetParent;
  const college = h.element('[data-fact="college"]');
  college.dataset.fact = "college";
  college.closest = () => new Element("label");
  h.setFactFields([budget, college]);
  h.ui.state.project = { id: "research", state: { skill_id: "innovation-research", skill_category: "research", confirmed_facts: { budget: "50000 元", college: "原学院" } } };
  h.ui.renderFacts();
  assert.equal(budget.value, "50000 元");
  h.ui.state.project = { id: "award", state: { skill_id: "outstanding-student", skill_category: "honor", confirmed_facts: { college: "新学院" } } };
  h.ui.renderFacts();
  assert.equal(budget.value, "");
  assert.equal(college.value, "新学院");
  assert.equal(budgetParent.classList.contains("hidden"), true);
  const requests = [];
  h.ui.setApi(async (url, options) => { requests.push({ url, options }); return {}; });
  h.ui.setLoadProject(async () => {});
  await h.ui.saveFacts();
  assert.deepEqual(requests, [], "unchanged fields and their evidence need no write");
  h.ui.state.project.state.confirmed_facts.budget = "200 元";
  h.ui.renderFacts();
  assert.equal(budgetParent.classList.contains("hidden"), false, "previously saved data remains editable");
  assert.equal(budget.value, "200 元");
});

test("chat-first entry creates a conversation without requiring a Skill", async () => {
  const h = harness();
  const requests = [];
  let loaded = "";
  h.ui.setApi(async (url, options) => {
    requests.push({ url, options });
    return { id: "conversation-one", state: { skill_id: "" } };
  });
  h.ui.setChatRefresh(async () => {});
  h.ui.setLoadProject(async (id) => { loaded = id; });
  h.element("#chatInput").focus = () => {};
  await h.ui.startGeneralChat();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/projects/conversation");
  assert.equal(requests[0].options.method, "POST");
  assert.equal(loaded, "conversation-one");
  assert.equal(h.element("#newChatBtn").disabled, false);
});

test("user can select a Skill for an unrouted conversation", async () => {
  const h = harness();
  h.ui.state.project = { id: "conversation-one", state: { skill_id: "" } };
  h.element("#conversationSkillSelect").value = "national-scholarship";
  let loaded = "";
  const requests = [];
  h.ui.setApi(async (url, options) => { requests.push({ url, options }); return {}; });
  h.ui.setLoadProject(async id => { loaded = id; });
  await h.ui.selectConversationSkill();
  assert.equal(requests[0].url, "/api/projects/conversation-one/skill");
  assert.equal(JSON.parse(requests[0].options.body).skill_id, "national-scholarship");
  assert.equal(loaded, "conversation-one");
  assert.equal(h.element("#conversationSkillApply").disabled, false);
});

function exportingProject(h) {
  h.ui.state.project = { id: "a".repeat(32), material_revision: 2,
    outline: [{ id: "purpose", title: "项目目的" }], sections: { purpose: "已保存正文" },
    state: { skill_name: "测试材料" } };
  h.ui.state.authSession = { access_token: "fake-session-jwt" };
}

test("batch generation stops on project switch without writing into another project", async () => {
  const h = harness(); exportingProject(h);
  const firstProject = h.ui.state.project;
  h.ui.state.project.outline = [{ id: "one", title: "第一章" }, { id: "two", title: "第二章" }];
  const calls = [];
  let finish;
  h.ui.setApi((url, options) => { calls.push({ url, options }); return new Promise(resolve => { finish = resolve; }); });
  const running = h.ui.startBatchGeneration();
  await new Promise(resolve => setImmediate(resolve));
  h.ui.state.project = { id: "second-project", sections: { one: "新项目正文" } };
  finish({ content: "旧项目生成结果" });
  await running;
  assert.equal(calls.length, 1);
  assert.equal(h.ui.state.project.sections.one, "新项目正文");
  assert.equal(firstProject.sections.one, undefined);
  assert.equal(h.ui.state.batchGenerating, false);
});

test("batch stops after an uncertain result rather than generating further chapters", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.project.outline = [{ id: "one", title: "第一章" }, { id: "two", title: "第二章" }];
  const calls = [];
  h.ui.setApi(async (url, options) => { calls.push({ url, options }); return { status: "processing", job_id: "job" }; });
  await h.ui.startBatchGeneration();
  assert.equal(calls.length, 1);
  assert.match(h.element("#batchProgressText").textContent, /后台处理/);
  assert.equal(h.ui.state.project.sections.one, undefined);
  assert.equal(h.ui.state.batchGenerating, false);
});

test("batch recovery resumes only the pending chapter and leaves prior and later chapters alone", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.project.outline = [{ id: "done", title: "第一章" }, { id: "pending", title: "第二章" },
    { id: "later", title: "第三章" }];
  h.ui.state.project.sections = { done: "已经生成" };
  let unresolved = true;
  const calls = [];
  h.context.window.MaterialPilot = {
    active: () => true,
    usesWorker: () => true,
    pendingJob: (_id, kind, sid) => kind === "section" && sid === "pending" && unresolved,
    section: async sid => {
      calls.push(sid);
      unresolved = false;
      return { content: "找回第二章", material_applied: true, material_revision: 3 };
    },
  };
  h.ui.renderSections();
  assert.equal(h.element("#batchGenerateSectionsBtn").textContent, "查看待确认的章节任务");
  await h.ui.startBatchGeneration();
  assert.deepEqual(calls, ["pending"]);
  assert.equal(h.ui.state.project.sections.done, "已经生成");
  assert.equal(h.ui.state.project.sections.pending, "找回第二章");
  assert.equal(h.ui.state.project.sections.later, undefined);
  assert.equal(h.element("#batchGenerateSectionsBtn").textContent, "⚡ 批量生成所有章节");
});

test("cancelling batch generation while the route is loading sends neither kind of generation request", async () => {
  const h = harness(); exportingProject(h);
  let ready;
  h.context.window.MaterialPilot = {
    usesWorker: () => new Promise(resolve => { ready = resolve; }),
    section: () => assert.fail("cancelled batch submitted a worker job"),
  };
  h.ui.setApi(() => assert.fail("cancelled batch submitted a legacy request"));
  const progress = h.ui.startBatchGeneration();
  assert.equal(typeof ready, "function");
  h.ui.cancelBatchGeneration();
  ready(true);
  await progress;
  assert.match(h.element("#batchProgressText").textContent, /停止后续章节/);
});

test("batch stops when a draft requires review", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.project.outline = [{ id: "one", title: "第一章" }, { id: "two", title: "第二章" }];
  const calls = [];
  h.ui.setApi(async (url, options) => { calls.push({ url, options });
    return { conflict_status: "user_edit_preserved", notice: "请核对草稿" }; });
  await h.ui.startBatchGeneration();
  assert.equal(calls.length, 1);
  assert.match(h.element("#batchProgressText").textContent, /请核对草稿.*停止后续章节/);
});

test("batch cancellation does not send subsequent model requests", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.project.outline = [{ id: "one", title: "第一章" }, { id: "two", title: "第二章" }];
  const calls = [];
  let finish;
  h.ui.setApi((url, options) => { calls.push({ url, options }); return new Promise(resolve => { finish = resolve; }); });
  const running = h.ui.startBatchGeneration();
  await new Promise(resolve => setImmediate(resolve));
  h.ui.cancelBatchGeneration();
  finish({ content: "后台当前章可能已保存" });
  await running;
  assert.equal(calls.length, 1);
  assert.match(h.element("#batchProgressText").textContent, /停止后续章节/);
});

test("batch preserves a student's edit made while a chapter is generating", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.project.outline = [{ id: "purpose", title: "第一章" }, { id: "two", title: "第二章" }];
  const area = new Element("textarea"); area.value = "已保存正文";
  area.closest = () => ({ id: "draft-sec-purpose" });
  h.setDraftAreas([area]);
  const calls = []; let finish;
  h.ui.setApi((url, options) => { calls.push({ url, options }); return new Promise(resolve => { finish = resolve; }); });
  const running = h.ui.startBatchGeneration();
  await new Promise(resolve => setImmediate(resolve));
  area.value = "等待期间手写的修改";
  finish({ content: "后台已生成的正文", material_applied: true, material_revision: 5 });
  await running;
  assert.equal(calls.length, 1);
  assert.equal(area.value, "等待期间手写的修改");
  assert.equal(h.ui.state.project.sections.purpose, "后台已生成的正文");
  assert.equal(h.ui.state.project.material_revision, 5);
  assert.match(h.element("#batchProgressText").textContent, /手写编辑已保留/);
});

test("Word export retries a failed download without generating a second document", async () => {
  const h = harness(); exportingProject(h);
  let exports = 0, attempts = 0;
  h.ui.setApi(async (url) => {
    if (url.endsWith("/chat-drafts")) return { items: [] };
    exports++;
    return { download_url: `/api/projects/${h.ui.state.project.id}/download`, download_digest: wordDigest };
  });
  h.context.fetch = async () => ++attempts === 1
    ? { ok: true, blob: async () => new Blob(["<html>gateway error</html>"], { type: "text/html" }) }
    : wordResponse();
  await h.ui.exportDoc();
  assert.equal(h.created.length, 0);
  assert.equal(h.element("#headingExportBtn").textContent, "重试下载 Word");
  await h.ui.exportDoc();
  assert.equal(exports, 1);
  assert.equal(attempts, 2);
  assert.equal(h.created.length, 1);
  assert.equal(h.element("#headingExportBtn").textContent, "📥 导出 Word");
});

test("a lost Word export receipt survives a reload and downloads the original file", async () => {
  const first = harness(); exportingProject(first);
  const project = first.ui.state.project;
  const exportUrl = `/api/projects/${project.id}/export`;
  first.ui.state.authSession.user = { id: "student-one" };
  let requestId;
  first.ui.setApi(async (url, opts) => {
    if (url.endsWith("/chat-drafts")) return { items: [] };
    assert.equal(url, exportUrl);
    requestId = JSON.parse(opts.body).request_id;
    throw new Error("连接已中断");
  });
  await first.ui.exportDoc();
  assert.match(requestId, /^[a-f0-9]{32}$/);
  assert.equal(first.element("#headingExportBtn").textContent, "检查 Word 导出结果");
  assert.equal(first.requests.length, 0);

  const reloaded = harness(first.session);
  reloaded.ui.state.authSession = { user: { id: "student-one" }, access_token: "new-jwt" };
  const calls = [];
  reloaded.ui.setApi(async url => {
    if (url === `/api/projects/${project.id}`) return project;
    if (url === "/api/user/balance") return { balance: 0 };
    calls.push(url);
    assert.equal(url, `${exportUrl}/status/${requestId}`);
    return calls.length === 1 ? { status: "processing" } : {
      status: "completed", download_url: `/api/projects/${project.id}/download`,
      download_digest: wordDigest, warnings: ["学校模板待核对。"],
    };
  });
  await reloaded.ui.loadProject(project.id, { refreshList: false });
  assert.equal(reloaded.element("#headingExportBtn").textContent, "检查 Word 导出结果");
  await reloaded.ui.exportDoc();
  assert.match(reloaded.element("#toast").textContent, /仍在生成/);
  assert.equal(reloaded.element("#headingExportBtn").textContent, "检查 Word 导出结果");
  await reloaded.ui.exportDoc();
  assert.equal(calls.length, 2);
  assert.equal(reloaded.requests[0].options.headers.Authorization, "Bearer new-jwt");
  assert.equal(reloaded.created.length, 1);
  assert.match(reloaded.element("#exportResult").children[0].textContent, /学校模板待核对/);
  assert.equal(reloaded.element("#headingExportBtn").textContent, "📥 导出 Word");
  assert.ok(![...first.session.keys()].some(key => key.startsWith("stardust-word-export-v1:")));
});

test("an uncertain Word export remains scoped and a new edit invalidates it", async () => {
  const h = harness(); exportingProject(h);
  const first = h.ui.state.project;
  const second = { ...first, id: "b".repeat(32) };
  h.ui.state.authSession.user = { id: "student-one" };
  h.ui.setApi(async url => {
    if (url.endsWith("/chat-drafts")) return { items: [] };
    if (url.endsWith("/export")) throw new Error("连接已中断");
    if (url.endsWith(first.id)) return first;
    if (url.endsWith(second.id)) return second;
    throw Error(`unexpected ${url}`);
  });
  await h.ui.exportDoc();
  await h.ui.loadProject(second.id, { refreshList: false });
  assert.equal(h.element("#headingExportBtn").textContent, "📥 导出 Word");
  h.ui.state.authSession.user = { id: "student-two" };
  await h.ui.loadProject(first.id, { refreshList: false });
  assert.equal(h.element("#headingExportBtn").textContent, "📥 导出 Word");
  h.ui.state.authSession.user = { id: "student-one" };
  await h.ui.loadProject(first.id, { refreshList: false });
  assert.equal(h.element("#headingExportBtn").textContent, "检查 Word 导出结果");
  h.ui.clearExportArtifacts();
  assert.equal(h.element("#headingExportBtn").textContent, "📥 导出 Word");
  assert.ok(![...h.session.keys()].some(key => key.startsWith("stardust-word-export-v1:")));
});

test("a missing background export receipt can be recovered after refresh", async () => {
  const first = harness(); exportingProject(first);
  first.ui.state.authSession.user = { id: "student-one" };
  const project = first.ui.state.project;
  const base = `/api/projects/${project.id}`;
  let submissions = 0, key;
  first.ui.setApi(async url => {
    assert.equal(url, base + "/chat-drafts");
    return { items: [] };
  });
  first.context.window.MaterialPilot = { active: () => true, usesWorker: () => true, exportDocument: async (options, _active, _ready, track) => {
    submissions++;
    key = options.idempotency_key;
    track({ kind: "worker-request", id: key });
    throw new Error("连接中断");
  } };
  await first.ui.exportDoc();
  assert.match(key, /^[a-f0-9]{32}$/);
  assert.equal(first.element("#headingExportBtn").textContent, "检查 Word 导出结果");

  const refreshed = harness(first.session);
  refreshed.ui.state.authSession = { user: { id: "student-one" }, access_token: "new-jwt" };
  const job = { kind: "export", project_id: project.id, id: "c".repeat(32), status: "pending" };
  const checks = [];
  refreshed.ui.setApi(async url => {
    if (url === base) return project;
    if (url === "/api/user/balance") return { balance: 0 };
    checks.push(url);
    if (url === `${base}/material/exports/by-request/${key}`) return job;
    if (url === `${base}/material/jobs/${job.id}`) return { ...job, status: "completed" };
    throw Error(`unexpected ${url}`);
  });
  refreshed.context.fetch = async (url, options) => {
    assert.equal(url, `${base}/material/jobs/${job.id}/files/proposal.docx?current=true`);
    assert.equal(options.headers.Authorization, "Bearer new-jwt");
    return wordResponse();
  };
  await refreshed.ui.loadProject(project.id, { refreshList: false });
  await refreshed.ui.exportDoc();
  assert.equal(refreshed.element("#headingExportBtn").textContent, "检查 Word 导出结果");
  await refreshed.ui.exportDoc();
  assert.equal(submissions, 1);
  assert.equal(checks.length, 2);
  assert.equal(refreshed.created.length, 1);
  assert.equal(refreshed.element("#headingExportBtn").textContent, "📥 导出 Word");
});

test("a stalled Word download times out and releases the toolbar for retry", async () => {
  const h = harness(); exportingProject(h);
  h.ui.setApi(async url => url.endsWith("/chat-drafts") ? { items: [] }
    : { download_url: `/api/projects/${h.ui.state.project.id}/download` });
  let deadline;
  h.context.setTimeout = (callback, time) => {
    if (time === 45000) deadline = callback;
    return 1;
  };
  h.context.fetch = (_url, { signal }) => new Promise((_, reject) => {
    signal.addEventListener("abort", () => reject(new Error("download aborted")), { once: true });
  });
  const attempt = h.ui.exportDoc();
  while (!deadline) await Promise.resolve();
  deadline();
  await attempt;
  assert.match(h.element("#toast").textContent, /Word 下载超时/);
  assert.equal(h.element("#headingExportBtn").disabled, false);
  assert.equal(h.element("#headingExportBtn").textContent, "重试下载 Word");
  assert.equal(h.created.length, 0);
});

test("Word download detects corrupted bytes with a valid MIME type and ZIP signature", async () => {
  const h = harness(); exportingProject(h);
  const response = wordResponse();
  response.blob = async () => new Blob([wordBytes, "changed"], { type: wordType });
  h.context.fetch = async () => response;
  await assert.rejects(h.ui.fetchExportBlob(`/api/projects/${h.ui.state.project.id}/download`, h.ui.state.project.id), /校验失败/);
});

test("a different server export invalidates the download receipt", async () => {
  const h = harness(); exportingProject(h);
  let exports = 0;
  h.ui.setApi(async url => {
    if (url.endsWith("/chat-drafts")) return { items: [] };
    exports++;
    return { download_url: `/api/projects/${h.ui.state.project.id}/download`,
      download_digest: exports === 1 ? "sha-256=:another-export:" : wordDigest };
  });
  await h.ui.exportDoc();
  assert.equal(h.created.length, 0);
  assert.match(h.element("#toast").textContent, /重新导出/);
  assert.equal(h.element("#headingExportBtn").textContent, "📥 导出 Word");
  await h.ui.exportDoc();
  assert.equal(exports, 2);
  assert.equal(h.created.length, 1);
});

test("the export toolbar can retry an existing background job file", async () => {
  const h = harness(); exportingProject(h);
  let jobs = 0, gets = 0;
  h.ui.setApi(async url => {
    assert.match(url, /chat-drafts$/);
    return { items: [] };
  });
  h.context.window.MaterialPilot = { active: () => true, usesWorker: () => true, exportDocument: (_opts, _current, onReady) => {
    jobs++;
    return onReady(`/api/projects/${h.ui.state.project.id}/material/jobs/${"b".repeat(32)}/files/proposal.docx`);
  } };
  h.context.fetch = async () => {
    if (++gets === 1) throw new TypeError("disconnected");
    return wordResponse();
  };
  await h.ui.exportDoc();
  assert.equal(h.element("#headingExportBtn").textContent, "重试下载 Word");
  await h.ui.exportDoc();
  assert.equal(jobs, 1);
  assert.equal(gets, 2);
  assert.equal(h.created.length, 1);
});

test("legacy export fetches private file with bearer and links only a blob", async () => {
  const h = harness(); exportingProject(h);
  const calls = [];
  const download = `/api/projects/${h.ui.state.project.id}/download`;
  h.ui.setApi(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith("/chat-drafts") ? { items: [] } : { download_url: download };
  });
  await h.ui.exportDoc();
  assert.equal(calls.filter(c => c.url.endsWith("/export")).length, 1);
  assert.equal(h.requests[0].url, download);
  assert.equal(h.requests[0].options.headers.Authorization, "Bearer fake-session-jwt");
  assert.equal(h.element("#exportResult").children[1].href, "blob:preview-1");
  h.ui.clearExportArtifacts();
  assert.deepEqual(h.revoked, ["blob:preview-1"]);
});

test("sending a new instruction revokes the previous Word download link", async () => {
  const h = harness(); exportingProject(h);
  h.ui.setChatRefresh(async () => {});
  h.ui.setLoadProject(async () => {});
  h.ui.setApi(async (url) => url.endsWith("/chat-drafts") ? { items: [] }
    : url.endsWith("/chat") ? { message: "已收到" }
    : { download_url: `/api/projects/${h.ui.state.project.id}/download` });
  await h.ui.exportDoc();
  assert.equal(h.element("#exportResult").children.length, 2);
  await h.ui.sendChat(null, "这次不要写家庭收入");
  assert.deepEqual(h.revoked, ["blob:preview-1"]);
  assert.equal(h.element("#exportResult").children.length, 0);
});

test("an in-flight old export cannot download after a newer instruction", async () => {
  const h = harness(); exportingProject(h);
  h.ui.setChatRefresh(async () => {});
  h.ui.setLoadProject(async () => {});
  let finishExport;
  h.ui.setApi(async (url) => {
    if (url.endsWith("/chat-drafts")) return { items: [] };
    if (url.endsWith("/chat")) return { message: "已收到" };
    return new Promise(resolve => { finishExport = resolve; });
  });
  const exporting = h.ui.exportDoc();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(typeof finishExport, "function");
  await h.ui.sendChat(null, "这次不要写家庭收入");
  finishExport({ download_url: `/api/projects/${h.ui.state.project.id}/download` });
  await exporting;
  assert.equal(h.created.length, 0);
  assert.equal(h.requests.length, 0);
});

test("download rejects foreign destination without sending the token", async () => {
  const h = harness(); exportingProject(h);
  await assert.rejects(h.ui.fetchExportBlob("https://evil.invalid/collect", h.ui.state.project.id), /当前项目/);
  assert.equal(h.requests.length, 0);
});

test("download failure is not reported as a started download", async () => {
  const h = harness(); exportingProject(h);
  h.ui.setApi(async url => url.endsWith("/chat-drafts") ? { items: [] } : {
    download_url: `/api/projects/${h.ui.state.project.id}/download`,
  });
  h.context.fetch = async () => ({ ok: false, json: async () => ({ detail: "登录过期" }) });
  await h.ui.exportDoc();
  assert.match(h.element("#toast").textContent, /登录过期.*重试下载 Word/);
  assert.equal(h.created.length, 0);
  assert.equal(h.element("#exportBtn").disabled, false);
});

test("export flushes the latest editor text with CAS before submitting", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.outline = [{ id: "purpose", title: "立项目的" }];
  const statusPill = Object.assign(new Element("span"), {
    className: "section-status-pill unsaved", textContent: "● 未保存",
  });
  const area = new Element("textarea"); area.value = "最新手写正文";
  area.closest = () => ({ id: "draft-sec-purpose", querySelector: () => statusPill });
  h.setDraftAreas([area]);
  const calls = [];
  h.ui.setApi(async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith("/sections/purpose")) return { material_revision: 4 };
    if (url.endsWith("/chat-drafts")) return { items: [] };
    if (url.endsWith("/export")) return { download_url: `/api/projects/${h.ui.state.project.id}/download` };
    return { ...h.ui.state.project, material_revision: 3 };
  });
  await h.ui.exportDoc();
  const put = calls.find(c => c.options.method === "PUT");
  assert.equal(JSON.parse(put.options.body).content, "最新手写正文");
  assert.equal(JSON.parse(put.options.body).base_content, "已保存正文");
  assert.equal(JSON.parse(put.options.body).expected_revision, 3);
  assert.ok(calls.indexOf(put) < calls.findIndex(c => c.url.endsWith("/export")));
  assert.equal(h.ui.state.project.sections.purpose, "最新手写正文");
  assert.equal(statusPill.className, "section-status-pill saved");
  assert.equal(statusPill.textContent, "✓ 已保存");
  assert.equal(h.element("#draftStatsBadge").textContent, "1/1 章节 · 共 6 字");
});

test("an edit during the export save remains unsaved and blocks the download", async () => {
  const h = harness(); exportingProject(h);
  const statusPill = new Element("span");
  const area = new Element("textarea");
  area.value = "本次提交的正文";
  area.closest = () => ({ id: "draft-sec-purpose", querySelector: () => statusPill });
  h.setDraftAreas([area]);
  const calls = [];
  let saveStarted;
  const saving = new Promise(resolve => { saveStarted = resolve; });
  let finishSave;
  h.ui.setApi(async (url, options = {}) => {
    calls.push(url);
    if (options.method !== "PUT") return { ...h.ui.state.project, material_revision: 3 };
    return new Promise(resolve => {
      finishSave = resolve;
      saveStarted();
    });
  });
  const exporting = h.ui.exportDoc();
  await saving;
  area.value = "保存期间追加的正文";
  finishSave({ material_revision: 4 });
  await exporting;
  assert.equal(h.ui.state.project.sections.purpose, "本次提交的正文");
  assert.equal(area.value, "保存期间追加的正文");
  assert.equal(statusPill.className, "section-status-pill unsaved");
  assert.match(h.element("#toast").textContent, /保存期间正文又有修改/);
  assert.equal(calls.some(url => url.endsWith("/export")), false);
  assert.equal(h.created.length, 0);
});

test("concurrent chapter edit stops export without losing visible input", async () => {
  const h = harness(); exportingProject(h);
  const area = new Element("textarea"); area.value = "我的手写内容";
  area.closest = () => ({ id: "draft-sec-purpose" }); h.setDraftAreas([area]);
  const calls = [];
  h.ui.setApi(async url => {
    calls.push(url);
    return { ...h.ui.state.project, sections: { purpose: "另一页面的内容" } };
  });
  await h.ui.exportDoc();
  assert.equal(calls.length, 1);
  assert.equal(area.value, "我的手写内容");
  assert.match(h.element("#toast").textContent, /未覆盖/);
});

test("chat draft recovery requires explicit selection and never auto-exports", async () => {
  const h = harness(); exportingProject(h);
  const calls = [];
  const preview = { snapshot: "f".repeat(64), notice: "不扣积分", items: [{
    id: "e".repeat(64), title: "<script>不执行</script>", content: "预算为建议，不是事实",
    already_saved: false, conflict: false,
  }] };
  h.ui.setApi(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith("/chat-drafts") ? preview : { notice: "已保存" };
  });
  h.ui.setLoadProject(async () => {});
  await h.ui.showChatDrafts();
  assert.equal(calls.length, 1);
  assert.equal(h.element("#chatDraftDialog").open, true);
  const row = h.element("#chatDraftItems").children[0];
  assert.equal(row.children[0].children[1].textContent, "<script>不执行</script>");
  row.children[2].value = "用户核对后的文字";
  await h.element("#confirmChatDraftsBtn").onclick();
  assert.equal(calls.length, 2);
  const body = JSON.parse(calls[1].options.body);
  assert.equal(body.items[0].content, "用户核对后的文字");
  assert.ok(calls[1].url.endsWith("/chat-drafts/confirm"));
});

test("a newer chat draft replaces existing text only when explicitly selected", async () => {
  const h = harness(); exportingProject(h);
  const calls = [];
  const preview = { snapshot: "f".repeat(64), notice: "选择草稿", items: [{
    id: "e".repeat(64), title: "申请书", content: "新版正文", already_saved: false, conflict: true,
  }] };
  h.ui.setApi(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith("/chat-drafts") ? preview : { notice: "已保存" };
  });
  h.ui.setLoadProject(async () => {});
  await h.ui.showChatDrafts();
  const row = h.element("#chatDraftItems").children[0];
  const checkbox = row.children[0].children[0];
  assert.equal(checkbox.checked, false);
  assert.equal(checkbox.disabled, false);
  assert.match(row.children[1].textContent, /历史版本/);
  checkbox.checked = true;
  await h.element("#confirmChatDraftsBtn").onclick();
  assert.equal(JSON.parse(calls[1].options.body).items[0].replace_existing, true);
});

test("project drafts with unsupported claims are unchecked until the student reviews them", async () => {
  const h = harness(); exportingProject(h);
  const calls = [];
  const preview = { snapshot: "f".repeat(64), notice: "请选择", items: [{
    id: "e".repeat(64), title: "项目简介", content: "市场规模 5000 亿元",
    already_saved: false, conflict: false, requires_review: true, warnings: ["项目数据需要核对。"],
  }] };
  h.ui.setApi(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith("/chat-drafts") ? preview : { notice: "已保存" };
  });
  h.ui.setLoadProject(async () => {});
  await h.ui.showChatDrafts();
  const row = h.element("#chatDraftItems").children[0];
  const checkbox = row.children[0].children[0];
  assert.equal(checkbox.checked, false);
  assert.match(row.children[1].textContent, /核对/);
  await h.element("#confirmChatDraftsBtn").onclick();
  assert.equal(calls.length, 1);
  row.children[2].value = "市场规模待补充。";
  checkbox.checked = true;
  await h.element("#confirmChatDraftsBtn").onclick();
  assert.equal(JSON.parse(calls[1].options.body).items[0].content, "市场规模待补充。");
});

test("section history can be reviewed and placed into the editor without saving automatically", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.project.sections = { purpose: "当前正文" };
  h.ui.state.outline = [{ id: "purpose", title: "申请书" }];
  const calls = [];
  h.ui.setApi(async (url) => { calls.push(url); return { versions: [{ content: "先前正文" }] }; });
  h.ui.renderSections();
  const section = h.element("#draftSections").children[0];
  const historyButton = section.children[0].children[1].children[3];
  const body = section.children[1];
  body.children[3].classList.add("hidden"); // Harness does not derive classList from className.
  await historyButton.click();
  const editor = body.children[1];
  const versionsBox = body.children[3];
  assert.equal(versionsBox.children[0].children[1].textContent, "先前正文");
  versionsBox.children[0].children[2].click();
  assert.equal(editor.value, "先前正文");
  assert.equal(h.ui.state.project.sections.purpose, "当前正文");
  assert.equal(calls.length, 1);
});

function chapterEditor(h) {
  h.ui.state.outline = h.ui.state.project.outline;
  h.ui.renderSections();
  const section = h.element("#draftSections").children[0];
  const editor = section.children[1].children[1];
  const status = section.children[0].children[0].children[3];
  editor.closest = () => section;
  section.querySelector = () => status;
  h.setDraftAreas([editor]);
  return { editor, status, save: section.children[0].children[1].children[2], section,
    type(value) { editor.value = value; editor.listeners.input(); } };
}

test("chapter drafts written by the previous recovery format still open after refresh", () => {
  const session = new Map();
  const h = harness(session); exportingProject(h);
  session.set(`stardust-section-recovery-v1:${h.ui.state.project.id}\0purpose\0local`,
    JSON.stringify({ base: "已保存正文", content: "上一版本工作台保存的草稿" }));
  assert.equal(chapterEditor(h).editor.value, "上一版本工作台保存的草稿");
});

test("reloading a tab restores chapter edits with their exact base, including empty edits", () => {
  const session = new Map();
  const first = harness(session); exportingProject(first);
  const blank = chapterEditor(first);
  blank.type("");
  const reloaded = harness(session); exportingProject(reloaded);
  let requests = 0;
  reloaded.ui.setApi(async () => { requests++; return {}; });
  const recovered = chapterEditor(reloaded);
  assert.equal(recovered.editor.value, "");
  assert.match(recovered.status.textContent, /已恢复.*未保存/);
  assert.equal(requests, 0);
});

test("a chapter recovered after reload still detects a competing server edit", async () => {
  const session = new Map();
  const first = harness(session); exportingProject(first);
  chapterEditor(first).type("刷新前打的草稿");
  const reloaded = harness(session); exportingProject(reloaded);
  reloaded.ui.state.project.sections.purpose = "其他页面的更新";
  reloaded.ui.state.project.material_revision = 3;
  const recovered = chapterEditor(reloaded);
  const conflict = recovered.section.children[1].children.at(-1);
  assert.equal(recovered.editor.value, "刷新前打的草稿");
  assert.equal(conflict.children[1].textContent, "其他页面的更新");
  reloaded.ui.setApi(async (_url, options = {}) => {
    assert.notEqual(options.method, "PUT");
    return reloaded.ui.state.project;
  });
  await assert.rejects(reloaded.ui.flushDraftEdits(), /其他页面/);
});

test("reload after an unacknowledged save preserves later typing against the committed text", async () => {
  const session = new Map();
  const first = harness(session); exportingProject(first);
  const editing = chapterEditor(first);
  editing.type("第一次提交的正文");
  let started;
  const requested = new Promise(resolve => { started = resolve; });
  first.ui.setApi(async (_url, options = {}) => {
    if (options.method !== "PUT") return first.ui.state.project;
    started();
    return new Promise(() => {});
  });
  void editing.save.click();
  await requested;
  editing.type("提交后继续输入的正文");
  const reloaded = harness(session); exportingProject(reloaded);
  reloaded.ui.state.project.sections.purpose = "第一次提交的正文";
  reloaded.ui.state.project.material_revision = 3;
  const recovered = chapterEditor(reloaded);
  assert.equal(recovered.editor.value, "提交后继续输入的正文");
  let written;
  reloaded.ui.setApi(async (url, options = {}) => {
    if (options.method !== "PUT") return reloaded.ui.state.project;
    written = JSON.parse(options.body);
    return { material_revision: 4 };
  });
  await recovered.save.click();
  assert.equal(written.base_content, "第一次提交的正文");
  assert.equal(written.content, "提交后继续输入的正文");
  const afterSaving = harness(session); exportingProject(afterSaving);
  afterSaving.ui.state.project.sections.purpose = "服务器之后的新版本";
  assert.equal(chapterEditor(afterSaving).editor.value, "服务器之后的新版本");
});

test("a reverted draft can be explicitly saved while a previous write remains unconfirmed", async () => {
  const session = new Map();
  const first = harness(session); exportingProject(first);
  const editing = chapterEditor(first);
  editing.type("此前发出的修改");
  first.ui.setApi(async (_url, options = {}) => {
    if (options.method !== "PUT") return first.ui.state.project;
    throw new TypeError("response lost");
  });
  await editing.save.click();
  editing.type("已保存正文");
  const reloaded = harness(session); exportingProject(reloaded);
  const recovered = chapterEditor(reloaded);
  assert.match(recovered.status.textContent, /保存待确认/);
  let written;
  reloaded.ui.setApi(async (_url, options = {}) => {
    if (options.method !== "PUT") return reloaded.ui.state.project;
    written = JSON.parse(options.body);
    return { material_revision: 3 };
  });
  await recovered.save.click();
  assert.equal(written.base_content, "已保存正文");
  assert.equal(written.content, "已保存正文");
  assert.match(recovered.status.textContent, /已保存/);
  assert.equal(session.size, 0);
});

test("browser recovery stays scoped to the authenticated account and removed projects", () => {
  const session = new Map();
  const old = harness(session); exportingProject(old);
  old.ui.state.cloudMode = true;
  old.ui.state.authSession = { user: { id: "owner" }, access_token: "sensitive-credential-token" };
  chapterEditor(old).type("当前账号的未提交正文");
  assert.ok([...session.values()].some(value => value.includes("当前账号的未提交正文")));
  assert.ok([...session.values()].every(value => !value.includes("sensitive-credential-token")));
  const other = harness(session); exportingProject(other);
  other.ui.state.cloudMode = true;
  other.ui.state.authSession = { user: { id: "other" } };
  assert.equal(chapterEditor(other).editor.value, "已保存正文");
  old.ui.closeDeletedProject(old.ui.state.project.id);
  assert.ok([...session.values()].every(value => !value.includes("当前账号的未提交正文")));
});

test("signing out clears chapter recovery and a late save cannot bring it back", async () => {
  const session = new Map([["unrelated", "retained"]]);
  const h = harness(session); exportingProject(h);
  h.ui.state.cloudMode = true;
  h.ui.state.authSession = { user: { id: "previous" }, access_token: "old-token" };
  const view = chapterEditor(h);
  view.type("离开账号前正在保存的正文");
  let respond, onWrite;
  const waiting = new Promise(resolve => { onWrite = resolve; });
  h.ui.setApi(async (_url, options = {}) => {
    if (options.method !== "PUT") return h.ui.state.project;
    onWrite();
    return new Promise(resolve => { respond = resolve; });
  });
  const saving = view.save.click();
  await waiting;
  h.ui.clearInterruptedChatMessages();
  h.ui.state.authSession = { user: { id: "next" } };
  assert.deepEqual([...session], [["unrelated", "retained"]]);
  respond({ material_revision: 3 });
  await saving;
  assert.deepEqual([...session], [["unrelated", "retained"]]);
});

test("a blocked browser storage keeps the chapter in memory and gives the student a save prompt", () => {
  const h = harness(); exportingProject(h);
  const view = chapterEditor(h);
  h.context.sessionStorage.setItem = () => { throw new Error("quota exceeded"); };
  view.type("写在受限环境中的草稿");
  assert.match(h.element("#toast").textContent, /先保存正文再刷新/);
  assert.equal(chapterEditor(h).editor.value, "写在受限环境中的草稿");
});

test("restoring a version first saves the current edit, then uses its revision and exact text", async () => {
  const h = harness(); exportingProject(h);
  const view = chapterEditor(h);
  view.type("需要保留的手稿");
  const requests = [];
  h.context.confirm = () => true;
  h.ui.setApi(async (url, options = {}) => {
    requests.push({ url, options });
    if (url.endsWith("/versions")) return { versions: [{ id: "v1", source: "before_restore", content: "历史稿" }] };
    if (options.method === "PUT") return { material_revision: 3 };
    if (url.endsWith("/restore")) return { restored: true, material_revision: 4 };
    return h.ui.state.project;
  });
  let reload;
  h.ui.setLoadProject(async id => { reload = id; });
  const versions = view.section.children[1].children[3];
  versions.classList.add("hidden");
  await view.section.children[0].children[1].children[3].click();
  const row = versions.children[0];
  assert.equal(row.children[0].textContent, "恢复前的正文");
  await row.children.find(child => child.textContent === "恢复并保存").click();
  assert.deepEqual(requests.filter(r => r.options.method).map(r => r.options.method), ["PUT", "POST"]);
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), { expected_revision: 3, base_content: "需要保留的手稿" });
  assert.equal(reload, h.ui.state.project.id);
});

test("a cleared chapter survives project navigation and detached controls cannot save to another project", async () => {
  const h = harness(); exportingProject(h);
  const a = h.ui.state.project;
  const first = chapterEditor(h);
  first.type("");
  h.ui.state.project = { ...a, id: "second", sections: { purpose: "乙的正文" } };
  assert.equal(chapterEditor(h).editor.value, "乙的正文");
  let writes = 0;
  h.ui.setApi(async () => { writes++; return {}; });
  await first.save.click();
  assert.equal(writes, 0);
  h.ui.state.project = { ...a };
  const restored = chapterEditor(h);
  assert.equal(restored.editor.value, "");
  assert.match(restored.status.textContent, /未保存/);
});

test("a refreshed chapter retains both sides of a conflict until the student chooses", async () => {
  const h = harness(); exportingProject(h);
  chapterEditor(h).type("我的修改");
  h.ui.state.project.sections.purpose = "另一页面的正文";
  h.ui.state.project.material_revision = 5;
  const view = chapterEditor(h);
  assert.equal(view.editor.value, "我的修改");
  const puts = [];
  h.ui.setApi(async (url, options = {}) => {
    if (options.method !== "PUT") return h.ui.state.project;
    puts.push(JSON.parse(options.body));
    return { material_revision: 6 };
  });
  await assert.rejects(h.ui.flushDraftEdits(), /其他页面/);
  assert.equal(puts.length, 0);
  const conflict = view.section.children[1].children.at(-1);
  assert.equal(conflict.children[1].textContent, "另一页面的正文");
  conflict.children.find(child => child.textContent === "保留我的编辑").click();
  await view.save.click();
  assert.deepEqual(puts, [{ content: "我的修改", base_content: "另一页面的正文", expected_revision: 5 }]);
  assert.match(view.status.textContent, /已保存/);
});

test("export recognizes a committed edit after a lost save response without a second write", async () => {
  const h = harness(); exportingProject(h);
  const edit = chapterEditor(h);
  edit.type("已送达的正文");
  const saved = { ...h.ui.state.project, sections: { ...h.ui.state.project.sections } };
  let writes = 0;
  h.ui.setApi(async (url, options = {}) => {
    if (options.method !== "PUT") return saved;
    writes++;
    saved.sections.purpose = JSON.parse(options.body).content;
    saved.material_revision++;
    throw new Error("连接中断");
  });
  await edit.save.click();
  assert.match(edit.status.textContent, /未保存/);
  await h.ui.flushDraftEdits();
  assert.equal(writes, 1);
  assert.equal(h.ui.state.project.sections.purpose, "已送达的正文");
  assert.match(edit.status.textContent, /已保存/);
});

test("an acknowledged section save preserves further typing in a newly rendered editor", async () => {
  const h = harness(); exportingProject(h);
  chapterEditor(h).type("提交的正文");
  let finish, start;
  const writing = new Promise(resolve => { start = resolve; });
  h.ui.setApi(async (url, options = {}) => {
    if (options.method !== "PUT") return h.ui.state.project;
    start();
    return new Promise(resolve => { finish = resolve; });
  });
  const saving = h.ui.flushDraftEdits();
  await writing;
  const rerendered = chapterEditor(h);
  rerendered.type("保存期间接着写");
  finish({ material_revision: 3 });
  await assert.rejects(saving, /保存期间/);
  assert.equal(h.ui.state.project.sections.purpose, "提交的正文");
  const restored = chapterEditor(h);
  assert.equal(restored.editor.value, "保存期间接着写");
  assert.match(restored.status.textContent, /未保存/);
});

test("accepting the latest server chapter updates the local project without writing", async () => {
  const h = harness(); exportingProject(h);
  const edit = chapterEditor(h);
  edit.type("我的修改");
  h.ui.setApi(async () => ({ ...h.ui.state.project, material_revision: 3, sections: { purpose: "" } }));
  await assert.rejects(h.ui.flushDraftEdits(), /其他页面/);
  const conflict = edit.section.children[1].children.at(-1);
  conflict.children.find(child => child.textContent === "使用最新正文").click();
  assert.equal(edit.editor.value, "");
  assert.equal(h.ui.state.project.sections.purpose, "");
  h.ui.setApi(() => assert.fail("accepting server content needs no write"));
  await h.ui.flushDraftEdits();
});

test("generation without a confirmed write leaves the old chapter and a recoverable candidate", async () => {
  const h = harness(); exportingProject(h);
  const edit = chapterEditor(h);
  let writes = 0;
  h.ui.setApi(async (url, options = {}) => {
    if (url.endsWith("/generate")) return { content: "模型生成的候选稿" };
    if (options.method !== "PUT") return h.ui.state.project;
    assert.deepEqual(JSON.parse(options.body), {
      content: "模型生成的候选稿", base_content: "已保存正文", expected_revision: 2,
    });
    writes++;
    throw new Error("网络中断");
  });
  await edit.section.children[0].children[1].children[1].click();
  assert.equal(writes, 1);
  assert.equal(h.ui.state.project.sections.purpose, "已保存正文");
  const restored = chapterEditor(h);
  assert.equal(restored.editor.value, "已保存正文");
  assert.equal(restored.section.children[1].children[4].children[0].children[1].textContent, "模型生成的候选稿");
});

test("a generated chapter acknowledged by the server becomes the base for later manual edits", async () => {
  const h = harness(); exportingProject(h);
  const edit = chapterEditor(h);
  h.ui.setApi(async () => ({ content: "模型正文", material_applied: true, material_revision: 3 }));
  await edit.section.children[0].children[1].children[1].click();
  edit.type("手写定稿");
  const puts = [];
  h.ui.setApi(async (url, options = {}) => {
    if (options.method !== "PUT") return h.ui.state.project;
    puts.push(JSON.parse(options.body));
    return { material_revision: 4 };
  });
  await edit.save.click();
  assert.deepEqual(puts, [{ content: "手写定稿", base_content: "模型正文", expected_revision: 3 }]);
});

test("a failed section save keeps the persisted state and the student's edit", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.project.sections = { purpose: "之前保存的正文" };
  h.ui.state.outline = [{ id: "purpose", title: "申请书" }];
  h.ui.setApi(async () => { throw new Error("网络中断"); });
  h.ui.renderSections();
  const section = h.element("#draftSections").children[0];
  const save = section.children[0].children[1].children[2];
  const editor = section.children[1].children[1];
  editor.value = "尚未保存的新正文";
  await save.click();
  assert.equal(h.ui.state.project.sections.purpose, "之前保存的正文");
  assert.equal(editor.value, "尚未保存的新正文");
});

test("section generation saves an existing unsaved edit before calling the model", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.outline = [{ id: "purpose", title: "申请书" }];
  h.ui.renderSections();
  const section = h.element("#draftSections").children[0];
  const editor = section.children[1].children[1];
  editor.value = "生成前的手写修改";
  editor.closest = () => ({ id: "draft-sec-purpose", querySelector: () => section.children[0].children[0].children[3] });
  h.setDraftAreas([editor]);
  const calls = [];
  h.ui.setApi(async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith("/sections/purpose/generate")) return { content: "新草稿", material_applied: true, material_revision: 4 };
    if (options.method === "PUT") return { material_revision: 3 };
    return { ...h.ui.state.project, material_revision: 2 };
  });
  await section.children[0].children[1].children[1].click();
  const savedAt = calls.findIndex(call => call.options.method === "PUT");
  const generatedAt = calls.findIndex(call => call.url.endsWith("/generate"));
  assert.ok(savedAt >= 0 && savedAt < generatedAt);
  assert.equal(JSON.parse(calls[savedAt].options.body).content, "生成前的手写修改");
});

test("section generation retains edits made during the model request", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.outline = [{ id: "purpose", title: "申请书" }];
  h.ui.renderSections();
  const section = h.element("#draftSections").children[0];
  const editor = section.children[1].children[1];
  let finish; const calls = [];
  h.ui.setApi((url, options) => { calls.push({ url, options }); return new Promise(resolve => { finish = resolve; }); });
  const running = section.children[0].children[1].children[1].click();
  await new Promise(resolve => setImmediate(resolve));
  editor.value = "学生刚写的内容";
  finish({ content: "服务器保存的生成正文", material_applied: true, material_revision: 5 });
  await running;
  assert.equal(editor.value, "学生刚写的内容");
  assert.equal(h.ui.state.project.sections.purpose, "服务器保存的生成正文");
  assert.equal(h.ui.state.project.material_revision, 5);
  assert.equal(calls.length, 1);
  assert.match(h.element("#toast").textContent, /新编辑已保留/);
});

test("section polling that remains active ends with a visible status and never resubmits", async () => {
  const h = harness(); exportingProject(h);
  h.context.setTimeout = (fn, delay) => { if (delay < 15000) fn(); return 1; };
  h.ui.state.outline = [{ id: "purpose", title: "申请书" }];
  h.ui.renderSections();
  const section = h.element("#draftSections").children[0];
  const calls = [];
  h.ui.setApi(async (url, options = {}) => {
    calls.push({ url, options });
    return options.method === "POST" ? { status: "processing", job_id: "job" } : { status: "running" };
  });
  const button = section.children[0].children[1].children[1];
  await button.click();
  assert.equal(calls.filter(call => call.options.method === "POST").length, 1);
  assert.equal(calls.length, 46);
  assert.equal(button.disabled, false);
  assert.match(h.element("#toast").textContent, /仍在后台处理/);
});

test("a silent section request unlocks with uncertain result, without retry", async () => {
  const h = harness(); exportingProject(h);
  const timers = new Map(); let id = 0;
  h.context.setTimeout = (callback, delay) => { timers.set(++id, { callback, delay }); return id; };
  h.context.clearTimeout = key => timers.delete(key);
  h.ui.state.outline = [{ id: "purpose", title: "申请书" }];
  h.ui.renderSections();
  const section = h.element("#draftSections").children[0];
  const button = section.children[0].children[1].children[1];
  let calls = 0;
  h.ui.setApi(() => { calls++; return new Promise(() => {}); });
  const running = button.click();
  await new Promise(resolve => setImmediate(resolve));
  const timeout = [...timers.values()].find(item => item.delay === 180000);
  assert.ok(timeout);
  timeout.callback();
  await running;
  assert.equal(calls, 1);
  assert.equal(button.disabled, false);
  assert.match(h.element("#toast").textContent, /可能仍在后台处理/);
  assert.equal(section.children[0].children[0].children[3].textContent, "● 结果待确认");
});

test("version write failure displays the returned draft without applying or regenerating it", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.project.sections = { purpose: "原正文" };
  h.ui.state.outline = [{ id: "purpose", title: "申请书" }];
  const calls = [];
  h.ui.setApi(async (url, options) => {
    calls.push({ url, options });
    return { conflict_status: "user_edit_preserved", result_save_uncertain: true,
      content: "生成的候选正文", notice: "保存状态不明" };
  });
  h.ui.renderSections();
  const section = h.element("#draftSections").children[0];
  await section.children[0].children[1].children[1].click();
  const body = section.children[1];
  assert.equal(body.children[4].children[0].children[1].textContent, "生成的候选正文");
  assert.equal(h.ui.state.project.sections.purpose, "原正文");
  assert.equal(calls.length, 1);
  body.children[4].children[0].children[3].click();
  assert.equal(body.children[1].value, "生成的候选正文");
  assert.equal(h.ui.state.project.sections.purpose, "原正文");
});

test("an uncertain generated draft stays scoped to its original project", async () => {
  const h = harness(); exportingProject(h);
  const original = h.ui.state.project;
  original.sections = { purpose: "原正文" };
  h.ui.state.outline = [{ id: "purpose", title: "申请书" }];
  let complete;
  h.ui.setApi(() => new Promise(resolve => { complete = resolve; }));
  h.ui.renderSections();
  const first = h.element("#draftSections").children[0];
  const generating = first.children[0].children[1].children[1].click();
  await new Promise(resolve => setImmediate(resolve));
  h.ui.state.project = { id: "second", outline: [{ id: "purpose", title: "新项目正文" }],
    sections: { purpose: "另一个项目" } };
  h.ui.renderSections();
  complete({ conflict_status: "user_edit_preserved", result_save_uncertain: true, content: "项目一私有草稿" });
  await generating;
  assert.equal(h.element("#draftSections").children[0].children[1].children[4].children.length, 0);
  h.ui.state.project = original;
  h.ui.renderSections();
  const pending = h.element("#draftSections").children[0].children[1].children[4];
  assert.equal(pending.children[0].children[1].textContent, "项目一私有草稿");
  assert.equal(original.sections.purpose, "原正文");
});

test("an old account's late draft is not retained after account switch", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.authSession = { user: { id: "old-account" } };
  h.ui.state.outline = [{ id: "purpose", title: "申请书" }];
  let complete;
  h.ui.setApi(() => new Promise(resolve => { complete = resolve; }));
  h.ui.renderSections();
  const generating = h.element("#draftSections").children[0].children[0].children[1].children[1].click();
  await new Promise(resolve => setImmediate(resolve));
  h.ui.state.authSession = { user: { id: "new-account" } };
  complete({ conflict_status: "user_edit_preserved", result_save_uncertain: true, content: "旧账号草稿" });
  await generating;
  h.ui.renderSections();
  assert.equal(h.element("#draftSections").children[0].children[1].children[4].children.length, 0);
});

test("export opens recovery instead of exporting a cover-only document", async () => {
  const h = harness(); exportingProject(h);
  h.ui.state.project.outline = []; h.ui.state.project.sections = {};
  const calls = [];
  h.ui.setApi(async url => {
    calls.push(url);
    return { snapshot: "f".repeat(64), notice: "请确认", items: [{
      id: "e".repeat(64), title: "项目目的", content: "聊天正文", already_saved: false, conflict: false,
    }] };
  });
  await h.ui.exportDoc();
  assert.equal(calls.length, 1);
  assert.equal(h.element("#chatDraftDialog").open, true);
  assert.equal(h.element("#confirmChatDraftsBtn").textContent, "确认保存并继续导出");
});

test("export presents a conflicting chat draft before using saved text", async () => {
  const h = harness(); exportingProject(h);
  const calls = [];
  const pid = h.ui.state.project.id;
  h.ui.setApi(async (url) => {
    calls.push(url);
    if (url.endsWith("/chat-drafts")) return { snapshot: "f".repeat(64), notice: "请确认", items: [{
      id: "e".repeat(64), title: "项目目的", content: "较新的对话草稿", already_saved: false, conflict: true,
    }] };
    if (url.endsWith("/export")) return { download_url: `/api/projects/${pid}/download` };
    throw Error(`unexpected ${url}`);
  });
  await h.ui.exportDoc();
  assert.equal(h.element("#chatDraftDialog").open, true);
  assert.equal(calls.filter(url => url.endsWith("/export")).length, 0);
  const root = h.element("#chatDraftItems");
  assert.equal(root.children[0].children[0].children[0].checked, false);
  const keep = root.children.find(el => el.textContent === "保持已保存正文并继续导出");
  assert.ok(keep);
  await keep.click();
  assert.equal(calls.filter(url => url.endsWith("/chat-drafts")).length, 1);
  assert.equal(calls.filter(url => url.endsWith("/export")).length, 1);
  assert.equal(h.created.length, 1);
});

test("export continues after saving selected drafts without re-prompting for unchecked ones", async () => {
  const h = harness(); exportingProject(h);
  const pid = h.ui.state.project.id;
  const calls = [];
  h.ui.setApi(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("/chat-drafts")) return { snapshot: "f".repeat(64), notice: "请确认", items: [
      { id: "e".repeat(64), title: "项目目的", content: "新版目的", already_saved: false, conflict: true },
      { id: "d".repeat(64), title: "研究背景", content: "待审核背景", already_saved: false, conflict: false, requires_review: true },
    ] };
    if (url.endsWith("/chat-drafts/confirm")) return { notice: "已保存" };
    if (url.endsWith("/export")) return { download_url: `/api/projects/${pid}/download` };
    throw Error(`unexpected ${url}`);
  });
  h.ui.setLoadProject(async () => {});
  await h.ui.exportDoc();
  h.element("#chatDraftItems").children[0].children[0].children[0].checked = true;
  await h.element("#confirmChatDraftsBtn").onclick();
  const confirm = calls.find(c => c.url.endsWith("/chat-drafts/confirm"));
  assert.equal(JSON.parse(confirm.options.body).items.length, 1);
  assert.equal(JSON.parse(confirm.options.body).items[0].replace_existing, true);
  assert.equal(calls.filter(c => c.url.endsWith("/chat-drafts")).length, 1);
  assert.equal(calls.filter(c => c.url.endsWith("/export")).length, 1);
  assert.equal(h.element("#chatDraftDialog").open, false);
});

test("persistent results show success, per-file errors, and preview actions", () => {
  const h = harness();
  h.ui.renderUploadResult([
    { document: doc },
    { document: { original_name: "<img onerror=alert(1)>.txt" }, error: "文件过大" },
  ]);
  const box = h.element("#uploadResult");
  assert.equal(box.children[0].textContent, "上传结果：1 个成功，1 个处理失败");
  assert.match(box.children[1].children[0].textContent, /上传成功/);
  assert.equal(box.children[1].children[1].textContent, "预览");
  assert.match(box.children[2].children[0].textContent, /<img onerror=alert\(1\)>.txt — 上传失败：文件过大/);
  assert.equal(box.classList.contains("error"), true);
  assert.equal(box.classList.contains("hidden"), false);
});

test("empty results never claim upload success", () => {
  const h = harness();
  h.ui.renderUploadResult([]);
  assert.match(h.element("#uploadResult").children[0].textContent, /未收到上传结果/);
  assert.equal(h.element("#uploadResult").classList.contains("error"), true);
});

test("all material categories display the upload ceiling, and an oversized selection sends no requests", async () => {
  const h = harness();
  for (const kind of ["source", "template", "photo"]) {
    h.ui.setUploadKind(kind);
    assert.match(h.element("#uploadHint").textContent, /单个文件最大 50 MB/);
  }
  h.ui.state.project = { id: "a".repeat(32) };
  let calls = 0;
  h.ui.setApi(async () => { calls++; });
  await h.ui.uploadFiles([{ name: "too-large.pdf", size: 50_000_001 }]);
  assert.equal(calls, 0);
  assert.equal(h.requests.length, 0);
  assert.match(h.element("#uploadResult").children[1].children[0].textContent, /单个文件最大 50 MB/);
  assert.equal(h.ui.oversizedUploadError([{ name: "exact.pdf", size: 50_000_000 }]), "");
});

function cloudHarness() {
  const h = harness();
  h.context.URL = URL;
  h.context.crypto = require("node:crypto").webcrypto;
  h.ui.state.cloudMode = true;
  h.ui.state.project = { id: "a".repeat(32) };
  h.ui.state.authConfig = { storage_upload_endpoint: "/uploads/resumable", max_upload_bytes: 50_000_000 };
  const session = { user: { id: "owner" }, access_token: "user-jwt" };
  h.ui.state.authSession = session;
  h.ui.state.authClient = { auth: { getSession: async () => ({ data: { session } }) } };
  h.uploads = [];
  h.context.window.tus = { Upload: class {
    constructor(file, options) { this.file = file; this.options = options; h.uploads.push(this); }
    async findPreviousUploads() { return []; }
    async start() {
      this.request = { getURL: () => "/uploads/resumable", setHeader: (k, v) => { this[k] = v; } };
      try {
        await this.options.onBeforeRequest(this.request);
        this.options.onProgress(this.file.size, this.file.size);
        this.options.onSuccess();
      } catch (error) { this.options.onError(error); }
    }
  } };
  h.context.fetch = async () => ({
    ok: true, headers: new Headers({ "Content-Type": "application/json" }),
  });
  return h;
}

test("a >4.5 MB file uses 6 MiB TUS chunks, then small registration and processing requests", async () => {
  const h = cloudHarness();
  const file = new File([new Uint8Array(7 * 1024 * 1024)], "report.pdf", { lastModified: 1 });
  const calls = [];
  h.ui.setApi(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("/from-storage")) return { documents: [{ document: { id: "b".repeat(32), original_name: file.name } }] };
    return { documents: [{ document: { id: "b".repeat(32), original_name: file.name, confirmed: true } }] };
  });
  const result = await h.ui.uploadFilesToCloud([file], "source");
  assert.equal(result.documents.length, 1);
  assert.equal(h.uploads[0].options.chunkSize, 6 * 1024 * 1024);
  assert.equal(h.uploads[0].options.uploadDataDuringCreation, false);
  assert.equal(h.uploads[0].Authorization, "Bearer user-jwt");
  assert.equal(JSON.parse(calls[0].options.body).process_now, false);
  assert.ok(calls[0].options.body.length < 1000);
  assert.match(calls[1].url, /\/documents\/b+\/process$/);
});

test("parse timeouts retain the acknowledged document and never delete uploaded bytes", async () => {
  const h = cloudHarness();
  h.ui.setApi(async (url) => {
    if (url.endsWith("/from-storage")) return { documents: [{ document: { id: "b".repeat(32), original_name: "report.pdf" } }] };
    throw new Error("HTTP 504");
  });
  const result = await h.ui.uploadFilesToCloud([new File(["pdf"], "report.pdf")], "source");
  assert.equal(result.documents[0].document.id, "b".repeat(32));
  assert.match(result.documents[0].error, /原文件已保存/);
});

test("registration failure reuses the completed object rather than uploading/deleting it again", async () => {
  const h = cloudHarness();
  const file = new File(["test"], "report.pdf", { lastModified: 3 });
  h.ui.setApi(async () => { throw new Error("HTTP 504"); });
  await h.ui.uploadFilesToCloud([file], "source");
  await h.ui.uploadFilesToCloud([file], "source");
  assert.equal(h.uploads.length, 1);
});

test("resume fingerprint is scoped to user/project/kind and samples file contents", async () => {
  const h = cloudHarness();
  const file = new File(["first"], "report.pdf", { lastModified: 3 });
  const first = await h.ui.cloudUploadIntent(file, "source", "a".repeat(32));
  assert.equal((await h.ui.cloudUploadIntent(file, "source", "a".repeat(32))).documentId, first.documentId);
  assert.notEqual((await h.ui.cloudUploadIntent(file, "template", "a".repeat(32))).documentId, first.documentId);
  assert.notEqual((await h.ui.cloudUploadIntent(new File(["other"], "report.pdf", { lastModified: 3 }), "source", "a".repeat(32))).documentId, first.documentId);
});

test("50 MB ceiling and disabled Worker prevent doomed Vercel uploads before sending bytes", async () => {
  const h = cloudHarness();
  const tooLarge = { name: "huge.pdf", size: 50_000_001 };
  let result = await h.ui.uploadFilesToCloud([tooLarge], "source");
  assert.match(result.documents[0].error, /50 MB/);
  h.ui.state.authConfig.storage_upload_endpoint = "";
  result = await h.ui.uploadFilesToCloud([{ name: "large.pdf", size: 5_000_000 }], "source");
  assert.match(result.documents[0].error, /大文件通道尚未启用/);
  assert.equal(h.uploads.length, 0);
});

test("a poisoned continuation URL cannot receive the user's JWT", async () => {
  const h = cloudHarness();
  const file = new File(["test"], "report.pdf");
  const intent = await h.ui.cloudUploadIntent(file, "source", "a".repeat(32));
  await h.ui.uploadResumable(file, intent, "application/pdf", "/uploads/resumable");
  let sent = false;
  await assert.rejects(h.uploads[0].options.onBeforeRequest({
    getURL: () => "https://evil.test/uploads/resumable/stolen",
    setHeader: () => { sent = true; },
  }), /续传地址无效/);
  assert.equal(sent, false);
});

test("vendored TUS client has a pinned checksum and loads before app.js", () => {
  const vendor = resolve(__dirname, "../../server/static/vendor");
  const lock = JSON.parse(readFileSync(resolve(vendor, "tus-js-client.lock.json"), "utf8"));
  const sha = require("node:crypto").createHash("sha256").update(readFileSync(resolve(vendor, lock.bundle))).digest("hex");
  assert.equal(sha, lock.bundle_sha256);
  const html = readFileSync(resolve(__dirname, "../../server/static/index.html"), "utf8");
  assert.ok(html.indexOf(lock.bundle) < html.indexOf('src="/static/app.js"'));
});

test("PDF preview fetches with JWT and embeds a PDF blob, not a private endpoint", async () => {
  const h = harness();
  h.ui.state.authSession = { access_token: "fake-session-jwt" };
  await h.ui.previewDocument(doc, true);
  assert.equal(h.requests[0].options.headers.get("Authorization"), "Bearer fake-session-jwt");
  assert.match(h.requests[0].url, /\/api\/documents\/a+\/file$/);
  const body = h.element("#documentPreviewBody");
  assert.equal(body.children[0].tagName, "iframe");
  assert.match(body.children[0].src, /^blob:/);
  assert.equal(h.created[0].type, "application/pdf");
  assert.equal(body.children[2].textContent, doc.extracted_text);
  assert.match(h.element("#documentPreviewStatus").textContent, /上传成功.*待核对/);
  assert.equal(body.attributes["aria-busy"], "false");
  assert.equal(h.ui.state.documents.length, 0); // Preview is not confirmation/indexing.
});

test("images uploaded as ordinary materials also have an authenticated image preview", async () => {
  const h = harness();
  await h.ui.previewDocument({ ...doc, original_name: "扫描件.JPG" });
  assert.equal(h.element("#documentPreviewBody").children[0].tagName, "img");
  assert.equal(h.created[0].type, "image/jpeg");
});

test("Word preview clearly labels text-only layout and uses safe textContent", async () => {
  const h = harness();
  await h.ui.previewDocument({
    ...doc, original_name: "模板.docx", extracted_text: "<script>alert(1)</script>",
  });
  const body = h.element("#documentPreviewBody");
  assert.match(body.children[0].textContent, /不保留原版式/);
  assert.equal(body.children[1].textContent, "<script>alert(1)</script>");
  assert.equal(h.requests.length, 0);
});

test("authorization failure falls back to extracted text and does not claim preview loaded", async () => {
  const h = harness();
  h.context.fetch = async () => ({ ok: false, status: 401 });
  await h.ui.previewDocument(doc);
  const body = h.element("#documentPreviewBody");
  assert.match(body.children[0].textContent, /登录已过期/);
  assert.equal(body.children[1].textContent, doc.extracted_text);
  assert.equal(h.created.length, 0);
  assert.equal(body.attributes["aria-busy"], "false");
});

test("recognition failure is distinct from storage failure", async () => {
  const h = harness();
  const broken = { ...doc, original_name: "损坏.docx", extracted_text: "", extraction_status: "error",
    extraction_warning: "无法识别文件" };
  await h.ui.previewDocument(broken, true);
  assert.match(h.element("#documentPreviewStatus").textContent, /已保存.*识别失败/);
  assert.doesNotMatch(h.element("#documentPreviewStatus").textContent, /上传成功/);
  h.ui.renderUploadResult([{ document: broken, error: "无法识别文件" }]);
  assert.match(h.element("#uploadResult").children[1].children[0].textContent, /已保存，识别失败/);
});

test("switching and closing previews revoke blob URLs", async () => {
  const h = harness();
  await h.ui.previewDocument(doc);
  await h.ui.previewDocument({ ...doc, original_name: "文字.txt" });
  assert.deepEqual(h.revoked, ["blob:preview-1"]);
  await h.ui.previewDocument(doc);
  h.ui.clearDocumentPreview();
  assert.deepEqual(h.revoked, ["blob:preview-1", "blob:preview-2"]);
  assert.equal(h.element("#documentPreviewBody").children.length, 0);
});

test("a late file response cannot replace the next document's preview", async () => {
  const h = harness();
  let finish;
  h.context.fetch = () => new Promise((resolve) => { finish = resolve; });
  const pending = h.ui.previewDocument(doc);
  await h.ui.previewDocument({ ...doc, original_name: "另一份.txt", extracted_text: "新的文字" });
  finish({ ok: true, blob: async () => new Blob(["old"]) });
  await pending;
  assert.equal(h.element("#documentPreviewBody").children[1].textContent, "新的文字");
  assert.equal(h.created.length, 0);
});

test("material cards expose preview and explicit uploaded state without unauthenticated images", () => {
  const h = harness();
  h.ui.state.documents = [{ ...doc, kind: "photo" }];
  h.ui.renderDocuments();
  const card = h.element("#documentsList").children[0];
  assert.equal(card.dataset.documentId, doc.id);
  assert.match(card.children[0].children[2].textContent, /已上传 · 待核对/);
  assert.equal(card.children.at(-1).children[0].textContent, "预览文件");
  assert.equal(card.children.some((child) => child.tagName === "img"), false);
});

test("successful upload shows persistent result and automatically opens preview", async () => {
  const h = harness();
  h.context.FormData = FormData;
  h.context.URLSearchParams = URLSearchParams;
  h.ui.state.project = { id: "project-1" };
  const textDoc = { ...doc, original_name: "记录.txt" };
  h.ui.setApi(async () => ({ documents: [{ document: textDoc }] }));
  h.ui.setLoadProject(async () => {});
  await h.ui.uploadFiles([new File(["内容"], "记录.txt")]);
  assert.match(h.element("#uploadResult").children[0].textContent, /1 个成功/);
  assert.equal(h.element("#documentPreviewDialog").open, true);
  assert.match(h.element("#documentPreviewStatus").textContent, /上传成功/);
  assert.equal(h.ui.state.uploading, false);
  assert.equal(h.element("#chooseFileBtn").disabled, false);
  assert.equal(h.element("#fileInput").value, "");
});

test("request failure persists its cause and restores upload controls", async () => {
  const h = harness();
  h.context.FormData = FormData;
  h.context.URLSearchParams = URLSearchParams;
  h.ui.state.project = { id: "project-1" };
  h.ui.setApi(async () => { throw new Error("文件超过 20 MB"); });
  await h.ui.uploadFiles([new File(["内容"], "记录.txt")]);
  assert.match(h.element("#uploadResult").children[1].children[0].textContent, /上传失败：文件超过 20 MB/);
  assert.equal(h.ui.state.uploading, false);
  assert.equal(h.element("#chatAttachBtn").disabled, false);
  assert.equal(h.element("#documentPreviewDialog").open, false);
});

test("registration refuses an uninitialized auth client without reading null.auth", async () => {
  const h = harness();
  h.ui.state.authMode = "signup";
  await h.ui.submitAuthForm({ preventDefault() {} });
  assert.match(h.element("#authFeedback").textContent, /正在初始化.*尚未发送/);
  assert.doesNotMatch(h.element("#authFeedback").textContent, /Cannot read|TypeError/);
  assert.equal(h.element("#authSubmitBtn").disabled, true);
  assert.equal(h.element("#authModeToggle").disabled, true);
});

test("missing SDK shows recovery and cannot enable registration after an error", async () => {
  const h = harness();
  await h.ui.initializeCloudAuth({ cloud_mode: true });
  assert.equal(h.ui.state.authClient, null);
  assert.equal(h.ui.state.authStatus, "error");
  assert.equal(h.element("#retryAuthBtn").classList.contains("hidden"), false);
  assert.equal(h.element("#authSubmitBtn").disabled, true);
  await h.ui.submitAuthForm({ preventDefault() {} });
  assert.match(h.element("#authFeedback").textContent, /尚未就绪.*重新连接/);
  assert.equal(h.element("#authSubmitBtn").disabled, true);
});

test("constructor or session failure leaves auth disabled instead of leaking technical errors", async () => {
  for (const createClient of [
    () => { throw new TypeError("bad client configuration"); },
    () => ({ auth: { getSession: async () => ({ error: new Error("session unavailable") }) } }),
  ]) {
    const h = harness();
    h.context.window.supabase = { createClient };
    await h.ui.initializeCloudAuth({ cloud_mode: true });
    assert.equal(h.ui.state.authClient, null);
    assert.equal(h.ui.state.authStatus, "error");
    assert.equal(h.ui.state.authInitializing, false);
    assert.equal(h.element("#authSubmitBtn").disabled, true);
    assert.match(h.element("#authFeedback").textContent, /无法初始化登录服务/);
  }
});

test("initialization holds submit disabled until session recovery completes", async () => {
  const h = harness();
  let finish;
  let clientsCreated = 0;
  const client = { auth: {
    getSession: () => new Promise((resolve) => { finish = resolve; }),
    onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }),
  } };
  h.context.window.supabase = { createClient: () => { clientsCreated++; return client; } };
  const config = { cloud_mode: true };
  const initializing = h.ui.initializeCloudAuth(config);
  assert.equal(h.element("#authSubmitBtn").disabled, true);
  assert.equal(h.ui.state.authClient, null);
  await h.ui.initializeCloudAuth(config);
  assert.equal(clientsCreated, 1); // Concurrent retries must not create duplicate clients.
  finish({ data: { session: null }, error: null });
  await initializing;
  assert.equal(h.ui.state.authStatus, "ready");
  assert.equal(h.ui.state.authClient, client);
  assert.equal(h.element("#authSubmitBtn").disabled, false);
  assert.equal(h.element("#authModeToggle").disabled, false);
  assert.equal(h.element("#retryAuthBtn").classList.contains("hidden"), true);
});

test("signup sends one request only after explicit submission and preserves email verification flow", async () => {
  const h = harness();
  const calls = [];
  let finish;
  h.ui.state.authStatus = "ready";
  h.ui.state.authMode = "signup";
  h.ui.state.authClient = { auth: { signUp: (input) => {
    calls.push(input);
    return new Promise((resolve) => { finish = resolve; });
  } } };
  h.element("#authEmail").value = "TEST@GMAIL.COM";
  h.element("#authPassword").value = "fake-password-for-unit-test";
  const submitting = h.ui.submitAuthForm({ preventDefault() {} });
  assert.equal(h.element("#authSubmitBtn").disabled, true);
  assert.equal(h.element("#authModeToggle").disabled, true);
  await h.ui.submitAuthForm({ preventDefault() {} });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].email, "test@gmail.com");
  assert.equal(calls[0].options.emailRedirectTo, "https://workbench.example.test");
  finish({ data: { session: null }, error: null });
  await submitting;
  assert.match(h.element("#authFeedback").textContent, /账号已创建.*验证后登录/);
  assert.equal(h.element("#authSubmitBtn").disabled, false);
  assert.equal(h.ui.state.authBusy, false);
});

test("network failures render actionable text and never automatically retry a signup", async () => {
  const h = harness();
  let requests = 0;
  h.ui.state.authStatus = "ready";
  h.ui.state.authMode = "signup";
  h.ui.state.authClient = { auth: { signUp: async () => {
    requests++;
    throw new TypeError("Failed to fetch");
  } } };
  h.element("#authEmail").value = "test@gmail.com";
  h.element("#authPassword").value = "fake-password-for-unit-test";
  await h.ui.submitAuthForm({ preventDefault() {} });
  assert.match(h.element("#authFeedback").textContent, /无法连接登录服务/);
  assert.equal(requests, 1);
  assert.equal(h.element("#authSubmitBtn").disabled, false);
});

test("vendored Supabase SDK is pinned, checksummed, and served from this origin before app.js", () => {
  const { createHash } = require("node:crypto");
  const vendor = resolve(__dirname, "../../server/static/vendor");
  const lock = JSON.parse(readFileSync(resolve(vendor, "supabase-js.lock.json")));
  assert.equal(lock.version, "2.118.0");
  const bundle = readFileSync(resolve(vendor, lock.bundle));
  assert.equal(createHash("sha256").update(bundle).digest("hex"), lock.bundle_sha256);
  const html = readFileSync(resolve(__dirname, "../../server/static/index.html"), "utf8");
  const sdkScript = `/static/vendor/${lock.bundle}`;
  assert.ok(html.includes(sdkScript));
  assert.ok(html.indexOf(sdkScript) < html.indexOf('/static/app.js'));
  assert.doesNotMatch(html, /https:\/\/cdn\.jsdelivr\.net\/npm\/@supabase/);
  assert.match(html, /id="authSubmitBtn"[^>]*disabled/);
  assert.match(html, /id="retryAuthBtn"/);
  // Evaluate the real bundle without constructing a client, sending credentials,
  // making any network call, or creating an account.
  const storage = new Map();
  const session = new Map();
  const context = vm.createContext({ console, URL, TextEncoder, TextDecoder });
  vm.runInContext(bundle.toString("utf8"), context);
  assert.equal(typeof context.supabase.createClient, "function");
});

test("chat allows only one pending request and clears its timer after a visible answer", async () => {
  const h = harness();
  h.ui.state.project = { id: "project-one" };
  h.ui.setChatRefresh(async () => {});
  let finish;
  let requests = 0;
  h.ui.setApi(async () => {
    requests++;
    return new Promise((resolve) => { finish = resolve; });
  });
  const first = h.ui.sendChat(null, "hello");
  assert.equal(h.ui.state.chatSending, true);
  assert.equal(h.element("#sendBtn").disabled, true);
  assert.equal(h.timers.size, 1);
  await h.ui.sendChat(null, "duplicate");
  assert.equal(requests, 1);
  finish({ message: "有正文的答复" });
  await first;
  assert.equal(h.ui.state.chatSending, false);
  assert.equal(h.element("#sendBtn").disabled, false);
  assert.equal(h.timers.size, 0);
  assert.equal(h.ui.state.messages[1].content, "有正文的答复");
});

test("blank success payload is an explicit error, never a successful empty answer", async () => {
  const h = harness();
  h.ui.state.project = { id: "project-one" };
  h.ui.setChatRefresh(async () => {});
  h.ui.setLoadProject(async () => {});
  h.ui.setApi(async () => ({ message: "" }));
  await h.ui.sendChat(null, "test");
  assert.equal(h.ui.state.messages.length, 0);
  const bubbles = h.element("#chatMessages").children.flatMap((item) => item.children);
  assert.ok(bubbles.some((item) => item.innerHTML?.includes("未返回可用正文")));
  assert.equal(h.ui.state.chatSending, false);
  assert.equal(h.timers.size, 0);
});

test("a late reply does not enter another project's conversation", async () => {
  const h = harness();
  h.ui.state.project = { id: "project-one" };
  h.ui.setChatRefresh(async () => {});
  let finish;
  h.ui.setApi(() => new Promise((resolve) => { finish = resolve; }));
  const pending = h.ui.sendChat(null, "test");
  h.ui.state.project = { id: "project-two" };
  finish({ message: "answer for project one" });
  await pending;
  assert.equal(h.ui.state.messages.length, 0);
  assert.equal(h.timers.size, 0);
  assert.equal(h.ui.state.chatSending, false);
});

test("public cache recovers from corrupt, expired or unavailable storage", () => {
  const h = harness();
  const list = [{ id: "innovation-research", name: "创新训练" }];
  h.ui.writePublicCache("skills", list);
  assert.equal(h.ui.readPublicCache("skills")[0].id, "innovation-research");
  h.context.localStorage.setItem("stardust-public-v1-skills", "{bad-json");
  assert.equal(h.ui.readPublicCache("skills"), null);
  h.context.localStorage.setItem("stardust-public-v1-skills", JSON.stringify({version:1,savedAt:0,data:list}));
  assert.equal(h.ui.readPublicCache("skills"), null);
  h.context.localStorage.getItem = () => { throw Error("denied"); };
  assert.equal(h.ui.readPublicCache("skills"), null);
});

test("all SDK auth and storage operations use same-origin proxy", async () => {
  const h = harness();
  h.context.URL = URL;
  const fetcher = h.ui.sameOriginAuthFetch("https://project.supabase.co");
  await fetcher("https://project.supabase.co/auth/v1/token?grant_type=refresh_token", {method:"POST"});
  await fetcher("https://project.supabase.co/auth/v1/user", {method:"GET"});
  await fetcher("https://project.supabase.co/auth/v1/logout", {method:"POST"});
  assert.equal(h.requests[0].url, "/api/auth/proxy/token?grant_type=refresh_token");
  assert.equal(h.requests[1].url, "/api/auth/proxy/user");
  assert.equal(h.requests[2].url, "/api/auth/proxy/logout");
  await fetcher("https://project.supabase.co/storage/v1/object/stardust-project-files/test");
  assert.equal(h.requests[3].url, "/api/storage/proxy/object/stardust-project-files/test");
});

test("cloud keys are memory-only; cloud deletion and account switch cannot restore stale key", async () => {
  const h = harness();
  h.ui.state.cloudMode = true;
  h.ui.state.authSession = {user:{id:"A"}};
  const key = "cloud-private-key";
  h.ui.storeLlmConfig({provider:"openai_compatible",api_key:key});
  assert.equal(h.context.sessionStorage.getItem("innovation-proposal-agent-llm-config"), null);
  assert.equal(h.context.localStorage.getItem("innovation-proposal-agent-llm-config"), null);
  let finish;
  h.ui.setApi(() => new Promise(resolve => {finish=resolve;}));
  const loading = h.ui.loadCloudModelConfig();
  h.ui.state.authSession = {user:{id:"B"}};
  h.ui.state.llmConfig = {provider:"preset",api_key:""};
  finish({config:{provider:"openai_compatible",api_key:key}});
  await loading;
  assert.equal(h.ui.state.llmConfig.api_key, "");
  h.ui.setApi(async () => ({config:null}));
  await h.ui.loadCloudModelConfig();
  assert.equal(h.ui.state.llmConfig.provider, "preset");
});

test("login initialization no longer awaits the public catalog or pricing", () => {
  const source = readFileSync(resolve(__dirname, "../../server/static/app.js"), "utf8");
  const init = source.slice(source.indexOf("  async function init()"));
  assert.match(init, /void loadSkills\(\)/);
  assert.match(init, /void loadPresetModelsAndPlans\(\)/);
  assert.doesNotMatch(init, /await Promise\.all/);
});

test("failed cloud save preserves previous model and never claims success", async () => {
  const h = harness();
  h.ui.state.cloudMode = true;
  h.ui.state.authSession = {user:{id:"A"}};
  h.ui.state.llmConfig = {provider:"preset",model:"workbench-bundle",api_key:""};
  h.element('input[name="llmProvider"]:checked').value = "openai_compatible";
  for (const [id,value] of Object.entries({customModelBase:"https://api.example.test/v1",customModelName:"model",
    customModelKey:"new-private-key",customImageModel:"",customImageBase:"",customImageKey:""})) {
    h.element("#" + id).value = value;
  }
  h.ui.setApi(async () => { throw Error("upstream down"); });
  await h.ui.saveSelectedLlmConfig();
  assert.equal(h.ui.state.llmConfig.provider, "preset");
  assert.match(h.element("#modelConfigFeedback").textContent, /保存失败/);
  assert.equal(h.element("#saveModelConfigBtn").disabled, false);
  const calls = [];
  h.ui.setApi(async (url,opts) => {calls.push({url,opts});return {saved:true};});
  await h.ui.saveSelectedLlmConfig();
  assert.equal(calls[0].url, "/api/user/llm-config");
  assert.equal(h.ui.state.llmConfig.api_key, "new-private-key");
  assert.equal(h.context.sessionStorage.getItem("innovation-proposal-agent-llm-config"), null);
});

test("SSE chat cannot bill the default model while cloud BYOK restore is pending or failed", async () => {
  const h = harness();
  h.ui.state.cloudMode = true;
  h.ui.state.project = {id:"project-A"};
  h.element("#chatInput").value = "不要提前扣费";
  for (const status of ["loading", "error"]) {
    h.ui.state.modelConfigSyncStatus = status;
    await h.ui.sendChat({preventDefault(){}});
    assert.equal(h.requests.length, 0);
    assert.equal(h.element("#chatInput").value, "不要提前扣费");
  }
});

function realChatStream(h, text) {
  h.ui.state.project = { id: "project-one" };
  h.ui.setChatRefresh(async () => {});
  h.ui.setLoadProject(async () => {});
  let read = false;
  let cancelled = 0;
  h.context.fetch = async () => ({
    ok: true,
    body: { getReader: () => ({
      read: async () => {
        if (read) return { done: true };
        read = true;
        return { done: false, value: new TextEncoder().encode(text) };
      },
      cancel: async () => { cancelled++; },
      releaseLock() {},
    }) },
  });
  return () => cancelled;
}

test("real SSE EOF without a done event never turns partial output into success", async () => {
  const h = harness();
  const cancelled = realChatStream(h, 'event: delta\ndata: {"text":"不完整的半句"}\n\n');
  await h.ui.sendChat(null, "测试");
  assert.equal(h.ui.state.messages.length, 0);
  const bubbles = h.element("#chatMessages").children.flatMap((item) => item.children);
  assert.ok(bubbles.some((item) => item.innerHTML?.includes("未完成")));
  assert.equal(h.ui.state.chatSending, false);
  assert.equal(h.timers.size, 0);
  assert.equal(cancelled(), 1);
});

test("real SSE done event is required and keeps the complete server response", async () => {
  const h = harness();
  const cancelled = realChatStream(h,
    'event: delta\ndata: {"text":"部分"}\n\n' +
    'event: done\ndata: {"message":"完整回复","tool_calls":[]}\n\n');
  await h.ui.sendChat(null, "测试");
  assert.equal(h.ui.state.messages[1].content, "完整回复");
  assert.equal(h.ui.state.chatSending, false);
  assert.equal(cancelled(), 1);
});

test("a tool call clears provisional streamed text before the final reply", async () => {
  const h = harness();
  realChatStream(h,
    'event: delta\ndata: {"text":"正在查询"}\n\n' +
    'event: reset\ndata: {}\n\n' +
    'event: status\ndata: {"message":"正在检索"}\n\n' +
    'event: delta\ndata: {"text":"最终答复"}\n\n' +
    'event: done\ndata: {"message":"最终答复","tool_calls":[]}\n\n');
  await h.ui.sendChat(null, "测试");
  assert.equal(h.ui.state.messages[1].content, "最终答复");
  const rows = h.element("#chatMessages").children;
  assert.ok(rows.some((row) => row.removed && row.children.some((child) => child.textContent === "正在查询")));
  assert.ok(rows.some((row) => !row.removed && row.children.some((child) => child.innerHTML?.includes("最终答复"))));
});

test("switching projects during real SSE cancels the reader and clears the wait timer", async () => {
  const h = harness();
  h.ui.state.project = { id: "project-one" };
  h.ui.setChatRefresh(async () => {});
  h.ui.setLoadProject(async () => {});
  let cancelled = false;
  h.context.fetch = async () => ({ ok: true, body: { getReader: () => ({
    read: async () => {
      h.ui.state.project = { id: "project-two" };
      return { done: false, value: new TextEncoder().encode('event: delta\ndata: {"text":"old"}\n\n') };
    },
    cancel: async () => { cancelled = true; },
    releaseLock() {},
  }) } });
  await h.ui.sendChat(null, "测试");
  assert.equal(h.ui.state.messages.length, 0);
  assert.equal(h.timers.size, 0);
  assert.equal(h.ui.state.chatSending, false);
  assert.equal(cancelled, true);
});

function factsWorkbench(h, project) {
  h.ui.state.project = project;
  const college = Object.assign(h.element('[data-fact="college"]'), { dataset: { fact: "college" } });
  const title = Object.assign(h.element('[data-fact="project_name"]'), { dataset: { fact: "project_name" } });
  const school = Object.assign(h.element('[data-project="school"]'), { dataset: { project: "school" } });
  h.setFactFields([college, title]);
  const query = h.context.document.querySelectorAll;
  h.context.document.querySelectorAll = selector => selector === "[data-project]" ? [school] : query(selector);
  h.ui.renderFacts();
  return { college, title, school, type(field, value) { field.value = value; h.ui.recordFactInput(field); } };
}

test("fact drafts and explicit deletions survive project refresh and a new browser page instance", () => {
  const session = new Map();
  const project = { id: "p", school: "服务器学校", state: { confirmed_facts: { college: "服务器学院" } } };
  const original = harness(session);
  const form = factsWorkbench(original, project);
  form.type(form.college, "");
  form.type(form.school, "我的学校");
  original.ui.recordFactInput({ dataset: { fact: "custom_field_1" }, value: "新增但未确认的事实" });
  original.ui.renderFacts();
  assert.equal(form.college.value, "");
  assert.equal(form.school.value, "我的学校");
  assert.equal(original.element("#dynamicFactsGrid").children[0].children[1].value, "新增但未确认的事实");

  const reopened = harness(session);
  const recovered = factsWorkbench(reopened, { ...project });
  assert.equal(recovered.college.value, "");
  assert.equal(recovered.school.value, "我的学校");
  assert.match(reopened.element("#factFormStatus").textContent, /已恢复未保存/);
  assert.equal(reopened.requests.length, 0);
  assert.equal(project.state.confirmed_facts.college, "服务器学院");
});

test("another page changing the same fact requires an explicit decision", async () => {
  const session = new Map();
  const prior = { id: "p", material_revision: 1, state: { confirmed_facts: { college: "旧学院" } } };
  const original = harness(session);
  const edit = factsWorkbench(original, prior);
  edit.type(edit.college, "我的学院");
  const h = harness(session);
  const remote = { ...prior, material_revision: 2, state: { confirmed_facts: { college: "服务器的新学院" } } };
  const form = factsWorkbench(h, remote);
  assert.equal(form.college.value, "我的学院");
  assert.equal(h.element("#saveFactsBtn").disabled, true);
  const conflict = h.element("#factConflictList").children[0];
  assert.match(conflict.children[1].textContent, /服务器的新学院/);
  let writes = [];
  h.ui.setApi(async (_url, options) => { writes.push(JSON.parse(options.body)); return {}; });
  h.ui.setLoadProject(async () => {});
  await h.ui.saveFacts();
  assert.equal(writes.length, 0);
  conflict.children[2].click();
  await h.ui.saveFacts();
  assert.deepEqual(writes, [{ facts: { college: "我的学院" }, project: {}, expected_revision: 2,
    base: { facts: { college: "服务器的新学院" }, project: {} } }]);
});

test("a field conflict is surfaced even when a partial update already advanced the project revision", async () => {
  const h = harness();
  const partial = { id: "p", material_revision: 2, state: { confirmed_facts: { college: "旧学院" } } };
  const form = factsWorkbench(h, partial);
  form.type(form.college, "本页改动");
  const attempts = [];
  h.ui.setApi(async (_url, options) => {
    attempts.push(JSON.parse(options.body));
    throw Object.assign(new Error("部分字段已有更新"), { status: 409 });
  });
  h.ui.setLoadProject(async () => {
    h.ui.state.project = { ...partial, state: { confirmed_facts: { college: "另一页的新学院" } } };
    h.ui.renderFacts();
  });
  await h.ui.saveFacts();
  assert.equal(attempts.length, 1);
  assert.equal(attempts[0].expected_revision, 2);
  assert.equal(attempts[0].base.facts.college, "旧学院");
  assert.equal(form.college.value, "本页改动");
  assert.equal(h.element("#saveFactsBtn").disabled, true);
  assert.match(h.element("#factConflictList").children[0].children[1].textContent, /另一页的新学院/);
});

test("an unrelated project revision lets the fact form retry only the original save", async () => {
  const h = harness();
  const initial = { id: "p", material_revision: 1, school: "旧学校", state: { confirmed_facts: { college: "旧学院" } } };
  const form = factsWorkbench(h, initial);
  form.type(form.college, "我的学院");
  const attempts = [];
  const latest = { ...initial, material_revision: 2, school: "另一页更新的学校" };
  h.ui.setLoadProject(async () => {
    h.ui.state.project = latest;
    h.ui.renderFacts();
  });
  h.ui.setApi(async (_url, options) => {
    attempts.push(JSON.parse(options.body));
    if (attempts.length === 1) throw Object.assign(new Error("项目已变化"), { status: 409 });
    return {};
  });
  await h.ui.saveFacts();
  assert.deepEqual(attempts, [
    { facts: { college: "我的学院" }, project: {}, expected_revision: 1, base: { facts: { college: "旧学院" }, project: {} } },
    { facts: { college: "我的学院" }, project: {}, expected_revision: 2, base: { facts: { college: "旧学院" }, project: {} } },
  ]);
  assert.equal(form.school.value, "另一页更新的学校");
});

test("refresh after an unacknowledged fact save preserves edits made during the request", async () => {
  const session = new Map();
  const h = harness(session);
  const initial = { id: "p", material_revision: 1, state: { confirmed_facts: { college: "旧学院" } } };
  const form = factsWorkbench(h, initial);
  form.type(form.college, "已提交给服务器");
  let start;
  const requestStarted = new Promise(resolve => { start = resolve; });
  h.ui.setApi(async () => { start(); return new Promise(() => {}); });
  void h.ui.saveFacts();
  await requestStarted;
  form.type(form.college, "保存期间继续编辑");
  const reopened = harness(session);
  const latest = { ...initial, material_revision: 2, state: { confirmed_facts: { college: "已提交给服务器" } } };
  const recovered = factsWorkbench(reopened, latest);
  assert.equal(recovered.college.value, "保存期间继续编辑");
  assert.equal(reopened.element("#saveFactsBtn").disabled, false);
  let payload;
  reopened.ui.setApi(async (_url, options) => { payload = JSON.parse(options.body); return {}; });
  reopened.ui.setLoadProject(async () => {});
  await reopened.ui.saveFacts();
  assert.deepEqual(payload, { facts: { college: "保存期间继续编辑" }, project: {}, expected_revision: 2,
    base: { facts: { college: "已提交给服务器" }, project: {} } });
});

test("explicitly reverting an unconfirmed edit sends a fenced fact save", async () => {
  const session = new Map();
  const h = harness(session);
  const project = { id: "p", material_revision: 1, state: { confirmed_facts: { college: "旧学院" } } };
  const original = factsWorkbench(h, project);
  original.type(original.college, "发出的修改");
  h.ui.setApi(async () => { throw new TypeError("network failure"); });
  await h.ui.saveFacts();
  original.type(original.college, "旧学院");
  const reopened = harness(session);
  factsWorkbench(reopened, project);
  assert.match(reopened.element("#factFormStatus").textContent, /上一次保存待确认/);
  let sent;
  reopened.ui.setApi(async (_url, options) => { sent = JSON.parse(options.body); return {}; });
  reopened.ui.setLoadProject(async () => {});
  await reopened.ui.saveFacts();
  assert.deepEqual(sent, { facts: { college: "旧学院" }, project: {}, expected_revision: 1, supersedes_pending_save: true,
    base: { facts: { college: "旧学院" }, project: {} } });
  assert.equal(session.size, 0);
});

test("renaming a project via its fact respects an independently changed project title", async () => {
  const session = new Map();
  const h = harness(session);
  const initial = { id: "p", title: "旧标题", state: { confirmed_facts: { project_name: "旧名称" } } };
  const fields = factsWorkbench(h, initial);
  fields.type(fields.title, "我正在写的新名称");
  const reopened = harness(session);
  factsWorkbench(reopened, { ...initial, title: "通过项目设置修改的标题" });
  assert.equal(reopened.element("#saveFactsBtn").disabled, true);
  assert.match(reopened.element("#factConflictList").children[0].children[1].textContent, /通过项目设置修改的标题/);
});

test("fact recovery stays under the owning account and deletion clears it", () => {
  const session = new Map([["other-feature", "unchanged"]]);
  const first = harness(session);
  first.ui.state.cloudMode = true;
  first.ui.state.authSession = { user: { id: "first" }, access_token: "first-token" };
  const project = { id: "p", state: { confirmed_facts: { college: "已确认" } } };
  const editing = factsWorkbench(first, project);
  editing.type(editing.college, "账号专属未保存信息");
  const second = harness(session);
  second.ui.state.cloudMode = true;
  second.ui.state.authSession = { user: { id: "second" } };
  const reopened = factsWorkbench(second, project);
  assert.equal(reopened.college.value, "已确认");
  assert.ok([...session.values()].every(value => !value.includes("first-token")));
  first.ui.closeDeletedProject(project.id);
  assert.deepEqual([...session], [["other-feature", "unchanged"]]);
});

test("saving the fact form sends one write to the originating project during navigation", async () => {
  const h = harness();
  h.ui.state.project = { id: "alpha", material_revision: 7 };
  const writes = [];
  const reloads = [];
  const selectors = h.context.document.querySelectorAll;
  h.context.document.querySelectorAll = sel => sel === "[data-project]"
    ? [{ dataset: { project: "school" }, value: "甲大学" }] : selectors(sel);
  h.setFactFields([{ dataset: { fact: "college" }, value: "甲学院" }]);
  h.ui.setApi(async (url, options) => {
    writes.push([url, JSON.parse(options.body)]);
    h.ui.state.project = { id: "beta" };
    return {};
  });
  h.ui.setLoadProject(async id => reloads.push(id));
  await h.ui.saveFacts();
  assert.deepEqual(writes, [["/api/projects/alpha/facts/form", {
    facts: { college: "甲学院" }, project: { school: "甲大学" }, expected_revision: 7,
    base: { facts: { college: "" }, project: { school: "" } },
  }]]);
  assert.deepEqual(reloads, []);
  assert.equal(h.element("#factsEditor").classList.contains("hidden"), false);
});

for (const kind of ["facts", "paper", "official"]) {
  test(`a ${kind} result and its confirmation stay in the originating project`, async () => {
    const h = harness();
    const projects = [{ id: "alpha" }, { id: "beta" }];
    h.ui.state.project = projects[0];
    h.element("#paperQuery").value = "资料";
    h.element("#officialUrl").value = "https://example.edu.cn/notice";
    const panel = h.element(kind === "facts" ? "#factCandidates" : "#sourceResults");
    const requests = [];
    const result = { candidates: [{ key: "college", value: "甲学院", source_quote: "本人就读甲学院" }],
      results: [{ title: "甲项目参考", authors: [] }], url: "https://example.edu.cn/notice", title: "甲校通知" };
    let finish;
    h.ui.setApi(url => {
      requests.push(url);
      return new Promise(resolve => { finish = () => resolve(result); });
    });
    const open = () => kind === "facts" ? h.ui.proposeFacts() :
      kind === "paper" ? h.ui.searchSources("paper") : h.ui.previewOfficialUrl();
    const first = open();
    finish();
    await first;
    const oldButton = panel.children.at(-1).children.at(-1);
    const second = open();
    h.ui.state.project = projects[1];
    const marker = new Element();
    panel.replaceChildren(marker);
    finish();
    await second;
    assert.deepEqual(panel.children, [marker]);
    // Even a retained card cannot confirm data into another project.
    oldButton.click();
    assert.equal(requests.length, 2);
  });
}

test("a delayed leader photo cannot replace another project's empty preview", async () => {
  const h = harness();
  h.ui.state.project = { id: "alpha", state: { leader_photo: "old" } };
  let finish;
  h.context.fetch = () => new Promise(resolve => {
    finish = () => resolve({ ok: true, blob: async () => new Blob(["old photo"]) });
  });
  const pending = h.ui.renderLeaderPhoto();
  h.ui.state.project = { id: "beta", state: {} };
  await h.ui.renderLeaderPhoto();
  finish();
  await pending;
  assert.equal(h.element("#leaderPhotoPreview").children[0].textContent, "暂无照片");
  assert.equal(h.created.length, 0);
});

test("recovered material corrections require review before overwriting a newer version", async () => {
  const session = new Map();
  const original = harness(session);
  original.ui.state.project = { id: "alpha" };
  original.ui.state.documents = [{ ...doc, review_revision: 3 }];
  original.ui.renderDocuments();
  const originalCard = original.element("#documentsList").children[0];
  const originalText = originalCard.children.find(child => child.tagName === "textarea");
  originalText.value = "本页还未提交的校对文稿";
  originalText.listeners.input();

  const h = harness(session);
  h.ui.state.project = { id: "alpha" };
  h.ui.state.documents = [{ ...doc, review_revision: 4, confirmed: true, extracted_text: "服务器上的新版校对" }];
  h.ui.renderDocuments();
  const card = h.element("#documentsList").children[0];
  assert.equal(card.children.find(child => child.tagName === "textarea").value, "本页还未提交的校对文稿");
  const comparison = card.children.find(child => child.className === "section-versions");
  assert.equal(comparison.children[1].textContent, "服务器上的新版校对");
  const submit = card.children.at(-1).children.find(child => child.textContent === "确认修改并重新索引");
  const attempts = [];
  h.ui.setApi(async (url, options) => {
    attempts.push([url, JSON.parse(options.body)]);
    return { document: { review_revision: 5 } };
  });
  h.ui.setLoadProject(async () => {});
  await submit.click();
  assert.equal(attempts.length, 0);
  comparison.children[2].click();
  await submit.click();
  assert.deepEqual(attempts, [[`/api/documents/${doc.id}/confirm`, {
    confirmed: true, corrected_text: "本页还未提交的校对文稿", expected_revision: 4,
  }]]);
  assert.equal(session.size, 0);
});

test("removing a material refreshes the project where the card was rendered", async () => {
  const h = harness();
  h.context.confirm = () => true;
  h.ui.state.project = { id: "alpha" };
  h.ui.state.documents = [doc];
  const calls = [];
  h.ui.setApi(async (url, options) => { calls.push([options.method, url]); return {}; });
  h.ui.setLoadProject(async id => calls.push(["refresh", id]));
  h.ui.renderDocuments();
  const remove = h.element("#documentsList").children[0].children.at(-1).children.at(-1);
  await remove.click();
  assert.deepEqual(calls, [["DELETE", `/api/documents/${doc.id}`], ["refresh", "alpha"]]);
});

test("clearing a confirmed fact sends a deletion while untouched evidence stays out of the form", async () => {
  const h = harness();
  h.ui.state.project = { id: "one", material_revision: 1, state: { confirmed_facts: {
    budget: "三千元", college: "甲学院", method: "  调研\r\n复核  ",
  } } };
  h.setFactFields([
    { dataset: { fact: "budget" }, value: "  " },
    { dataset: { fact: "college" }, value: "甲学院" },
    { dataset: { fact: "method" }, value: "  调研\n复核  " },
  ]);
  let form;
  h.ui.setApi(async (_url, options) => { form = JSON.parse(options.body); return {}; });
  h.ui.setLoadProject(async () => {});
  await h.ui.saveFacts();
  assert.deepEqual(form.facts, { budget: "" });
});

test("a completed upload does not display the old file or send chat after navigating during reload", async () => {
  const h = harness();
  h.context.FormData = FormData;
  h.context.URLSearchParams = URLSearchParams;
  h.ui.state.project = { id: "one" };
  const paths = [];
  h.ui.setApi(async url => { paths.push(url); return { documents: [{ document: doc }] }; });
  h.ui.setLoadProject(async () => { h.ui.state.project = { id: "two" }; });
  await h.ui.uploadFiles([new File(["1"], "第一项目.pdf")], "source", true);
  assert.equal(h.element("#documentPreviewDialog").open, false);
  assert.equal(h.element("#uploadResult").children.length, 0);
  assert.equal(paths.length, 1);
});

test("a project switch suppresses pending cloud upload progress", async () => {
  const h = cloudHarness();
  h.context.FormData = FormData;
  h.context.URLSearchParams = URLSearchParams;
  const placeholder = "新项目正在打开";
  h.context.fetch = async () => {
    h.ui.state.project = { id: "another" };
    h.element("#uploadResult").textContent = placeholder;
    return { ok: true, headers: new Headers({ "Content-Type": "application/json" }) };
  };
  h.ui.setApi(async () => ({ documents: [{ document: doc }] }));
  await h.ui.uploadFiles([new File(["abc"], "毕业材料.pdf")]);
  assert.equal(h.element("#uploadResult").textContent, placeholder);
});

test("a small direct upload retains its object identity after an unacknowledged registration", async () => {
  const h = cloudHarness();
  h.ui.state.authConfig.storage_upload_endpoint = "";
  const uploads = [];
  h.ui.state.authClient.storage = { from: () => ({ upload: async path => { uploads.push(path); return {}; } }) };
  const registrations = [];
  h.ui.setApi(async (url, options) => {
    if (url.endsWith("/from-storage")) {
      const id = JSON.parse(options.body).document_id;
      registrations.push(id);
      if (registrations.length === 1) throw new Error("响应超时");
      return { documents: [{ document: { id } }] };
    }
    return { documents: [{ document: { id: registrations.at(-1) } }] };
  });
  const file = new File(["附件"], "甲项目.txt", { lastModified: 9 });
  const first = await h.ui.uploadFilesToCloud([file], "source");
  assert.match(first.documents[0].error, /响应超时/);
  const second = await h.ui.uploadFilesToCloud([file], "source");
  assert.equal(second.documents[0].document.id, registrations[0]);
  assert.equal(uploads.length, 1);
  assert.deepEqual(registrations, [registrations[0], registrations[0]]);
});

test("two attachments with identical metadata and edges but different middle bytes resume independently", async () => {
  const h = cloudHarness();
  const edge = new Uint8Array(65536);
  const file = middle => new File([edge, middle, edge], "附件.pdf", { lastModified: 7 });
  const intent = value => h.ui.cloudUploadIntent(file(value), "source", h.ui.state.project.id);
  const first = await intent("A");
  assert.notEqual((await intent("B")).documentId, first.documentId);
  assert.equal((await intent("A")).documentId, first.documentId);
});

test("changing account during the first upload stops registration and later files", async () => {
  const h = cloudHarness();
  h.ui.state.authConfig.storage_upload_endpoint = "";
  let uploads = 0;
  h.ui.state.authClient.storage = { from: () => ({ upload: async () => {
    uploads++;
    h.ui.state.authSession = { user: { id: "another" }, access_token: "another-session" };
    return {};
  } }) };
  const requests = [];
  h.ui.setApi(async url => { requests.push(url); return { documents: [{ document: doc }] }; });
  const result = await h.ui.uploadFilesToCloud([new File(["a"], "a.txt"), new File(["b"], "b.txt")], "source");
  assert.equal(uploads, 1);
  assert.equal(requests.length, 0);
  assert.ok(result.documents.every(item => item.error));
});

for (const sameFile of [true, false]) {
  test(`direct upload verifies a conflicting object's bytes before registration (match=${sameFile})`, async () => {
    const h = cloudHarness();
    h.ui.state.authConfig.storage_upload_endpoint = "";
    const file = new File(["abc"], "相同文件名.txt");
    h.ui.state.authClient.storage = { from: () => ({
      upload: async () => ({ error: Object.assign(new Error("已存在"), { code: "KeyAlreadyExists" }) }),
      download: async () => ({ data: new Blob([sameFile ? "abc" : "xyz"]) }),
    }) };
    const calls = [];
    h.ui.setApi(async url => { calls.push(url); return { documents: [{ document: doc }] }; });
    const result = await h.ui.uploadFilesToCloud([file], "source");
    assert.equal(calls.length, sameFile ? 2 : 0);
    assert.equal(Boolean(result.documents[0].error), !sameFile);
  });
}

test("deleting the project being opened cancels its in-flight detail response", async () => {
  const h = harness();
  h.ui.state.project = { id: "old" };
  let finish;
  h.ui.setApi(() => new Promise(resolve => { finish = resolve; }));
  const loading = h.ui.loadProject("deleted");
  h.ui.closeDeletedProject("deleted");
  finish({ id: "deleted", title: "已删除", state: {} });
  await loading;
  assert.equal(h.ui.state.project.id, "old");
});

test("late list results cannot restore a deleted sidebar entry or discard a typed filter", async () => {
  const h = harness();
  const finish = [];
  h.ui.setApi(() => new Promise(resolve => finish.push(resolve)));
  const first = h.ui.refreshProjects();
  const second = h.ui.refreshProjects();
  h.element("#projectSearchInput").value = "甲";
  const visible = { id: "a", title: "甲项目" };
  const filtered = { id: "b", title: "乙项目" };
  finish[1]({ projects: [visible, filtered] });
  await second;
  finish[0]({ projects: [{ id: "gone", title: "已删除" }] });
  await first;
  assert.deepEqual(Array.from(h.ui.state.projectsList, p => p.id), ["a", "b"]);
  assert.deepEqual(h.element("#projectList").children.map(el => el.dataset.id), ["a"]);
});

test("the initial project list is fetched once and manual navigation takes precedence", async () => {
  const h = harness();
  h.context.localStorage.setItem("stardust_onboarding_dismissed", "1");
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const calls = [];
  let listRequests = 0;
  h.ui.setApi(async url => {
    calls.push(url);
    if (url === "/api/projects") {
      listRequests++;
      if (listRequests === 1) await held;
      return { projects: [{ id: "automatic", title: "自动恢复项目" }] };
    }
    if (url === "/api/projects/manual") return { id: "manual", title: "手动选择的项目", state: {} };
    return {};
  });
  const starting = h.ui.startApplication();
  await new Promise(resolve => setImmediate(resolve));
  await h.ui.loadProject("manual");
  release();
  await starting;
  assert.equal(calls.includes("/api/projects/automatic"), false);
  assert.equal(h.ui.state.project?.id, "manual");
});

for (const transientFailure of [false, true]) {
  test(`startup restores the selected project and retries the list only on failure (failure=${transientFailure})`, async () => {
    const h = harness();
    h.context.localStorage.setItem("stardust_onboarding_dismissed", "1");
    h.context.localStorage.setItem("stardust-agent-active-project-id", "saved");
    let lists = 0;
    const opened = [];
    h.ui.setApi(async url => {
      if (url !== "/api/projects") return {};
      if (++lists === 1 && transientFailure) throw new Error("短暂断网");
      return { projects: [{ id: "first", title: "首个" }, { id: "saved", title: "上次的项目" }] };
    });
    h.ui.setLoadProject(async (id, options) => opened.push([id, options?.refreshList]));
    await h.ui.startApplication();
    assert.deepEqual(opened, [["saved", false]]);
    assert.equal(lists, transientFailure ? 2 : 1);
  });
}
