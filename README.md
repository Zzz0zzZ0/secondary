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

手动 Review 预览不导入 Outbox，也不会发送消息。自动二级线索调度生成的有效消息
会进入 `message_version.pending_review` 审核队列；Review UI 支持修改、输入新要求后
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

输出渠道自动确定：

1. 有邮箱时生成 Email；
2. 没有邮箱但有 LinkedIn 时生成 LinkedIn；
3. 两者都没有时仍生成 Email 格式，并添加 `CONTACT_CHANNEL_MISSING`。

## 本地生成审阅界面

测试阶段可运行：

```bash
./bin/start-review-ui
```

默认直接使用已构建的前端。修改 `review_web/src` 后可执行
`REVIEW_UI_REBUILD=true ./bin/start-review-ui`，缺少前端依赖时会自动运行 `npm ci`。

页面从本地已排期队列选择 1～20 条记录，可按最早排期、最近分类或随机抽样，
然后逐条展示调度器实际消息输入与 Hermes 结果。预览不会改变线索状态或排期，
渠道由 CRM 联系信息自动确定。页面也可读取正式 `pending_review` 消息，保存人工
修改、按审核要求重新生成、批准或拒绝；重新生成结果仍停留在审核队列。该入口强制
设置：

```dotenv
OUTBOX_AUTO_IMPORT=false
GMAIL_SEND_ENABLED=false
EMAIL_LIVE_SEND_ENABLED=false
```

因此当前审阅测试不需要启动 Outbox 服务。

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

Review UI 可查看二级线索队列并从已排期记录生成消息预览。详细说明见
[`docs/HERMES_POLLING.md`](docs/HERMES_POLLING.md)。

当日运行报告按上海时区当天 00:00 至手动触发时刻汇总，只读访问调度 SQLite 和 Outbox
PostgreSQL，输出到 `outputs/runtime-reports/`。可在 Review UI 点击“生成当日报告”，
或使用命令行手动生成：

```bash
./bin/hermes-runtime-report
```

同一天再次触发会更新当日归档。Review UI 可查看并下载 JSON/Markdown；报告不会自动
生成、发送或提交。

## Outbox 链路

```text
Review/Approval → Outbox → Gmail 或 LinkedIn 消费者
```

Outbox 使用独立 PostgreSQL 的 `sales_automation` schema。批准和写入
`delivery_outbox` 在同一事务中完成；下游消费者通过 Outbox HTTP API 领取任务，
无需直接访问数据库。同事接入发信消费者时参照
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
4. `12) Authorize Gmail send-only access`；
5. `5) Run complete CRM → Hermes pipeline`。

数据库密码不写入 `config/local.env`。本机测试可从 macOS 钥匙串服务
`twenty-hermes-outbox` 读取，未找到时才提示输入。生产环境建议给 Outbox 使用独立数据库和最小权限账号。

## Gmail OAuth

在 Google Cloud 中创建 Desktop app OAuth client，启用 Gmail API，下载 JSON 到：

```text
~/.config/twenty-hermes/credentials/gmail-client.json
```

授权只请求 `gmail.send`。Token 保存为同目录的 `gmail-token.json`，权限为 `0600`。
程序不申请读取收件箱权限。

## 审批与安全发送（后续阶段）

运行完生成流程后：

1. `10) List review messages and Outbox`；
2. 用 `show` 命令或 HTML 报告检查正文；
3. `11) Approve one email and enqueue it`；
4. `13) Preview next Gmail delivery (no send)`；
5. 在 `config/local.env` 中将 `GMAIL_SEND_ENABLED=false` 改为 `true`；
6. `14) Send one Gmail delivery`。

默认安全配置：

```dotenv
EMAIL_SEND_MODE=redirect
EMAIL_TEST_RECIPIENT=你的Gmail地址
GMAIL_SEND_ENABLED=false
EMAIL_LIVE_SEND_ENABLED=false
```

`redirect` 会把邮件只发到测试 Gmail，并在主题中标记原客户地址。真实发送必须同时设置：

```dotenv
EMAIL_SEND_MODE=live
GMAIL_SEND_ENABLED=true
EMAIL_LIVE_SEND_ENABLED=true
```

不要在首轮测试中开启 `live`。

## 命令行方式

```bash
./bin/twenty-hermes setup
./bin/twenty-hermes migrate
./bin/twenty-hermes products
./bin/twenty-hermes run
./bin/twenty-hermes queue
./bin/twenty-hermes show MESSAGE_ID
./bin/twenty-hermes approve MESSAGE_ID --reviewer "Your Name" --yes
```

`products` 以只读事务读取 Twenty CRM `_product` 中未删除的 `name` 和
`ename`，与消息生成 Skill 的 `Product Portfolio` 对比，并将完整结果写入
`outputs/product-catalog-comparison.json`。报告分别列出匹配项、仅存在于 CRM 的产品
和仅存在于 Skill 的产品；该命令不会修改 CRM 或自动改写 Skill。

## Outbox HTTP 服务

下游消费者无需直接连接 PostgreSQL，可通过独立 FastAPI 服务领取任务、续租并报告
成功或失败。服务支持邮件和 LinkedIn 路由，消费者使用按渠道/provider 限定的 Bearer
Token。面向消费者开发者的中文说明见
[`docs/OUTBOX_DOWNSTREAM_INTEGRATION_GUIDE_CN.md`](docs/OUTBOX_DOWNSTREAM_INTEGRATION_GUIDE_CN.md)，
服务部署说明见 [`docs/OUTBOX_SERVICE.md`](docs/OUTBOX_SERVICE.md)。

运行产物：

- `outputs/runs/<时间>/`：输入、Hermes 原始输出和校验结果；
- `reports/latest.html`：最近一次运行报告；
- PostgreSQL `sales_automation.*`：版本、审批、Outbox 和发送尝试。

## 状态与故障边界

- `queued → sending → sent`：正常发送；
- HTTP 429/5xx：按 1、5、30、120 分钟退避；
- OAuth 401/403：`failed`，不盲目重试；
- 请求提交后的连接超时：`unknown`，必须人工核查，避免重复发送；
- Worker 中断留下 `sending`：不自动重置，先检查 Gmail 后人工处理。

Twenty 连接始终执行 `BEGIN READ ONLY`，不会写回 CRM。当前模块边界见
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)，轮询与业务验证规则见
[`docs/HERMES_POLLING.md`](docs/HERMES_POLLING.md)。
