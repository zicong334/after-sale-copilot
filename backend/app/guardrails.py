"""guardrails.py — W4-C-FIX 确定性护栏常量与纯函数(单一可审计入口)。

设计铁律(任务书 W4-C-FIX):**不指望模型变聪明,给每个失败模式加一层非 LLM 的
确定性兜底**。本模块只放"可单测、无 IO、无随机"的常量与纯函数,节点/工具在调用点
使用它们;护栏清单集中在此,便于审计与回归。

覆盖:
- FIX-1 订单核验强制链:``ORDER_ID_PATTERN`` / ``needs_order_verification``;
- FIX-3 注入感知:``INJECTION_RULES`` / ``detect_injection`` / ``strip_injection``
  / ``is_privacy_probe`` / ``injection_flags``;
- FIX-4 反编造:``STATUS_CLAIM_RULES`` / ``audit_status_claims``;
- FIX-5 引用保真:``citation_phrases`` / ``ensure_citation_fidelity``;
- FIX-6 转人工原因枚举:``HANDOFF_REASON_ENUM`` / ``map_handoff_reason``;
- FIX-8 查单快捷路径/统一拒绝话术:``is_order_query_shortcut``
  / ``order_lookup_denied`` / ``ensure_order_not_found_clause``;
- FIX-9 偏好引用保真:``memory_reference_phrase`` / ``needs_memory_reference``
  / ``ensure_memory_reference``。

注意:本模块不 import app 内其他业务模块(避免循环依赖),可被 perception /
memory / graph / tools 任意层安全导入。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# FIX-1:订单核验强制链
# ---------------------------------------------------------------------------

#: 与工具层一致的订单号形态(seed-order-xxx / ORD-xxx)。
ORDER_ID_PATTERN = re.compile(
    r"\b(?:seed-order-|ORD-)[A-Za-z0-9-]{2,63}\b", re.IGNORECASE
)

#: 无订单号时仍必须核验/追问的意图(纯政策问答除外:那是知识问答,不该查订单)。
ORDER_INTENTS = frozenset({"order_judge"})


def extract_order_id(text: str, context: Mapping[str, Any] | None = None) -> str | None:
    """订单号来源:请求上下文(已解析槽位)优先,其次消息文本;都没有返回 None。"""
    ctx = dict(context or {})
    explicit = ctx.get("order_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    match = ORDER_ID_PATTERN.search(text or "")
    return match.group(0) if match else None


def needs_order_verification(
    *,
    text: str,
    context: Mapping[str, Any] | None,
    intent_name: str | None,
    observations: Sequence[Mapping[str, Any]],
) -> bool:
    """本轮是否必须补一次 ``shop__get_order``(已调过则永不重复注入)。"""
    for item in observations or ():
        if str(item.get("name") or "") == "shop__get_order":
            return False
    intent = str(intent_name or "")
    if extract_order_id(text, context):
        # 上下文/文本带订单号:任何"可能涉及具体订单"的请求都先核验;
        # 纯政策问答(用户问规则,不要求查单)例外,避免多余工具调用。
        return intent != "policy_qa"
    return intent in ORDER_INTENTS


# ---------------------------------------------------------------------------
# FIX-3:注入感知(确定性特征清单)
# ---------------------------------------------------------------------------

#: (kind, 可读标签, 正则, 是否硬拦截)。
#:
#: 硬拦截 = 技术型注入(SQL / 命令拼接 / 伪造工具调用),整段请求不可执行;
#: 软命中 = 自然语言越权/伪造政策/伪造系统话术,剥离命中片段后继续处理正当诉求。
INJECTION_RULES: tuple[tuple[str, str, re.Pattern[str], bool], ...] = (
    (
        "sql",
        "SQL 片段",
        re.compile(
            r"(?is)(\bdrop\s+table\b|\bunion\s+select\b|\bor\s+1\s*=\s*1\b|"
            r"\binsert\s+into\b|\bdelete\s+from\b|\bupdate\s+\w+\s+set\b|"
            r";\s*--|'\s*;)"
        ),
        True,
    ),
    (
        "command",
        "命令拼接",
        re.compile(
            r"(?is)(&&|\|\||;\s*(?:rm|curl|wget|powershell|pwsh|cmd|bash|sh)\b|"
            r"\$\(|`[^`]{1,120}`|rm\s+-rf\b)"
        ),
        True,
    ),
    (
        "tool_injection",
        "伪造工具调用",
        re.compile(
            r"(?is)(调用\s*[A-Za-z_]\w*__\w+\s*工具|"
            r"tool_call|function_call|"
            r"<\|?tool\|?>|"
            r"amount_cents\s*=\s*\d+)"
        ),
        True,
    ),
    (
        "override",
        "忽略/覆盖指令",
        re.compile(
            r"(忽略|无视|忘记|覆盖|跳过)(以上|之前|上述|前面|所有|全部|你的)?"
            r"(所有|全部)?(指令|提示|规则|设定|约束)"
        ),
        False,
    ),
    (
        "override_en",
        "英文忽略指令",
        re.compile(r"(?is)ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?)"),
        False,
    ),
    (
        "authority",
        "伪造权限/身份",
        re.compile(
            r"(我是(你们)?(店长|老板|管理员|主管|内部人员|官方)|我有(管理)?权限|"
            r"管理员权限|内部权限|超级管理员)"
        ),
        False,
    ),
    (
        "policy_forgery",
        "伪造政策/绕过期限",
        re.compile(
            r"(政策(写的|写的就是|规定|说的?就是)|你们政策就是|"
            r"按\s*\d+\s*天\s*无理由|别管几天|不用管几天|不管几天)"
        ),
        False,
    ),
    (
        "fake_system",
        "伪造系统话术",
        re.compile(
            r"(?is)(system\s*:|\[系统|系统消息|系统提示|开发者模式|"
            r"developer\s+mode|你现在是|越狱|jailbreak|最高权限)"
        ),
        False,
    ),
)

#: Percept.flags 使用的固定标记。
INJECTION_FLAG = "injection_suspect"
INJECTION_BLOCK_FLAG = "injection_block"
PRIVACY_BLOCK_FLAG = "privacy_block"


def detect_injection(text: str) -> dict[str, Any]:
    """确定性注入检测。

    返回 ``{"suspect": bool, "kinds": [...], "labels": [...], "hard": bool,
    "spans": [(start, end), ...]}``;``spans`` 用于剥离注入片段。
    """
    body = str(text or "")
    kinds: list[str] = []
    labels: list[str] = []
    spans: list[tuple[int, int]] = []
    hard = False
    for kind, label, pattern, is_hard in INJECTION_RULES:
        matched = False
        for match in pattern.finditer(body):
            matched = True
            spans.append(match.span())
            if is_hard:
                hard = True
        if matched:
            kinds.append(kind)
            labels.append(label)
    return {
        "suspect": bool(kinds),
        "kinds": kinds,
        "labels": labels,
        "hard": hard,
        "spans": _merge_spans(spans),
    }


def injection_flags(text: str) -> list[str]:
    """给 ``Percept.flags`` 用的稳定标记列表(命中才出现)。"""
    profile = detect_injection(text)
    if not profile["suspect"]:
        return []
    flags = [INJECTION_FLAG]
    flags.extend(f"injection_kind:{kind}" for kind in profile["kinds"])
    if profile["hard"]:
        flags.append(INJECTION_BLOCK_FLAG)
    return flags


def strip_injection(text: str) -> str:
    """剥离所有注入命中片段,只保留正当诉求(供分类/记忆/工具参数使用)。"""
    body = str(text or "")
    profile = detect_injection(body)
    if not profile["suspect"]:
        return body
    pieces: list[str] = []
    cursor = 0
    for start, end in profile["spans"]:
        pieces.append(body[cursor:start])
        cursor = max(cursor, end)
    pieces.append(body[cursor:])
    cleaned = re.sub(r"[ \t]{2,}", " ", "".join(pieces))
    return cleaned.strip(" ，,;；\t")


def is_privacy_probe(text: str, has_order_id: bool) -> bool:
    """是否在索要**他人**隐私信息(手机号/地址/身份证等)。

    有订单号槽位时不做此拦截:越权订单的拒绝由 ``shop__get_order`` 的
    NOT_FOUND_OR_FORBIDDEN 证据链负责(越权查单的期望话术)。
    """
    if has_order_id:
        return False
    body = str(text or "")
    pii = ("手机号", "手机号码", "电话", "联系方式", "收货地址", "地址", "身份证", "真实姓名")
    other = re.search(r"(seed-cust-|客户|买家|他人|别人的|对方的)", body)
    return bool(other and any(word in body for word in pii))


# ---------------------------------------------------------------------------
# FIX-4:反编造(状态声称 → 证据)
# ---------------------------------------------------------------------------

#: (类别, 正则)。类别与 ``audit_status_claims`` 的 evidence 键对应。
STATUS_CLAIM_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "handoff",
        re.compile(
            r"(已(为您|经)?(转接|转交|转入)(人工|客服|人工客服)|已转人工|"
            r"已(为您|经)?(创建|生成|提交)(人工)?工单|工单(已|已经)?(创建|生成|建好|提交)|"
            r"已进入人工(队列|处理)|人工(已|将)接手)"
        ),
    ),
    (
        "refund",
        re.compile(
            r"(已(为您|经)?(提交|发起|创建|办理)(退款|退款申请)|"
            r"退款(申请)?已(提交|受理|审核|通过|完成)|"
            r"已(为您|经)?退款|已退款)"
        ),
    ),
    (
        "address",
        re.compile(
            r"((地址|收货地址)(已|已经)?(修改|更新|变更)(成功)?|"
            r"已(为您|经)?(修改|更新)地址|修改成功)"
        ),
    ),
    (
        "lookup",
        re.compile(
            r"(查不到|未找到|没有找到|未能.{0,8}找到|暂时没有查到|没有查询到|"
            r"无此订单|订单不存在|无法查询到)"
        ),
    ),
    (
        "write",
        re.compile(r"已(为您|经)?(提交|受理|处理|登记|办理|执行)"),
    ),
)

#: 无证据时使用的诚实话术(任务书要求以"我尚未执行该操作"为骨架)。
HONEST_REPLACEMENTS: dict[str, str] = {
    "handoff": "我尚未为您转接人工;如需要,请确认后我再为您创建人工工单。",
    "refund": "我尚未执行退款操作;请确认退款金额与退货资格后我再继续办理。",
    "address": "我尚未修改收货地址;该操作需要人工核实后才能执行。",
    "lookup": "我暂时无法确认该订单的状态;请提供正确的订单号后我再查询。",
    "write": "我尚未执行该操作;请确认后我再继续办理。",
}

#: 类别 → 所需证据键。``write`` 是泛化兜底,任一写操作证据即可。
CLAIM_EVIDENCE_KEYS: dict[str, tuple[str, ...]] = {
    "handoff": ("handoff_done",),
    "refund": ("refund_done",),
    "address": ("address_done",),
    "lookup": ("lookup_denied", "no_hit"),
    "write": ("write_done",),
}

_SENTENCE_RE = re.compile(r"[^。！？\n]+[。！？]?")


def _merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(spans)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def audit_status_claims(
    reply: str, evidence: Mapping[str, bool]
) -> tuple[str, list[str]]:
    """把"没有证据的状态声称"逐句替换为诚实话术。

    返回 ``(新回复, 被替换的类别列表)``。有证据的声称原样保留;同一类别只替换一次
    连续出现的句子,避免模板刷屏。
    """
    body = str(reply or "")
    if not body:
        return body, []
    replaced: list[str] = []
    out: list[str] = []
    previous_replacement: str | None = None
    for sentence in _SENTENCE_RE.findall(body):
        categories = [
            category
            for category, pattern in STATUS_CLAIM_RULES
            if pattern.search(sentence)
        ]
        lacking = None
        for category in categories:
            keys = CLAIM_EVIDENCE_KEYS.get(category, ())
            if not any(bool(evidence.get(key)) for key in keys):
                lacking = category
                break
        if lacking is None:
            out.append(sentence)
            previous_replacement = None
            continue
        replacement = HONEST_REPLACEMENTS[lacking]
        replaced.append(lacking)
        if replacement != previous_replacement:
            out.append(replacement)
        previous_replacement = replacement
    return "".join(out), replaced


# ---------------------------------------------------------------------------
# FIX-5:引用保真(关键数字短语)
# ---------------------------------------------------------------------------

_NUM_UNIT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:元|天|日|小时|个?工作日|%|折|件|次|个月|年)"
)
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_CLAUSE_SPLIT_RE = re.compile(r"[；;。！？\n]")
_PART_SPLIT_RE = re.compile(r"[，,]")


def citation_phrases(text: str) -> list[str]:
    """从条款原文提取"含数字+单位"的关键短语(去括注、按标点切分、去重)。

    例:``单笔实付满 99 元包邮（偏远地区除外）；不满 99 元收 8 元基础运费``
    → ``["单笔实付满 99 元包邮", "不满 99 元收 8 元基础运费"]``。
    """
    body = _PAREN_RE.sub("", str(text or ""))
    phrases: list[str] = []
    for segment in _CLAUSE_SPLIT_RE.split(body):
        for part in _PART_SPLIT_RE.split(segment):
            candidate = part.strip(" \t，,、:：;；")
            if not candidate or not _NUM_UNIT_RE.search(candidate):
                continue
            if candidate not in phrases:
                phrases.append(candidate)
    return phrases


def ensure_citation_fidelity(
    reply: str, clauses: Sequence[Mapping[str, Any]]
) -> tuple[str, list[str]]:
    """关键数字短语未逐字出现时,追加条款原文(确定性拼接,不回炉模型)。

    返回 ``(新回复, 缺失短语列表)``;无缺失时原样返回。
    """
    body = str(reply or "")
    missing: list[str] = []
    append_parts: list[str] = []
    for index, clause in enumerate(clauses or ()):
        text = str(clause.get("text") or "")
        clause_missing = [p for p in citation_phrases(text) if p not in body]
        if not clause_missing:
            continue
        missing.extend(clause_missing)
        label = str(clause.get("clause_id") or index + 1)
        append_parts.append(f"[{label}] {text}")
    if not missing:
        return body, []
    appendix = "\n\n依据条款原文:\n" + "\n".join(append_parts)
    return body + appendix, missing


# ---------------------------------------------------------------------------
# FIX-6:handoff_reason 枚举
# ---------------------------------------------------------------------------

#: canonical 枚举(工具 schema / 入库 handoff_reason 只接受这些值)。
HANDOFF_REASON_ENUM: tuple[str, ...] = (
    "amount_dispute",
    "complaint",
    "unresolved_3rounds",
    "explicit_request",
    "step_limit",
    "security_risk",
    "other",
)

#: 枚举 → 客户可读中文(写 summary,不写 handoff_reason 列)。
HANDOFF_REASON_LABELS: dict[str, str] = {
    "amount_dispute": "金额争议",
    "complaint": "投诉/风险信号",
    "unresolved_3rounds": "连续多轮未解决",
    "explicit_request": "用户主动要求人工",
    "step_limit": "自动处理步数超限",
    "security_risk": "安全风险",
    "other": "其他原因",
}


def map_handoff_reason(raw: str) -> str:
    """把内部自然语言触发原因映射到 canonical 枚举(顺序即优先级)。"""
    text = str(raw or "").strip()
    if text in HANDOFF_REASON_ENUM:
        return text
    if "step_limit" in text or "步数" in text:
        return "step_limit"
    if "连续" in text and ("轮" in text or "3" in text):
        return "unresolved_3rounds"
    # 投诉/风险信号优先于金额争议:"风险信号(投诉/金额争议)"是升级信号,
    # 进投诉队列;真正的金额争议分支文本是"金额争议:客户声称金额…"(无投诉词)。
    if "投诉" in text or "曝光" in text or "12315" in text or "风险信号" in text:
        return "complaint"
    if "金额争议" in text or "金额不符" in text or "声称金额" in text:
        return "amount_dispute"
    if "人工" in text or "转接" in text:
        return "explicit_request"
    if "安全" in text or "越权" in text or "注入" in text or "隐私" in text:
        return "security_risk"
    return "other"


def handoff_reason_label(reason: str) -> str:
    """枚举 → 中文标签(未知值按 other 处理,不把脏值透给客户)。"""
    return HANDOFF_REASON_LABELS.get(reason, HANDOFF_REASON_LABELS["other"])


# ---------------------------------------------------------------------------
# FIX-5 辅助:政策问答必须出现"政策"字样(EDGE-002 的确定性兜底)
# ---------------------------------------------------------------------------


def ensure_policy_word(reply: str) -> str:
    """政策问答回复若缺"政策"字样,补一个确定性前缀(不改变事实内容)。"""
    body = str(reply or "")
    if "政策" in body:
        return body
    return "根据店铺政策:" + body


# ---------------------------------------------------------------------------
# FIX-8:订单号 + 售后诉求的规则快捷路径 / 越权查单统一话术
# ---------------------------------------------------------------------------

#: 订单/售后诉求词:出现任一即视为"要处理这个订单",可绕过 LLM 意图分类。
#: 只收动宾层面的诉求词,不收"订单""订单号"这类纯指代,避免"仅报订单号"被误判。
ORDER_QUERY_HINTS: tuple[str, ...] = (
    "查",
    "查询",
    "状态",
    "物流",
    "快递",
    "到哪",
    "签收",
    "退",
    "换",
    "判责",
    "责任",
    "售后",
    "退款",
    "退货",
)

#: shop__get_order 对"不存在/越权"统一返回的错误码。
ORDER_LOOKUP_DENIED_CODE = "NOT_FOUND_OR_FORBIDDEN"

#: 越权/不存在查单的标准拒绝句(对客统一口径,模型话术只能补充不能替代)。
ORDER_NOT_FOUND_CLAUSE = "未能在当前会话下找到该订单"

#: 判定"已包含标准句"的保守子串(兼容历史文案"未能在当前会话下找到您提到的订单")。
ORDER_NOT_FOUND_KEY = "未能在当前会话下找到"

#: 模型/历史文案里的近似说法:统一归一化成 ``ORDER_NOT_FOUND_CLAUSE``,避免两句并存。
_ORDER_NOT_FOUND_VARIANT_RE = re.compile(r"未能在当前会话下找到(?:您提到的)?订单")


def is_order_query_shortcut(
    text: str, context: Mapping[str, Any] | None = None
) -> bool:
    """文本/上下文含订单号形态,且文本含查询·退换·判责类诉求词。

    只看"有没有订单号 + 有没有诉求",不判断订单归属;归属由工具证据链裁决。
    """
    if not extract_order_id(text, context):
        return False
    body = str(text or "")
    return any(hint in body for hint in ORDER_QUERY_HINTS)


def order_lookup_denied(observations: Iterable[Mapping[str, Any]]) -> bool:
    """本轮工具观察里是否存在"查单被拒(不存在/越权)"的确定性证据。"""
    for item in observations or ():
        if str(item.get("name") or "") != "shop__get_order":
            continue
        if item.get("ok"):
            continue
        if str(item.get("error_code") or "") == ORDER_LOOKUP_DENIED_CODE:
            return True
    return False


def ensure_order_not_found_clause(reply: str) -> str:
    """查单被拒后,回复必须以标准句打头(模型原文保留在后,不整体替换)。

    模型或历史文案里若只有近似说法(如"未能在当前会话下找到您提到的订单"),
    先归一化成精确标准句再返回;完全没有标准句时补在正文前。
    """
    body = str(reply or "").strip()
    if ORDER_NOT_FOUND_CLAUSE in body:
        return body
    if not body:
        return ORDER_NOT_FOUND_CLAUSE + "。"
    normalized = _ORDER_NOT_FOUND_VARIANT_RE.sub(ORDER_NOT_FOUND_CLAUSE, body)
    if ORDER_NOT_FOUND_CLAUSE in normalized:
        return normalized
    return ORDER_NOT_FOUND_CLAUSE + "。" + body


# ---------------------------------------------------------------------------
# FIX-9:注入偏好后,相关意图必须在回复里逐字引用偏好正文
# ---------------------------------------------------------------------------

#: 注入块的偏好行格式:`- [preference/size_recommendation] 尺码偏好:M 偏紧,优先推荐 L`
MEMORY_LINE_RE = re.compile(r"^-\s*\[([^/\]]+)/([^\]]+)\]\s*(.+)$", re.MULTILINE)

#: 需要引用偏好的意图(偏好陈述常被判 other;政策问答等不引用,避免生硬)。
MEMORY_REFERENCE_INTENTS = frozenset({"other"})

#: 需要引用偏好的 scope(目前只有尺码推荐有"逐字引用"要求)。
MEMORY_REFERENCE_SCOPES = frozenset({"size_recommendation"})

#: 确定性引用前缀。
MEMORY_REFERENCE_PREFIX = "根据您的偏好记录:"

#: 本轮文本必须真的在问尺码/选购,才算"意图与 scope 相关";
#: 避免问候等无关 other 轮也被拼上一句偏好引用。
MEMORY_REFERENCE_HINTS: tuple[str, ...] = (
    "尺码",
    "尺寸",
    "码",
    "穿",
    "买",
    "推荐",
    "偏好",
)


def _memory_lines(memory_text: str) -> list[tuple[str, str, str]]:
    """解析注入块,返回 [(类型, scope, 正文)];不匹配的行直接丢弃。"""
    rows: list[tuple[str, str, str]] = []
    for match in MEMORY_LINE_RE.finditer(str(memory_text or "")):
        rows.append((match.group(1).strip(), match.group(2).strip(), match.group(3).strip()))
    return rows


def memory_reference_phrase(memory_text: str) -> str | None:
    """从注入块提取应逐字引用的偏好正文:优先尺码推荐 scope,否则首条。"""
    rows = _memory_lines(memory_text)
    for _kind, scope, body in rows:
        if scope in MEMORY_REFERENCE_SCOPES and body:
            return body
    for _kind, _scope, body in rows:
        if body:
            return body
    return None


def needs_memory_reference(
    memory: Mapping[str, Any] | None,
    intent_name: str | None,
    text: str = "",
) -> bool:
    """有偏好注入、意图相关、文本确实在问相关话题、且 scope 命中时才要求引用。"""
    data = memory or {}
    if int(data.get("count") or 0) <= 0:
        return False
    if str(intent_name or "") not in MEMORY_REFERENCE_INTENTS:
        return False
    scopes = {str(scope) for scope in (data.get("scopes") or ())}
    if not (scopes & MEMORY_REFERENCE_SCOPES):
        return False
    body = str(text or "")
    return any(hint in body for hint in MEMORY_REFERENCE_HINTS)


def ensure_memory_reference(reply: str, memory: Mapping[str, Any] | None) -> str:
    """回复未逐字包含偏好正文时,补一句确定性引用;已包含则原样返回。"""
    body = str(reply or "").strip()
    phrase = memory_reference_phrase(str((memory or {}).get("text") or ""))
    if not phrase or phrase in body:
        return body
    if not body:
        return MEMORY_REFERENCE_PREFIX + phrase + "。"
    return body + MEMORY_REFERENCE_PREFIX + phrase + "。"
