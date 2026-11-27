const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

test("a silent job submission ends observation and does not submit a second task", async () => {
  const calls = [], timers = new Map(); let timerId = 0;
  const state = { project: { id: "project", material_revision: 2, sections: {} }, llmConfig: {} };
  const host = { state, toast() {}, api: (url, options = {}) => {
    calls.push({ url, method: options.method }); return new Promise(() => {});
  } };
  const context = vm.createContext({ window: {}, console, confirm: () => true,
    crypto: { randomUUID: () => "once" },
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimeout: id => timers.delete(id), document: { querySelectorAll: () => [] } });
  const source = readFileSync(resolve(__dirname, "../../server/static/material.js"), "utf8")
    .replace("window.MaterialPilot =", `
      globalThis.init = config => { host = config; projectId = config.state.project.id;
        data = { context: { revision: 2 }, worker_ready: true }; refresh = async () => {}; };
      window.MaterialPilot =`);
  vm.runInContext(source, context); context.init(host);
  const running = context.window.MaterialPilot.section("chapter", null);
  await new Promise(resolve => setImmediate(resolve));
  const timeout = [...timers.values()].find(entry => entry.delay === 30000);
  assert.ok(timeout);
  timeout.fn();
  await assert.rejects(running, /任务可能已经开始/);
  assert.equal(calls.length, 1);
});

test("switching projects while loading material state cannot submit to another project", async () => {
  const calls = [];
  const state = { project: { id: "first", material_revision: 2, sections: {} }, llmConfig: {} };
  const host = { state, toast() {}, api: async (...args) => { calls.push(args); return {}; } };
  const context = vm.createContext({ window: {}, console, confirm: () => true,
    crypto: { randomUUID: () => "once" }, setTimeout: () => 1, clearTimeout() {},
    document: { querySelectorAll: () => [] } });
  const source = readFileSync(resolve(__dirname, "../../server/static/material.js"), "utf8")
    .replace("window.MaterialPilot =", `
      globalThis.init = config => { host = config; projectId = config.state.project.id;
        data = { context: { revision: 2 }, worker_ready: true };
        refresh = () => new Promise(resolve => { globalThis.finish = resolve; }); };
      window.MaterialPilot =`);
  vm.runInContext(source, context); context.init(host);
  const running = context.window.MaterialPilot.section("chapter", null);
  state.project = { id: "second", material_revision: 2, sections: {} };
  context.finish();
  await assert.rejects(running, /项目已切换/);
  assert.equal(calls.length, 0);
});

test("an incomplete section review remains in its single task without auto-apply", async () => {
  const calls = [];
  const state = { project: { id: "project", material_revision: 2, sections: { chapter: "原文" } }, llmConfig: {} };
  const host = { state, toast() {}, api: async (url, options = {}) => {
    calls.push({ url, method: options.method });
    if (options.method === "POST") return { id: "job" };
    return { status: "completed", result: { status: "review_failed", content: "可查看的备选草稿" } };
  } };
  const context = vm.createContext({ window: {}, console, confirm: () => true,
    crypto: { randomUUID: () => "once" }, setTimeout: (fn, delay) => { if (delay < 15000) fn(); return 1; }, clearTimeout() {},
    document: { querySelectorAll: () => [] } });
  const source = readFileSync(resolve(__dirname, "../../server/static/material.js"), "utf8")
    .replace("window.MaterialPilot =", `
      globalThis.init = config => { host = config; projectId = config.state.project.id;
        data = { context: { revision: 2 }, worker_ready: true }; refresh = async () => {}; };
      window.MaterialPilot =`);
  vm.runInContext(source, context);
  context.init(host);
  const result = await context.window.MaterialPilot.section("chapter", null);
  assert.equal(result.review_incomplete, true);
  assert.equal(result.content, "可查看的备选草稿");
  assert.equal(state.project.sections.chapter, "原文");
  assert.equal(calls.filter(call => call.method === "POST").length, 1);
  assert.ok(!calls.some(call => call.url.endsWith("/apply")));
});

test("explicitly choosing the saved task draft sends the required confirmation", async () => {
  class Element {
    constructor() { this.children = []; this.classList = { toggle() {} }; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
  }
  const calls = [];
  const state = { project: { id: "project", material: { enabled: true }, material_revision: 2, sections: {} }, outline: [] };
  const host = { state, toast() {}, reload: async () => {}, api: async (url, options = {}) => {
    calls.push({ url, ...options });
    return { applied: true };
  } };
  const context = vm.createContext({ window: {}, console, URL: { revokeObjectURL() {} },
    document: { createElement: () => new Element(), querySelectorAll: () => [] } });
  const source = readFileSync(resolve(__dirname, "../../server/static/material.js"), "utf8")
    .replace("window.MaterialPilot =", `
      globalThis.show = config => { host = config; projectId = config.state.project.id; root = document.createElement('div');
        data = { worker_ready: true, context: { revision: 2, stale: {}, sources: [], requirements: { priority: [], conflicts: [] } },
          requirements: [], plans: [], jobs: [{ id: 'job', kind: 'section', status: 'completed',
            result: { status: 'review_failed', content: '待查看草稿' } }] };
        refresh = async () => {}; draw(); return root; };
      window.MaterialPilot =`);
  vm.runInContext(source, context);
  const root = context.show(host);
  function find(node, text) {
    if (node.textContent === text) return node;
    for (const child of node.children || []) { const found = find(child, text); if (found) return found; }
  }
  const button = find(root, "使用此草稿");
  assert.ok(button);
  await button.onclick();
  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.endsWith("/jobs/job/apply"));
  assert.equal(JSON.parse(calls[0].body).use_unreviewed_draft, true);
});

test("uncorroborated personal numbers remain in the task and prompt the student to check", async () => {
  const calls = [];
  const state = { project: { id: "project", material_revision: 2, sections: { chapter: "原文" } }, llmConfig: {} };
  const host = { state, toast() {}, api: async (url, options = {}) => {
    calls.push({ url, method: options.method });
    if (options.method === "POST") return { id: "job" };
    return { status: "completed", result: { status: "passed", review_incomplete: true,
      grounding_issues: ["绩点满分制"], content: "我的绩点3.8，满分4.0。" } };
  } };
  const context = vm.createContext({ window: {}, console, confirm: () => true,
    crypto: { randomUUID: () => "once" }, setTimeout: (fn, delay) => { if (delay < 15000) fn(); return 1; }, clearTimeout() {},
    document: { querySelectorAll: () => [] } });
  const source = readFileSync(resolve(__dirname, "../../server/static/material.js"), "utf8")
    .replace("window.MaterialPilot =", `
      globalThis.init = config => { host = config; projectId = config.state.project.id;
        data = { context: { revision: 2 }, worker_ready: true }; refresh = async () => {}; };
      window.MaterialPilot =`);
  vm.runInContext(source, context);
  context.init(host);
  const result = await context.window.MaterialPilot.section("chapter", null);
  assert.match(result.notice, /个人数字需要你核对/);
  assert.equal(state.project.sections.chapter, "原文");
  assert.ok(!calls.some(call => call.url.endsWith("/apply")));
});

function paidJobPage(shared, api, confirm, owner = "user:one") {
  const id = "a".repeat(32);
  const state = { project: { id, material: { enabled: true }, material_revision: 2, sections: {} },
    llmConfig: { api_key: "hidden-config" } };
  const context = vm.createContext({ window: {}, console, confirm,
    crypto: { randomUUID: () => "12345678-1234-1234-1234-123456789012" },
    setTimeout: (fn, delay) => { if (delay < 15000) fn(); return 1; }, clearTimeout() {},
    document: { querySelectorAll: () => [] }, sessionStorage: {
      getItem: key => shared.get(key) ?? null, setItem: (key, value) => shared.set(key, value),
      removeItem: key => shared.delete(key), get length() { return shared.size; },
      key: index => [...shared.keys()][index] ?? null,
    } });
  const source = readFileSync(resolve(__dirname, "../../server/static/material.js"), "utf8")
    .replace("window.MaterialPilot =", `
      globalThis.init = config => { host = config; projectId = config.state.project.id;
        data = { context: { revision: 2 }, worker_ready: true, jobs: [] }; refresh = async () => {}; };
      window.MaterialPilot =`);
  vm.runInContext(readFileSync(resolve(__dirname, "../../server/static/draft-recovery.js"), "utf8"), context);
  vm.runInContext(source, context);
  context.init({ state, api, toast() {} });
  const pilot = context.window.MaterialPilot;
  pilot.configureRecovery(() => owner);
  return { id, pilot };
}

for (const kind of ["section", "outline"]) {
  test(`lost ${kind} work is recovered after reload without confirming another model call`, async () => {
    const shared = new Map(), posts = [], followups = [];
    const target = kind === "section" ? "chapter" : "";
    const start = pilot => kind === "section" ? pilot.section(target, null) : pilot.outline();
    const first = paidJobPage(shared, async (url, options) => {
      assert.ok(url.endsWith("/jobs") && options.method === "POST");
      posts.push(JSON.parse(options.body));
      throw Error("connection lost");
    }, () => true);
    await assert.rejects(start(first.pilot), /任务可能已经开始/);
    assert.equal(posts.length, 1);
    const key = posts[0].idempotency_key;
    assert.ok(![...shared.values()].join("").includes("hidden-config"));
    const noConfirm = () => assert.fail("checking a finished job should not confirm a new charge");
    const other = paidJobPage(shared, () => assert.fail("unexpected request"), noConfirm, "user:two");
    assert.equal(other.pilot.pendingJob(other.id, kind, target), null);

    const jobId = "b".repeat(32);
    const result = kind === "section" ? { content: "这次任务的正文" } : { outline: [{ id: "next", title: "候选目录" }] };
    const recovered = paidJobPage(shared, async (url, options) => {
      followups.push([url, options.method]);
      if (url.endsWith(`/jobs/by-request/${key}`))
        return { id: jobId, kind, target, project_id: first.id, status: "completed", result };
      if (kind === "section" && url.endsWith(`/jobs/${jobId}/apply`))
        return { ...result, applied: true, material_revision: 3 };
      assert.fail(`unexpected request ${url}`);
    }, noConfirm);
    const outcome = await start(recovered.pilot);
    assert.equal(kind === "section" ? outcome.content : outcome.outline[0].title,
      kind === "section" ? "这次任务的正文" : "候选目录");
    assert.equal(followups.filter(([url]) => url.endsWith("/jobs")).length, 0);
    assert.equal(shared.size, 0);
  });
}

test("a missing receipt can be explicitly retried using the original submission number", async () => {
  const shared = new Map(), ids = [], confirms = [];
  const original = paidJobPage(shared, async (_url, options) => {
    ids.push(JSON.parse(options.body).idempotency_key);
    throw Error("network interrupted");
  }, () => true);
  await assert.rejects(original.pilot.outline());
  let allowRetry = false;
  const recovered = paidJobPage(shared, async (url, options) => {
    if (url.includes("/jobs/by-request/"))
      throw Object.assign(new Error("not found"), { status: 404 });
    assert.ok(url.endsWith("/jobs"));
    ids.push(JSON.parse(options.body).idempotency_key);
    return { id: "b".repeat(32), status: "completed", result: { outline: [{ id: "next", title: "候选目录" }] } };
  }, message => { confirms.push(message); return allowRetry; });
  await assert.rejects(recovered.pilot.outline(), /已取消/);
  assert.equal(ids.length, 1);
  allowRetry = true;
  assert.equal((await recovered.pilot.outline()).outline[0].title, "候选目录");
  assert.equal(ids.length, 2);
  assert.equal(ids[0], ids[1]);
  assert.ok(confirms.every(message => message.includes("沿用原编号")));
});
