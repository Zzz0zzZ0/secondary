# Twenty → Hermes 二级线索跟进

## 当前测试范围

当前正式测试链路截断在发送之前：

```text
Twenty CRM（只读）→ Hermes 判断与生成 → Review UI 人工审阅
```

1. 按 `person.lifeCycle` 从 Twenty PostgreSQL 读取二级线索；
2. 联合联系人、公司背调、询盘、产品需求和销售人员信息；
3. 根据联系人信息自动选择 Email 或 LinkedIn 输出格式；
4. Hermes 按 Skill 生成客户语言正文、中文翻译和内部判断；
5. 在 Review UI 中逐条检查 CRM 输入与 Hermes 结果。

Review UI 不再从已排期线索生成临时消息。自动二级线索调度生成的有效消息会进入
`message_version.pending_review` 审核队列；Review UI 支持修改、输入新要求后
让 Hermes 重新生成、批准或拒绝。只有人工批准后才创建可投递的
`delivery_outbox` 任务。

## CRM 类型与渠道规则

候选范围以 `person.lifeCycle` 为准，不能使用目前大多为空的 `leadtype` 作为过滤条件：

| `person.lifeCycle` | Hermes 类型 |
|---|---|
| `INQUIRY` | `inquiry` |
| `QUALIFIED` | 等待本地四类二级线索分类 |
| `NO_DEMAND` | 兼容历史数据并重新分类 |

本地 Hermes 分类结果为：暂无需求、未知需求、（被）推荐、订量不够。当前阶段不写回
CRM。

二级线索模块只处理上述四类线索，不处理真正的 CRM `inquiry`。消息路由只有：

| 路由 | 人工动作 |
|---|---|
| `qualification` | 确认产品方向、用途、规格或数量；一次只问一个低负担问题 |
| `conversation_follow_up` | 销售已经回复或发送资料；到期后只做简短跟进 |
| `recommender_thanks` | 感谢推荐人；不得询问其产品需求 |
| `referred_intro` | 联系被推荐人并说明已核实的推荐关系 |
| `referral_review` | 推荐关系方向未确认，先人工核对；禁止批准发送 |
| `email_reply` | Notes 邮件：按完整会话生成直接回复或转介绍感谢；内部待办进入独立“会话动作”队列；当前只允许人工审阅 |

有具体产品、数量、报价或资料请求的 CRM 证据不会在本模块内自动升级为询盘；只有 CRM
后续将记录生命周期更新为 `inquiry`，才由其他流程处理。

CRM remark 明确记录销售已经回复或发送资料时，系统不会重新生成首次开发信。跟进时间
可靠且尚未到期时继续等待；到期后使用 `conversation_follow_up` 生成一封简短跟进；
跟进时间回退到 `createdAt` 时进入人工确认，不生成客户消息。一次人工回复之后最多再
允许一次自动跟进。

输出渠道自动确定：

1. 有邮箱时生成 Email；
2. 没有邮箱但有 LinkedIn 时生成 LinkedIn；
3. 两者都没有时仍生成 Email 格式，并添加 `CONTACT_CHANNEL_MISSING`。

## 本地审阅界面

测试阶段可运行：

```bash
# 仅本机访问时绑定回环地址
REVIEW_UI_HOST=127.0.0.1 ./bin/start-review-ui
```

默认直接使用已构建的前端。修改 `review_web/src` 后可执行
`REVIEW_UI_REBUILD=true ./bin/start-review-ui`，缺少前端依赖时会自动运行 `npm ci`。

页面不再从本地已排期队列生成临时预览。消息由正式 Poller 到期生成并进入
`pending_review` 后，页面可保存人工修改、按审核要求重新生成、批准或拒绝；重新生成
结果仍停留在审核队列。渠道由 CRM 联系信息自动确定。Review UI 启动时会执行 Outbox
数据库预检。

Poller 已正式读取二级联系人的 Notes 邮件。默认每 10 分钟扫描最近 500 位有邮件 Notes 的
联系人，每轮最多分析 3 条；相同最新 Note 只处理一次，`no_action` 和等待客户回复也会写入
本地幂等状态，不会重复消耗 Hermes。首次上线会把既有 Notes 登记为基线，仅自动处理此后
出现的新 Note；历史回放必须显式设置 `HERMES_NOTES_PROCESS_EXISTING=true`。Hermes 先使用
完整邮件上下文独立判断直接回复、内部待办、
转介绍、无需发送或人工判断；直接回复、转介绍感谢和内部待办才由第二次独立调用生成文案，
生成阶段不能修改动作。直接回复和转介绍感谢进入消息审核队列；`internal_task` 和
`manual_review` 进入独立“会话动作”队列。内部待办同时展示一份销售处理草稿，供销售在
邮箱中人工添加附件后复制完善，不会进入消息队列或 Outbox。会话动作可查看 Notes 原文与
判断理由并标记已处理或忽略。本地校验失败时最多纠错重试一次。Notes 消息在前端可
修改、重新生成或拒绝；当前仍采用审阅模式，前后端禁止批准，不会创建
`delivery_outbox`。诊断命令和详细边界见 [`notes_trial/README.md`](notes_trial/README.md)。
标题以“邮件沟通记录”开头的旧 Notes 不参与判断。审核页的销售归属来自联系人
`createdByName`；回信署名与发件账号优先采用当前邮件往来使用名，并按
`<使用名>@okgmineral.com` 生成，邮件使用名缺失时才使用 CRM 创建人映射。两类身份分别
保存，邮件署名不会覆盖 CRM 销售归属。旧格式
邮件时间读取正文 `**日期**`；可信自动来源的新格式只有在邮件标识、方向和标题时间均匹配
时才使用 CRM `createdAt` 排序，引用正文中的普通 `Date:` 不作为邮件时间。

需要在前台查看完整本地链路时，分别打开三个终端：

```bash
# 终端 1：Outbox API（仅本机访问）
OUTBOX_API_HOST=127.0.0.1 ./bin/start-outbox-service

# 终端 2：常驻调度器
./bin/hermes-poller run

# 终端 3：Review UI（仅本机访问）
REVIEW_UI_HOST=127.0.0.1 ./bin/start-review-ui
```

随后访问 `http://127.0.0.1:8000`。停止前台进程使用 `Ctrl-C`；不会删除本地 SQLite 或运行日志。

## 二级线索调度

调度器每 6 小时扫描全部二级线索，使用记录 ID 分批读取，不设创建或更新时间
范围。它以 `lastFollowUp`（缺失时回退 `createdAt`）为业务时间基准，等待 48
小时沉淀后分类。单纯打开 CRM 记录造成的 `updatedAt` 变化不会重置分类或排期。
消息仅在 `next_action_at` 到期并重新核对 CRM 后生成：

```bash
./bin/hermes-poller once
./bin/hermes-poller run
```

运行状态：

```bash
./bin/hermes-poller status
./bin/hermes-poller queue --limit 50
```

Review UI 可查看二级线索队列和正式待审消息。详细说明见
[`docs/HERMES_POLLING.md`](docs/HERMES_POLLING.md)。

看板的“需人工处理”阶段汇总 `needs_review`、`needs_contact` 和 `failed`，点击后可直接
定位这些记录；“暂停与已转出”只包含 `paused` 和 `converted`。缺少联系人或消息生成失败的
记录可在详情中人工重新排期，分别用于补充 CRM 联系方式后的重试和失败后的显式重试。

当日运行报告按上海时区当天 00:00 至手动触发时刻汇总，只读访问调度 SQLite 和 Outbox
PostgreSQL，输出到 `outputs/runtime-reports/`。可在 Review UI 点击“生成当日报告”，
或使用命令行手动生成：

```bash
./bin/hermes-runtime-report
```

同一天再次触发会更新当日归档。Review UI 可查看并下载 JSON/Markdown；报告不会自动
生成、发送或提交。

### 隔离的三级线索实验

`tertiary_trial/` 仅保存已搁置的手动实验，不被 `app/`、Poller、Review UI 或 Outbox
导入和调度。需要重新评估三级线索流程时，按该目录 README 单独运行；实验输出不会提交。

## Outbox 链路

```text
Review/Approval → Outbox → 下游渠道消费者
```

Outbox 使用独立 PostgreSQL 的 `sales_automation` schema。批准和写入
`delivery_outbox` 在同一事务中完成；下游消费者通过 Outbox HTTP API 领取任务，
无需直接访问数据库。接入邮件、LinkedIn 或其他渠道消费者时参照
[`docs/OUTBOX_SENDER_QUICKSTART_CN.md`](docs/OUTBOX_SENDER_QUICKSTART_CN.md)。

## 完整链路配置

运行交互界面：

```bash
./bin/twenty-hermes
```

依次执行：

1. `1) Configure connection`：配置只读 Twenty 和独立可写 Outbox 数据库；
2. `8) Install Python dependencies`；
3. `9) Initialize/update Outbox database`；
4. `5) Run complete CRM → Hermes pipeline`。

数据库密码不写入 `config/local.env`。本机测试可从 macOS 钥匙串服务
`twenty-hermes-outbox` 读取，未找到时才提示输入。生产环境建议给 Outbox 使用独立数据库和最小权限账号。

审核通过后，系统只把任务写入 Outbox；实际发送由独立的下游渠道消费者负责。
消费者协议、幂等键和失败处理见
[`docs/OUTBOX_DOWNSTREAM_INTEGRATION_GUIDE_CN.md`](docs/OUTBOX_DOWNSTREAM_INTEGRATION_GUIDE_CN.md)。

## 命令行方式

```bash
./bin/twenty-hermes setup
./bin/twenty-hermes migrate
./bin/twenty-hermes products
./bin/twenty-hermes run
```

消息查看、修改和批准统一在 Review UI 中完成，命令行不再提供第二套审批入口。

`products` 以只读事务读取 Twenty CRM `_product` 中未删除的 `name` 和
`ename`，与消息生成 Skill 的 `Product Portfolio` 对比，并将完整结果写入
`outputs/product-catalog-comparison.json`。报告分别列出匹配项、仅存在于 CRM 的产品
和仅存在于 Skill 的产品；该命令不会修改 CRM 或自动改写 Skill。

## Outbox HTTP 服务

下游消费者无需直接连接 PostgreSQL，可通过独立 FastAPI 服务领取任务并报告
成功或失败。服务支持邮件和 LinkedIn 路由，消费者使用按渠道/provider 限定的 Bearer
Token。面向消费者开发者的中文说明见
[`docs/OUTBOX_DOWNSTREAM_INTEGRATION_GUIDE_CN.md`](docs/OUTBOX_DOWNSTREAM_INTEGRATION_GUIDE_CN.md)，
服务部署说明见 [`docs/OUTBOX_SERVICE.md`](docs/OUTBOX_SERVICE.md)。

运行产物：

- `outputs/runs/<时间>/`：输入、Hermes 原始输出和校验结果；
- `reports/latest.html`：最近一次运行报告；
- PostgreSQL `sales_automation.*`：消息版本、审批、独立会话动作、Outbox 和发送尝试。

## 状态与故障边界

- `queued → sending → sent`：正常发送；
- HTTP 429/5xx：按 1、5、30、120 分钟退避；
- OAuth 401/403：`failed`，不盲目重试；
- 请求提交后的连接超时：`unknown`，必须人工核查，避免重复发送；
- Worker 中断留下 `sending`：不自动重置，先检查对应渠道平台后人工处理。

Twenty 连接始终执行 `BEGIN READ ONLY`，不会写回 CRM。当前模块边界见
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)，轮询与业务验证规则见
[`docs/HERMES_POLLING.md`](docs/HERMES_POLLING.md)。
