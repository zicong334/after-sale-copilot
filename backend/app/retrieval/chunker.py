"""政策 md → 条款块解析器(chunker)。

按"条款"分块(条款为最小单位),每块 = {clause_id, version, title, text, effective_date}。
条款号从 md 的 ``## {clause_id} {标题}`` 结构解析,**不手写任何条款号映射**:
新增/删除条款只需改 md,解析器自动跟随。

政策 md 约定格式(见 data/policies/*.md):

    # 退换货政策
    - 商户:seed-merchant-1
    - 版本:v1
    - 生效日期:2026-01-01

    ## RP-01 七天无理由退货
    - 生效日期:2026-01-01          # 可选,缺省继承文件头生效日期

    买家自签收之日起 7 天内……

解析规则:
- 文件头 ``- 版本:`` → 全部条款共享的 version。
- 文件头 ``- 生效日期:`` → 全局生效日期(条款内可覆盖)。
- 每个 ``## `` 行开启一条新条款;行首空白分隔后第一个 token 是 clause_id,
  其余是 title。
- 条款正文 = 该 ``## `` 与下一个 ``## `` 之间的非元信息、非空行文本。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyChunk:
    """一个政策条款块(分块最小单位)。"""

    clause_id: str
    version: str
    title: str
    text: str
    effective_date: str


class PolicyChunker:
    """把政策 markdown 解析成 PolicyChunk 列表。"""

    def parse(self, markdown: str) -> list[PolicyChunk]:
        lines = markdown.splitlines()

        version = "v1"
        global_date = ""
        chunks: list[PolicyChunk] = []

        cur_clause_id: str | None = None
        cur_title = ""
        cur_date = ""
        cur_text: list[str] = []

        def flush() -> None:
            nonlocal cur_clause_id, cur_title, cur_date, cur_text
            if cur_clause_id is not None:
                text = "\n".join(cur_text).strip()
                if text:
                    chunks.append(
                        PolicyChunk(
                            clause_id=cur_clause_id,
                            version=version,
                            title=cur_title,
                            text=text,
                            effective_date=cur_date or global_date,
                        )
                    )
            cur_clause_id = None
            cur_title = ""
            cur_date = ""
            cur_text = []

        in_header = True
        for raw in lines:
            line = raw.rstrip()
            s = line.strip()

            if s.startswith("## "):
                flush()
                in_header = False
                rest = s[3:].strip()
                parts = rest.split(None, 1)
                cur_clause_id = parts[0]
                cur_title = parts[1] if len(parts) > 1 else ""
                continue

            if in_header:
                # 文件头只认 `- 版本:` 与 `- 生效日期:`;`- 商户:` 由 load 脚本固定
                if s.startswith("- 版本:"):
                    version = s.split(":", 1)[1].strip()
                elif s.startswith("- 生效日期:"):
                    global_date = s.split(":", 1)[1].strip()
                continue

            if cur_clause_id is None:
                continue

            if s.startswith("- 生效日期:"):
                cur_date = s.split(":", 1)[1].strip()
            elif s and not s.startswith("#"):
                cur_text.append(raw)

        flush()
        return chunks
