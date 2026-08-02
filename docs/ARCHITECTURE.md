# Twenty–Hermes 代码结构

## 职责边界

- `app/secondary/domain.py`：二级线索枚举、标签和纯排期计算。
- `app/secondary/schema.py`：二级线索本地数据库结构。
- `app/secondary/classification.py`：分类结果契约与 CRM 原文证据校验。
- `app/secondary/classification_runner.py`：调用 Hermes 完成分类，不处理排期。
- `app/secondary/message_policy.py`：唯一的消息准入、候选结果规范化和结构校验。
- `app/secondary/validate_candidate.py`：供流水线调用统一消息策略的命令入口。
- `app/secondary_scheduler.py`：流程编排和状态转换，不解释自然语言。

Review API 和正式 Pipeline 必须复用 `message_policy.py`，不得各自实现消息规则。

## 语义处理原则

业务语义不使用正则或关键词表判断。分类阶段输出：

- `message_evidence`
- `contact_permission`
- `customer_used_sender_name`
- `recommended_by`

其中引用字段必须逐字存在于 CRM 原文。分类契约只校验引用是否真实存在，不解释文本
含义。消息生成阶段不会接收原始 `internal_note`，只能读取已经通过契约校验的
`crm_message_evidence`。

正则只允许用于邮箱、URL、时间戳等格式校验。

## 生命周期原则

构造 `SecondaryLeadScheduler` 只完成依赖和数据库结构初始化，不修复或重新排队业务
状态。维护动作由 `maintain()` 显式执行，并只在 Poller 的写入型命令或守护进程启动时
调用。Review API 复用同一个缓存实例，读取请求不会重复执行维护动作。
