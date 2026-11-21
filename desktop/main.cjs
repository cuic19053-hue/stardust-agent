const { app, BrowserWindow, Menu, session, shell } = require("electron");
const { spawn } = require("node:child_process");
const { randomBytes } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");
const { setTimeout: delay } = require("node:timers/promises");
const { pathToFileURL } = require("node:url");

const statusPage = path.join(__dirname, "status.html");
const statusURL = pathToFileURL(statusPage).href;
const requestedProfile = app.commandLine.getSwitchValue("user-data-dir");
if (path.isAbsolute(requestedProfile)) {
  fs.mkdirSync(requestedProfile, { recursive: true });
  app.setPath("userData", requestedProfile);
}

const dataDirectory = path.join(app.getPath("userData"), "workbench");
const logPath = path.join(app.getPath("userData"), "workbench.log");
const settingsPath = path.join(app.getPath("userData"), "launcher.json");
const runtimeDirectory = app.isPackaged
  ? path.join(process.resourcesPath, "workbench-runtime")
  : path.join(__dirname, ".stage", "runtime");

let workbenchWindow;
let service;
let startup;
let origin = "";
let token = "";
let quitting = false;

function preferredPort() {
  try {
    const { port } = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
    if (Number.isInteger(port) && port > 1023 && port < 65536) return port;
  } catch {
    // The operating system selects an available port on a first run.
  }
  return 0;
}

function localEnvironment() {
  const osVariables = new Set([
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "WINDIR",
    "COMSPEC", "SYSTEMDRIVE", "LANG", "LC_ALL", "LD_LIBRARY_PATH",
  ]);
  const env = Object.fromEntries(
    Object.entries(process.env).filter(([name]) => osVariables.has(name.toUpperCase())),
  );
  return {
    ...env,
    STARDUST_DESKTOP_TOKEN: token,
    PROPOSAL_AGENT_DATA_DIR: dataDirectory,
    OLLAMA_HOST: "http://127.0.0.1:11434",
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONNOUSERSITE: "1",
    PYTHONUTF8: "1",
    MPLCONFIGDIR: path.join(dataDirectory, "matplotlib"),
  };
}

async function status(state, message = "") {
  if (workbenchWindow && !workbenchWindow.isDestroyed()) {
    await workbenchWindow.loadFile(statusPage, { query: { state, message } });
  }
}

function stopService() {
  const child = service;
  service = undefined;
  origin = "";
  token = "";
  child?.stdin.end();
}

async function waitForPort(child) {
  const output = readline.createInterface({ input: child.stdout });
  let timer;
  try {
    return await new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error("本机服务启动超时。")), 40_000);
      child.once("error", reject);
      child.once("exit", (code) => reject(new Error(`本机服务启动失败（退出码 ${code ?? "未知"}）。`)));
      output.on("line", (line) => {
        const announcement = /^STARDUST_PORT ([0-9]+)$/.exec(line);
        if (announcement) resolve(Number(announcement[1]));
      });
    });
  } finally {
    clearTimeout(timer);
    output.close();
    child.stdout.resume();
  }
}

async function waitForHealth(baseURL) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const result = await fetch(`${baseURL}/api/ping`, {
        headers: { "X-Stardust-Desktop-Token": token },
        signal: AbortSignal.timeout(500),
      });
      if (result.ok && (await result.json()).status === "ok") return;
    } catch {
      // The server may still be starting its event loop.
    }
    if (quitting || !service) break;
    await delay(200);
  }
  throw new Error("无法连接本机工作台。");
}

async function launchWorkbench() {
  await status("loading");
  fs.mkdirSync(dataDirectory, { recursive: true });
  const metadata = JSON.parse(fs.readFileSync(path.join(runtimeDirectory, "runtime.json"), "utf8"));
  const python = path.join(runtimeDirectory, metadata.python);
  const cwd = path.join(runtimeDirectory, "workbench");
  token = randomBytes(32).toString("hex");
  const log = fs.createWriteStream(logPath, { flags: "w", mode: 0o600 });
  log.on("error", (error) => console.error("Workbench log:", error.message));
  const child = spawn(python, ["desktop_service.py", "--port", String(preferredPort())], {
    cwd,
    env: localEnvironment(),
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  service = child;
  child.stdin.on("error", () => {});
  child.stderr.pipe(log);
  child.on("exit", () => {
    if (service !== child || quitting || startup) return;
    stopService();
    void status("error", "本机服务已停止。");
  });
  const port = await waitForPort(child);
  const baseURL = `http://127.0.0.1:${port}`;
  await waitForHealth(baseURL);
  if (child.exitCode !== null || child.signalCode !== null) throw new Error("本机服务已停止。");
  fs.writeFileSync(settingsPath, JSON.stringify({ port }), { mode: 0o600 });
  origin = baseURL;
  await workbenchWindow.loadURL(origin);
}

function startWorkbench() {
  if (startup || quitting) return;
  startup = launchWorkbench().catch(async (error) => {
    stopService();
    await status("error", error.message);
  }).finally(() => { startup = undefined; });
}

function openExternal(rawURL) {
  const url = new URL(rawURL);
  if (url.protocol === "https:" || url.protocol === "http:") {
    void shell.openExternal(url.href).catch(console.error);
  }
}

function configureNavigation(workbenchSession) {
  const filter = { urls: ["http://*/*", "https://*/*"] };
  workbenchSession.webRequest.onBeforeRequest(filter, (details, respond) => {
    respond({ cancel: new URL(details.url).origin !== origin });
  });
  workbenchSession.webRequest.onBeforeSendHeaders(filter, (details, respond) => {
    const requestHeaders = details.requestHeaders;
    if (new URL(details.url).origin === origin &&
        details.webContentsId === workbenchWindow?.webContents.id) {
      requestHeaders["X-Stardust-Desktop-Token"] = token;
    }
    respond({ requestHeaders });
  });
  workbenchSession.setPermissionRequestHandler((_contents, _permission, respond) => respond(false));
  workbenchSession.setPermissionCheckHandler(() => false);
  workbenchSession.setDownloadPath(app.getPath("downloads"));
}

function makeWindow() {
  const workbenchSession = session.fromPartition("persist:workbench");
  configureNavigation(workbenchSession);
  workbenchWindow = new BrowserWindow({
    width: 1380, height: 900, minWidth: 760, minHeight: 540,
    backgroundColor: "#f5f7fb",
    webPreferences: {
      session: workbenchSession, contextIsolation: true, sandbox: true, nodeIntegration: false,
    },
  });
  const contents = workbenchWindow.webContents;
  contents.on("will-navigate", (event, rawURL) => {
    if (new URL(rawURL).origin === origin) return;
    event.preventDefault();
    if (contents.getURL().startsWith(statusURL + "?")) {
      if (rawURL === "stardust-desktop:retry") startWorkbench();
      if (rawURL === "stardust-desktop:logs") void shell.openPath(logPath);
      return;
    }
    openExternal(rawURL);
  });
  contents.on("will-redirect", (event, rawURL) => {
    if (new URL(rawURL).origin !== origin) event.preventDefault();
  });
  contents.setWindowOpenHandler(({ url }) => {
    openExternal(url);
    return { action: "deny" };
  });
  startWorkbench();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!workbenchWindow || workbenchWindow.isDestroyed()) return;
    if (workbenchWindow.isMinimized()) workbenchWindow.restore();
    workbenchWindow.focus();
  });
  app.on("before-quit", () => {
    quitting = true;
    stopService();
  });
  app.on("window-all-closed", () => app.quit());
  app.whenReady().then(() => {
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      ...(process.platform === "darwin" ? [{ role: "appMenu" }] : []),
      { label: "工作台", submenu: [
        { label: "打开本机项目目录", click: () => void shell.openPath(dataDirectory) },
        { label: "查看启动日志", click: () => void shell.openPath(logPath) },
        { type: "separator" },
        { role: "quit", label: "退出" },
      ] },
      { role: "editMenu", label: "编辑" },
      { role: "viewMenu", label: "视图" },
      { label: "帮助", submenu: [
        { label: "安装本机 Ollama", click: () => openExternal("https://ollama.com/download") },
      ] },
    ]));
    makeWindow();
  });
}
