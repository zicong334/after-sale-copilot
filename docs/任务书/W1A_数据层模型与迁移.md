# 任务书 W1-A:数据层——14 张表 SQLAlchemy 模型 + Alembic 迁移 + 模型测试

> 先读 `docs/任务书/00_通用约定.md` 并全程遵守。产物目录:`backend/`。

## 目标

把 03§4.1.2 的 14 张表落成 SQLAlchemy 2.0 声明式模型 + Alembic 首个迁移,约束全部写进数据库(CHECK/UNIQUE/FK/部分唯一索引),并配模型测试。这是全项目的地基:**约束交给数据库,不靠 Python 自觉**。

## 只需读这几段(参考,全部只读)

1. `docs/PRD_v1.md` §4、§5(风险契约与数据铁律)
2. `D:\科研工作台\06_课程学习\Agent三方向进阶_20261102\03_本地项目对齐_演进空间诊断.md` 第 497-603 行(§4.1 实体模板:14 表对照、三张核心表 DDL、状态机迁移表、12 条检查清单)
3. `D:\科研工作台\06_课程学习\Agent三方向进阶_20261102\learning_pack\concepts\stage7_Capstone售后Copilot设计.md` 的 G5 卡(order/refund_request DDL 参考答案)和 G2 卡(状态机三要素)
4. 正例骨架:`D:\科研工作台\10_归档封存\点头直通车项目\CLI深度使用（好像项目还没玩明白）\app\models\listing.py` 第 22-56 行(SAEnum 落列+索引、金额整数、FK ondelete)和同项目 `alembic\env.py` 第 9-17 行(target_metadata 驱动)

## 产物文件(白名单,只许写这些)

```
backend/app/__init__.py
backend/app/db.py               # engine/session:SQLite WAL、每连接 PRAGMA foreign_keys=ON、UTC now 默认
backend/app/models/__init__.py  # Base + 全部模型导出
backend/app/models/merchant.py  # merchant
backend/app/models/catalog.py   # product, product_variant, product_metric, review
backend/app/models/orders.py    # "order", order_item, refund_request
backend/app/models/chat.py      # chat_session, chat_message(attachments 存 URL 的 JSON 列,禁 base64)
backend/app/models/ticket.py    # ticket, ticket_message
backend/app/models/kb.py        # knowledge_item(含 embedding_model/dim 可空列=向量扩展点, version)
backend/app/models/eval.py      # eval_case(17 字段,见 W1-C;case_id UNIQUE)
backend/alembic.ini             # sqlalchemy.url 指向 backend/dev.db(可用环境变量覆盖)
backend/alembic/env.py          # target_metadata = Base.metadata
backend/alembic/script.py.mako
backend/alembic/versions/0001_initial.py   # 由 autogenerate 生成后人工核对
backend/tests/conftest.py       # 内存/临时文件 DB fixture,每个测试独立 schema
backend/tests/test_models.py    # 模型测试,≥25 个
backend/requirements.txt        # 只列直接依赖(不锁版本,锁版本在根 requirements.lock.txt)
```

## 实现要点(硬要求)

1. **14 张表字段**照 03§4.1.2 对照表;三张核心表(`"order"`、`refund_request`、`ticket`)的 CHECK/UNIQUE/索引照 §4.1.3 DDL 原样落,其中:
   - `refund_request`:`amount_cents INTEGER CHECK(amount_cents>0)`;`status CHECK IN ('申请','审核','退款','驳回')`;`reason CHECK IN ('质量问题','错发漏发','七天无理由','物流破损')`;`idempotency_key TEXT NOT NULL UNIQUE`;`CHECK((status='申请')=(decided_by IS NULL))`;**部分唯一索引** `CREATE UNIQUE INDEX uq_refund_active ON refund_request(order_id) WHERE status IN ('申请','审核','退款')`
   - `"order"`:`status CHECK IN ('created','paid','shipped','delivered','closed','refunded')`;`total_cents CHECK(total_cents>=0)`
   - `ticket`:`status CHECK IN ('queued','claimed','waiting_customer','closed')`;不变量 CHECK(queued⇔claimed_by IS NULL, closed 除外);索引 `(merchant_id, status, last_msg_at DESC)`
2. 每张业务表带 `merchant_id`(FK→merchant);`order_item`、`ticket_message`、`product_variant` 等纯关系/从属表可经父表推导,允许不带,但要在模型 docstring 里写明理由
3. 金额一律 `*_cents INTEGER`;时间一律 UTC(`datetime.now(timezone.utc)`,列默认值统一走 db.py 的一个函数);软删除 `deleted_at`(chat_session、knowledge_item、eval_case、review 至少这四张)
4. 状态用 Python `enum.StrEnum` + SQLAlchemy `Enum(native_enum=False)` 或直接 TEXT+CHECK——两种都可以,但 **CHECK 必须真实存在于迁移 DDL 里**(用 `native_enum=False` 时 SQLAlchemy 不自动加 CHECK,要手写)
5. `db.py`:SQLite 连接事件里执行 `PRAGMA foreign_keys=ON`;engine 开 WAL(`PRAGMA journal_mode=WAL`)
6. Alembic 迁移必须**一次 `upgrade head` 成功**(对全新空库);迁移文件里能看到全部 CHECK/UNIQUE/索引
7. 测试 ≥25 个,核心四表(order/refund_request/ticket/chat_session)每张 ≥3 个,必测:非法 status 值被 CHECK 拒、`idempotency_key` 重复被拒、`uq_refund_active` 让同一订单第二张进行中退款单被拒、负金额被拒、FK 级联(删 merchant/order 时的行为符合你声明的 ondelete)、`decided_by` 不变量(申请态带 decided_by 被拒)

## 验收标准(主会话逐条核)

1. `ROOT\.venv\Scripts\python -m pytest backend/tests -q` 全绿,回执贴原始输出
2. 删掉 dev.db 后 `ROOT\.venv\Scripts\python -m alembic -c backend/alembic.ini upgrade head` 一次成功,回执贴输出
3. 用 sqlite3 或 python 脚本 dump `refund_request` 与 `ticket` 的建表 SQL + 索引清单,证明 CHECK/UNIQUE/部分索引真实存在,回执贴原文
4. `grep -rn "TODO\|FIXME\|NotImplemented" backend/app backend/tests` 输出为空(或逐条解释且主会话认可)
5. 14 张表全部出现在迁移文件里(数一遍贴清单)

## 禁止

- 不写 service 层、不写 API、不写 seed(那是 W1-B/W1-C)
- 不装 requirements.txt 之外的包;不 `pip install -e` 任何归档项目
- 不用 float 存金额;不用 naive 字符串状态而无 CHECK
