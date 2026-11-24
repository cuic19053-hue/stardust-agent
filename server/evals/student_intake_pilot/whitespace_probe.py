"""Preregistered decoder sensitivity check using the same adapter, prompt and prediction code."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from llguidance import LLMatcher
import mlx.core as mx
from mlx_lm.tuner.utils import load_adapters

from server.proposal.student_intake import IntakeCandidate
from .data import DATA, digest, split_cases, write_new
from .model import CONTEXT, MAX_NEW, PROMPT, SEED, compact
from .predict import Decoder


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base",type=Path,required=True)
    parser.add_argument("--fit",type=Path)
    parser.add_argument("--split",choices=("validation","test"),required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    mx.random.seed(SEED)
    mx.set_cache_limit(256 * 1024**2)
    engine=Decoder(args.base)
    schema=IntakeCandidate.model_json_schema()
    engine.grammar=LLMatcher.grammar_from_json_schema(schema,overrides={"whitespace_flexible":True})
    issue=LLMatcher.validate_grammar(engine.grammar,engine.tokenizer)
    if issue:
        raise ValueError(issue)
    selection=None
    if args.fit:
        cfg=json.loads((args.fit/"config.json").read_text())
        if cfg["base_sha256"]!=engine.spec.files:
            raise ValueError("base mismatch")
        selection=json.loads((args.fit/"selection.json").read_text())["selected"]
        checkpoint=args.fit/f"epoch-{selection['epoch']}"
        if digest(checkpoint/"adapters.safetensors")!=selection["sha256"]:
            raise ValueError("checkpoint mismatch")
        load_adapters(engine.model,checkpoint)
        engine.model.eval()
    cases=split_cases(args.split)
    args.out.mkdir(parents=True,exist_ok=False)
    (args.out/"system-prompt.txt").write_text(PROMPT)
    write_new(args.out/"schema.json",schema)
    write_new(args.out/"manifest.json",{"created_utc":datetime.now(timezone.utc).isoformat(),
        "model":engine.spec.model,"revision":engine.spec.revision,"base":engine.spec.files,"selected":selection,
        "split":args.split,"dataset_sha256":digest(DATA/f"{args.split}.jsonl"),"case_ids":[c["id"] for c in cases],
        "prompt_sha256":digest(args.out/"system-prompt.txt"),"schema_sha256":digest(args.out/"schema.json"),
        "source_sha256":{f:digest(Path(__file__).with_name(f)) for f in ("model.py","predict.py","whitespace_probe.py")},
        "decode":"greedy, same schema and forced syntax, whitespace_flexible=True, stop on accepting root",
        "seed":SEED,"max_new":MAX_NEW,"context":CONTEXT,"retries":0})
    with (args.out/"predictions.jsonl").open("x") as writer:
        for c in cases:
            t=time.perf_counter()
            try:
                row=engine.generate(c["turns"],PROMPT)
            except Exception as exc:
                row={"raw":"","finish":"error","error":f"{type(exc).__name__}: {exc}"}
            row.update(id=c["id"],turns=c["turns"],wall_seconds=round(time.perf_counter()-t,4))
            writer.write(compact(row)+"\n")
            writer.flush()
            print(c["id"],row["finish"],row.get("output_tokens"),row["wall_seconds"],flush=True)


if __name__=="__main__":
    main()
