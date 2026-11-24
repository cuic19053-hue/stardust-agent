"""Paired gains/losses over the same cases. Invalid and abstained rows stay in the denominator."""
import argparse
from pathlib import Path

from .data import read_jsonl, write_new


def fields_f1(s):
    denom = 2*s["tp"]+s["fp"]+s["fn"]
    return 2*s["tp"]/denom if denom else float(s["case_exact"])


def comparison(before, after, mode):
    b = [r for r in read_jsonl(before/"scored.jsonl") if r["mode"]==mode]
    a = [r for r in read_jsonl(after/"scored.jsonl") if r["mode"]==mode]
    if not b or [r["case_id"] for r in b] != [r["case_id"] for r in a]:
        raise ValueError("paired samples must match exactly")
    pairs = []
    for pre, post in zip(b,a,strict=True):
        bs,ps = pre["score"],post["score"]
        pairs.append({"id":pre["case_id"],"tags":pre["tags"],
            "field_f1_before":fields_f1(bs),"field_f1_after":fields_f1(ps),
            "legacy_exact_before":bs["case_exact"],"legacy_exact_after":ps["case_exact"],
            "strict_before":pre["review"]["strict_v2"],"strict_after":post["review"]["strict_v2"],
            "grounded_delta":post["review"]["grounded_matches"]-pre["review"]["grounded_matches"],
            "new_hazards":sorted(set(post["review"]["hazards"])-set(pre["review"]["hazards"])),
            "removed_hazards":sorted(set(pre["review"]["hazards"])-set(post["review"]["hazards"]))})
    return {"cases":len(pairs),"exact_gains":[r["id"] for r in pairs if r["strict_after"] and not r["strict_before"]],
            "exact_losses":[r["id"] for r in pairs if r["strict_before"] and not r["strict_after"]],
            "field_f1_gains":[r["id"] for r in pairs if r["field_f1_after"]>r["field_f1_before"]],
            "field_f1_losses":[r["id"] for r in pairs if r["field_f1_after"]<r["field_f1_before"]],"pairs":pairs}


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("before","after","out"):
        parser.add_argument("--"+key,type=Path,required=True)
    args=parser.parse_args()
    write_new(args.out,{m:comparison(args.before,args.after,m) for m in ("raw","guarded")})
