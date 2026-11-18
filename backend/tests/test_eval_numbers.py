"""H1(2026-11-18 第四轮审计):评测硬断言的数字校验必须带语义边界。

缺陷:`eval/hard_assert.py` 原实现是 `number in reply` —— **裸数字子串匹配**,
而 `data/eval/cases_v1.json` 的 expected_numbers 全是裸数字('7'/'15'/'8'/'99'/'119'/'11900'/'48'/'1-7')。
两个方向都会错:

- **假阳性**:回复根本没讲政策,只要文中任何位置出现该数字就算过
  (`seed-order-007` 里的 7、`¥115.00` 里的 15、`13700137000` 里的 7);
- **假阴性**:回复用中文数字讲对了政策(「十五天」)反而判失败。

所以这一项断言原本**不具备判别力**,不能据总分声称「数字口径已对齐 PRD」。

修法(用户裁定):
1. 中文数字归一化(十五 → 15、七 → 7);
2. 数字必须带**业务上下文边界**——后接量词(天/日/小时/元/分/个工作日…)或前接货币符号,
   且数字本身有词边界(不是更长数字/小数的一部分)。

口径说明:**不改冻结评测集**。期望值仍是裸数字,量词要求落在**校验器**里,
功能上等价于「期望值带量词」,但不动 cases_v1.json,避免走用例版本升级程序。
"""
from __future__ import annotations

import pytest

from app.eval.hard_assert import evaluate


def _number_check_ok(expected: list[str], reply: str) -> bool:
    """只取 expected_number 这一项的判定(HardResult.to_dict 只投影 failures)。"""
    case = {"case_id": "H1", "expected_numbers": expected, "ground_truth": {}}
    artifact = {"case_id": "H1", "reply": reply, "tool_names": []}
    result = evaluate(case, artifact)
    checks = [c for c in result.checks if c.check == "expected_number"]
    assert checks, "用例应产生 expected_number 断言"
    return all(c.ok for c in checks)


# --- 假阳性:必须判失败 -----------------------------------------------------

@pytest.mark.parametrize("expected,reply", [
    (["7"], "已收到您的反馈,订单 seed-order-007 正在核实。"),
    (["15"], "您的订单实付 ¥115.00,已为您登记。"),
    (["7"], "已记录您的联系方式 13700137000,稍后回复。"),
    (["8"], "您的订单编号是 seed-order-018,已受理。"),
    (["99"], "工单号 TK-1999 已创建。"),
])
def test_bare_digit_elsewhere_no_longer_passes(expected, reply):
    """数字出现在订单号/无关金额/电话里,不得算「数字口径对齐」。"""
    assert _number_check_ok(expected, reply) is False, f"不应通过: {expected} / {reply}"


# --- 假阴性:中文数字讲对了必须判过 -----------------------------------------

@pytest.mark.parametrize("expected,reply", [
    (["15"], "质量问题自签收起十五天内可退换。"),
    (["7"], "签收后七天内可申请无理由退货。"),
    (["48"], "验收通过后四十八小时内原路退回。"),
    (["10"], "十个工作日内到账。"),
])
def test_chinese_numerals_are_normalized(expected, reply):
    """中文数字讲对了政策必须判过(原实现反判失败)。"""
    assert _number_check_ok(expected, reply) is True, f"应通过: {expected} / {reply}"


# --- 真实用例口径:必须仍然判过(防守卫过度) -------------------------------

@pytest.mark.parametrize("expected,reply", [
    (["7"], "签收后 7 天内可申请七天无理由退货。"),
    (["15"], "质量问题自签收起 15 天内可退换。"),
    (["48", "1-7"], "验收通过后 48 小时内原路退回,到账通常 1-7 个工作日。"),
    (["99", "8"], "单笔实付满 99 元包邮;不满 99 元收 8 元基础运费。"),
    (["119"], "退款金额 ¥119.00,原因「七天无理由」。"),
    (["11900", "119"], "退款金额 ¥119.00(11900 分),原因「质量问题」。"),
    (["119"], "退款金额 119 元。"),
])
def test_real_case_wordings_still_pass(expected, reply):
    """cases_v1.json 里这些数字的真实话术形态必须照常通过。"""
    assert _number_check_ok(expected, reply) is True, f"应通过: {expected} / {reply}"


def test_missing_number_still_fails():
    """正向对照:回复真的没讲这个数字,仍必须判失败。"""
    assert _number_check_ok(["7"], "退货运费由买家承担。") is False