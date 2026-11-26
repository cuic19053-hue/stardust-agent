"""Conservative evidence and scope checks for a proposed candidate. No persistence interface."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import json
import re

from pydantic import ValidationError

from .student_intake import Evidence, FIELDS, MAX_FACTS, MAX_QUOTE_CHARS, IntakeCandidate, IntakeFact

ECON = re.compile(r"年收[入益]|年入|(?:我家|家里|家中)(?:的)?收入|家庭(?:经济|收入|人口|人数)|困难(?:认定|户)|家(?:里|庭|中)?.{0,7}[零一二两三四五六七八九十\d]+[口人]")
EXTERNAL = re.compile(r"范文|样表|示例|示范|范本|台词|角色|举例|通知说|政策|校规|手册|海报上|助手说|机器人说|"
                      r"室友|同桌|同学(?:的|说|家|绩点)|队友(?:的|以前)|学姐|学长|别人|另[一个名]+(?:人|位)|"
                      r"我(?:妈|爸|朋友|同伴)|如果|假[如设]|听说")
OWN = re.compile(r"(?:我自己|我本人|而我|但我|我的(?:绩点|成绩单|部分)|我这次|我们(?:的)?(?:团队|项目))")
FAB = re.compile(r"编(?:个|一个|造|一篇)|捏造|虚构|杜撰|凭空|冒充|伪造|包装成|就当.{0,3}真的|忽略.{0,5}规则|抄到")
REFUSE = re.compile(r"不想(?:提供|说|填|透露)|不(?:愿|便|方便|打算透露)|拒绝|不填|不要再问|别再问|不透露|不公开")
SELF_REFUSAL = re.compile(rf"我(?:本人|自己)?(?:暂时|目前|选择)?(?:{REFUSE.pattern})|(?:不要再问|别再问)我")
UNKNOWN = re.compile(r"不(?:知道|清楚|确定)|没(?:听清|弄清|定|选)|还.{0,4}定|记不[清准]|说不[上准]|没法确定|录得.{0,2}糊")
NEG = re.compile(r"没(?:有|找到|发表|拿过|参加|经历|导师)|尚无|从未|未曾|还没|未找到")
PLAN = re.compile(r"打算|计划|准备|预计|拟(?:用|做|申|交|采)|希望|考虑|想(?:要|申请|参加|报|请|用|交|投|做)|要申请")
APPROX = re.compile(r"大约|大概|大致|粗算|估计|差不多|左右|约")
CORRECT = re.compile(r"更正|说错|改口|纠正|以.{1,10}为准|应改为|打错")
PERIOD = re.compile(r"(?:上|下|本|这|上一|下一)学期|(?:今年|去年|明年)|大[一二三四](?:全年|上|下)")
AID = {"助学金", "国家助学金", "国家励志奖学金", "励志奖学金"}
TARGET = re.compile(r"国家励志奖学金|励志奖学金|(?:国家)?助学金|国家奖学金|国奖|[校院]级奖学金|大创|科研立项|奖学金")
APPLICATION = re.compile(r"申请|申报|办理|在办|要办|办的是|报的是|想报|要报|改报|递交|报名")
CANCEL = re.compile(r"取消|放弃|撤回|不再|不(?:申|办|打算申)|原先|原来|以为|条件|流程|资格|如何|能否|能不能")
PRIVACY_FIELDS = {"financial.annual_income_yuan":re.compile("收入|年入"),
                  "financial.household_size":re.compile("家庭(?:人数|人口)|家.{0,4}几口|家里人数"),
                  "financial.hardship_status":re.compile("困难|认定")}
REFUSAL_MARKERS = {**PRIVACY_FIELDS, "academic.gpa_scale": re.compile("满分|分制"),
                   "application.track": re.compile("赛道|类别")}
MONEY_OR_NUMBER = r"[\d零〇一二两三四五六七八九十百千万]+(?:[.点][\d零〇一二两三四五六七八九]+)?(?:万|千|百)?"
NUM = re.compile(MONEY_OR_NUMBER)
DIGITS = dict(zip("零〇一二两三四五六七八九", [0,0,1,2,2,3,4,5,6,7,8,9]))
UNITS = {"十":10,"百":100,"千":1000,"万":10000}
SCALAR = {
    "academic.gpa": re.compile(r"(?:绩点|GPA)(?:折算后)?[为是约大概\s]*("+MONEY_OR_NUMBER+r")",re.I),
    "project.budget_yuan": re.compile(r"(?:总预算|项目预算|预算)[约为是大概\s]*("+MONEY_OR_NUMBER+r")\s*元"),
    "project.team_size": re.compile(r"(?:团队总人数|团队人数|团队一共)[有是为\s]*("+MONEY_OR_NUMBER+r")\s*(?:个)?人"),
    "service.volunteer_hours": re.compile(r"(?:志愿(?:服务)?时长|服务时长)[是为有\s]*("+MONEY_OR_NUMBER+r")\s*小时"),
}
UNCERTAIN_FIELDS = {"academic.gpa","academic.rank","academic.rank_total","academic.rank_percent",
                    "service.volunteer_hours","project.budget_yuan","project.duration","project.team_size",
                    "financial.annual_income_yuan","financial.household_size"}
PLAN_FIELDS = {"application.type","application.track","research.output","project.method","project.advisor",
               "project.expected_output","service.role"}
IDENTITY_LABELS = {"学校": "identity.school", "学院": "identity.college", "专业": "identity.major",
                   "姓名": "identity.name", "年级": "identity.grade"}
IDENTITY_EVIDENCE = {"identity.school": re.compile(r"学校|就读于|校名"),
                     "identity.college": re.compile(r"学院|院系|学部|所属系"),
                     "identity.major": re.compile(r"专业|主修"),
                     "identity.name": re.compile(r"姓名|我叫|名叫|名字"),
                     "identity.grade": re.compile(r"年级|[大研博][一二三四五六]|\d{4}\s*级")}
IDENTITY_CLAIM = re.compile(r"我(?:本人|自己)?(?:目前|现在)?(?:的|所在的|所学的)(学校|学院|专业|姓名|年级)"
                            r"\s*(?:是|为|叫|[：:])\s*([\u3400-\u9fffA-Za-z][\u3400-\u9fffA-Za-z0-9· -]*)")
MAJOR_STUDY = re.compile(r"我(?:本人|自己)?(?:目前|现在)?(?:主修(?:的专业是|的是)?|学的专业是)\s*"
                         r"([\u3400-\u9fffA-Za-z][\u3400-\u9fffA-Za-z0-9· -]{0,39})")
NON_ASSERTION = re.compile(r"如果|假如|假设|要是|倘若|的话|是否|要不要|[?？]|范文|样表|举例|照着念")
QUALIFIED_PREFIX = re.compile(r"(?:过去|曾经|当时|原本|原来|小时候|上学期|之前|以前|也许|可能|"
                              r"不确定|记不清|不一定|说不准|好像|假定)[，,]\s*$")
FINANCIAL_CONTEXT = re.compile(r"收入|工资|薪水|家庭经济|经济(?:情况|状况|困难)|困难认定|家庭人数|家里几口")
QUALIFIED_TAIL = re.compile(r"[，,]\s*(?:但|不过)?(?:我)?(?:也|还)?(?:不确定|记不清|说不准|不一定|只是举例)")
NON_VALUE = re.compile(r"还是|或者|或是|也许|可能|好像|大概|要不要|不再|并非|不叫|不是|没想好|"
                       r"尚未|还没|的话|但|其实|我|吗|也说不准|并且|而且|并在|同时|已经")
NO_ADVISOR = re.compile(r"我(?:本人|自己)?(?:目前|现在)?(?:还没有|没有|尚无)(?:指导老师|导师)")
PLANNED_ARTIFACT = re.compile(
    r"(?:我(?:们)?(?:这次)?)?(?:(?:准备|打算|计划|预计|想|拟)(?:要)?"
    r"(?:制作|做出|做|绘制|编写|开发|交付|设计|编制|撰写|完成|整理出)|(?:的)?预期成果(?:是|为))"
    r"([\u3400-\u9fffA-Za-z0-9·-]{0,40}(?:教具|卡片?|地图|导览图|手册|小册子|图册|"
    r"原型|样机|模型|工具包?|装置|软件|程序|标本|报告|记录表|清单|数据[库集]|作品集|短片|问卷))")
COMPLETED_PLAN = re.compile(r"(?:后来|现在|现已|已经|其实已)(?:全部|提前)?(?:完成|做完|制成|做好|交付)")
WITHDRAWAL = re.compile(r"取消|作废|撤回|放弃|不再|不做了|决定不做|改做|没决定要不要|不打算|与(?:本|这次)?项目无关")
NAME_CUE = re.compile(r"项目名|名称|题名|题目|命名|取名|叫作|名叫|叫做")
OTHER_PROJECT = re.compile(r"另(?:一[个项]|外的?)(?:项目|课题|团队)|子项目")
PROJECT_LABEL = re.compile(r"([甲乙丙丁AB])项目|项目([甲乙丙丁AB])|第([一二三四\d]+)个项目")
SENTENCE_BREAK = re.compile(r"[。！？!?；;\n]")


@dataclass
class Clause:
    turn: str
    text: str
    start: int
    end: int
    external: bool


def clauses(turns):
    parts = []
    closing_quote = None
    example_continues = False
    for turn in turns:
        if turn["role"] != "user":
            closing_quote = None
            example_continues = False
            continue
        external = False
        for m in re.finditer(r"[^，,。；;！？!?\n]+", turn["content"]):
            s = m.group()
            if example_continues and re.match(r"(?:回到|再说)?我(?:自己|本人)", s.strip()):
                example_continues = False
            if closing_quote or example_continues or EXTERNAL.search(s) or FAB.search(s):
                external = True
            elif OWN.search(s) or (s.startswith(("我", "我们")) and "“" not in s and "”" not in s):
                external = False
            parts.append(Clause(turn["turn_id"],s,m.start(),m.end(),external))
            if closing_quote:
                if closing_quote in s:
                    closing_quote = None
            elif external:
                for opener, closer in (("“", "”"), ("「", "」"), ('"', '"')):
                    if opener in s and closer not in s.split(opener, 1)[1]:
                        closing_quote = closer
                        break
        # A template introduced on its own line can also be dictated without quotes.
        if external and turn["content"].rstrip().endswith(("：", ":")) and not closing_quote:
            example_continues = True
    return parts


def financial_scope(parts):
    allowed = False
    for p in parts:
        if p.external:
            continue
        if re.search(r"撤回|取消|放弃|不再|改报|改成|改为", p.text):
            allowed = False
        found = TARGET.findall(p.text)
        if found and APPLICATION.search(p.text):
            allowed = (set(found) <= AID and not CANCEL.search(p.text)
                       and bool(re.search(r"我|这次|正在|正办|本次", p.text)))
    return allowed


def number(text):
    """Exact decimal for simple spoken numerals, including one 万 group."""
    if re.fullmatch(r"\d+(?:\.\d+)?[万千百]?", text):
        return Decimal(text.rstrip("万千百")) * UNITS.get(text[-1],1)
    if "点" in text:
        a,b = text.split("点")
        scale = UNITS.get(b[-1],1)
        if scale != 1:
            b=b[:-1]
        return (number(a) + Decimal("0."+"".join(str(DIGITS[x]) for x in b))) * scale
    if text and all(x in DIGITS for x in text):
        return Decimal("".join(str(DIGITS[x]) for x in text))
    total = section = digit = 0
    for x in text:
        if x in DIGITS:
            digit = DIGITS[x]
        elif x == "万":
            total += (section + digit or 1) * 10000
            section = digit = 0
        else:
            section += (digit or 1) * UNITS[x]
            digit = 0
    return Decimal(total + section + digit)


def numeric_values(text):
    result = set()
    for m in NUM.finditer(text):
        try:
            result.add(number(m.group()))
        except (ValueError, KeyError, ArithmeticError):
            continue
    return result


def contains_disclosed_number(text):
    """The 一 in 这一栏 or 那一项 identifies a form field, not an amount."""
    return bool(NUM.search(re.sub(r"[这那]一[栏项]", "", text)))


def source_parts(e, turns, parts):
    source = turns.get(e.turn_id,"")
    hits = [m.start() for m in re.finditer(re.escape(e.quote),source)]
    if not hits or not e.quote.strip():
        return []
    return [p for p in parts if p.turn == e.turn_id
            and any(p.start < i+len(e.quote) and i < p.end for i in hits)]


def review_field(f, turns, parts, allowed, refused, identities, plans):
    quotes = " ".join(e.quote for e in f.evidence)
    contexts = [source_parts(e,turns,parts) for e in f.evidence]
    if not all(contexts) or any(p.external for ps in contexts for p in ps):
        return None, "unverified_source_or_owner"
    if (not allowed and (f.field.startswith("financial.") or ECON.search(quotes))):
        return None, "economic_scope_or_quote"
    if any(field != f.field and field in PRIVACY_FIELDS and PRIVACY_FIELDS[field].search(quotes)
           and contains_disclosed_number(quotes) for field in refused):
        return None, "refused_information_in_other_quote"
    if f.field in refused and f.status != "withheld":
        return None, "refused_field"
    plan_parts = [p for ps in contexts for p in ps if (p.turn, p.start) in plans]
    if (f.field in {"project.name", "project.stage"} and f.value is not None
            and any(f.value in p.text for p in plan_parts)
            and not any(f.value in p.text for ps in contexts for p in ps if p not in plan_parts)):
        return None, "deliverable_is_not_a_name_or_stage"
    if f.field == "project.expected_output" and f.value is not None:
        if (any(plans[p.turn, p.start] is None for p in plan_parts)
                or any(turns[p.turn][p.end:p.end+1] in ("?", "？") for ps in contexts for p in ps)):
            return None, "inactive_or_conditional_plan"
    if f.field in IDENTITY_EVIDENCE and f.value is not None:
        for context in contexts:
            for p in context:
                label = IDENTITY_CLAIM.fullmatch(p.text.strip())
                if f.field == "identity.major" and (label or MAJOR_STUDY.fullmatch(p.text.strip())) and (
                        p.turn, p.start) not in identities:
                    return None, "qualified_major"
                if label and f.value in p.text:
                    declared = identities.get((p.turn, p.start))
                    if (IDENTITY_LABELS[label[1]] != f.field or (declared and declared[1] != f.value)
                            or turns[p.turn][p.end:p.end+1] in ("?", "？")
                            or re.search(r"还是|或者|也许|吗$", label[2])):
                        return None, "identity_label_or_value_not_supported"
    if f.field in {"research.output","project.advisor"}:
        cue = r"论文|文章|专利|投稿|投给|发表|综述" if f.field == "research.output" else r"导师|指导|老师|教授"
        if not re.search(cue, quotes) or FAB.search(quotes):
            return None, "unsupported_paper_or_advisor"
        if f.value is not None and f.status == "stated" and (UNKNOWN.search(quotes) or NEG.search(quotes)):
            return None, "qualified_paper_or_advisor"
    if f.field == "project.stage" and PLAN.search(quotes):
        return None, "future_in_current_stage"
    if f.field == "project.name" and f.value is not None and PLAN.search(quotes) and not NAME_CUE.search(quotes):
        return None, "plan_does_not_name_project"
    if f.status == "conflict" and len({(e.turn_id,e.quote) for e in f.evidence}) < 2:
        return None, "missing_conflicting_claims"
    status = f.status
    if f.value is None:
        if status == "negated" and UNKNOWN.search(quotes) and not NEG.search(quotes):
            status = "unknown"
        cues = {"withheld":REFUSE,"unknown":UNKNOWN,"negated":NEG}
        if status in cues and not cues[status].search(quotes):
            return None, "unsupported_null_status"
    else:
        # A single quote can describe several fields. Apply qualifiers only in
        # the clause carrying this value (e.g. an approximate budget and exact headcount).
        local = [p.text for ps in contexts for p in ps]
        if re.fullmatch(r"\d+(?:\.\d+)?", f.value):
            val = Decimal(f.value)
            if val not in numeric_values(quotes):
                return None, "unsupported_number"
            local = [text for text in local if val in numeric_values(text)]
        else:
            if f.value not in quotes:
                return None, "value_absent_from_quote"
            local = [text for text in local if f.value in text]
        if status == "stated" and f.field in UNCERTAIN_FIELDS and any(APPROX.search(text) for text in local):
            status = "approximate"
        if status == "stated" and f.field in PLAN_FIELDS and any(PLAN.search(text) for text in local):
            status = "planned"
    rec = f.record_id
    if f.field.startswith("award.") or f.field.startswith("academic.rank"):
        prefix = "award" if f.field.startswith("award.") else "rank"
        if not re.fullmatch(prefix+r"_[1-9]\d*", rec):
            return None, "ambiguous_record"
    else:
        rec = "self"
    return f.model_copy(update={"status":status, "record_id":rec}), None


def assertion_context(part, source):
    """Inspect the original sentence, including qualifiers before a masked comma."""
    text = source[part.turn]
    before = SENTENCE_BREAK.split(text[:part.start])[-1]
    end = SENTENCE_BREAK.search(text, part.end)
    after = text[part.end:end.end() if end else len(text)]
    sentence = before + part.text + after
    if NON_ASSERTION.search(sentence) or FAB.search(sentence):
        return False
    previous = re.split("[，,]", before.rstrip(" ，,"))[-1]
    following = re.split("[，,]", after.lstrip(" ，,"))[0]
    return (not QUALIFIED_PREFIX.search(before) or bool(FINANCIAL_CONTEXT.search(previous))) and (
        not QUALIFIED_TAIL.match(after) or bool(FINANCIAL_CONTEXT.search(following)))


def explicit_identity(part, source):
    """Recover a current self-labelled identity with its own contiguous source."""
    if part.external or len(part.text) > MAX_QUOTE_CHARS:
        return None
    claim = IDENTITY_CLAIM.fullmatch(part.text.strip())
    study = MAJOR_STUDY.fullmatch(part.text.strip()) if not claim else None
    if not (claim or study):
        return None
    if claim:
        field, value = IDENTITY_LABELS[claim[1]], claim[2].strip()
    else:
        field, value = "identity.major", study[1].strip()
    if (UNKNOWN.search(value) or REFUSE.search(value) or PLAN.search(value)
            or re.search(r"还是|或者|可能|好像|也许|不再|并非|以前|不叫|不是|吗|的话", value)):
        return None
    if field == "identity.major":
        if len(value) > 40 or NON_VALUE.search(value) or not assertion_context(part, source):
            return None
    else:
        text = source[part.turn]
        prefix = SENTENCE_BREAK.split(text[:part.start])[-1].strip(" ，,")
        if prefix and not re.fullmatch(r"(?:我)?(?:更正一下|更正|说错了|纠正一下)", prefix):
            return None
        if text[part.end:part.end+1] not in ("", "。", "！", "!", "；", ";", "\n"):
            return None
    return field, value


def source_declarations(original, visible_parts):
    """Map explicit declarations back to the visible clauses without guessing a turn ID."""
    source = {t["turn_id"]: t["content"] for t in original if t["role"] == "user"}
    # Evidence has a turn and a quote, but no offset. Repeated text in that turn
    # is usable only if every occurrence supports the same interpretation.
    identity_readings = defaultdict(set)
    plan_readings = defaultdict(set)
    for part in clauses(original):
        key = (part.turn, part.text)
        identity_readings[key].add(explicit_identity(part, source))
        artifact = PLANNED_ARTIFACT.fullmatch(part.text.strip())
        if artifact:
            value = artifact[1]
            active = (not part.external and assertion_context(part, source)
                      and not (NON_VALUE.search(value) or NEG.search(value) or REFUSE.search(value)))
            plan_readings[key].add(value if active else None)
    # A later cancellation or completion can supersede a plan. An earlier one
    # cannot cancel a new plan that the student goes on to declare.
    plan_superseded = False
    major_retracted = False
    identities, plans = {}, {}
    for visible in reversed(visible_parts):
        if not visible.external and not FINANCIAL_CONTEXT.search(visible.text):
            if WITHDRAWAL.search(visible.text) or COMPLETED_PLAN.search(visible.text):
                plan_superseded = True
            if IDENTITY_EVIDENCE["identity.major"].search(visible.text) and (
                    UNKNOWN.search(visible.text) or REFUSE.search(visible.text)
                    or re.search(r"专业.{0,3}(?:说错|记错|换了|改了)|(?:不是|不再主修)", visible.text)):
                major_retracted = True
        if visible.external:
            continue
        literal = (visible.turn, visible.text)
        location = (visible.turn, visible.start)
        readings = identity_readings.get(literal, ())
        if len(readings) == 1:
            identity = next(iter(readings))
            if identity and not (identity[0] == "identity.major" and major_retracted):
                identities[location] = identity
        if literal in plan_readings:
            # A rejected match also prevents the model from releasing an inactive plan.
            readings = plan_readings[literal]
            plans[location] = next(iter(readings)) if len(readings) == 1 and not plan_superseded else None
    return identities, plans


def source_reconciliation(parts, source, identities, plans):
    """Recover explicit singleton values and reconcile competing claims using student turns."""
    found = defaultdict(list)
    for p in parts:
        if p.external or UNKNOWN.search(p.text) or REFUSE.search(p.text):
            continue
        if (source[p.turn][p.end:p.end+1] in ("？", "?") or NEG.search(p.text)
                or re.search(r"是不是|是否|并非|不是|假设|能否|吗$|作废", p.text)):
            continue
        if (identity := identities.get((p.turn, p.start))) is not None:
            field, value = identity
            found[field].append((p, value))
        artifact = plans.get((p.turn, p.start))
        if artifact and p.text.lstrip().startswith("我"):
            found["project.expected_output"].append((p, artifact))
        for field, pattern in SCALAR.items():
            for m in pattern.finditer(p.text):
                try:
                    val=number(m.group(1))
                except (KeyError, ValueError, ArithmeticError):
                    continue
                found[field].append((p,val))
    resolved = {}
    for field, claims in found.items():
        if len({v for _,v in claims}) == 1:
            if field in {"identity.major", "project.expected_output"}:
                part, value = claims[0]
                resolved[field] = IntakeFact(field=field, record_id="self", value=value,
                    status="planned" if field == "project.expected_output" else "stated",
                    evidence=[Evidence(turn_id=part.turn, quote=part.text.strip())])
            continue
        if field == "project.expected_output":
            # Distinct deliverables cannot safely overwrite each other in this singleton.
            resolved[field] = None
            continue
        if any(len(q.text) > MAX_QUOTE_CHARS for q,_ in claims):
            resolved[field] = None
            continue
        # Link only an immediate, unambiguous correction cue to this claim.
        p, latest = claims[-1]
        prior = [q for q in parts if q.turn == p.turn and q.end <= p.start]
        start = p.start
        cue = CORRECT.search(p.text)
        while prior and re.fullmatch(r"(?:我|刚才|刚刚|刚|前面|刚才我|前一句|上次|一下|了|说错|更正|纠正|改口)+",prior[-1].text):
            previous = prior.pop()
            cue = cue or CORRECT.search(previous.text)
            start = previous.start
        # A qualifier can precede a comma ("上学期，我的绩点是…").
        scopes = [SENTENCE_BREAK.split(source[q.turn][:q.end])[-1] for q,_ in claims]
        periods = {m.group() for scope in scopes for m in PERIOD.finditer(scope)}
        project_labels = {m.group(m.lastindex) for scope in scopes for m in PROJECT_LABEL.finditer(scope)}
        if len(periods) > 1 or (field.startswith("project.") and
                (len(project_labels) > 1 or any(OTHER_PROJECT.search(scope) for scope in scopes))):
            resolved[field] = None
            continue
        if cue:
            quote = source[p.turn][start:p.end]
            if len(quote) > MAX_QUOTE_CHARS:
                resolved[field] = None
                continue
            value = format(latest,"f") if isinstance(latest, Decimal) else latest
            resolved[field] = IntakeFact(field=field,record_id="self",value=value,
                status="approximate" if APPROX.search(p.text) else "stated", evidence=[{"turn_id":p.turn,"quote":quote}])
            continue
        if periods:
            resolved[field] = None
            continue
        unique = {}
        for q,v in claims:
            unique.setdefault(v,{"turn_id":q.turn,"quote":q.text})
        if 2 <= len(unique) <= 8 and len({(e["turn_id"],e["quote"]) for e in unique.values()}) == len(unique):
            resolved[field] = IntakeFact(field=field,record_id="self",value=None,status="conflict",
                                          evidence=list(unique.values()))
        else:
            resolved[field] = None
    return resolved


def declared_fact(fact, source, parts, identities):
    """Only relabel when the cited clause itself supplies the field and its value or denial."""
    if len(fact.evidence) != 1:
        return None
    evidence = fact.evidence[0]
    contexts = source_parts(evidence, source, parts)
    if len(contexts) != 1 or contexts[0].external:
        return None
    part = contexts[0]
    identity = identities.get((part.turn, part.start))
    if identity and fact.status == "stated" and fact.value == identity[1] and fact.value in evidence.quote:
        field, value = identity
        status = "stated"
    elif (fact.field in {"research.experience", "project.advisor"} and fact.status in {"unknown", "negated"}
          and NO_ADVISOR.fullmatch(part.text.strip()) and NO_ADVISOR.fullmatch(evidence.quote.strip().rstrip("。！!"))
          and (not part.start or source[part.turn][part.start-1] in "。！？!?；;\n")
          and source[part.turn][part.end:part.end+1] not in ("?", "？")
          and not any(other != part and not other.external and re.search(r"导师|指导老师", other.text)
                      and not NO_ADVISOR.fullmatch(other.text.strip()) for other in parts)):
        field, value, status = "project.advisor", None, "negated"
    else:
        return None
    return IntakeFact(field=field, value=value, status=status, record_id="self",
                      evidence=[Evidence(turn_id=part.turn, quote=part.text.strip())])


def parse_fact(item, source, parts, plans):
    # Relabel only an explicitly proposed deliverable supported by a literal
    # creation clause. A plan to conduct an activity need not name any artifact.
    if (item["field"] in {"project.name", "project.stage", "project.expected_output"}
            and item.get("status") in ("planned", "stated") and item.get("record_id") == "self"
            and isinstance(item.get("evidence"), list) and len(item["evidence"]) == 1):
        evidence = Evidence.model_validate(item["evidence"][0])
        contexts = source_parts(evidence, source, parts)
        matches = [(p, value) for p in contexts if (value := plans.get((p.turn, p.start)))
                   and p.text.strip() in evidence.quote]
        if len(matches) == 1 and not any(p.external for p in contexts):
            part, value = matches[0]
            proposed = item.get("value")
            missing_plan = proposed is None and item.get("status") == "planned"
            quoted_value = isinstance(proposed, str) and proposed.strip() and proposed in value
            if missing_plan or quoted_value:
                if item["field"] == "project.expected_output" and item["status"] == "planned" and proposed == value:
                    return IntakeFact.model_validate(item)
                item = {**item, "field": "project.expected_output", "status": "planned", "value": value,
                        "evidence": [{"turn_id": part.turn, "quote": part.text.strip()}]}
    return IntakeFact.model_validate(item)


def parse_proposal(raw, source, parts, identities, plans, reconciled):
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list) or len(data["facts"]) > MAX_FACTS:
        raise ValueError("invalid candidate envelope")
    envelope = IntakeCandidate.model_validate({**data, "facts": []})
    normalized = {}
    blocked = set()
    events = []
    for item in data["facts"]:
        if not isinstance(item, dict) or not isinstance(item.get("field"), str) or item["field"] not in FIELDS:
            raise ValueError("cannot identify the affected field")
        try:
            fact = parse_fact(item, source, parts, plans)
        except ValidationError:
            # An invalid claim can contradict a valid-looking sibling. Withhold
            # the entire field, retaining only independently checked other fields.
            blocked.add(item["field"])
            events.append(f"{item['field']}/invalid_claim")
            continue
        if fact.field == "project.expected_output" and (fact.field, fact.status, fact.value) != (
                item["field"], item.get("status"), item.get("value")):
            events.append(f"{item['field']}/source_planned_artifact")
        declared = declared_fact(fact, source, parts, identities)
        if declared is not None:
            if fact != declared:
                events.append(f"{fact.field}/source_declares_{declared.field}")
            fact = declared
        key = (fact.field, fact.record_id)
        if key in normalized and normalized[key] != fact:
            resolved = reconciled.get(fact.field)
            if fact.record_id == "self" and resolved is not None:
                fact = resolved
            else:
                blocked.add(fact.field)
                events.append(f"{fact.field}/ambiguous_duplicate")
        normalized[key] = fact
    facts = [f for f in normalized.values() if f.field not in blocked]
    return envelope.model_copy(update={"facts": facts}), blocked, events


def inspect_candidate(raw, turns, original_turns=None):
    source = {t["turn_id"]:t["content"] for t in turns if t["role"]=="user"}
    parts = clauses(turns)
    identities, plans = source_declarations(turns if original_turns is None else original_turns, parts)
    reconciled = source_reconciliation(parts, source, identities, plans)
    try:
        parsed, invalid_fields, events = parse_proposal(raw, source, parts, identities, plans, reconciled)
    except (ValueError, TypeError, RecursionError):
        return {"decision":"needs_manual_review","candidate":None,"events":["invalid_response"]}
    allowed = financial_scope(parts)
    refused = set()
    refusals = {}
    for p in parts:
        if p.external or not REFUSE.search(p.text):
            continue
        fields = {k for k,(label,_) in FIELDS.items() if label in p.text}
        fields.update(k for k, marker in REFUSAL_MARKERS.items() if marker.search(p.text))
        if "academic.gpa_scale" in fields and "绩点" not in p.text.replace("绩点满分", ""):
            fields.discard("academic.gpa")
        refused.update(fields)
        if (len(fields) == 1 and SELF_REFUSAL.search(p.text) and not contains_disclosed_number(p.text)
                and len(p.text) <= MAX_QUOTE_CHARS and source[p.turn][p.end:p.end+1] not in ("?", "？")):
            field = next(iter(fields))
            if field in REFUSAL_MARKERS and (allowed or not (field.startswith("financial.") or ECON.search(p.text))):
                refusals[field] = IntakeFact(field=field, record_id="self", status="withheld", value=None,
                                             evidence=[Evidence(turn_id=p.turn, quote=p.text.strip())])
    accepted = {}
    collisions = set()
    for fact in parsed.facts:
        f, why = review_field(fact,source,parts,allowed,refused,identities,plans)
        if why:
            events.append(f"{fact.field}/{why}")
            continue
        # When recovering only part of a malformed response, a name occurring
        # in a quote is insufficient to identify it as someone's school or major.
        marker = IDENTITY_EVIDENCE.get(f.field)
        if invalid_fields and marker and not any(marker.search(e.quote) for e in f.evidence):
            events.append(f"{f.field}/missing_identity_context")
            continue
        key = (f.field, f.record_id)
        if key in accepted:
            collisions.add(key)
            events.append(f"{f.field}/duplicate_after_linking")
        accepted[key] = f
        if (f.status,f.record_id)!=(fact.status,fact.record_id):
            events.append(f"{f.field}/normalized_state_or_link")
    for key in collisions:
        accepted.pop(key)
    for field, conflict in reconciled.items():
        if field in refused:
            continue
        if field in invalid_fields and (conflict is None or conflict.status != "conflict"):
            continue
        accepted.pop((field,"self"),None)
        if conflict is not None and (allowed or not any(ECON.search(e.quote) for e in conflict.evidence)):
            accepted[field,"self"] = conflict
            invalid_fields.discard(field)
            events.append(f"{field}/source_{conflict.status}")
        else:
            events.append(f"{field}/ambiguous_period_or_scope")
    for field, refusal in refusals.items():
        if (field, "self") not in accepted:
            events.append(f"{field}/source_withheld")
        accepted.setdefault((field, "self"), refusal)
        invalid_fields.discard(field)
    facts = list(accepted.values())
    blocked = refused | {f.field for f in facts if f.status=="withheld"}
    qs = [f.field for st in ("conflict","unknown") for f in facts if f.status==st]
    by_field = {f.field:f for f in facts}
    if "academic.gpa" in by_field and by_field["academic.gpa"].status in {"stated","approximate"} and "academic.gpa_scale" not in by_field:
        qs.append("academic.gpa_scale")
    if "application.type" in by_field and by_field["application.type"].value=="大创" and "application.track" not in by_field:
        qs.append("application.track")
    qs = list(dict.fromkeys(q for q in qs if q not in blocked and (allowed or not q.startswith("financial."))))[:3]
    if qs != parsed.follow_up_fields:
        events.append("recomputed_follow_up")
    if len(facts) > MAX_FACTS or (invalid_fields and not facts):
        return {"decision":"needs_manual_review","candidate":None,"events":events}
    candidate = IntakeCandidate(schema_version="student-intake-v1",facts=facts,follow_up_fields=qs)
    decision = "partial_review" if invalid_fields else "pending_student_confirmation"
    return {"decision":decision,"candidate":candidate.model_dump(),"events":events}
