const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tag = tag;
    this.children = [];
    this.textContent = "";
    this.classList = { add() {}, remove() {} };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  set innerHTML(_) { throw new Error("private model output must not be parsed as HTML"); }
}

function harness({ failPost = false, items = [], failSave = false, capabilities } = {}) {
  const elements = new Map(), calls = [], notices = [], storage = new Map(), timers = [];
  let count = 0;
  const state = { project: { id: "a".repeat(32), state: { skill_id: "innovation-research" } } };
  const context = vm.createContext({ window: {}, console,
    document: { createElement: tag => new Element(tag), querySelector: selector => {
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    } },
    crypto: { randomUUID: () => `request-${++count}` },
    sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    setTimeout: (callback, delay) => { timers.push({ callback, delay }); return 1; }, clearTimeout() {},
  });
  vm.runInContext(readFileSync(resolve(__dirname, "../../server/static/review.js"), "utf8"), context);
  const host = { state, toast: message => notices.push(message), flushEdits: async () => {
    if (failSave) throw new Error("保存期间正文又有修改");
  }, api: async (path, options = {}) => {
    calls.push({ path, options });
    if (path === "/api/reviews/capabilities") return capabilities ? capabilities() : { enabled: true };
    if (options.method === "POST") {
      if (failPost) throw new Error("连接中断，结果尚未确认");
      return { id: "b".repeat(32), fingerprint: "c".repeat(64), status: "pending" };
    }
    return { items, current_fingerprint: "c".repeat(64) };
  } };
  return { mount: () => context.window.DraftReview.mount(host), state, calls, elements, notices, timers };
}

function flatten(element) {
  return [element.textContent, ...element.children.map(flatten)].join(" ");
}

test("uncertain submit retries the exact same key and snapshot without creating a new logical request", async () => {
  const h = harness({ failPost: true });
  await h.mount();
  await h.elements.get("#draftReviewBtn").onclick();
  await h.elements.get("#draftReviewBtn").onclick();
  const posts = h.calls.filter(call => call.options.method === "POST");
  assert.equal(posts.length, 2);
  assert.deepEqual(JSON.parse(posts[0].options.body), JSON.parse(posts[1].options.body));
  assert.match(h.notices[0], /结果尚未确认/);
});

test("a failed save never submits a check of an older draft", async () => {
  const h = harness({ failSave: true });
  await h.mount();
  await h.elements.get("#draftReviewBtn").onclick();
  assert.equal(h.calls.filter(call => call.options.method === "POST").length, 0);
  assert.match(h.notices[0], /正文又有修改/);
});

test("old reports remain marked historical and model HTML is rendered as literal text", async () => {
  const attack = '<img src=x onerror="steal()">';
  const h = harness({ items: [{ id: "b".repeat(32), fingerprint: "d".repeat(64), status: "completed",
    report: { notice: "仅供修改参考", complete: true, section_titles: { method: "研究方法" },
      issues: [{ section_id: "method", problem: attack, suggestion: "补充依据", origin: "model", citations: [] }] } }] });
  await h.mount();
  const output = flatten(h.elements.get("#draftReviewResults"));
  assert.match(output, /历史版本/);
  assert.ok(output.includes(attack));
  assert.match(output, /模型意见待核对/);
});

test("switching projects during capability loading cannot render the previous project's report", async () => {
  let finish;
  const h = harness({ capabilities: () => new Promise(resolve => { finish = resolve; }) });
  const mounting = h.mount();
  h.state.project = { id: "e".repeat(32) };
  finish({ enabled: true });
  await mounting;
  assert.equal(h.calls.length, 1);
  assert.equal(h.elements.get("#draftReviewResults").children.length, 0);
});

test("explicitly checking again allocates a new idempotency key", async () => {
  const h = harness({ items: [{ id: "b".repeat(32), fingerprint: "c".repeat(64), status: "completed" }] });
  await h.mount();
  await h.elements.get("#draftReviewBtn").onclick();
  const results = h.elements.get("#draftReviewResults");
  const retry = results.children[0].children.find(element => element.tag === "button");
  await retry.onclick();
  const posts = h.calls.filter(call => call.options.method === "POST");
  assert.equal(posts.length, 2);
  assert.notEqual(JSON.parse(posts[0].options.body).idempotency_key, JSON.parse(posts[1].options.body).idempotency_key);
});

test("cloud AI consent displays its reservation price before submission", async () => {
  const h = harness({ capabilities: () => ({ enabled: true, execution: "cloud_queue", ai_available: true,
    ai_label: "平台 AI 预审（预留 2 积分，按实际用量结算）" }) });
  await h.mount();
  assert.match(h.elements.get("#draftReviewModelLabel").textContent, /预留 2 积分/);
  assert.equal(h.elements.get("#draftReviewModel").checked, false);
});

test("review billing displays the actual ledger amount with micro-CNY precision", async () => {
  const h = harness({ items: [{ id: "b".repeat(32), fingerprint: "c".repeat(64), status: "completed",
    billing: { status: "settled", reserved_micro_cny: 200000, actual_micro_cny: 12345 } }] });
  await h.mount();
  assert.match(flatten(h.elements.get("#draftReviewResults")), /0\.12345 积分（已结算）/);
});

test("a completed report continues polling until delayed billing settles", async () => {
  const run = { id: "b".repeat(32), fingerprint: "c".repeat(64), status: "completed",
    billing: { status: "reserved", reserved_micro_cny: 200000 } };
  const h = harness({ items: [run] });
  await h.mount();
  assert.equal(h.timers.length, 1);
  assert.equal(h.timers[0].delay, 10000);
  run.billing = { status: "settled", actual_micro_cny: 12345 };
  h.timers[0].callback();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(flatten(h.elements.get("#draftReviewResults")), /0\.12345 积分（已结算）/);
  assert.equal(h.timers.length, 1);
});
