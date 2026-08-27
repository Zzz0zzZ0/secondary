# Twenty–Hermes 代码结构

## 职责边界

- `app/secondary/domain.py`：二级线索枚举、标签和纯排期计算。
- `app/secondary/schema.py`：二级线索本地数据库结构。
- `app/secondary/classification.py`：分类结果契约与 CRM 原文证据校验。
- `app/secondary/classification_runner.py`：调用 Hermes 完成分类，不处理排期。
- `app/secondary/message_policy.py`：唯一的消息准入、候选结果规范化和结构校验。
- `app/secondary/notes_followup.py`：Notes 发现、幂等、重试和人工队列路由，不解释正文语义。
- `app/secondary/validate_candidate.py`：供流水线调用统一消息策略的命令入口。
- `app/secondary_scheduler.py`：流程编排和状态转换，不解释自然语言。

Review API 和正式 Pipeline 必须复用 `message_policy.py`，不得各自实现消息规则。

## 语义处理原则

业务语义不使用正则、关键词表、短语黑名单或标点计数判断。分类阶段输出：

- `message_evidence`
- `contact_permission`
- `customer_used_sender_name`
- `recommended_by`

其中引用字段必须逐字存在于 CRM 原文。分类契约只校验引用是否真实存在，不解释文本
含义。消息生成阶段不会接收原始 `internal_note`，只能读取已经通过契约校验的
`crm_message_evidence`。

Notes 邮件判断由 Hermes 阅读完整往来后输出结构化动作和逐字证据。代码只校验 JSON
结构、枚举、证据引用、渠道、发件身份和签名等确定性边界；不能从正文词语推断承诺、
询盘、需求、推荐或回复是否合适。语义不确定时进入人工审阅。

Notes 使用两个独立的 Hermes 契约和调用：语义阶段只能输出动作、理由和证据索引，不能
输出草稿；只有动作是 `reply` 或 `referral` 时才启动生成阶段，且生成阶段不能输出或
修改动作。两阶段结果分别校验和保存，发布适配层只组合结果，不重新解释正文语义。

Poller 每 10 分钟调用 Notes 适配器；它使用独立的 `notes_follow_up_state`，不会改变旧的
二级分类/排期状态。`no_action`、等待客户和已发布结果都按最新 Note 记忆，避免重复调用
Hermes。客户消息和内部动作仍分别进入待审消息与会话动作队列。

语义阶段还必须输出 `contact_permission`。客户明确要求停止联系时为 `blocked`，其优先级
高于转交、推荐或其他内容，并且动作只能是 `no_action`；权限冲突或不明确时为
`uncertain`，动作只能是 `manual_review`。只有 `allowed` 能进入客户消息生成和发布。

正则只允许用于邮箱、URL、时间戳、CRM Notes 标记和邮件主题前缀等结构解析或格式校验，
不得用于解释业务含义。

## 生命周期原则

构造 `SecondaryLeadScheduler` 只完成依赖和数据库结构初始化，不修复或重新排队业务
状态。维护动作由 `maintain()` 显式执行，并只在 Poller 的写入型命令或守护进程启动时
调用。Review API 复用同一个缓存实例，读取请求不会重复执行维护动作。
