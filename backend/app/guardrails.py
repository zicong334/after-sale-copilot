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
  / ``ensure_memory_reference``;
- W16 范围内辅助信息(天气)与越界主题:``is_in_scope_weather_query``
  / ``has_delay_context`` / ``is_out_of_scope`` / ``extract_city``
  / ``format_weather_note`` / ``ensure_weather_note``。

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
        # ISS-61:只有"分隔符/反引号后紧跟高危系统命令词"才算命令注入;
        # 普通反引号(如 `seed-order-020`)与普通 `&&` 不再一律硬拦截。
        "command",
        "命令拼接",
        re.compile(
            r"(?is)(?:;|&&|\|\||\$\(|`)\s*"
            r"(?:rm|curl|wget|powershell|pwsh|cmd|bash|sh|zsh|del|copy|move|"
            r"chmod|chown|mkdir|rmdir|netstat|taskkill|whoami|ipconfig|ifconfig|"
            r"ping|nc|ssh|scp|shutdown|reboot|format|diskpart|eval|exec)\b"
            r"|rm\s+-rf\b"
        ),
        True,
    ),
    (
        # ISS-61:金额参数只在"同时出现工具名/工具调用痕迹"时才算伪造调用。
        "tool_injection",
        "伪造工具调用",
        re.compile(
            r"(?is)(调用\s*[A-Za-z_]\w*__\w+\s*工具|"
            r"tool_call|function_call|"
            r"<\|?tool\|?>|"
            r"[A-Za-z_]\w*__\w+[^\n]{0,40}?amount_cents\s*=\s*\d+)"
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
    """给 ``Percept.flags`` 用的稳定标记列表(命中才出现)。

    W6-A5 起同时并入 ``prompt_probe``:套取系统提示/上下文擦除/越狱/索要凭据,
    由图上 ``perceive`` 节点在图前拦截(见 ``PROMPT_PROBE_RULES``)。
    """
    profile = detect_injection(text)
    probe = detect_prompt_probe(text)
    flags: list[str] = []
    if profile["suspect"]:
        flags.append(INJECTION_FLAG)
        flags.extend(f"injection_kind:{kind}" for kind in profile["kinds"])
        if profile["hard"]:
            flags.append(INJECTION_BLOCK_FLAG)
    if probe["probe"]:
        flags.append(PROMPT_PROBE_FLAG)
        flags.extend(f"prompt_probe_kind:{kind}" for kind in probe["kinds"])
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
# W6-A5:prompt 探测(上下文擦除 / 套取系统提示 / 越狱 / 索要凭据)
# ---------------------------------------------------------------------------
#
# 与 FIX-3 的软注入(ADV-001:剥离注入片段后继续处理正当诉求)不同,这一类请求
# **不包含可继续服务的业务诉求**,目的是抹掉/套出系统自身的约束与秘密,因此
# 在图前直接硬拦截(固定话术、零工具、零业务事件),不进分类、不进检索。
#
# 注意:纯中文"忽略以上所有指令 + 正当售后诉求"仍走既有的软剥离路径
# (``strip_injection`` + ADV-001),不在此升级为硬拦截——否则会破坏
# "剥离注入后仍要处理正当诉求"的既有回归(test_graph.py::test_fix3_*)。
PROMPT_PROBE_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "prompt_exfil",
        "套取系统提示词",
        re.compile(
            r"(?is)(system\s*prompt|系统提示词|系统指令|初始指令|预设指令|"
            r"你的(系统)?提示(词)?|重复(你的)?(系统)?(提示|指令)|"
            r"(打印|输出|展示|原样|逐字)(一下)?(你的|收到的)?(系统)?(提示词|prompt|指令))"
        ),
    ),
    (
        "context_erase",
        "上下文擦除",
        re.compile(
            r"(?is)((忽略|无视|忘记|忘掉|清除|清空|重置|覆盖)(掉)?(你)?"
            r"(以上|之前|上述|前面)?(的)?(所有|全部)?"
            r"(对话|上下文|历史|记忆|设定|限制|规则|约束|人设|身份)|"
            r"(forget|ignore|disregard|discard)\s+(all\s+)?(your\s+)?"
            r"(previous|above|prior|earlier)\s+"
            r"(conversation|context|instructions?|prompts?|rules?|messages?))"
        ),
    ),
    (
        "jailbreak",
        "越狱/扮演",
        re.compile(
            r"(?is)(开发者模式|developer\s+mode|越狱|jailbreak|\bDAN\b|"
            r"你现在是(一个)?(没有(任何)?限制|不受限制|无限制)|"
            r"扮演(一个)?(不受限制|没有限制|无限制)|"
            r"(不受|没有)(任何)?限制的(助手|AI|模型|机器人)|"
            r"进入(管理员|root|调试|开发者)模式)"
        ),
    ),
    (
        "credential_probe",
        "索要凭据",
        re.compile(
            r"(?is)((数据库|db)?\s*(密码|口令|密钥|秘钥|api\s*key|访问令牌|token|环境变量|\.env)"
            r"\s*(是\s*(什么|多少)|发给|告诉我|给我|打印|输出|展示|透露|泄露)|"
            r"(告诉|给我|发我|提供|打印|输出|展示|透露|泄露)\s*我?(一下)?\s*(你的|你们的)?\s*"
            r"(数据库)?\s*(密码|口令|密钥|秘钥|api\s*key|token|环境变量|\.env))"
        ),
    ),
)

#: 命中"prompt 探测"时写入 ``Percept.flags`` 的固定标记(A5 图前拦截依据)。
PROMPT_PROBE_FLAG = "prompt_probe"


def detect_prompt_probe(text: str) -> dict[str, Any]:
    """确定性 prompt 探测(套取/擦除/越狱/凭据)。

    返回 ``{"probe": bool, "kinds": [...], "labels": [...]}``;与
    ``detect_injection`` 分离,便于单独审计与回归(前者决定"剥离后继续",
    这里决定"图前硬拦截")。
    """
    body = str(text or "")
    kinds: list[str] = []
    labels: list[str] = []
    for kind, label, pattern in PROMPT_PROBE_RULES:
        if pattern.search(body):
            kinds.append(kind)
            labels.append(label)
    return {"probe": bool(kinds), "kinds": kinds, "labels": labels}


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

    A3(2026-11-16):**未被任何句子 match 覆盖的字符必须逐字保留**。
    ``_SENTENCE_RE`` 的字符类排除了句读与换行,所以句首标点、连续标点、换行符
    不属于任何 match;旧实现用 ``findall`` + ``"".join`` 重组,会把这些字符静默丢掉
    (最明显是多段回复的段落换行丢失,两段被粘成一段),**即使没有任何替换发生也会改文本**。
    现改用 ``finditer`` 并把 match 之间的空隙原样拷回,保证"没被替换的部分逐字不变"。
    """
    body = str(reply or "")
    if not body:
        return body, []
    replaced: list[str] = []
    out: list[str] = []
    previous_replacement: str | None = None
    cursor = 0
    for match in _SENTENCE_RE.finditer(body):
        # 空隙(句首/连续句读、换行等)原样保留,不参与替换判定。
        if match.start() > cursor:
            out.append(body[cursor:match.start()])
        cursor = match.end()
        sentence = match.group(0)
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
    # 尾部未覆盖字符(如结尾多出来的标点/换行)同样保留。
    if cursor < len(body):
        out.append(body[cursor:])
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

#: W8-FIX:记录偏好轮的确认回执前缀(回声守卫)。
RECORDED_PREF_PREFIX = "已记录您的偏好:"

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
    intent_name: str | None = None,  # noqa: ARG001 — W8-FIX 起弃用:真实模型意图漂移,gate 与 intent 解耦
    text: str = "",
) -> bool:
    """有偏好注入、scope 命中、且本轮文本确实在问相关话题时才要求引用。

    W8-FIX(2026-11-10 主会话微修):原先额外要求 ``intent_name`` 属于
    ``MEMORY_REFERENCE_INTENTS``,真实模型把尺码偏好轮漂移到 policy_qa 等
    意图时 gate 关闭,导致 CS-MEMORY-001 三轮未逐字引用。现改为
    scope+文本双命中即触发,与 intent 标签解耦;无关文本(问运费等)
    因 hints 不命中仍不会拼接。
    """
    data = memory or {}
    if int(data.get("count") or 0) <= 0:
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


def ensure_recorded_preference_echo(reply: str, phrase: str) -> str:
    """W8-FIX 回声守卫:本轮新记录了一条可执行偏好时,确认回复必须逐字回声偏好正文。

    CS-MEMORY-001 真因:must_contain 的关键词来自"记录确认轮"的回复,而该轮
    记忆尚未注入(注入发生在任务开始),注入拼接 gate 永不触发;故对**抽取写入**
    的偏好做回声保证。隔离态(定价指令等)不回声。
    """
    body = str(reply or "").strip()
    phrase = str(phrase or "").strip()
    if not phrase or phrase in body:
        return body
    line = RECORDED_PREF_PREFIX + phrase + "。"
    return (body + " " + line).strip() if body else line


# ---------------------------------------------------------------------------
# W7-1:出网前 PII 识别正则(纯常量;替换与映射表由 app/pii.py 负责)
# ---------------------------------------------------------------------------
# 说明:正则只负责"认出片段",不负责改写。三条规则按 ID → 手机号 → 地址 的
# 顺序应用(pii.py 保证),ID 与手机号都带数字边界,避免把 18 位身份证的
# 中间 11 位误判成手机号。
PII_ID_CARD_RE = re.compile(
    r"(?<![0-9A-Za-z])([1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9A-Za-z])"
)

PII_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

#: 地址启发式:省/市/区县 + 街道门牌等后缀。只要求"像收货地址"即可,
#: 单说"杭州市西湖区"(无街道门牌)不算,避免把政策问答里的地名也涂掉。
#:
#: ISS-50:街道/路名可以连续出现(如"五常街道文一西路"),但**门牌后缀只收
#: 数字 + 号/室/栋/座/单元/层/楼**,不再无标点地继续向后吞中文动词短语
#: (旧版尾部 `[\u4e00-\u9fa5A-Za-z0-9 \-]{0,20}` 会把"帮我改一下"一起涂掉)。
PII_ADDRESS_RE = re.compile(
    r"(?:[\u4e00-\u9fa5]{2,8}(?:省|自治区|特别行政区))?"
    r"[\u4e00-\u9fa5]{2,10}(?:市|自治州|地区)"
    r"[\u4e00-\u9fa5]{2,10}(?:区|县|市|旗)"
    r"(?:[\u4e00-\u9fa5A-Za-z0-9 \-]{0,30}?"
    r"(?:路|街|道|巷|弄|村|小区|花园|苑|大厦|号楼|号院))+"
    r"(?:\d+(?:\s*[-－]\s*\d+)?"
    r"(?:\s*(?:号|室|栋|座|单元|层|楼|门))*"
    r"(?:\s*\d+\s*(?:单元|室|楼|层))*)?"
)

#: (占位前缀, 正则) —— 顺序即替换顺序;占位符形如 [PHONE_1]。
PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ID", PII_ID_CARD_RE),
    ("PHONE", PII_PHONE_RE),
    ("ADDR", PII_ADDRESS_RE),
)

#: 反解用的占位符形状;只把"本进程本轮真实分配过"的占位符换回原文。
PII_PLACEHOLDER_RE = re.compile(r"\[(?:ID|PHONE|ADDR)_\d+\]")


# ---------------------------------------------------------------------------
# W7-4:坐席建议回复的承诺性话术禁词(命中即删整句,不整段重写)
# ---------------------------------------------------------------------------
#: 到账时间承诺:"3 个工作日内到账""明天退款""24 小时退回"等。
COMMITMENT_ETA_RE = re.compile(
    r"(?:\d+\s*(?:个)?工作日|\d+\s*(?:个)?小时|\d+\s*天|今天|当天|今日|明天|明日|次日)"
    r"[^,。;!?！?]{0,12}?(?:到账|退回|退款|原路返回|返还)"
)

#: 金额承诺:"赔偿 200 元""补偿您 50 元"等。
COMMITMENT_AMOUNT_RE = re.compile(
    r"(?:赔偿|赔付|补偿|补偿金)[^,。;!?！?]{0,12}?(?:\d+(?:\.\d+)?\s*元|\d+\s*(?:元|块))"
)

#: 兜底承诺动词:"保证退款""一定到账""100% 退"等。
COMMITMENT_GUARANTEE_RE = re.compile(
    r"(?:保证|承诺|一定|百分百|100\s*%|包退|包赔)"
    r"[^,。;!?！?]{0,16}?(?:退款|退货|到账|赔偿|赔付|退回)"
)

COMMITMENT_BAN_PATTERNS: tuple[re.Pattern[str], ...] = (
    COMMITMENT_ETA_RE,
    COMMITMENT_AMOUNT_RE,
    COMMITMENT_GUARANTEE_RE,
)

#: 分句:保留标点,逐句判断,避免把整段建议都删掉。
SENTENCE_SPLIT_RE = re.compile(r"[^。！？!?\n]*[。！？!?\n]?")


def has_commitment_claim(text: str) -> bool:
    """文本是否含承诺性话术(到账时间/赔偿金额/保证类)。"""
    body = str(text or "")
    return any(pattern.search(body) for pattern in COMMITMENT_BAN_PATTERNS)


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点与换行切句,标点留在原句尾。"""
    body = str(text or "")
    if not body:
        return []
    parts = [part for part in SENTENCE_SPLIT_RE.findall(body) if part]
    return parts


def strip_commitment_sentences(text: str) -> str:
    """剔除含承诺性话术的整句,其余句子原样保留(顺序不变)。

    若剔除后为空,返回空串由调用方决定兜底话术(不做新的承诺)。
    """
    kept = [part for part in split_sentences(text) if not has_commitment_claim(part)]
    return "".join(kept).strip()


# ---------------------------------------------------------------------------
# W13-ISS-21:对客话术脱敏(内部条款代号 / 英文状态枚举 → 客户可读中文)
# ---------------------------------------------------------------------------

#: 内部条款代号(RP-xx 退货政策 / LP-xx 物流政策),禁止出现在买家可见文本里。
CLAUSE_CODE_RE = re.compile(r"\b(?:RP|LP)-\d{2}\b")

#: 条款代号 → 客户可读标题(镜像 data/policies/*.md 的二级标题,便于审计)。
CLAUSE_TITLES: dict[str, str] = {
    "LP-01": "发货时效",
    "LP-02": "包邮门槛",
    "LP-03": "偏远地区运费",
    "LP-04": "物流破损处理",
    "LP-05": "修改收货地址",
    "LP-06": "拒收与包裹退回",
    "RP-01": "七天无理由退货",
    "RP-02": "七天无理由退货运费承担",
    "RP-03": "十五天质量问题退换",
    "RP-04": "质量问题退换运费承担",
    "RP-05": "尺码不符换货",
    "RP-06": "退款到账时效",
    "RP-07": "退款金额计算",
    "RP-08": "不予退货情形(影响二次销售)",
    "RP-09": "不予退货情形(贴身衣物与食品)",
    "RP-10": "换货流程与时效",
    "RP-11": "运费险与运费补贴",
    "RP-12": "拒收与超期退回",
}

#: 订单状态英文枚举 → 对客中文标签(seed 订单状态见 models/orders.py)。
ORDER_STATUS_LABELS: dict[str, str] = {
    "created": "待付款",
    "paid": "待发货",
    "shipped": "运输中",
    "delivered": "已签收",
    "closed": "已关闭",
    "refunded": "已退款",
}


def order_status_label(status: Any) -> str:
    """订单状态英文枚举 → 中文标签;未知值原样返回(由 sanitize 兜底不外泄)。"""
    raw = str(status or "").strip()
    return ORDER_STATUS_LABELS.get(raw.lower(), raw)


def sanitize_customer_text(
    text: str, *, clause_titles: Mapping[str, str] | None = None
) -> str:
    """把买家可见文本里的内部条款代号替换为条款标题。

    - ``RP-xx`` / ``LP-xx`` 一律不外泄;优先用调用方给的 ``clause_titles``
      (来自本轮检索到的条款),缺失时回退模块内 ``CLAUSE_TITLES``,
      再缺失则替换为"相关规定",保证任何情况下不残留代号。
    """
    body = str(text or "")
    if not body:
        return body
    titles: dict[str, str] = dict(CLAUSE_TITLES)
    for key, value in (clause_titles or {}).items():
        title = str(value or "").strip()
        if key and title:
            titles[str(key)] = title

    def _replace(match: re.Match[str]) -> str:
        return titles.get(match.group(0), "相关规定")

    return CLAUSE_CODE_RE.sub(_replace, body)


# ---------------------------------------------------------------------------
# W13-ISS-20:人工会话的逆向路由(买家要求 "退出人工 / 转回 AI" 时 AI 重新接管)
# ---------------------------------------------------------------------------

#: 逆向路由关键词(去空白 + 小写后做包含匹配):明确表达"结束人工、要机器人"。
RESUME_AI_PATTERNS: tuple[str, ...] = (
    "退出人工",
    "结束人工",
    "离开人工",
    "取消人工",
    "转回ai",
    "回到ai",
    "恢复ai",
    "转ai客服",
    "转回机器人",
    "回到机器人",
    "返回机器人",
    "ai接管",
    "让ai服务",
)


def is_resume_ai_request(text: str) -> bool:
    """买家是否明确要求"退出人工、由 AI 继续服务"(ISS-20)。

    只做强匹配(短语包含),避免把"人工怎么转"之类的咨询误判成退出:
    必须同时出现"退出/结束/转回/返回"等动作 + 人工/AI/机器人目标。
    """
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    if not compact:
        return False
    return any(pattern in compact for pattern in RESUME_AI_PATTERNS)


# ---------------------------------------------------------------------------
# W16:售后范围内的辅助信息(天气)+ 越界主题拦截
#
# 定位:天气只在"售后履约语境"下作为附注出现(暴雨/延误),
# 售后 Copilot 不做通用天气助手;越界主题(投资/菜谱等)一律转人工。
# 新增天气白名单不得削弱既有越界拦截:两者互斥且越界判定优先。
# ---------------------------------------------------------------------------

#: 强档天气问句:明确在问天气,直接算"范围内辅助信息"。
WEATHER_QUESTION_HINTS: tuple[str, ...] = (
    "查天气",
    "查一下天气",
    "查查天气",
    "查询天气",
    "天气查询",
    "天气预报",
    "天气怎么样",
    "天气如何",
    "天气情况",
    "天气咋样",
    "会下雨",
    "下雨了吗",
    "会下雪",
    "下雪了吗",
    "气温多少",
    "今天多少度",
)

#: 弱档天气词:必须叠加物流/履约语境才算数,否则就变成通用天气助手。
WEATHER_KEYWORD_HINTS: tuple[str, ...] = (
    "天气",
    "下雨",
    "暴雨",
    "大雨",
    "下雪",
    "气温",
    "台风",
    "雷阵雨",
    "阵雨",
)

#: 物流/履约语境词:辅助信息只服务于售后履约链。
LOGISTICS_CONTEXT_HINTS: tuple[str, ...] = (
    "物流",
    "快递",
    "配送",
    "派送",
    "发货",
    "运输",
    "到货",
    "签收",
    "订单",
    "收货",
    "上门",
    "揽收",
)

#: 延误语境词:会话已锁订单时"履约链优先、天气只作附注"的判定依据。
DELAY_CONTEXT_HINTS: tuple[str, ...] = (
    "延误",
    "延迟",
    "晚到",
    "没到",
    "还没到",
    "没收到",
    "未收到",
    "还没收到",
    "卡住",
    "停运",
    "发不出",
    "发不了",
)

#: 越界主题:不在售后服务范围内,一律转人工(不受天气白名单影响)。
OUT_OF_SCOPE_HINTS: tuple[str, ...] = (
    "投资",
    "股票",
    "基金",
    "理财",
    "证券",
    "期货",
    "炒股",
    "比特币",
    "加密货币",
    "菜谱",
    "食谱",
    "做饭",
    "烹饪",
    "彩票",
    "游戏攻略",
    "买房",
)

#: 常见城市名(用于从买家消息里抽城市;按长度倒序匹配,先长后短)。
CITY_NAMES: tuple[str, ...] = (
    "北京", "上海", "天津", "重庆", "广州", "深圳", "杭州", "南京", "苏州",
    "成都", "武汉", "西安", "长沙", "郑州", "青岛", "大连", "沈阳", "哈尔滨",
    "长春", "济南", "合肥", "福州", "厦门", "南昌", "昆明", "贵阳", "南宁",
    "海口", "三亚", "兰州", "西宁", "银川", "乌鲁木齐", "拉萨", "呼和浩特",
    "太原", "石家庄", "宁波", "温州", "无锡", "常州", "南通", "扬州", "徐州",
    "唐山", "洛阳", "襄阳", "桂林", "珠海", "东莞", "佛山", "中山", "汕头",
    "惠州", "嘉兴", "绍兴", "金华", "台州", "湖州", "盐城", "泰州", "宿迁",
    "连云港", "镇江", "威海", "日照", "潍坊", "烟台", "淄博", "济宁", "临沂",
    "宜昌", "荆州", "黄石", "十堰", "株洲", "湘潭", "衡阳", "岳阳", "常德",
    "遵义", "大理", "丽江", "绵阳", "德阳", "自贡", "泸州", "宜宾", "乐山",
    "北海", "六盘水", "毕节", "安顺", "包头", "赤峰", "通辽", "鞍山", "抚顺",
    "丹东", "锦州", "营口", "盘锦", "四平", "辽源", "通化", "白山", "松原",
    "牡丹江", "齐齐哈尔", "大庆", "鸡西", "西双版纳", "呼伦贝尔", "鄂尔多斯",
)

#: 城市名正则(按长度倒序,允许带一个「市」后缀)。
_CITY_RE = re.compile(
    "(?:" + "|".join(sorted(set(CITY_NAMES), key=len, reverse=True)) + ")市?"
)

#: 天气来源固定附注(任务书指定口径,逐字出现在天气相关回复末尾)。
WEATHER_SOURCE_NOTE = "数据来自 Open-Meteo,仅供参考;物流时效以承运方为准"

#: 天气附注小标题(用于识别"已带附注",避免重复追加)。
WEATHER_NOTE_MARK = "【天气参考】"

#: 城市缺失时的追问话术。
WEATHER_CITY_PROMPT = "请告诉我您所在的城市(例如「武汉」),我再为您查询天气。"

#: 上游不可用时的兜底话术(不掩盖售后处置本身)。
WEATHER_UNAVAILABLE = (
    "天气信息暂时查询失败(上游数据源未响应),不影响售后处置,需要时可稍后再试。"
)


def _normalized(text: str) -> str:
    """归一化文本:去空白 + 小写(用于关键词包含匹配)。"""
    return "".join(str(text or "").split()).lower()


def _contains_hint(text: str, hints: tuple[str, ...]) -> bool:
    body = _normalized(text)
    if not body:
        return False
    return any(hint.lower() in body for hint in hints)


def is_in_scope_weather_query(text: str) -> bool:
    """是否命中"售后范围内的辅助信息-天气"白名单。

    两档:
    - 强档:明确的天气问句(查天气/天气怎么样/会下雨吗…);
    - 弱档:天气词 + 物流/履约语境(暴雨导致快递没到…)。

    弱档必须叠加物流语境,否则"今天天气不错"这类闲聊不进天气工具,
    售后 Copilot 仍然只做售后,不做通用天气助手。
    """
    if _contains_hint(text, WEATHER_QUESTION_HINTS):
        return True
    return _contains_hint(text, WEATHER_KEYWORD_HINTS) and _contains_hint(
        text, LOGISTICS_CONTEXT_HINTS
    )


def has_delay_context(text: str) -> bool:
    """是否处于"延误/未达"语境(履约链优先、天气只作附注的判定依据)。"""
    return _contains_hint(text, DELAY_CONTEXT_HINTS)


def is_out_of_scope(text: str) -> bool:
    """是否属于售后服务范围之外的主题(投资/菜谱等)→ 转人工。"""
    return _contains_hint(text, OUT_OF_SCOPE_HINTS)


def extract_city(text: str) -> str | None:
    """从买家消息里抽城市名(取最长匹配,去掉末尾「市」);抽不到返回 None。"""
    matched = _CITY_RE.search(str(text or ""))
    if not matched:
        return None
    city = matched.group(0)
    if city.endswith("市"):
        city = city[:-1]
    return city or None


def format_weather_note(weather: Mapping[str, Any] | None) -> str:
    """把天气数据拼成一段买家可读的附注(末尾固定带来源与免责口径)。"""
    data = dict(weather or {})
    if not data:
        return ""
    city = str(data.get("city") or "")
    phenomenon = str(data.get("weather") or "")
    temperature = data.get("temperature")
    apparent = data.get("apparent_temperature")
    temp_max = data.get("temp_max")
    temp_min = data.get("temp_min")
    probability = data.get("precipitation_probability")

    head = f"{city}{phenomenon}".strip()
    parts = [f"{WEATHER_NOTE_MARK}{head or '当地'}"]
    if temperature is not None:
        item = f"当前 {temperature}℃"
        if apparent is not None:
            item += f"(体感 {apparent}℃)"
        parts.append(item)
    if temp_min is not None and temp_max is not None:
        parts.append(f"今日 {temp_min}~{temp_max}℃")
    if probability is not None:
        parts.append(f"降水概率 {probability}%")
    return "，".join(parts) + "。" + WEATHER_SOURCE_NOTE


def ensure_weather_note(reply: str, weather: Mapping[str, Any] | None = None) -> str:
    """确定性追加天气附注(幂等:已带来源口径则原样返回)。

    - 有数据:追加「【天气参考】…(实况) + 来源口径」;
    - 无数据(城市缺失/上游失败):也补一句来源口径,保证口径一致;
    - 回复里已经带过来源口径:不重复追加(避免双份附注)。
    """
    text = str(reply or "").strip()
    if not text:
        return text
    if WEATHER_SOURCE_NOTE in text:
        return text
    note = format_weather_note(weather) or WEATHER_SOURCE_NOTE
    return f"{text}\n\n{note}"
