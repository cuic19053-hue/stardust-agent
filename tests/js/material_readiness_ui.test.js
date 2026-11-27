const assert = require("node:assert/strict");
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


function workspace() {
  class Element {
    children = [];
    classList = { toggle() {} };
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
  }
  const panel = new Element();
  const state = { project: null, outline: [] };
  const requests = [], toasts = [];
  const context = vm.createContext({
    window: {}, document: {
      querySelector: () => panel, createElement: () => new Element(),
    }, URL: { revokeObjectURL() {} },
  });
  vm.runInContext(readFileSync(resolve(__dirname, "../../server/static/material.js"), "utf8"), context);
  const pilot = context.window.MaterialPilot;
  const config = { state, toast: text => toasts.push(text),
    api: (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })),
  };
  function mount(id, revision) {
    state.project = { id, material_revision: revision, material: { enabled: true } };
    return pilot.mount(config);
  }
  function reply(request, revision, text, jobs = [], ready = true) {
    request.resolve({
      context: { revision, stale: {}, requirements: { priority: [], conflicts: [] } },
      worker_ready: ready, requirements: [], plans: [],
      jobs: jobs.length ? jobs : [{ kind: "section", target: text, status: "running" }],
    });
  }
  function find(value, node = panel) {
    if (node.textContent?.includes(value)) return node;
    for (const child of node.children) {
      const match = find(value, child);
      if (match) return match;
    }
  }
  return { pilot, config, state, requests, mount, reply, find, toasts };
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
