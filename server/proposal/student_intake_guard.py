"""Conservative evidence and scope checks for a proposed candidate. No persistence interface."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import re

from pydantic import ValidationError

from .student_intake import FIELDS, IntakeCandidate, IntakeFact

ECON = re.compile(r"年收[入益]|年入|(?:我家|家里|家中)(?:的)?收入|家庭(?:经济|收入|人口|人数)|困难(?:认定|户)|家(?:里|庭|中)?.{0,7}[零一二两三四五六七八九十\d]+[口人]")
EXTERNAL = re.compile(r"范文|样表|示例|示范|范本|台词|角色|举例|通知说|政策|校规|手册|海报上|助手说|机器人说|"
                      r"室友|同桌|同学(?:的|说|家|绩点)|队友(?:的|以前)|学姐|学长|别人|另[一个名]+(?:人|位)|"
                      r"我(?:妈|爸|朋友|同伴)|如果|假[如设]|听说")
OWN = re.compile(r"(?:我自己|我本人|而我|但我|我的(?:绩点|成绩单|部分)|我这次|我们(?:的)?(?:团队|项目))")
FAB = re.compile(r"编(?:个|一个|造|一篇)|捏造|虚构|杜撰|凭空|冒充|伪造|包装成|就当.{0,3}真的|忽略.{0,5}规则|抄到")
REFUSE = re.compile(r"不想(?:提供|说|填|透露)|不(?:愿|便|方便|打算透露)|拒绝|不填|不要再问|别再问|不透露|不公开")
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


@dataclass
class Clause:
    turn: str
    text: str
    start: int
    end: int
    external: bool


def clauses(turns):
    parts = []
    for turn in turns:
        if turn["role"] != "user":
            continue
        external = False
        in_external_quote = False
        for m in re.finditer(r"[^，,。；;！？!?\n]+", turn["content"]):
            s = m.group()
            if in_external_quote or EXTERNAL.search(s) or FAB.search(s):
                external = True
            elif OWN.search(s) or (s.startswith(("我", "我们")) and "“" not in s and "”" not in s):
                external = False
            parts.append(Clause(turn["turn_id"],s,m.start(),m.end(),external))
            if external and "“" in s:
                in_external_quote = True
            if "”" in s:
                in_external_quote = False
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


def source_parts(e, turns, parts):
    source = turns.get(e.turn_id,"")
    hits = [m.start() for m in re.finditer(re.escape(e.quote),source)]
    if not hits or not e.quote.strip():
        return []
    return [p for p in parts if p.turn == e.turn_id
            and any(p.start < i+len(e.quote) and i < p.end for i in hits)]


def review_field(f, turns, parts, allowed, refused):
    quotes = " ".join(e.quote for e in f.evidence)
    contexts = [source_parts(e,turns,parts) for e in f.evidence]
    if not all(contexts) or any(p.external for ps in contexts for p in ps):
        return None, "unverified_source_or_owner"
    if (not allowed and (f.field.startswith("financial.") or ECON.search(quotes))):
        return None, "economic_scope_or_quote"
    if any(field != f.field and field in PRIVACY_FIELDS and PRIVACY_FIELDS[field].search(quotes) and NUM.search(quotes) for field in refused):
        return None, "refused_information_in_other_quote"
    if f.field in refused and f.status != "withheld":
        return None, "refused_field"
    if f.field in {"research.output","project.advisor"}:
        cue = r"论文|文章|专利|投稿|投给|发表|综述" if f.field == "research.output" else r"导师|指导|老师|教授"
        if not re.search(cue, quotes) or FAB.search(quotes):
            return None, "unsupported_paper_or_advisor"
        if f.value is not None and f.status == "stated" and (UNKNOWN.search(quotes) or NEG.search(quotes)):
            return None, "qualified_paper_or_advisor"
    if f.field == "project.stage" and PLAN.search(quotes):
        return None, "future_in_current_stage"
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


def scalar_reconciliation(parts, source):
    """Only resolve clear single-entity scalar claims with local source anchors."""
    found = defaultdict(list)
    for p in parts:
        if p.external or UNKNOWN.search(p.text) or REFUSE.search(p.text):
            continue
        for field, pattern in SCALAR.items():
            for m in pattern.finditer(p.text):
                try:
                    val=number(m.group(1))
                except (KeyError, ValueError, ArithmeticError):
                    continue
                found[field].append((p,val))
    conflicts = {}
    for field, claims in found.items():
        if len({v for _,v in claims}) <= 1:
            continue
        # Link only an immediate, unambiguous correction cue to the current scalar.
        p, latest = claims[-1]
        prior = [q for q in parts if q.turn == p.turn and q.end <= p.start]
        start = p.start
        cue = CORRECT.search(p.text)
        while prior and re.fullmatch(r"(?:我|刚才|刚刚|刚|前面|刚才我|前一句|上次|一下|了|说错|更正|纠正|改口)+",prior[-1].text):
            previous = prior.pop()
            cue = cue or CORRECT.search(previous.text)
            start = previous.start
        if cue:
            quote = source[p.turn][start:p.end]
            conflicts[field] = IntakeFact(field=field,record_id="self",value=format(latest,"f"),
                status="approximate" if APPROX.search(p.text) else "stated", evidence=[{"turn_id":p.turn,"quote":quote}])
            continue
        if any(PERIOD.search(q.text) for q,_ in claims):
            conflicts[field] = None  # v1 cannot represent term-specific singleton fields
            continue
        unique = {}
        for q,v in claims:
            unique.setdefault(v,{"turn_id":q.turn,"quote":q.text})
        if 2 <= len(unique) <= 8 and len({(e["turn_id"],e["quote"]) for e in unique.values()}) == len(unique):
            conflicts[field] = IntakeFact(field=field,record_id="self",value=None,status="conflict",
                                          evidence=list(unique.values()))
        else:
            conflicts[field] = None
    return conflicts


def inspect_candidate(raw, turns):
    try:
        parsed = IntakeCandidate.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError):
        return {"decision":"needs_manual_review","candidate":None,"events":["invalid_response"]}
    source = {t["turn_id"]:t["content"] for t in turns if t["role"]=="user"}
    parts = clauses(turns)
    allowed = financial_scope(parts)
    refused = {k for k,(label,_) in FIELDS.items() if any(
        not p.external and REFUSE.search(p.text) and label in p.text for p in parts)}
    refused.update(k for k,pattern in PRIVACY_FIELDS.items() if any(
        not p.external and REFUSE.search(p.text) and pattern.search(p.text) for p in parts))
    accepted, events = {}, []
    collisions = set()
    for fact in parsed.facts:
        f, why = review_field(fact,source,parts,allowed,refused)
        if why:
            events.append(f"{fact.field}/{why}")
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
    for field, conflict in scalar_reconciliation(parts,source).items():
        if field in refused:
            continue
        accepted.pop((field,"self"),None)
        if conflict is not None and (allowed or not any(ECON.search(e.quote) for e in conflict.evidence)):
            accepted[field,"self"] = conflict
            events.append(f"{field}/source_{conflict.status}")
        else:
            events.append(f"{field}/ambiguous_period_or_scope")
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
    candidate = IntakeCandidate(schema_version="student-intake-v1",facts=facts,follow_up_fields=qs)
    return {"decision":"pending_student_confirmation","candidate":candidate.model_dump(),"events":events}
