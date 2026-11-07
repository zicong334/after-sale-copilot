# 任务书 W4-B:交付——一键起服务/一键评测 + README + 回滚 + S1-S4 自检

> 先读 `docs/任务书/00_通用约定.md`。前置:W4-A 已验收。

## 目标

项目达到"可演示 + 可评测 + 可回滚"的交付态;README 数字全部可复算;跑完验收 SOP 的 S1-S4。

## 只需读这几段(只读)

1. `docs/PRD_v1.md` §6(S1-S6)、§7(W4 门)
2. 03 文档第 803-822 行(§4.4 六步 SOP + 三档判定)
3. G9 卡(README 101 vs 75 的教训:数字必须脚本生成或可复算)

## 产物文件(白名单)

```
serve.bat / serve.sh               # 一键起服务(激活 .venv→seed 若空→uvicorn;打印地址)
run_eval.bat / run_eval.sh         # 一键评测(调 scripts/run_eval.py,默认 repeat=3)
backend/README.md                  # 项目 README:五条件对照、三场景、架构图(文字版)、"不做清单"(照 PRD §3)、快速开始(两条命令)、测试与评测数字(由 scripts/gen_readme_numbers.py 生成后粘贴,注明生成命令)、MOCK 声明清单、扩展点清单
backend/scripts/gen_readme_numbers.py  # 从 pytest --collect-only/评测报告 JSON 里重算 README 数字并输出 markdown 片段
backend/scripts/rollback.py        # 回滚:备份当前 db→恢复到指定 alembic 版本→校验;附使用说明
backend/tests/test_delivery.py     # ≥6:serve 脚本可执行(子进程起服务→/health 200→停)、run_eval --smoke 可跑、rollback 演练(升级→回滚→再升级)、README 数字与重算一致
docs/自检_S1-S4.md                  # 允许写这一个 docs 文件:S1 冻结三件套版本号(引用 VERSION.txt/rubric/硬断言清单路径:行号)、S2 契约图+副作用点清单(每个副作用点的幂等键/确认门 路径:行号)、S3 静态对齐(grep TODO/FIXME/NotImplemented/mock 全仓结果原文+每条 MOCK 声称核对)、S4 零成本预检(py_compile 全量+pytest -q+无 key 启动证据)
```

## 验收标准

1. 两条命令(serve/run_eval)在干净 shell 里从零跑通,贴输出
2. `gen_readme_numbers.py` 输出与 README 里的数字逐字一致,贴证据
3. S1-S4 自检文档四节齐全,每节证据=路径:行号或命令输出原文
4. pytest 全量绿;rollback 演练测试输出贴原文
5. 全仓 `grep -rn "TODO\|FIXME"` 为空或逐条豁免说明

## 禁止

- README 不许出现无法复算的数字;不许声称任何 MOCK 是真实实现
