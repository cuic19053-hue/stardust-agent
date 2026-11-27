const assert = require("node:assert/strict");
const { createHash, webcrypto } = require("node:crypto");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

test("material generation stays on the old chain until its worker is ready", () => {
  const context = vm.createContext({ window: {} });
  const source = readFileSync(resolve(__dirname, "../../server/static/material.js"), "utf8")
    .replace("window.MaterialPilot =", `
      globalThis.configure = (config, id, value) => { host = config; projectId = id; data = value; };
      window.MaterialPilot =`);
  vm.runInContext(source, context);
  const state = { project: { id: "one", material: { enabled: true } } };
  context.configure({ state }, "one", { worker_ready: false });
  assert.equal(context.window.MaterialPilot.active(), false);
  context.configure({ state }, "one", { worker_ready: true });
  assert.equal(context.window.MaterialPilot.active(), true);
  state.project = { id: "two", material: { enabled: true } };
  assert.equal(context.window.MaterialPilot.active(), false);
  state.project = { id: "one", material: { enabled: false } };
  assert.equal(context.window.MaterialPilot.active(), false);
});


function workspace(shared = new Map(), owner = "local") {
  class Element {
    children = [];
    classList = { toggle() {} };
    listeners = {};
    constructor(tag) { this.tag = tag; }
    get value() { return this.valueSet ?? (this.tag === "select" ? this.children[0]?.value ?? "" : ""); }
    set value(value) { this.valueSet = value; }
    get options() { return this.tag === "select" ? this.children : undefined; }
    addEventListener(event, listener) { this.listeners[event] = listener; }
    async decode() {}
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
  }
  const panel = new Element();
  const state = { project: null, outline: [] };
  const requests = [], toasts = [];
  const context = vm.createContext({
    window: {}, document: {
      querySelector: () => panel, createElement: tag => new Element(tag),
    }, URL: { createObjectURL: () => "blob:figure", revokeObjectURL() {} },
    AbortController, Blob, setTimeout, clearTimeout, confirm: () => true,
    crypto: { randomUUID: () => "abcdef00-abcd-abcd-abcd-abcdef000000", subtle: webcrypto.subtle },
    sessionStorage: {
      getItem: key => shared.get(key) ?? null, setItem: (key, value) => shared.set(key, value),
      removeItem: key => shared.delete(key), get length() { return shared.size; },
      key: i => [...shared.keys()][i] ?? null,
    },
  });
  vm.runInContext(readFileSync(resolve(__dirname, "../../server/static/draft-recovery.js"), "utf8"), context);
  vm.runInContext(readFileSync(resolve(__dirname, "../../server/static/material.js"), "utf8"), context);
  const pilot = context.window.MaterialPilot;
  pilot.configureRecovery(() => owner);
  const config = { state, toast: text => toasts.push(text),
    api: (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })),
  };
  function mount(id, revision, ready) {
    state.project = { id, material_revision: revision, material: { enabled: true, worker_ready: ready } };
    return pilot.mount(config);
  }
  function reply(request, revision, text, jobs = [], ready = true, lists = {}) {
    request.resolve({
      context: { revision, stale: {}, requirements: { priority: [], conflicts: [] } },
      worker_ready: ready, requirements: [], plans: [],
      jobs: jobs.length ? jobs : [{ kind: "section", target: text, status: "running" }],
      ...lists,
    });
  }
  function find(value, node = panel, exact = false) {
    if (exact ? node.textContent === value : node.textContent?.includes(value)) return node;
    for (const child of node.children) {
      const match = find(value, child, exact);
      if (match) return match;
    }
  }
  const field = label => find(label, undefined, true).children[0];
  return { pilot, config, state, requests, mount, reply, find, field, toasts, context };
}

test("an older task refresh at the same version cannot rewind the panel or advance the editor version", async () => {
  const h = workspace();
  const starting = h.mount("one", 5);
  const newer = h.pilot.refresh();
  h.reply(h.requests[1], 7, "最新任务");
  await newer;
  h.reply(h.requests[0], 7, "先前的任务", [], false);
  await starting;
  assert.ok(h.find("最新任务"));
  assert.equal(h.find("先前的任务"), undefined);
  assert.equal(h.pilot.active(), true);
  assert.equal(h.state.project.material_revision, 5);
  assert.equal(h.state.project.material.revision, undefined);

  h.state.project.material_revision = 8;
  const afterWrite = h.pilot.refresh();
  h.reply(h.requests[2], 7, "写入前的任务");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.requests.length, 4);
  h.reply(h.requests[3], 8, "写入后的任务");
  await afterWrite;
  assert.ok(h.find("写入后的任务"));
});

test("returning to a project ignores responses and errors from earlier workspace mounts", async () => {
  const h = workspace();
  const first = h.mount("one", 2);
  const second = h.mount("two", 9);
  assert.ok(h.find("正在读取材料任务"));
  const reopened = h.mount("one", 3);
  h.reply(h.requests[2], 3, "当前项目任务");
  await reopened;
  h.reply(h.requests[0], 15, "前一次打开的任务", [], false);
  h.requests[1].reject(new Error("其他项目返回的错误"));
  await Promise.all([first, second]);
  assert.equal(h.state.project.material_revision, 3);
  assert.equal(h.pilot.active(), true);
  assert.ok(h.find("当前项目任务"));
  assert.equal(h.find("前一次打开的任务"), undefined);
  assert.equal(h.find("其他项目返回的错误"), undefined);
  assert.deepEqual(h.toasts, []);
});

test("an old task control cannot apply a result after saving edits and switching projects", async () => {
  const h = workspace();
  let saved;
  h.config.flushEdits = () => new Promise(resolve => { saved = resolve; });
  const first = h.mount("one", 2);
  h.reply(h.requests[0], 2, "", [{ kind: "section", id: "completed", target: "章节", status: "completed",
    result: { content: "原项目草稿" } }]);
  await first;
  const button = h.find("保存结果／保留为备选");
  const applying = button.onclick();
  const second = h.mount("two", 5);
  h.reply(h.requests[1], 5, "新项目任务");
  await second;
  saved();
  await applying;
  await button.onclick();
  assert.equal(h.requests.length, 2);
  assert.ok(h.find("新项目任务"));
  assert.deepEqual(h.toasts, []);
});

test("the project declares the worker route before the task panel responds", async () => {
  const h = workspace();
  const pending = h.mount("legacy", 2, false);
  assert.equal(await h.pilot.usesWorker(), false);
  assert.equal(h.pilot.active(), false);
  h.reply(h.requests[0], 2, "同步任务", [], false);
  await pending;

  const opening = h.mount("background", 3, true);
  assert.equal(await h.pilot.usesWorker(), true);
  assert.equal(h.pilot.active(), true);
  assert.equal(h.requests.length, 2);
  h.reply(h.requests[1], 3, "后台任务");
  await opening;
});

test("missing worker status waits for the mounted response and propagates a lookup failure", async () => {
  const h = workspace();
  const opening = h.mount("one", 3);
  let decided = false;
  const route = h.pilot.usesWorker().finally(() => { decided = true; });
  await Promise.resolve();
  assert.equal(decided, false);
  assert.equal(h.requests.length, 1);
  h.reply(h.requests[0], 3, "后台任务");
  assert.equal(await route, true);
  await opening;

  const reopened = h.mount("two", 5);
  const failed = h.pilot.usesWorker();
  h.requests[1].reject(new Error("连接中断"));
  await assert.rejects(failed, /暂时无法确认材料后台状态/);
  await reopened;
});

test("a workspace switch while checking the worker state cannot choose a different project's route", async () => {
  const h = workspace();
  const old = h.mount("one", 3);
  const route = h.pilot.usesWorker();
  const next = h.mount("two", 5, false);
  h.reply(h.requests[1], 5, "新项目", [], false);
  await next;
  h.reply(h.requests[0], 3, "旧项目");
  await assert.rejects(route, /项目已切换/);
  await old;
  assert.equal(await h.pilot.usesWorker(), false);
});


const eventLoop = () => new Promise(resolve => setImmediate(resolve));
function type(h, label, value) {
  const field = h.field(label);
  field.value = value;
  field.listeners.input();
}

test("material form input survives unrelated task refresh, navigation, and same-account reload", async () => {
  const shared = new Map();
  const h = workspace(shared, "student-one");
  const initial = h.mount("p", 4, true);
  h.reply(h.requests[0], 4, "原任务");
  await initial;
  type(h, "图题", "测试中的图题");
  type(h, "具体要求", "提交前检查图注");
  const refreshing = h.pilot.refresh();
  h.reply(h.requests[1], 4, "新的任务");
  await refreshing;
  assert.equal(h.field("具体要求").value, "提交前检查图注");
  assert.equal(h.field("图题").value, "测试中的图题");
  const otherProject = h.mount("other", 5, true);
  h.reply(h.requests[2], 5, "另一个项目");
  await otherProject;
  assert.equal(h.field("图题").value, "");
  assert.equal(h.field("具体要求").value, "");

  const reopened = workspace(shared, "student-one");
  const opened = reopened.mount("p", 4, true);
  reopened.reply(reopened.requests[0], 4, "刷新成功");
  await opened;
  assert.equal(reopened.field("图题").value, "测试中的图题");
  assert.equal(reopened.field("具体要求").value, "提交前检查图注");
  const otherAccount = workspace(shared, "student-two");
  const accountView = otherAccount.mount("p", 4, true);
  otherAccount.reply(otherAccount.requests[0], 4, "另一个账户");
  await accountView;
  assert.equal(otherAccount.field("图题").value, "");
  assert.equal(otherAccount.field("具体要求").value, "");
  reopened.pilot.clearRecovery("p");
  assert.equal(shared.size, 0);
});

test("a committed requirement with a lost response keeps later typing and avoids a duplicate rule", async () => {
  const h = workspace();
  const initial = h.mount("p", 4, true);
  h.reply(h.requests[0], 4, "任务");
  await initial;
  type(h, "具体要求", "提交的第一条");
  type(h, "出处／文件名", "学校通知");
  const saving = h.find("确认这条要求适用并保存").onclick();
  const body = JSON.parse(h.requests[1].options.body);
  type(h, "具体要求", "保存时写下的第二条");
  h.requests[1].reject(new Error("网络断开"));
  await eventLoop();
  h.reply(h.requests[2], 5, "任务", [], true, { requirements: body.items });
  await saving;
  assert.equal(h.field("具体要求").value, "保存时写下的第二条");
  const next = h.find("确认这条要求适用并保存").onclick();
  const second = JSON.parse(h.requests[3].options.body);
  assert.deepEqual(second.items.map(rule => rule.value), ["提交的第一条", "保存时写下的第二条"]);
  h.requests[3].resolve({ revision: 6 });
  await eventLoop();
  h.reply(h.requests[4], 6, "任务", [], true, { requirements: second.items });
  await next;
  assert.equal(h.field("具体要求").value, "");
});

test("editing a plan retains its original version until a concurrent change is explicitly resolved", async () => {
  const h = workspace();
  h.state.outline = [{ id: "chapter", title: "章节" }];
  const plan = { id: "flow", title: "原图题", section_id: "chapter", renderer: "svg", chart: "flow",
    nodes: ["起点", "终点"], edges: [], labels: [], values: [], starts: [], source: "", prompt: "",
    scene_text: [], overlays: [], base_asset_job: "", data_status: "planned", document_id: "",
    anchor: 3, source_ids: ["source-id"], confirmed: true, text_confirmed: false };
  const initial = h.mount("p", 4, true);
  h.reply(h.requests[0], 4, "任务", [], true, { plans: [plan] });
  await initial;
  await h.find("编辑", undefined, true).onclick();
  type(h, "图题", "本页编辑");
  const refreshing = h.pilot.refresh();
  h.reply(h.requests[1], 5, "任务", [], true, { plans: [{ ...plan, title: "别处保存" }] });
  await refreshing;
  assert.equal(h.field("图题").value, "本页编辑");
  assert.ok(h.find("这份计划已在其他页面更新"));
  assert.equal(h.find("保存并确认图表计划").disabled, true);
  await h.find("保留本页图表输入").onclick();
  const saving = h.find("保存并确认图表计划").onclick();
  const body = JSON.parse(h.requests[2].options.body);
  assert.equal(body.revision, 5);
  assert.equal(body.items[0].id, plan.id);
  assert.equal(body.items[0].title, "本页编辑");
  assert.deepEqual(body.items[0].source_ids, plan.source_ids);
  assert.equal(body.items[0].anchor, plan.anchor);
  h.requests[2].resolve({ plans: body.items });
  await eventLoop();
  h.reply(h.requests[3], 6, "任务", [], true, { plans: body.items });
  await saving;
  assert.equal(h.field("图题").value, "");
});


test("figure text review requires opening the current image and uses its task and verified digest", async () => {
  const h = workspace();
  const currentJobId = "b".repeat(32);
  const oldJobId = "a".repeat(32);
  const scene = ["场景文字样例"];
  const figure = { id: "target", title: "当前图表", material_job_id: currentJobId, scene_text: scene };
  const job = id => ({ id, kind: "figure", target: figure.id, status: "completed",
    result: { applied: true, asset: { title: "图表", scene_text: scene } } });
  const context = { revision: 5, requirements: { priority: [] }, diagrams: [figure] };
  const mount = h.mount("p", 5, true);
  h.reply(h.requests[0], 5, "", [job(oldJobId), job(currentJobId)], true, { context });
  await mount;
  const oldRow = h.find("核对记录适用于项目当前采用的图片。");
  assert.ok(oldRow);
  const currentRow = h.find("打开这张图后即可核对文字。");
  assert.ok(currentRow);
  const confirmation = h.find("已逐字核对并确认");
  assert.equal(confirmation.disabled, true);
  await confirmation.onclick();
  assert.equal(h.requests.length, 1);

  const bytes = Buffer.from("figure bytes");
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  let displayedHash = "0".repeat(64);
  const urls = [];
  h.context.fetch = async url => {
    urls.push(url);
    return { ok: true, status: 200, headers: new Headers({ "X-Figure-SHA256": displayedHash }),
      blob: async () => new Blob([bytes], { type: "image/png" }) };
  };
  // The first job has a historical preview, while only the second has a confirmation control.
  const task = h.find("打开这张图后即可核对文字。");
  const findParent = (target, node) => {
    if (node.children.includes(target)) return node;
    for (const child of node.children) {
      const parent = findParent(target, child);
      if (parent) return parent;
    }
  };
  const preview = findParent(task, h.context.document.querySelector()).children.find(el => el.textContent === "查看生成图片");
  await preview.onclick();
  assert.equal(confirmation.disabled, true);
  assert.match(h.toasts.at(-1), /图片校验失败/);
  displayedHash = sha256;
  await preview.onclick();
  assert.equal(confirmation.disabled, false);
  assert.ok(urls.every(url => url.endsWith(`/jobs/${currentJobId}/files/figure.png`)));

  const submitting = confirmation.onclick();
  assert.deepEqual(JSON.parse(h.requests[1].options.body), { revision: 5, job_id: currentJobId, image_sha256: sha256 });
  h.requests[1].resolve({});
  await eventLoop();
  h.reply(h.requests[2], 6, "", [job(currentJobId)], true, { context: { ...context, revision: 6, diagrams: [{
    ...figure, text_confirmed: true, text_confirmation: { job_id: currentJobId, sha256, scene_text: scene },
  }] } });
  await submitting;
  assert.equal(h.find("已逐字核对并确认").disabled, true);
  assert.ok(h.find("已保存此版本的文字核对记录"));
});
