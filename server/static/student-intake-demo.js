"use strict";
const replay = document.querySelector("#replay");
const choice = document.querySelector("#examples");
const content = document.querySelector("#demoContent");
const statusNames = { stated: "自述", approximate: "模糊", planned: "计划", negated: "否定",
  unknown: "未知", withheld: "拒答", conflict: "冲突" };

function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

function resultCard(title, output, labels) {
  const card = element("article");
  card.className = "condition";
  card.append(element("h3", title));
  const candidate = output.review.candidate;
  if (!candidate || !candidate.facts.length) {
    card.append(element("p", candidate ? "来源校验后没有候选。" : "结构校验未通过，需人工核对。"));
  }
  for (const f of candidate?.facts || []) {
    const block = element("div");
    block.className = "fact";
    block.append(element("h4", labels[f.field] + " · " + statusNames[f.status]));
    if (f.value !== null) block.append(element("p", f.value));
    for (const e of f.evidence) block.append(element("small", e.turn_id + "　“" + e.quote + "”"));
    card.append(block);
  }
  for (const field of candidate?.follow_up_fields || []) card.append(element("p", "待追问　" + labels[field]));
  const raw = element("details");
  raw.append(element("summary", "查看校验前的模型原答"));
  let display = output.raw;
  try { display = JSON.stringify(JSON.parse(display), null, 2); } catch { /* Show the original failure. */ }
  raw.append(element("pre", display));
  card.append(raw);
  return card;
}

async function loadReplay() {
  const response = await fetch(replay.dataset.src);
  if (!response.ok) throw new Error("replay");
  const data = await response.json();
  choice.replaceChildren();
  for (const [index, example] of data.examples.entries()) choice.add(new Option(example.title, String(index)));
  function render() {
    const current = data.examples[Number(choice.value)];
    const source = element("div");
    source.className = "source";
    for (const t of current.turns) source.append(element("p", t.turn_id + "　“" + t.content + "”"));
    const compare = element("div");
    compare.className = "compare";
    compare.append(resultCard("微调前 · 同底座", current.base, data.labels),
      resultCard("微调后 · 第 3 轮 LoRA", current.tuned, data.labels));
    content.replaceChildren(source, compare);
  }
  choice.addEventListener("change", render);
  choice.disabled = false;
  render();
}
loadReplay().catch(() => content.replaceChildren(element("p", "实测样例暂时无法载入，请刷新重试。")));
