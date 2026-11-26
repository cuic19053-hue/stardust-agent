"""外部存活拨测：只访问无副作用的生产 ping，不调用模型或数据库。"""
import argparse
import json
import os
from pathlib import Path
import re
import time
import urllib.request

URL = "https://agent.stardust.website/api/ping"
COMMIT_URL = "https://github.com/cuic19053-hue/stardust-agent/commit/"
SHA = re.compile(r"[0-9a-f]{40}")


def check(url, expected_commit, attempts):
    last_error = "未取得响应"
    for attempt in range(attempts):
        started = time.monotonic()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Stardust-Uptime/1.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read(4096))
                if response.status != 200 or not isinstance(payload, dict) or payload.get("status") != "ok":
                    raise ValueError("存活响应格式不符")
            commit = payload.get("commit_sha", "")
            if not (isinstance(commit, str) and SHA.fullmatch(commit)
                    and payload.get("commit_url") == COMMIT_URL + commit):
                commit = None
            if expected_commit and commit != expected_commit:
                raise ValueError(f"期望提交 {expected_commit}，实测 {commit or '未报告完整提交号'}")
            detail = f"提交 {commit}" if commit else "未报告完整提交号"
            print(f"健康检查通过，响应耗时 {time.monotonic() - started:.2f}s，{detail}")
            return True, detail
        except (OSError, ValueError) as exc:
            last_error = str(exc)
            print(f"健康检查第 {attempt + 1} 次失败：{last_error}")
            if attempt + 1 < attempts:
                time.sleep(2 ** (attempt + 1))
    return False, last_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--expected-commit")
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    if args.expected_commit is not None and not SHA.fullmatch(args.expected_commit):
        parser.error("--expected-commit 须为 40 位小写十六进制 Git SHA")
    if not 1 <= args.attempts <= 3:
        parser.error("--attempts 须为 1 到 3")
    ok, detail = check(args.url, args.expected_commit, args.attempts)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(f"## 生产站点健康检查\n\n{'✅ 正常' if ok else '❌ 未通过'} · {detail}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
