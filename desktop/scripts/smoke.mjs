import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { _electron as electron } from "playwright-core";

const desktop = fileURLToPath(new URL("../", import.meta.url));
const dist = path.join(desktop, "dist");
const development = process.argv.includes("--dev");
const temporary = mkdtempSync(path.join(os.tmpdir(), "stardust-desktop-smoke-"));
const profile = path.join(temporary, "profile");
let running;

function findApp() {
  const entries = readdirSync(dist);
  const packaged = entries.find(name => process.platform === "darwin"
    ? /^mac(-arm64|-x64)?$/.test(name)
    : /^(win|linux)(-arm64|-x64)?-unpacked$/.test(name));
  assert.ok(packaged, `No unpacked application found in ${dist}`);
  const install = path.join(dist, packaged);
  if (process.platform === "darwin") {
    const contents = path.join(install, "Stardust Agent.app", "Contents");
    return {
      executable: path.join(contents, "MacOS", "Stardust Agent"),
      runtime: path.join(contents, "Resources", "workbench-runtime"),
    };
  }
  return {
    executable: path.join(install, process.platform === "win32" ? "Stardust Agent.exe" : "stardust-agent"),
    runtime: path.join(install, "resources", "workbench-runtime"),
  };
}

const installed = development
  ? { executable: undefined, runtime: path.join(desktop, ".stage", "runtime") }
  : findApp();
const runtimeInfo = JSON.parse(readFileSync(path.join(installed.runtime, "runtime.json"), "utf8"));
const python = path.join(installed.runtime, runtimeInfo.python);
const image = path.join(temporary, "desktop-ocr.png");

async function open() {
  running = await electron.launch({
    ...(development ? {} : { executablePath: installed.executable }),
    args: [...(development ? [desktop] : []), `--user-data-dir=${profile}`],
    env: {
      ...process.env,
      SUPABASE_URL: "https://not-used.invalid",
      SUPABASE_PUBLISHABLE_KEY: "must-not-switch-desktop-to-cloud",
    },
    timeout: 45_000,
  });
  const page = await running.firstWindow();
  await page.waitForURL(/^http:\/\/127\.0\.0\.1:\d+\/$/, { timeout: 45_000 });
  await page.locator("#appShell").waitFor({ state: "visible", timeout: 15_000 });
  return page;
}

async function close() {
  if (running) {
    await running.close();
    running = undefined;
  }
}

async function verifyStopped(baseURL) {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      await fetch(`${baseURL}/api/ping`, { signal: AbortSignal.timeout(500) });
    } catch {
      return;
    }
    await delay(200);
  }
  throw new Error("The local service did not stop after the window closed.");
}

try {
  execFileSync(python, ["-I", "-B", "-c", [
    "import sys",
    "from PIL import Image, ImageDraw, ImageFont",
    'im = Image.new("RGB", (1050, 280), "white")',
    'ImageDraw.Draw(im).text((70, 70), "STARDUST DESKTOP QA", fill="black", font=ImageFont.load_default(size=58))',
    "im.save(sys.argv[1])",
  ].join("\n"), image]);

  const first = await open();
  const baseURL = new URL(first.url()).origin;
  const externalRequest = await fetch(`${baseURL}/api/projects`);
  assert.equal(externalRequest.status, 403);
  const project = await first.evaluate(async () => {
    const cfg = await (await fetch("/api/config")).json();
    if (cfg.cloud_mode) throw new Error("Desktop started in cloud mode");
    const skills = await (await fetch("/api/skills")).json();
    if (skills.skills.length !== 35) throw new Error("Bundled skills are incomplete");
    const headers = { "Content-Type": "application/json" };
    const response = await fetch("/api/projects", {
      method: "POST", headers,
      body: JSON.stringify({ title: "Desktop smoke test", skill_id: "innovation-research", has_template: false }),
    });
    if (!response.ok) throw new Error(`Creating a local project failed (${response.status})`);
    const created = await response.json();
    const outline = await fetch(`/api/projects/${created.id}/outline`, {
      method: "PUT", headers,
      body: JSON.stringify({ outline: [{ id: "abstract", title: "项目摘要" }] }),
    });
    if (!outline.ok) throw new Error(`Saving the test outline failed (${outline.status})`);
    localStorage.setItem("stardust_onboarding_dismissed", "true");
    localStorage.setItem("stardust-agent-active-project-id", created.id);
    return created;
  });
  await first.reload();
  await first.locator("#projectHeading").filter({ hasText: /^Desktop smoke test$/ }).waitFor();
  await first.locator("#workspaceTabs [data-target='panel-materials']").click();
  await first.locator("#fileInput").setInputFiles(image);
  await first.locator("#documentPreviewBody img").waitFor({ timeout: 35_000 });
  await first.locator("#documentsList").getByText("desktop-ocr.png", { exact: true }).waitFor({ state: "attached" });
  const documentText = await first.locator("#documentPreviewBody").textContent();
  assert.match(documentText, /STARDUST/i, "the packaged OCR model should extract the test text");
  await first.locator("#closeDocumentPreviewBtn").click();
  await first.locator("#workspaceTabs [data-target='draft-panel']").click();
  const text = "Stardust Desktop acceptance test. 这是用于验收的合成正文。";
  const editor = first.locator("#draft-panel textarea");
  await editor.fill(text);
  const file = path.join(temporary, "exported.docx");
  await running.evaluate(({ BrowserWindow }, destination) => {
    const session = BrowserWindow.getAllWindows()[0].webContents.session;
    session.once("will-download", (_event, item) => item.setSavePath(destination));
  }, file);
  await first.locator("#quickExportTopBtn").click();
  await first.locator("#exportResult a").waitFor({ state: "attached", timeout: 30_000 });
  for (let attempt = 0; attempt < 75 && !existsSync(file); attempt += 1) {
    await delay(200);
  }
  assert.ok(existsSync(file), "the app should download the generated Word file");
  execFileSync(python, ["-I", "-B", "-c", [
    "import sys, zipfile",
    "with zipfile.ZipFile(sys.argv[1]) as archive:",
    "    assert archive.testzip() is None",
    '    assert sys.argv[2] in archive.read("word/document.xml").decode("utf-8")',
  ].join("\n"), file, text]);
  await first.locator(".section-status-pill").filter({ hasText: "✓ 已保存" }).waitFor();
  await close();
  await verifyStopped(baseURL);

  const second = await open();
  const persisted = await second.evaluate(async (id) => {
    const response = await fetch(`/api/projects/${id}`);
    if (!response.ok) throw new Error(`Opening the saved project failed (${response.status})`);
    return response.json();
  }, project.id);
  assert.equal(persisted.sections.abstract, text);
  assert.ok(persisted.documents.some(document => document.original_name === "desktop-ocr.png"));
  console.log(`${process.platform}/${process.arch}: startup, local isolation, OCR, Word export, and restart persistence passed.`);
} catch (error) {
  try {
    console.error(readFileSync(path.join(profile, "workbench.log"), "utf8").slice(-8000));
  } catch {
    // The launcher may have failed before the log was opened.
  }
  throw error;
} finally {
  await close();
  rmSync(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
