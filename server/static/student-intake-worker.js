/* Device-only inference. The UI sends a transcript only after all network assets are ready. */
const pyRoot = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/";
const pyLoaderSha = "04c052c815abb0bf2981fa38c2dfa58e3120ee5288c956df2b2a15e301782c5f";
const network = self.fetch.bind(self);
let engine, prepare, review, config, virtualBase, manifestUrl;
let assemblerPromise;
let sealed = false;
let stage = "idle";
let issue = "模型文件加载失败，请检查网络、浏览器存储空间并重试。";
const signalStatus = (text) => self.postMessage({ type: "status", text });

async function digest(buffer) {
  const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", buffer));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

async function download(url, options = {}) {
  if (sealed) throw new Error("Network disabled during extraction");
  const response = await network(url, { ...options, credentials: "omit", referrerPolicy: "no-referrer" });
  if (!response.ok) throw new Error("Asset returned " + response.status);
  return response;
}

async function downloadBase(file, url, chunkSize = 16 * 1024 * 1024) {
  const bytes = new Uint8Array(file.bytes), count = Math.ceil(bytes.length / chunkSize);
  const received = new Array(count).fill(0);
  let lastPercent = -1;

  async function readChunk(index) {
    const start = index * chunkSize, end = Math.min(start + chunkSize, bytes.length) - 1;
    for (let attempt = 0; attempt < 3; attempt++) {
      const source = new URL(url);
      source.searchParams.set("download", "true");
      source.searchParams.set("part", String(index));
      if (attempt) source.searchParams.set("retry", String(attempt));
      try {
        const response = await download(source, {
          cache: "no-store", headers: { Range: "bytes=" + start + "-" + end },
        });
        // Some hosts ignore Range. Accept a whole file only on the first request.
        const whole = response.status === 200 && index === 0;
        const range = response.headers.get("Content-Range");
        if (!whole && (response.status !== 206 || (range && range !== "bytes " + start + "-" + end + "/" + bytes.length))) {
          await response.body?.cancel();
          throw new Error("Unexpected base model range");
        }
        const target = whole ? bytes.length : end - start + 1;
        const reader = response.body.getReader();
        received[index] = 0;
        try {
          while (true) {
            const {value, done} = await reader.read();
            if (done) break;
            if (received[index] + value.length > target) throw new Error("Oversize base model block");
            bytes.set(value, start + received[index]);
            received[index] += value.length;
            const total = received.reduce((sum, n) => sum + n, 0);
            const percent = Math.floor(100 * total / bytes.length);
            if (percent > lastPercent) {
              signalStatus("正在下载公开底座 " + percent + "%（约 278 MB）。");
              lastPercent = percent;
            }
          }
        } finally { reader.releaseLock(); }
        if (received[index] !== target) throw new Error("Incomplete base model block");
        return whole;
      } catch (error) {
        received[index] = 0;
        if (attempt === 2) throw error;
        signalStatus("正在重试未完成的模型分片，已下载的其他分片会保留。");
        await new Promise(resolve => setTimeout(resolve, 500 * (attempt + 1)));
      }
    }
  }

  if (await readChunk(0)) return bytes.buffer;
  let next = 1;
  async function remaining() {
    while (next < count) await readChunk(next++);
  }
  await Promise.all([remaining(), remaining()]);
  return bytes.buffer;
}

async function verifiedBytes(file) {
  const url = new URL(file.url, manifestUrl);
  if (url.origin === location.origin) url.searchParams.set("v", file.sha256.slice(0, 16));
  const data = file === config?.source
    ? await downloadBase(file, url)
    : await (await download(url)).arrayBuffer();
  if ((file.bytes !== undefined && data.byteLength !== file.bytes) || await digest(data) !== file.sha256) {
    throw new Error("Asset integrity check failed");
  }
  return data;
}

async function importVerified(file) {
  const bytes = await verifiedBytes(file);
  const resource = URL.createObjectURL(new Blob([bytes], { type: "text/javascript" }));
  try { return await import(resource); }
  finally { URL.revokeObjectURL(resource); }
}

async function loadAssembler() {
  const [module, source, layout] = await Promise.all([
    importVerified(config.assembler), verifiedBytes(config.source), verifiedBytes(config.files["tensor-cache.json"]),
  ]);
  signalStatus("底座已核对，正在此设备转换权重并合入微调参数。");
  return module.createAssembler(source, JSON.parse(new TextDecoder().decode(layout)));
}

async function modelFile(name, file) {
  if (!file.fromBase) return new Response(await verifiedBytes(file));
  assemblerPromise ||= loadAssembler();
  const assemble = await assemblerPromise;
  const bytes = assemble(name);
  if (file.patch) {
    const compressed = await verifiedBytes(file.patch);
    const unpacked = new Blob([compressed]).stream().pipeThrough(new DecompressionStream("gzip"));
    const diff = new Uint8Array(await new Response(unpacked).arrayBuffer());
    if (bytes.length !== diff.length) throw new Error("Invalid adapter patch length");
    for (let i = 0; i < bytes.length; i++) bytes[i] ^= diff[i];
  }
  if (bytes.length !== file.bytes || await digest(bytes) !== file.sha256) {
    throw new Error("Tuned tensor integrity check failed");
  }
  return new Response(bytes, { headers: { "Content-Type": "application/octet-stream" } });
}

self.fetch = async (request, options) => {
  const url = new URL(request instanceof Request ? request.url : request, location.href);
  if (url.protocol === "blob:" || url.protocol === "data:") return network(request, options);
  if (sealed || (options?.method || request?.method || "GET") !== "GET") throw new Error("Network disabled");
  if (virtualBase && url.href.startsWith(virtualBase)) {
    const name = url.href.slice(virtualBase.length);
    if (!Object.hasOwn(config.files, name)) throw new Error("Unknown model file");
    return modelFile(name, config.files[name]);
  }
  if (url.href.startsWith(pyRoot) && !url.search && !url.hash) return download(url, options);
  throw new Error("Unexpected runtime resource");
};

async function loadReview() {
  const rules = await (await download("/student-intake/local/rules")).json();
  const { loadPyodide } = await importVerified({ url: pyRoot + "pyodide.mjs", sha256: pyLoaderSha });
  const python = await loadPyodide({ indexURL: pyRoot });
  await python.loadPackage("pydantic", { checkIntegrity: true });
  const folder = "/home/pyodide/intake";
  python.FS.mkdirTree(folder);
  python.FS.writeFile(folder + "/__init__.py", "");
  for (const [name, source] of Object.entries(rules)) {
    if (!/^[a-z_]+$/.test(name)) throw new Error("Invalid review module");
    python.FS.writeFile(folder + "/" + name + ".py", source);
  }
  python.runPython("from intake.student_intake_browser import prepare, review");
  prepare = python.globals.get("prepare");
  review = python.globals.get("review");
}

async function loadModel() {
  const { CreateMLCEngine } = await importVerified(config.runtime);
  engine = await CreateMLCEngine(config.version, {
    appConfig: { cacheBackend: "indexeddb", model_list: [{
      model: virtualBase, model_id: config.version, model_lib: virtualBase + "edge.wasm",
      overrides: { context_window_size: 4096 }, low_resource_required: true,
    }] },
    initProgressCallback(progress) {
      const fraction = Math.max(0, Math.min(1, Number(progress.progress) || 0));
      signalStatus("正在准备端侧模型 " + Math.round(fraction * 100) + "%，首次加载可能需要几分钟。");
    },
  });
}

async function initialize(url) {
  stage = "loading";
  const device = await navigator.gpu?.requestAdapter();
  if (!device?.features.has("shader-f16") || !self.DecompressionStream) {
    issue = "此设备需要支持 WebGPU、半精度运算和解压缩的浏览器。可尝试新版桌面 Chrome 或 Edge。";
    throw new Error("WebGPU unavailable");
  }
  manifestUrl = new URL(url, location.origin);
  if (manifestUrl.origin !== location.origin || manifestUrl.pathname !== "/static/intake-0p5b-v1/model.json") {
    throw new Error("Invalid model manifest");
  }
  config = await (await download(manifestUrl)).json();
  virtualBase = new URL("/static/intake-model/resolve/" + config.version + "/", location.origin).href;
  await Promise.all([loadReview(), loadModel()]);
  assemblerPromise = undefined;
  sealed = true;
  stage = "ready";
  self.postMessage({ type: "ready" });
}

async function extract(text) {
  stage = "running";
  issue = "本次端侧推理未完成，请缩短口述或重新加载模型。";
  const prepared = prepare(text);
  const { visible, message } = JSON.parse(prepared);
  let result = { decision: "empty", facts: [], questions: [] };
  if (visible.length) {
    const answer = await engine.chat.completions.create({
      model: config.version,
      messages: [{ role: "system", content: config.prompt }, { role: "user", content: message }],
      temperature: 0, top_p: 1, repetition_penalty: 1, seed: 427, max_tokens: 768,
      response_format: { type: "json_object", schema: JSON.stringify(config.schema) },
    });
    const choice = answer.choices[0];
    result = JSON.parse(review(prepared, choice.message.content || "", choice.finish_reason));
    // Release the last conversation from the engine's KV cache between independent inputs.
    await engine.resetChat();
  }
  stage = "ready";
  self.postMessage({ type: "result", result });
}

self.onmessage = async ({ data }) => {
  try {
    if (data.type === "load" && stage === "idle") await initialize(data.manifest);
    else if (data.type === "extract" && stage === "ready") await extract(data.text);
  } catch (error) {
    // Only load errors concern public assets. Student input/outputs are never logged.
    if (stage === "loading") console.warn("Browser model setup:", String(error?.message || error).slice(0, 350));
    stage = "failed";
    self.postMessage({ type: "error", text: issue });
  }
};
