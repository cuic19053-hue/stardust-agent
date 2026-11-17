"""Reproducible synthetic review demo; never opens an existing project database."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))


def main():
    parser = argparse.ArgumentParser(description="用合成材料验证本地 LangGraph 预审")
    parser.add_argument("--model", action="store_true", help="额外调用本机 Ollama；默认只运行规则检查")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from proposal.review_runs import ReviewRuns
    from proposal.review_worker import LocalReviewModel, ReviewWorker
    from proposal.storage import Store

    with tempfile.TemporaryDirectory(prefix="stardust-review-demo-") as folder:
        store = Store(folder)
        project = store.create_project("合成演示：校园节能研究", school="合成大学")
        project = store.update_project(project["id"], state={"skill_id": "innovation-research", "budget": "2000元"},
            outline=[{"id": "method", "title": "研究方法"}, {"id": "budget", "title": "经费预算"}],
            sections={"method": "本项目拟研究教室照明节能。我们已经完成全部对比实验，准确率达到98%。目前尚未整理实验记录，样本来源待补充。",
                      "budget": "预算合计为3000元。设备费1000元，材料费500元，其他费用待补充。"})
        notice = "[第 2 页] 合成大学创新训练申报研究要求：已完成实验必须附原始记录；预算应与项目基本信息一致。"
        doc = store.add_document(project["id"], "合成学校通知.txt", "synthetic-only", "source", "text/plain")
        store.update_document_extraction(doc["id"], notice, "completed", confirmed=True, confirmed_by_user=True)
        store.replace_chunks(doc["id"], [{"source_ref": "第 2 页", "content": notice}], expected_text=notice)
        runs = ReviewRuns(store)
        submitted = runs.submit(project["id"], "demo-one-request", runs.current(project["id"]),
                                "model" if args.model else "rules", os.environ.get("OLLAMA_MODEL", "qwen2.5:7b") if args.model else "")
        before = store.get_project(project["id"])
        with ReviewWorker(store, model_call=LocalReviewModel() if args.model else None) as worker:
            result = worker.run_once()
        verified = {"synthetic_only": True, "draft_unchanged": store.get_project(project["id"]) == before,
                    "same_run_id": result["id"] == submitted["id"],
                    "requested_checks_completed": bool((result.get("report") or {}).get("complete")), "result": result}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(verified, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "draft_unchanged": verified["draft_unchanged"],
                      "model_status": (result.get("report") or {}).get("model_status"),
                      "issue_count": len((result.get("report") or {}).get("issues", [])),
                      "output": str(args.output.resolve())}, ensure_ascii=False))
    if result["status"] != "completed" or not verified["draft_unchanged"] or not verified["requested_checks_completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
