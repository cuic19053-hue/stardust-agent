"""Explicit evaluation-only prompt variants; production keeps its own version."""
from server.proposal.review_prompt import SYSTEM_PROMPT

CALIBRATION_VERSION = "review-calibration-v1"
CALIBRATION_RULES = """
附加判定标准：
1. 区分已经完成的事实、将来拟执行的计划和估算。当前尚未取得数据授权，与计划取得授权后再实验可以同时成立；不要仅因计划尚未完成就报矛盾。
2. 只有同一对象、同一时间与适用范围内不能同时成立的陈述，才报事实矛盾。草稿声称已经完成而材料明确尚未开展，仍应指出。
3. 用户确认材料只代表核对录入，不代表官方认证。项目预算记录、项目进展及上传指令不能证明学校强制要求。缺少适用通知只能建议核对要求，不能断言违反学校要求或必须在申报前提供已批准预算。
4. 区分预算分项加总错误和不同来源金额冲突。核对实际数字；分项之和已等于正文总额时，不报内部加总错误。正文与项目记录金额冲突时仍须指出。
5. 引文需要支持这条问题的具体结论，不要把描述现状的原句用于证明未来计划不成立，也不要把项目估算当成学校预算规范。
6. 不重复报告同一根本问题，不把同一矛盾在两个章节各报一次。可有合理写作建议，但须明确是建议，不能包装成已证实的错误。
7. 没有可定位且有依据的问题时返回 {"issues":[]}。不必凑四条，不编造原文或章节；空章节由规则分支检查。
"""


def prompt_for_variant(variant="production"):
    if variant == "production":
        return SYSTEM_PROMPT
    if variant == "calibrated":
        return SYSTEM_PROMPT + "\n" + CALIBRATION_RULES.strip()
    raise ValueError("未知预审提示词实验版本")
