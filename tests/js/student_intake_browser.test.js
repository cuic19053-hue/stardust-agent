const test = require("node:test");
const assert = require("node:assert/strict");

test("MLX affine groups are repacked with the reference fp16 rounding and independent half-group scales", async () => {
  const { createAssembler } = await import("../../server/static/intake-0p5b-v1/assemble.mjs");
  const prefix = "model.small", header = {};
  for (const [name, dtype, shape, from, to] of [
    ["weight", "U32", [1, 8], 0, 32], ["scales", "F16", [1, 1], 32, 34], ["biases", "F16", [1, 1], 34, 36],
  ]) header[prefix + "." + name] = { dtype, shape, data_offsets: [from, to] };
  const text = JSON.stringify(header), padded = Math.ceil(text.length / 8) * 8;
  const offset = 8 + padded, model = new ArrayBuffer(offset + 36);
  new DataView(model).setBigUint64(0, BigInt(padded), true);
  new Uint8Array(model, 8, padded).set(Buffer.from(text.padEnd(padded)));
  new Uint32Array(model, offset, 8).set([0xfedcba98, 0x76543210, 0x01234567, 0x89abcdef,
    0x22223333, 0x77778888, 0x44441111, 0x9999aaaa]);
  new Uint16Array(model, offset + 32, 2).set([0x3400, 0xbe00]); // 0.25, -1.5
  const records = [
    { name: prefix + ".q_weight", nbytes: 32, byteOffset: 0 },
    { name: prefix + ".q_scale", nbytes: 4, byteOffset: 32 },
  ];
  const assemble = createAssembler(model, { records: [{ dataPath: "only", records, nbytes: 36 }] });
  const output = assemble("only").buffer;
  assert.deepEqual([...new Uint32Array(output, 0, 8)], [0xedccba99, 0x87655432, 0x23455678, 0x99abccde,
    0x11113333, 0x8888aaaa, 0x44440000, 0xbbbbdddd]);
  assert.deepEqual([...new Uint16Array(output, 32, 2)], [13605, 12727]);
});

function rangeLoader(fetch, caches) {
  const fs = require("node:fs"), vm = require("node:vm");
  const worker = fs.readFileSync(require.resolve("../../server/static/student-intake-worker.js"), "utf8");
  const context = vm.createContext({
    self: { fetch, caches, postMessage() {} }, crypto: require("node:crypto").webcrypto, URL, Response, Request,
    location: { href: "https://example.test/static/worker.js" }, setTimeout: callback => callback(),
  });
  vm.runInContext(worker, context);
  return vm.runInContext("downloadBase", context);
}

test("device download retries just an incomplete range and preserves the other bytes", async () => {
  const src = Buffer.from("0123456789"), calls = [];
  const load = rangeLoader(async (url, options) => {
    const [,from,to] = /^bytes=(\d+)-(\d+)$/.exec(options.headers.Range).map(Number);
    calls.push(from);
    if (from === 4 && !url.searchParams.has("retry")) throw Error("transient");
    return new Response(src.subarray(from, to + 1), {status: 206, headers: {
      "Content-Range": "bytes " + from + "-" + to + "/" + src.length,
    }});
  });
  assert.deepEqual(Buffer.from(await load({bytes: src.length}, new URL("https://example.test/base"), 4)), src);
  assert.deepEqual(calls.sort((a,b) => a-b), [0,4,4,8]);
});

test("device download accepts a complete first response and rejects ranges from the wrong offset", async () => {
  const src = Buffer.from("0123456789");
  let requests = 0;
  const load = rangeLoader(async () => { requests++; return new Response(src); });
  const url = new URL("https://example.test/base");
  assert.deepEqual(Buffer.from(await load({bytes: src.length}, url, 4)), src);
  assert.equal(requests, 1);
  const invalid = rangeLoader(async () => new Response(src.subarray(0,4), {status:206, headers: {
    "Content-Range":"bytes 4-7/10",
  }}));
  await assert.rejects(invalid({bytes:src.length}, url, 4), /Unexpected base model range/);
});

const crypto = require("node:crypto");
const baseUrl = new URL("https://example.test/immutable-model");

function rangeManifest(bytes) {
  const hash = data => crypto.createHash("sha256").update(data).digest("hex");
  const chunkBytes = 4, chunkSha256 = [];
  for (let start = 0; start < bytes.length; start += chunkBytes) {
    chunkSha256.push(hash(bytes.subarray(start, start + chunkBytes)));
  }
  return {bytes: bytes.length, sha256: hash(bytes), chunkBytes, chunkSha256};
}

function cacheStorage() {
  const stores = new Map();
  return {
    stores,
    async open(name) {
      if (!stores.has(name)) stores.set(name, new Map());
      const entries = stores.get(name);
      return {
        async match(key) { return entries.has(key) ? new Response(entries.get(key)) : undefined; },
        async put(key, response) { entries.set(key, Buffer.from(await response.arrayBuffer())); },
        async delete(key) { return entries.delete(key); },
      };
    },
    async delete(name) { return stores.delete(name); },
  };
}

function serveRanges(bytes, calls, responseFor) {
  return async (_url, {headers}) => {
    const [,start,end] = /^bytes=(\d+)-(\d+)$/.exec(headers.Range).map(Number);
    calls.push(start);
    const data = bytes.subarray(start, end + 1);
    return responseFor?.(start, data) ?? new Response(data, {status: 206, headers: {
      "Content-Range": "bytes " + start + "-" + end + "/" + bytes.length,
    }});
  };
}

test("a new worker reads committed parts and fetches only the range interrupted previously", async () => {
  const bytes = Buffer.from("0123456789"), file = rangeManifest(bytes), caches = cacheStorage();
  const interrupted = serveRanges(bytes, [], (start, data) =>
    start === 8 ? new Response(data.subarray(0, 1), {status: 206}) : undefined);
  await assert.rejects(rangeLoader(interrupted, caches)(file, baseUrl), /Incomplete/);
  const entries = [...caches.stores.values()][0];
  assert.deepEqual([...entries.values()].map(b => b.toString()).sort(), ["0123", "4567"]);

  const calls = [];
  const resumed = await rangeLoader(serveRanges(bytes, calls), caches)(file, baseUrl);
  assert.deepEqual(Buffer.from(resumed), bytes);
  assert.deepEqual(calls, [8]);
  const allLocal = rangeLoader(() => assert.fail("all three parts were saved"), caches);
  assert.deepEqual(Buffer.from(await allLocal(file, baseUrl)), bytes);
});

test("recovery repairs only a corrupt cached part and retries a wrong network payload of equal length", async () => {
  const bytes = Buffer.from("0123456789"), file = rangeManifest(bytes), caches = cacheStorage();
  await rangeLoader(serveRanges(bytes, []), caches)(file, baseUrl);
  const entries = [...caches.stores.values()][0];
  const middle = [...entries.keys()].find(key => key.endsWith("/1"));
  entries.set(middle, Buffer.from("xxxx"));
  const calls = [];
  const serve = serveRanges(bytes, calls, () => calls.length === 1 ? new Response("yyyy", {status: 206}) : undefined);
  assert.deepEqual(Buffer.from(await rangeLoader(serve, caches)(file, baseUrl)), bytes);
  assert.deepEqual(calls, [4, 4]);

  const other = Buffer.from("abcdefghij"), changed = [];
  await rangeLoader(serveRanges(other, changed), caches)(rangeManifest(other), baseUrl);
  assert.deepEqual(changed.sort((a,b) => a-b), [0,4,8]);
});

test("a host ignoring a resumed Range can supply one whole response starting at byte zero", async () => {
  const bytes = Buffer.from("0123456789"), file = rangeManifest(bytes), caches = cacheStorage();
  await rangeLoader(serveRanges(bytes, []), caches)(file, baseUrl);
  const entries = [...caches.stores.values()][0];
  for (const key of entries.keys()) if (!key.endsWith("/0")) entries.delete(key);
  const calls = [];
  const serve = serveRanges(bytes, calls, () => new Response(bytes));
  assert.deepEqual(Buffer.from(await rangeLoader(serve, caches)(file, baseUrl)), bytes);
  assert.deepEqual(calls, [4]);
});

test("unavailable or full temporary storage still permits this page to download the base", async () => {
  const bytes = Buffer.from("0123456789"), file = rangeManifest(bytes);
  const denied = {open() { throw new Error("disabled"); }};
  let dropped = false;
  const full = {async open() { return {
    async match() {}, async put() { throw new DOMException("full", "QuotaExceededError"); },
  }; }, async delete() { dropped = true; }};
  for (const caches of [denied, full]) {
    assert.deepEqual(Buffer.from(await rangeLoader(serveRanges(bytes, []), caches)(file, baseUrl)), bytes);
  }
  assert.equal(dropped, true);
});

test("sampling defers only string upper bounds to the full intake validator", () => {
  const fs = require("node:fs"), vm = require("node:vm");
  const source = fs.readFileSync(require.resolve("../../server/static/student-intake-worker.js"), "utf8");
  const context = vm.createContext({self: {fetch() {}}});
  vm.runInContext(source, context);
  const full = require("../../server/static/intake-0p5b-v1/model.json").schema;
  const expected = structuredClone(full);
  delete expected.$defs.Evidence.properties.turn_id.maxLength;
  delete expected.$defs.Evidence.properties.quote.maxLength;
  delete expected.$defs.IntakeFact.properties.record_id.maxLength;
  delete expected.$defs.IntakeFact.properties.value.anyOf.find(v => v.type === "string").maxLength;
  const sampling = JSON.parse(vm.runInContext("samplingSchema", context)(full));
  assert.deepEqual(sampling, expected);
  assert.equal(full.$defs.Evidence.properties.quote.maxLength, 1500);
});
