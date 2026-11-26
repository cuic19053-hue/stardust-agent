/* Repack the pinned MLX affine groups of 64 into MLC signed groups of 32.
   The original CPU export rounds through fp16 before quantizing each new group. */
const smallest = new Uint8Array(256), largest = new Uint8Array(256);
for (let byte = 0; byte < 256; byte++) {
  smallest[byte] = Math.min(byte & 15, byte >>> 4);
  largest[byte] = Math.max(byte & 15, byte >>> 4);
}

// WebGPU shader-f16 also exists on browsers predating JavaScript Float16Array.
const halfValues = new Float32Array(65536);
for (let h = 0; h < halfValues.length; h++) {
  const exponent = (h >>> 10) & 31, mantissa = h & 1023;
  let magnitude = exponent ? 2 ** (exponent - 15) * (1 + mantissa / 1024) : mantissa * 2 ** -24;
  if (exponent === 31) magnitude = mantissa ? NaN : Infinity;
  halfValues[h] = h & 32768 ? -magnitude : magnitude;
}
const scratch = new Float32Array(1), wordBits = new Uint32Array(scratch.buffer);
function halfBits(value) {
  scratch[0] = value;
  const word = wordBits[0], sign = (word >>> 16) & 32768;
  const exponent = (word >>> 23) & 255, mantissa = word & 0x7fffff;
  if (exponent === 255) return sign | (mantissa ? 0x7e00 : 0x7c00);
  if (exponent > 142) return sign | 0x7c00;
  if (exponent < 102) return sign;
  const normal = exponent >= 113, shift = normal ? 13 : 126 - exponent;
  const significand = normal ? mantissa : mantissa | 0x800000;
  const top = (normal ? (exponent - 112) * 1024 : 0) + (significand >>> shift);
  const tail = significand & ((1 << shift) - 1), halfway = 1 << (shift - 1);
  return sign | (top + Number(tail > halfway || (tail === halfway && (top & 1))));
}
const roundHalf = Math.f16round || (value => halfValues[halfBits(value)]);

function requantize(words, scales, biases) {
  const packed = new Uint32Array(words.length);
  const quantScales = new Uint16Array(words.length / 4);
  const values = new Float32Array(16), codes = new Uint32Array(16);
  for (let group = 0; group < scales.length; group++) {
    const oldScale = halfValues[scales[group]], bias = halfValues[biases[group]];
    for (let i = 0; i < 16; i++) values[i] = roundHalf(Math.fround(i * oldScale + bias));
    for (let half = 0; half < 2; half++) {
      const offset = group * 8 + half * 4;
      let low = 15, high = 0;
      for (let j = 0; j < 4; j++) {
        let word = words[offset + j];
        for (let k = 0; k < 4; k++, word >>>= 8) {
          low = Math.min(low, smallest[word & 255]);
          high = Math.max(high, largest[word & 255]);
        }
      }
      const max = Math.max(Math.abs(values[low]), Math.abs(values[high]));
      const bits = halfBits(Math.fround(max / 7));
      quantScales[group * 2 + half] = bits;
      const scale = halfValues[bits];
      for (let i = low; i <= high; i++) {
        const code = Math.floor(Math.fround(Math.fround(values[i] / (scale || 1)) + 7.5));
        codes[i] = Math.min(14, Math.max(0, code));
      }
      for (let j = 0; j < 4; j++) {
        let old = words[offset + j], next = 0;
        for (let bit = 0; bit < 32; bit += 4, old >>>= 4) next |= codes[old & 15] << bit;
        packed[offset + j] = next;
      }
    }
  }
  return { q_weight: new Uint8Array(packed.buffer), q_scale: new Uint8Array(quantScales.buffer) };
}

function components(prefix) {
  if (prefix.endsWith(".self_attn.c_attn")) return ["q", "k", "v"].map(k => prefix.replace(/c_attn$/, k + "_proj"));
  if (prefix.endsWith(".mlp.gate_up_proj")) return ["gate", "up"].map(k => prefix.replace(/gate_up_proj$/, k + "_proj"));
  return [prefix];
}

export function createAssembler(source, layout) {
  const size = Number(new DataView(source).getBigUint64(0, true)), start = size + 8;
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(source, 8, size)));
  const pending = new Map();
  function tensor(name) {
    const { dtype, data_offsets: [from, to] } = header[name];
    const Type = dtype === "F16" ? Uint16Array : dtype === "U32" ? Uint32Array : null;
    if (!Type) throw new Error("Unexpected base tensor dtype");
    return new Type(source, start + from, (to - from) / Type.BYTES_PER_ELEMENT);
  }
  function piece(prefix, suffix) {
    if (suffix !== "q_weight" && suffix !== "q_scale") {
      const view = tensor(prefix + "." + suffix);
      return new Uint8Array(view.buffer, view.byteOffset, view.byteLength);
    }
    let pair = pending.get(prefix);
    if (!pair?.[suffix]) {
      pair = requantize(tensor(prefix + ".weight"), tensor(prefix + ".scales"), tensor(prefix + ".biases"));
      pending.set(prefix, pair);
    }
    const data = pair[suffix];
    delete pair[suffix];
    if (!pair.q_weight && !pair.q_scale) pending.delete(prefix);
    return data;
  }
  return (shardName) => {
    const shard = layout.records.find(entry => entry.dataPath === shardName);
    if (!shard) throw new Error("Unknown weight shard");
    const bytes = new Uint8Array(shard.nbytes);
    for (const record of shard.records) {
      const last = record.name.lastIndexOf("."), suffix = record.name.slice(last + 1);
      let position = record.byteOffset;
      for (const prefix of components(record.name.slice(0, last))) {
        const data = piece(prefix, suffix);
        bytes.set(data, position); position += data.length;
      }
      if (position !== record.byteOffset + record.nbytes) throw new Error("Invalid tensor layout");
    }
    return bytes;
  };
}
