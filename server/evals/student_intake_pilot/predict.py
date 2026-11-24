"""Greedy schema-constrained prediction. Inputs contain only dialogue and system instructions."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import time

import mlx.core as mx
from llguidance import LLMatcher, LLTokenizer
from llguidance.mlx import allocate_token_bitmask, apply_token_bitmask, fill_next_token_bitmask
from mlx_lm.models.cache import make_prompt_cache
from mlx_lm.tuner.utils import load_adapters

from server.evals.student_intake_baseline import DATASET, load_cases
from server.proposal.student_intake import IntakeCandidate, SYSTEM_PROMPT
from .data import DATA, digest, split_cases, write_new
from .model import CONTEXT, MAX_NEW, PROMPT, SEED, Chat, base_model, compact, output_logits


class Decoder:
    def __init__(self, base):
        self.chat = Chat(base)
        self.model, self.spec = base_model(base)
        self.model.eval()
        self.vocab = self.model.args.vocab_size
        self.tokenizer = LLTokenizer((base / "tokenizer.json").read_text(), n_vocab=self.vocab, eos_token=self.chat.end)
        self.grammar = LLMatcher.grammar_from_json_schema(
            IntakeCandidate.model_json_schema(), overrides={"whitespace_flexible": False})
        issue = LLMatcher.validate_grammar(self.grammar, self.tokenizer)
        if issue:
            raise ValueError(issue)

    def generate(self, turns, prompt):
        ids = self.chat.prompt(turns, prompt)
        if len(ids) + MAX_NEW > CONTEXT:
            raise ValueError("input exceeds configured context")
        emitted = []
        cache = make_prompt_cache(self.model)
        matcher = LLMatcher(self.tokenizer, self.grammar, log_level=0)
        mask = allocate_token_bitmask(1, self.vocab)
        pending = ids
        reason = "token_limit"
        while len(emitted) < MAX_NEW:
            # Feed forced JSON syntax to the model as a block; subsequent scores
            # see exactly the same context as token-at-a-time generation.
            forced = matcher.compute_ff_tokens()
            if forced:
                span = forced[:MAX_NEW-len(emitted)]
                if not matcher.consume_tokens(span):
                    raise ValueError(matcher.get_error())
                pending = pending + span
                emitted.extend(span)
            if matcher.is_accepting():
                reason = "complete"
                break
            if matcher.is_error():
                raise ValueError(matcher.get_error())
            if len(emitted) >= MAX_NEW:
                break
            # The tied output head only needs the last hidden position.
            hidden = self.model.model(mx.array(pending)[None], cache=cache)
            logits = output_logits(self.model, hidden[:, -1, :])
            fill_next_token_bitmask(matcher, mask)
            token = mx.argmax(apply_token_bitmask(logits, mask), axis=-1).item()
            if not matcher.consume_token(token):
                raise ValueError(matcher.get_error())
            if token == self.chat.end:
                reason = "complete" if matcher.is_accepting() else "premature_eos"
                break
            emitted.append(token)
            pending = [token]
            if matcher.is_accepting():
                reason = "complete"
                break
        return {"raw": self.chat.tok.decode(emitted, skip_special_tokens=False), "finish": reason,
                "prompt_tokens": len(ids), "output_tokens": len(emitted)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--fit", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--split", choices=("test","validation","regression"), required=True)
    parser.add_argument("--prompt", choices=("original","revised"), default="revised")
    args = parser.parse_args()
    cases = load_cases()["cases"] if args.split == "regression" else split_cases(args.split)
    mx.random.seed(SEED)
    mx.set_cache_limit(256 * 1024**2)
    engine = Decoder(args.base)
    selected = None
    if args.fit:
        if json.loads((args.fit / "config.json").read_text())["base_sha256"] != engine.spec.files:
            raise ValueError("checkpoint trained on a different base")
        selected = json.loads((args.fit / "selection.json").read_text())["selected"]
        checkpoint = args.fit / f"epoch-{selected['epoch']}"
        if digest(checkpoint / "adapters.safetensors") != selected["sha256"]:
            raise ValueError("adapter hash mismatch")
        load_adapters(engine.model, checkpoint)
        engine.model.eval()
    prompt = PROMPT if args.prompt == "revised" else SYSTEM_PROMPT
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "system-prompt.txt").write_text(prompt)
    write_new(args.out / "schema.json", IntakeCandidate.model_json_schema())
    write_new(args.out / "manifest.json", {
        "created_utc": datetime.now(timezone.utc).isoformat(), "model":engine.spec.model,"revision":engine.spec.revision,
        "base":engine.spec.files,"selected":selected,"seed":SEED,"split":args.split,"prompt":args.prompt,
        "prompt_sha256":digest(args.out / "system-prompt.txt"),"schema_sha256":digest(args.out/"schema.json"),
        "dataset_sha256":digest(DATASET if args.split=="regression" else DATA/f"{args.split}.jsonl"),
        "source_sha256":{f:digest(Path(__file__).with_name(f)) for f in ("predict.py","model.py","data.py")},
        "decode":"greedy, compact JSON Schema, forced syntax, stop on accepting root", "context":CONTEXT,
        "max_new":MAX_NEW,"retries":0,"case_ids":[c["id"] for c in cases]})
    with (args.out / "predictions.jsonl").open("x") as writer:
        for case in cases:
            start = time.perf_counter()
            try:
                row = engine.generate(case["turns"], prompt)
            except Exception as exc:
                row = {"raw":"","finish":"error","error":f"{type(exc).__name__}: {exc}"}
            row.update(id=case["id"], turns=case["turns"], wall_seconds=round(time.perf_counter()-start, 4))
            writer.write(compact(row)+"\n")
            writer.flush()
            print(case["id"], row["finish"], row.get("output_tokens"), row["wall_seconds"], flush=True)


if __name__ == "__main__":
    main()
