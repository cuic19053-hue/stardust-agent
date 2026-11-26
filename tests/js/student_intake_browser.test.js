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
