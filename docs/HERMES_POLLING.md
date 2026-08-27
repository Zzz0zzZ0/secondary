# CRM 二级线索发现、分类与调度

当前调度器不再采用“CRM 更新后立即生成消息”的方式。它按以下阶段运行：

```text
全部二级线索扫描
  → 48 小时数据沉淀
  → Hermes 二级线索分类
  → 按分类计算 next_action_at
  → 到期重新读取 CRM
  → 按需生成消息
  → Outbox 待审阅消息
```

CRM 全程只读，分类结果和调度时间只保存在本地 SQLite。

## 候选范围

- `lifeCycle = QUALIFIED`；
- 兼容历史 `lifeCycle = NO_DEMAND`；
- `leadtype` 为空也会进入分类；
- 排除 `INQUIRY`、`NEW`、`NO_REPLY`、`CUSTOMER` 和删除记录。

全部二级线索每 6 小时扫描一次。扫描没有日期范围，按 `person.id` 分批读取，
因此不会依赖 `updatedAt` 判断记录是否进入扫描。`updatedAt` 不作为业务跟进时间，
也不进入业务内容指纹；单纯打开记录不会重置分类或排期。

## 分类和周期

新记录或业务内容发生变化后进入 `settling`，沉淀到期时间为联系人
`lastFollowUp + 48 小时 + 0～24 小时确定性哈希偏移`。偏移按分钟分配，由联系人 ID
和业务内容指纹共同决定；同一业务版本在重启后保持不变。即使扫描时已经过期，也保留
真实历史到期时间，不再统一改写为扫描时间。Twenty 的 `lastFollowUp` 只有日期精度，系统按
`TWENTY_BUSINESS_TIMEZONE` 解释当天零点；字段为空时回退 `createdAt`，并添加
`FOLLOW_UP_TIME_FALLBACK_CREATED_AT`。Hermes 只分类，不生成消息：

| 本地分类 | CRM 中文含义 | 默认下次动作 |
|---|---|---|
| `no_current_demand` | 暂无需求 | 30-40 天 |
| `unknown_demand` | 未知需求 | 3-7 天 |
| `referred` | （被）推荐 | 1-3 天 |
| `below_moq` | 订量不够 | 60-90 天 |

`person.leadtype` 不参与候选过滤，但销售明确填写的结构化
`SMALL_QUANTITY` 对分类具有优先级，固定映射为 `below_moq`。这类记录不得进入
当前产品的普通跟进路由，只能按订量不够策略低频询问合并采购、未来用量、其他产品需求
或时机是否变化。

区间内日期根据联系人、分类和跟进次数稳定分散，避免任务集中在同一天。分类置信度低于
`0.65` 会进入 `needs_review`。业务人员可在 Review UI 中选择“暂无需求、
未知需求、（被）推荐、订量不够”完成确认或纠正；确认后从当天起按所选分类计算
`next_action_at`。

分类标准 `secondary-lead-v3` 将“分类置信度”“CRM 信息完整度”和“消息证据”分开。记录明显只有
行业关键词、目录请求或联系方式时，可以高置信度归入 `unknown_demand`，同时保留较低
的信息完整度和不足的消息证据；只有多个分类均合理或证据互相冲突时才使用低于
`0.65` 的分类置信度。

自动调度每轮以成功分类 20 条为目标；分类校验失败不占成功名额。为避免异常数据导致
单轮无限运行，每轮最多尝试 40 条。仍有已到期积压时，下一批至少等待 10 分钟，因此
默认成功排期上限约为每小时 120 条。消息生成到期时间独立计算，不受分类积压等待影响。

## 到期信号

常驻进程查询最近的 `classification_due_at`、`next_action_at` 和下次全量扫描时间，
然后直接休眠到最近的时间点。没有固定的高频到期轮询。

到期后会重新读取当前 CRM 记录：

- 已转询盘、客户、被删除或离开二级线索范围：标记为 `converted`；
- 每次完整 CRM 扫描成功后，以本次扫描集合对账本地活动记录；缺失记录同时退回待审草稿、
  取消待处理会话动作并终止未完成的生成任务。扫描中断时不执行清理；
- CRM 内容有变化：重新进入 48 小时沉淀；
- 状态未变化且仍满足条件：调用消息生成 Hermes；
- 缺少有效邮箱和 LinkedIn：保留本地结果并标记 `needs_contact`。

分类阶段还会单独识别 CRM remark 中有逐字证据的销售动作：

- 销售已经回复或发送资料，且 `lastFollowUp` 可靠但尚未到期：继续保持 `scheduled`；
- 同类记录到达所属分类的跟进窗口（未知需求为 3～7 天）：使用
  `conversation_follow_up` 生成简短跟进；
- 只有 `createdAt` 回退时间：进入 `needs_review`，不生成消息；
- 已有一次人工回复并完成一次自动跟进：进入 `paused`。

原始 remark 仍不会交给消息生成 Hermes；生成阶段只能看到分类器验证过的销售动作类型
和逐字证据引用。

## Outbox 链路

到期生成的有效消息自动导入独立 Outbox PostgreSQL，并始终以
`sales_automation.message_version.pending_review` 等待人工审阅。系统没有自动批准旁路。

人工批准时，`message_version` 和审批记录被更新，并在同一事务中创建
`delivery_outbox`。Outbox 批准和成功投递会向本地调度状态发送信号：

- `approved`：进入 `waiting_delivery`；
- `sent`：以实际发送时间计算下一次动作；
- 未知需求和被推荐联系人成功发送两次后暂停短周期跟进。

真实发送由 Outbox 下游消费者负责；调度器只生成和排队消息，不直接调用发送平台。

人工审阅固定开启，Review UI 顶部显示只读状态。低置信度分类和已生成消息都必须由人
处理，配置文件和 API 不提供关闭入口。

## 运行

常驻事件调度：

```bash
./bin/hermes-poller run
```

`run` 会自动判断 Twenty CRM 和 Outbox 是否可用：

- 两个数据库均可用时为 `online`，执行 CRM 扫描、分类、排期和到期消息生成；
- 任一数据库不可用时为 `local_only`，继续处理本地 SQLite 已缓存记录的分类和排期，
  暂停 CRM 扫描及消息生成；
- 默认每 10 分钟重新检查一次。连接恢复后先全量扫描 CRM，再恢复到期消息生成，避免
  使用离线期间的旧快照直接生成消息。

运行模式、最近连接错误和下次重试时间会写入本地 `scheduler_state`，并显示在 Review UI
运行看板和常驻进程 JSON 日志中。可通过 `HERMES_REMOTE_RETRY_MINUTES` 调整重试间隔。

`once`、`scan` 和 `dispatch` 仍保持严格模式：缺少对应数据库配置或连接失败时直接退出。
离线模式不会获取新的 CRM 数据，也不会写入 Outbox。

Review UI 看板将需要人工介入的状态集中在“需人工处理”阶段：

- `needs_review`：人工确认分类；
- `needs_contact`：在 CRM 补充有效邮箱或 LinkedIn 后，人工重新排期；
- `failed`：确认失败原因后，人工重新排期生成消息。

“暂停与已转出”阶段只包含 `paused` 和 `converted`，避免把可重试异常隐藏在暂停队列中。
重新排期是显式操作，不会自动无限重试，并写入 `manual_retry_queued` 事件：

```text
POST /api/polling/leads/{lead_id}/retry
```

当前 Review UI 仅适合在可信网络或受保护的反向代理后运行；项目本身尚未提供公网登录和
角色权限层，不应直接暴露 `8000` 端口。

## 当日运行报告

默认手动报告按 `TWENTY_BUSINESS_TIMEZONE`（默认 `Asia/Shanghai`）统计当天 00:00 至
实际触发时刻，分别呈现窗口内发生的流量和报告生成时的当前积压。同一天再次触发会更新
当日归档。任一数据源读取失败时仍会归档报告，但会明确标记“数据不完整”，不会把缺失
数据按 0 处理。

```bash
# 生成今天 00:00 至当前时刻
./bin/hermes-runtime-report

# 补生成指定日期
./bin/hermes-runtime-report --date 2026-08-02
```

报告保存在 `outputs/runtime-reports/<日期>.json` 和 `<日期>.md`，权限为仅当前用户可读写。
Review UI 提供“生成当日报告”、只读查看和下载。系统不会自动生成，也不通过业务 Outbox
发送；报告由人工触发并提交。可用 `HERMES_RUNTIME_REPORT_DIR` 覆盖归档目录。

本机常驻运行建议把 Outbox 密码交互式保存到 macOS 钥匙串，不写入配置文件：

```bash
set -a; source config/local.env; set +a
security add-generic-password -U \
  -a "$OUTBOX_DB_USER" \
  -s "$OUTBOX_KEYCHAIN_SERVICE" \
  -w
./bin/twenty-hermes migrate
```

最后的 `-w` 会安全提示输入密码。

手动执行一个完整周期：

```bash
./bin/hermes-poller once
```

独立操作：

```bash
./bin/hermes-poller scan
./bin/hermes-poller classify --limit 20
./bin/hermes-poller dispatch --limit 20
./bin/hermes-poller status
./bin/hermes-poller queue --limit 50
```

Review UI 提供：

```text
GET /api/polling/status
GET /api/polling/dashboard?days=30&run_limit=10
GET /api/polling/runs?limit=20
GET /api/polling/queue?limit=50
GET /api/polling/queue?limit=500&status=needs_review,scheduled
GET /api/polling/leads/{lead_id}
POST /api/polling/leads/{lead_id}/classification
POST /api/polling/leads/{lead_id}/retry
GET /api/review/messages?status=pending_review&limit=200
GET /api/review/messages/{message_id}
PATCH /api/review/messages/{message_id}
POST /api/review/messages/{message_id}/regenerate
POST /api/review/messages/{message_id}/approve
POST /api/review/messages/{message_id}/reject
```

页面默认展示只读运行看板，包括状态总量、处理流程、到期积压、未来 30 天排期、最近
运行和线索事件轨迹。页面不提供从 `scheduled` 队列临时生成预览的入口；消息只能由正式
调度流程到期生成并进入待审队列。只有低置信度分类记录提供人工分类确认按钮。正式待审
消息可人工修改，或输入新限制和方向让 Hermes
重新生成；重新生成后仍为 `pending_review`。批准会创建 `delivery_outbox`，拒绝不会
创建投递任务。`needs_contact` 和 `failed` 记录可通过 retry 接口显式重新排期，不会自动
无限重试。页面没有扫描或直接发送按钮。

对于“（被）推荐”记录，当前 CRM 联系人就是被推荐人和消息接收人。分类阶段只从 CRM
原文提取结构化 `recommended_by` 推荐人姓名和关系证据，不替换当前联系人，也不要求
当前联系人的姓名或 LinkedIn 在备注中重复出现。

原始 CRM 内文本只供分类和页面审阅，不会传入消息生成模型。消息模型只能读取分类阶段
输出的逐字证据片段，因此备注中的日期、标签或其他未采纳内容不会被解释为产品信息。
`COMPANY_CONTEXT_MISSING` 和 `COMPANY_CONTEXT_THIN` 仍由数据层决定。

## 本地表

- `secondary_lead_state`：每位联系人的当前分类、状态和时间戳；
- `secondary_lead_classification`：每个 CRM 内容版本的分类结果；
- `secondary_lead_event`：发现、分类、生成和 Outbox 信号审计；
- `notes_follow_up_state`：按最新 Note 记录 Notes 路由的幂等、分析、发布和重试状态；
- `scheduler_state`：下次全量二级线索扫描时间；
- `scheduler_run`：从本版本起记录每次周期的开始、完成、扫描、分类、生成和 Token
  汇总；写入失败不会阻断原有调度；
- `analysis_job` / `analysis_result`：到期消息生成任务和有效结果。

同一联系人同一 CRM 内容版本只分类一次；同一到期动作只创建一个确定性的消息任务。

## 配置

```dotenv
HERMES_SECONDARY_FULL_SCAN_HOURS=6
HERMES_SECONDARY_SETTLE_HOURS=48
HERMES_SECONDARY_SCAN_BATCH_SIZE=500
HERMES_CLASSIFICATION_MIN_CONFIDENCE=0.65
HERMES_CLASSIFICATION_TIMEOUT_SECONDS=600
HERMES_POLL_MAX_ATTEMPTS=3
HERMES_NOTES_ENABLED=true
HERMES_NOTES_SCAN_MINUTES=10
HERMES_NOTES_SCAN_LIMIT=500
HERMES_NOTES_BATCH_SIZE=3
HERMES_NOTES_MAX_ATTEMPTS=3
HERMES_NOTES_PROCESS_EXISTING=false
TWENTY_BUSINESS_TIMEZONE=Asia/Shanghai
```

Notes 路由只处理二级联系人的当前格式邮件备注，忽略标题以“邮件沟通记录”开头的旧记录。
它与旧的二级分类排期共用 Poller 进程，但使用独立状态表：最后一封为客户来信时才调用
Hermes；最后一封由销售发出、无需回复或已处理的同一 Note 不会重复调用。客户草稿进入
待审消息，内部任务和不确定项进入会话动作。当前 Notes 客户草稿只允许人工审阅，不允许
批准进入 Outbox。

首次启用时，当前最新 Notes 只写为上线基线，不自动回放历史会话；同一联系人之后出现新的
Note 才进入自动处理。需要一次性回放历史时，可临时设置
`HERMES_NOTES_PROCESS_EXISTING=true`，但应先评估 Hermes 额度和人工队列容量。

默认使用已确认的 6 小时、24 小时扫描周期；如修改配置，应同步评估 CRM 压力和业务
跟进窗口。
消息生成还有独立的 CRM 证据准入检查：如果记录没有来自 `lastFollowUp` 的可靠跟进时间，
同时分类结果的结构化消息证据为不足，到期时会直接暂停并记为
`cannot_generate`，不会调用 Hermes，也不会进入 Outbox。单独的“陶瓷”“冶金”“碳化硅”
或“小助手”等标签不能通过该检查；补充 CRM 信息后，记录会按正常变更流程重新分类、排期。
