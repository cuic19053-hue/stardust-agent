"""Reproduce the public examples with the same MLX decoder before and after LoRA."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import mlx.core as mx
from mlx_lm.tuner.utils import load_adapters

from .data import digest, write_new
from .guard import inspect_candidate
from .model import PROMPT, SEED
from .predict import Decoder
from server.proposal.student_intake import FIELDS

# Written specifically for public demonstration. These examples are fictional
# and are separate from every local training, validation and evaluation split.
EXAMPLES = [
    ("成绩与获奖", ["我读环境设计专业。校园英语配音赛我拿了二等奖。"]),
    ("两份预算", ["我的项目预算是8400元。", "我看到另一份表，项目预算是4800元。"]),
    ("尚在计划中的作品", ["我准备做一面校园声景地图，还没开始录音。"]),
    ("范文里的论文", ["我读到样表写着“我已经发表过两篇论文”。我本人还没有发表过论文。"]),
    ("国奖与家庭信息", ["我正在申请国家奖学金。家庭人数我不想透露。"]),
]
CHECKPOINT = "62a732c6f8c870fa8abd18ca0412df988af03c0e2b53341bea590e17304d299c"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if digest(args.checkpoint / "adapters.safetensors") != CHECKPOINT:
        raise ValueError("unexpected adapter")
    mx.random.seed(SEED)
    mx.set_cache_limit(256 * 1024**2)
    engine = Decoder(args.base)
    examples = [{"title": title, "turns": [
        {"role": "user", "turn_id": f"u{i}", "content": content}
        for i, content in enumerate(contents, 1)
    ]} for title, contents in EXAMPLES]
    for condition in ("base", "tuned"):
        if condition == "tuned":
            load_adapters(engine.model, args.checkpoint)
            engine.model.eval()
        for example in examples:
            output = engine.generate(example["turns"], PROMPT)
            example[condition] = {
                **output, "review": inspect_candidate(output["raw"], example["turns"])
            }
            print(condition, example["title"], output["finish"], flush=True)
    write_new(args.out, {"synthetic": True, "purpose": "public demonstration",
        "created_utc": datetime.now(timezone.utc).isoformat(), "model": engine.spec.model,
        "revision": engine.spec.revision, "adapter_sha256": CHECKPOINT, "epoch": 3,
        "labels": {field: info[0] for field, info in FIELDS.items()}, "examples": examples})


if __name__ == "__main__":
    main()
