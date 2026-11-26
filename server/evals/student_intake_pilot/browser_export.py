"""Reproduce the deployed Qwen MLX-to-MLC conversion and publish only LoRA differences."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np
from safetensors.numpy import load_file


BASE = "https://huggingface.co/mlx-community/Qwen2.5-0.5B-Instruct-4bit/resolve/a5339a4131f135d0fdc6a5c8b5bbed2753bbe0f3/"
BASE_SHA = "ddffab9cbc7bf6dde941c6724841eeca8981fcfa81ca20ff8efff1396326d153"
ADAPTER_SHA = "f0c50e648d8b57fdc0c9118e52e92b4ee8e7e024190a277a68850486b58f6fa7"
RUNTIME = "https://cdn.jsdelivr.net/npm/@mlc-ai/web-llm@0.2.89/lib/index.js"
WASM = ("https://cdn.jsdelivr.net/gh/mlc-ai/binary-mlc-llm-libs@5b7251f3bdce957be2a7bf73a6f6c560d9874df4/"
        "web-llm-models/v0_2_87/base/Qwen2-0.5B-Instruct-q4f16_1_cs1k-webgpu.wasm")
BASE_CHUNK_BYTES = 16 * 1024 * 1024
SHIFTS = np.arange(8, dtype=np.uint32) * 4


def sha(data):
    return hashlib.sha256(data).hexdigest()


def describe(data, url):
    return {"url": url, "bytes": len(data), "sha256": sha(data)}


def describe_base(data):
    source = describe(data, BASE + "model.safetensors")
    view = memoryview(data)
    source["chunkBytes"] = BASE_CHUNK_BYTES
    source["chunkSha256"] = [sha(view[start:start + BASE_CHUNK_BYTES])
                             for start in range(0, len(view), BASE_CHUNK_BYTES)]
    return source


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def quant32(weights):
    groups = weights.reshape(len(weights), -1, 32).astype(np.float32)
    scales = (np.abs(groups).max(axis=-1) / 7).astype("<f2")
    units = np.where(scales == 0, 1, scales).astype(np.float32)
    codes = np.clip(np.floor(groups / units[:, :, None] + 7.5), 0, 14).astype("<u4")
    return np.bitwise_or.reduce(codes.reshape(len(weights), -1, 8) << SHIFTS, axis=-1), scales


def components(prefix):
    if prefix.endswith(".self_attn.c_attn"):
        return [prefix.rsplit(".", 1)[0] + "." + k + "_proj" for k in ("q", "k", "v")]
    if prefix.endswith(".mlp.gate_up_proj"):
        return [prefix.rsplit(".", 1)[0] + "." + k + "_proj" for k in ("gate", "up")]
    return [prefix]


def export(assets, pilot, output):
    weight_path = pilot / "base/model.safetensors"
    checkpoint = pilot / "fit/epoch-3/adapters.safetensors"
    source = describe_base(weight_path.read_bytes())
    if source["sha256"] != BASE_SHA or sha(checkpoint.read_bytes()) != ADAPTER_SHA:
        raise ValueError("unexpected base or adapter")
    config = json.loads((pilot / "fit/epoch-3/adapter_config.json").read_text())
    assert config["lora_parameters"]["scale"] == 2 and config["lora_parameters"]["rank"] == 8
    base, adapter = load_file(weight_path), load_file(checkpoint)
    layout = json.loads((assets / "tensor-cache.json").read_text())
    pending, changed = {}, set()

    def quantized(prefix):
        packed = base[prefix + ".weight"]
        rows, columns = packed.shape
        plain = np.empty_like(packed), np.empty((rows, columns // 4), "<f2")
        tuned = tuple(np.empty_like(m) for m in plain) if prefix + ".lora_a" in adapter else plain
        for start in range(0, rows, 256):
            span = slice(start, min(rows, start + 256))
            codes = ((packed[span, :, None] >> SHIFTS) & 15).astype(np.float32).reshape(len(packed[span]), -1, 64)
            rows_f16 = (codes * base[prefix+".scales"][span, :, None] + base[prefix+".biases"][span, :, None])
            values = rows_f16.astype("<f2").reshape(len(packed[span]), -1)
            plain[0][span], plain[1][span] = quant32(values)
            if tuned is not plain:
                delta = (2 * adapter[prefix+".lora_b"].T[span] @ adapter[prefix+".lora_a"].T).astype("<f2")
                tuned[0][span], tuned[1][span] = quant32(values + delta)
        if tuned is not plain and any(not np.array_equal(m, n) for m, n in zip(plain, tuned)):
            changed.add(prefix)
        return {"q_weight": (plain[0], tuned[0]), "q_scale": (plain[1], tuned[1])}

    output.mkdir(parents=True, exist_ok=True)
    files = {}
    for shard in layout["records"]:
        name = shard["dataPath"]
        original, adapted = bytearray(), bytearray()
        for item in shard["records"]:
            assert len(adapted) == item["byteOffset"]
            prefix, suffix = item["name"].rsplit(".", 1)
            for part in components(prefix):
                if suffix in ("q_weight", "q_scale"):
                    if part not in pending:
                        pending[part] = quantized(part)
                    a, b = pending[part].pop(suffix)
                    if not pending[part]:
                        del pending[part]
                else:
                    a = b = base[part + "." + suffix]
                original.extend(memoryview(np.ascontiguousarray(a)).cast("B"))
                adapted.extend(memoryview(np.ascontiguousarray(b)).cast("B"))
            assert len(adapted) == item["byteOffset"] + item["nbytes"]
        assert len(adapted) == shard["nbytes"]
        info = {"bytes": len(original), "sha256": sha(adapted), "fromBase": True}
        if original != adapted:
            xor = np.bitwise_xor(np.frombuffer(original, np.uint8), np.frombuffer(adapted, np.uint8))
            compressed = gzip.compress(xor.tobytes(), compresslevel=9, mtime=0)
            patch = name.removesuffix(".bin") + "-" + sha(compressed)[:12] + ".xor.gz"
            (output / patch).write_bytes(compressed)
            info["patch"] = describe(compressed, "./" + patch)
        files[name] = info
        shard["md5sum"] = hashlib.md5(adapted).hexdigest()
    assert len(changed) == 24 and not pending
    def local(name, obj):
        dump(output / name, obj)
        return describe((output / name).read_bytes(), "./" + name)
    files["ndarray-cache.json"] = files["tensor-cache.json"] = local("layout.json", layout)
    chat = json.loads((assets / "mlc-chat-config.json").read_text())
    chat["tokenizer_files"] = ["tokenizer.json", "tokenizer_config.json"]
    files["mlc-chat-config.json"] = local("chat.json", chat)
    for name in chat["tokenizer_files"]:
        files[name] = describe((pilot / "base" / name).read_bytes(), BASE + name)
    runtime = describe((assets / "runtime.js").read_bytes(), RUNTIME)
    files["edge.wasm"] = describe((assets / "edge.wasm").read_bytes(), WASM)
    assert runtime["sha256"] == "2655840c7eb90cd8b310a3e3394a91fae30d20012fd8274e9291d688e88325b5"
    assert files["edge.wasm"]["sha256"] == "2a203aa9280044fd4b6d279c2c8bf3ef11e9e450435b973ef494e5d461e4f2dd"
    assembler = describe((output / "assemble.mjs").read_bytes(), "./assemble.mjs")
    fingerprint = sha(json.dumps(files, sort_keys=True).encode())[:16]
    dump(output/"model.json", {"version":"qwen-intake-05b-"+fingerprint, "adapterSha256":ADAPTER_SHA,
                               "runtime":runtime, "source":source, "assembler":assembler, "files":files,
                               "prompt":(pilot/"fit/system-prompt.txt").read_text(),
                               "schema":json.loads((pilot/"lora-validation/schema.json").read_text())})
    print(len(changed), "merged projections,", sum(f["patch"]["bytes"] for f in files.values() if "patch" in f), "patch bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("assets", "pilot", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    export(args.assets, args.pilot, args.output)
