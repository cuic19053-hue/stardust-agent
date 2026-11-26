"""String boundary for the shared intake rules inside a browser Python worker."""
import json

from .student_intake import follow_up_questions
from .student_intake_review import model_turns, present, verified_review

MAX_CHARS = 600
MAX_TURNS = 12


def prepare(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or len(text) > MAX_CHARS or len(lines) > MAX_TURNS:
        raise ValueError("请输入 1 至 600 字的口述，最多分成 12 行。")
    if "<|" in text or "|>" in text:
        raise ValueError("请移除模型控制标记，再提取口述。")
    turns = [{"turn_id": f"u{i}", "role": "user", "content": line} for i, line in enumerate(lines, 1)]
    visible = model_turns(turns)
    message = json.dumps({"turns": visible}, ensure_ascii=False)
    return json.dumps({"original": turns, "visible": visible, "message": message}, ensure_ascii=False)


def review(prepared, answer, finish):
    source = json.loads(prepared)
    result = {"decision": "needs_manual_review", "facts": [], "questions": []}
    if finish == "stop" and source["visible"]:
        candidate, decision = verified_review(answer, source["visible"], source["original"])
        if candidate is not None:
            result = {"decision": decision, "facts": [present(f) for f in candidate.facts],
                      "questions": follow_up_questions(candidate)}
    return json.dumps(result, ensure_ascii=False)
