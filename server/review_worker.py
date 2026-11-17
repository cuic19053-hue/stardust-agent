"""Run: python server/review_worker.py --local-data /absolute/path/.agent-data"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="本地草稿预审 worker（不用于云端生产）")
    parser.add_argument("--local-data", type=Path, required=True)
    parser.add_argument("--once", action="store_true", help="处理一个任务后退出")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("本地预审 worker 目前支持 macOS 和 Linux")
    if os.environ.get("SUPABASE_URL") or os.environ.get("VERCEL"):
        parser.error("此入口仅用于本地开发，云端生产须接入独立生产队列")
    if not args.local_data.is_absolute():
        parser.error("--local-data 必须是绝对路径")
    from proposal.review_worker import LocalReviewModel, ReviewWorker
    from proposal.storage import Store
    with ReviewWorker(Store(args.local_data), model_call=LocalReviewModel()) as worker:
        while True:
            result = worker.run_once()
            if args.once:
                return
            if result is None:
                time.sleep(1)


if __name__ == "__main__":
    main()
