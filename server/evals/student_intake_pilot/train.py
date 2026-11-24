"""Fit Q/V/O LoRA on the final eight frozen-4-bit Qwen blocks. Local files only."""
import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import math
import random
import time

import mlx.core as mx
import mlx.nn as nn
import mlx.optimizers as optim
from mlx.utils import tree_flatten
from mlx_lm.tuner.utils import linear_to_lora_layers

from pathlib import Path

from .data import DATA, digest, split_cases, target, write_new
from .model import ADAPTER, CONTEXT, PROMPT, SEED, Chat, base_model, compact, output_logits

EPOCHS = 3


def examples(chat, split):
    result = []
    for c in split_cases(split):
        prompt = chat.prompt(c["turns"])
        answer = chat.encode(compact(target(c))) + [chat.end]
        if len(prompt) + len(answer) > CONTEXT:
            raise ValueError(f"overlong example {c['id']}")
        result.append((c["id"], mx.array(prompt + answer)[None], len(prompt) - 1, len(answer)))
    return result


def loss(model, ids, start):
    # The transformer sees all preceding tokens. Only assistant targets (including EOS)
    # get loss. Projecting only those positions avoids a prompt-length x vocab allocation.
    hidden = model.model(ids[:, :-1])[:, start:, :]
    logits = output_logits(model, hidden)
    return nn.losses.cross_entropy(logits.astype(mx.float32), ids[:, start+1:], reduction="mean")


def validation(model, items):
    model.eval()
    total = sum(n for _, _, _, n in items)
    result = sum(loss(model, ids, start).item() * n for _, ids, start, n in items) / total
    model.train()
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    mx.random.seed(SEED)
    mx.set_cache_limit(256 * 1024**2)
    chat = Chat(args.base)
    train, val = examples(chat, "train"), examples(chat, "validation")
    model, spec = base_model(args.base)
    model.freeze()
    linear_to_lora_layers(model, ADAPTER["num_layers"], ADAPTER["lora_parameters"])
    params = tree_flatten(model.trainable_parameters())
    assert all(k.endswith(("lora_a", "lora_b")) for k, _ in params)
    run = {"created_utc": datetime.now(timezone.utc).isoformat(), "model": spec.model, "revision": spec.revision,
           "base_sha256": spec.files, "dataset_manifest_sha256": digest(DATA/"manifest.json"),
           "source_sha256": {f: digest(Path(__file__).with_name(f)) for f in ("train.py","model.py","data.py")},
           "runtime": {n: version(n) for n in ("mlx", "mlx-lm", "transformers", "tokenizers", "numpy", "llguidance")},
           "seed": SEED, "epochs": EPOCHS, "optimizer": "AdamW", "lr": spec.learning_rate, "weight_decay": 0.01,
           "batch": 1, "accumulation": 1, "context": CONTEXT, "trainable_parameters": sum(v.size for _,v in params),
           "layers": ADAPTER, "loss": "mean next-token CE on assistant JSON and EOS only",
           "select": "lowest token-weighted validation NLL after a full epoch, earliest tie",
           "train_cases":len(train), "validation_cases":len(val), "train_target_tokens_per_epoch":sum(x[3] for x in train),
           "validation_target_tokens":sum(x[3] for x in val)}
    write_new(args.out / "config.json", run)
    (args.out / "system-prompt.txt").write_text(PROMPT)
    optimizer = optim.AdamW(learning_rate=spec.learning_rate, weight_decay=0.01)
    loss_grad = nn.value_and_grad(model, loss)
    val_base = validation(model, val)
    print(f"base validation NLL {val_base:.4f}", flush=True)
    history = []
    rng = random.Random(SEED)
    t0 = time.perf_counter()
    with (args.out/"steps.jsonl").open("x") as log:
        for epoch in range(1, EPOCHS+1):
            order = list(train)
            rng.shuffle(order)
            nll = 0.0
            for step, (key, ids, start, n) in enumerate(order, 1):
                v, grad = loss_grad(model, ids, start)
                optimizer.update(model, grad)
                mx.eval(model.trainable_parameters(), optimizer.state, v)
                value = v.item()
                if not math.isfinite(value):
                    raise FloatingPointError(key)
                nll += value * n
                log.write(compact({"epoch":epoch,"step":step,"id":key,"answer_tokens":n,"loss":value})+"\n")
                log.flush()
                if step % 20 == 0:
                    print(f"epoch {epoch} step {step} loss {value:.4f}", flush=True)
            vloss = validation(model, val)
            if not math.isfinite(vloss):
                raise FloatingPointError("validation")
            folder = args.out / f"epoch-{epoch}"
            folder.mkdir()
            write_new(folder / "adapter_config.json", ADAPTER)
            checkpoint = folder / "adapters.safetensors"
            mx.save_safetensors(str(checkpoint), dict(tree_flatten(model.trainable_parameters())))
            history.append({"epoch": epoch, "train_nll": nll/run["train_target_tokens_per_epoch"],
                            "validation_nll": vloss, "sha256":digest(checkpoint)})
            print(history[-1], flush=True)
    selected = min(history, key=lambda row: row["validation_nll"])
    write_new(args.out / "selection.json", {"base_validation_nll":val_base,"epochs":history,
              "selected":selected, "fit_wall_seconds":time.perf_counter()-t0,
              "peak_mlx_gib":mx.get_peak_memory()/1024**3})


if __name__ == "__main__":
    main()
