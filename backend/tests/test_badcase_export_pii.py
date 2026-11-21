"""R14(2026-11-21 企业工程评审核验):坏例草稿不得把买家 PII 带进 git。

核验查实的出口路径:
`promote_badcase.build_case()` 把**买家原始消息**写成草稿 `user_input`,
把**坐席对客最终回复**(`badcase.agent_final`)写成 `ground_truth.answer_fact`,
落盘到 `data/eval/drafts/` —— 而该目录**未被 gitignore**,
且已有两个被 git 跟踪的草稿文件。

⚠️ 当前样本恰好干净(现存草稿 user_input 无手机号),所以这是**机制无守卫**,
不是已发生泄露。但真实买家一句「我的号码138…,订单不发货」就会**提交进 git 仓库**,
入集后成为冻结评测资产,清理成本极高。

修法:导出前用**主链路同一个 PIIMasker** 脱敏(不另写一份正则 ——
「同一概念两处定义必分叉」是第四轮 11 条发现里 3 条的共同病根)。
草稿是**待人工评审**的中间产物,占位符形态合适;人工升版时可再决定如何改写。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.models import BadCase
from scripts.promote_badcase import build_case

PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ROOT = Path(__file__).resolve().parents[2]


def _badcase(**kw) -> BadCase:
    base = dict(
        id="bc-r14-0001", ticket_id="tk-r14", merchant_id="seed-merchant-1",
        handoff_reason="other", transcript="[]", tool_chain="[]", note="",
    )
    base.update(kw)
    return BadCase(**base)


def test_buyer_phone_in_user_input_is_masked(engine):
    """买家原话里的手机号不得原样进草稿。"""
    row = _badcase(transcript=json.dumps([
        {"role": "user", "content": "我的号码13800138000,订单一直不发货"},
    ], ensure_ascii=False))
    with Session(engine) as s:
        case = build_case(s, row)
    assert not PHONE.search(case["user_input"]), case["user_input"]
    assert "[PHONE_1]" in case["user_input"]


def test_agent_final_phone_in_answer_fact_is_masked(engine):
    """坐席对客回复也可能带手机号/订单信息,answer_fact 同样要脱敏。"""
    row = _badcase(
        transcript=json.dumps([{"role": "user", "content": "我要退款"}], ensure_ascii=False),
        agent_final="已登记,回电 13900139000 联系您",
    )
    with Session(engine) as s:
        case = build_case(s, row)
    fact = str(case["ground_truth"].get("answer_fact") or "")
    assert not PHONE.search(fact), fact


def test_address_is_masked_too(engine):
    """地址也在主链路 PII 定义里,不能只脱敏手机号。"""
    row = _badcase(transcript=json.dumps([
        {"role": "user", "content": "送到杭州市西湖区文一西路 100 号 3 单元 501,东西没到"},
    ], ensure_ascii=False))
    with Session(engine) as s:
        case = build_case(s, row)
    assert "文一西路" not in case["user_input"], case["user_input"]
    assert "[ADDR_1]" in case["user_input"]


def test_clean_text_is_unchanged(engine):
    """正向:无 PII 的原话必须**逐字保留**(防守卫过度把正常语料也涂掉)。"""
    text = "我要退款,这个订单一直不发货"
    row = _badcase(transcript=json.dumps([{"role": "user", "content": text}], ensure_ascii=False))
    with Session(engine) as s:
        case = build_case(s, row)
    assert case["user_input"] == text


def test_drafts_dir_is_gitignored():
    """新草稿不得默认进 git(已跟踪的历史证据文件按 git 语义仍保留,不删证据)。"""
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/eval/drafts/" in ignore, "drafts 目录必须进 .gitignore"