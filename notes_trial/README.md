# Notes 二级联系人邮件诊断工具

Notes 已由 `app.secondary.notes_followup` 正式接入 Poller。本目录保留只读诊断、自检和
单联系人兼容入口，方便排查联系人 Notes 结构与 Hermes 结果。

诊断命令默认只读 Twenty CRM，不写 CRM、Review DB 或生产 SQLite，也没有批准或发送
能力。只有显式 `--publish-review` 会写一条人工审核消息或内部动作；正式生产处理无需
调用该参数，由 Poller 定期完成。
二级线索没有首次触达路径；没有 Notes、最后一封是发信、
邮件日期不可靠时都不会生成消息。

只读取当前格式的联系人邮件 Notes：标题以“邮件沟通记录”开头的旧版本会被忽略。
审核页销售归属读取联系人 `createdByName`。当前格式正文首行的销售使用名只用于回信
身份，例如 `**Buddy**` 对应 `buddy@okgmineral.com`；无法从首行识别时使用 CRM 创建人
映射，映射仍缺失才进入人工复核。CRM 销售归属与回信身份不得互相覆盖。
旧格式优先读取正文 `**日期**`。可信自动来源的新格式记录缺少该字段时，只有同时具备
邮件标识、收发方向和匹配的标题时间，才使用 CRM `createdAt` 作为邮件事件排序时间；
其他记录仍进入人工复核。引用邮件或翻译块中的普通 `Date:` 不参与时间判断。客户把既有销售
提案转到另一邮箱或联系人时按 `referral` 处理，感谢当前联系人，不重新询问产品需求，
也不承诺后续联系、报价或发信动作；正文只保留问候、一个致谢段落、结束语和署名。
客户草稿沿用最新一封 `SHOU` 原文的语言，不受更早销售发信语言影响。

## 正式 Poller 接入与人工审阅

Notes 将完整会话收敛为五种动作：`reply`、`internal_task`、`referral`、
`no_action`、`manual_review`。只有 `reply` 和 `referral` 会形成客户草稿；需要先准备
资料或核实事实的 `internal_task`，以及无法自动判断的 `manual_review`，会进入独立的
“会话动作”队列。`internal_task` 同时保存一条直接面向销售的中文执行动作，说明需要
准备、核实或完成什么；它不是邮件草稿，不会进入消息审核队列或 Outbox。

判断和生成是两次独立的 Hermes 调用。第一次只能返回动作、理由和证据索引；第二次仅在
动作是 `reply` 或 `referral` 时生成客户邮件；`internal_task` 则生成一至三步中文销售动作，
不能重新分类。代码分别校验语义、客户草稿和销售动作 JSON 契约，并把语义结果单独保存在
审阅快照中；不使用正文正则或关键词代替判断。
语义结果必须同时给出联系权限：明确停止联系时，即使同一封邮件还提到转交或推荐，也
必须是 `blocked + no_action`；权限不明确时进入人工复核。生成层和发布层都只接受
`allowed`。

如需让一条 Notes 判断结果出现在现有 Review UI，可显式指定一个联系人：

```bash
notes_trial/run.sh --record-id <person-id> --publish-review
```

这个开关只接受显式 `--record-id`。`reply` 或 `referral` 且有可靠邮箱的结果写入
`message_version.pending_review`；`internal_task` 或 `manual_review` 写入独立的
`conversation_action.pending`。重复运行使用最新 Note ID 和固定策略版本生成相同的
`run_id` 幂等处理。命令只输出消息或动作 ID、`lead_id` 和状态，不会在终端打印邮件正文
或完整邮件链。

Review UI 的“处理会话动作”只提供“标记已处理”和“忽略”。该表没有消息批准接口，
也不关联 `delivery_outbox`，所以内部动作无法进入发信链路。

Notes 消息当前仅用于人工审阅，不能批准或发送；CRM 和三级线索不会被写入。正式 Poller
只写自己的 `notes_follow_up_state` 以及人工审阅/会话动作队列。未显式传
`--publish-review` 时，以下诊断命令保持只读。

分析 5 条记录并写入本地只读报告：

```bash
notes_trial/run.sh --limit 5
```

只验证 Notes 读取、时间顺序和回复侧，不调用 Hermes：

```bash
notes_trial/run.sh --no-hermes --limit 5
```

指定一个联系人或运行内置自检：

```bash
notes_trial/run.sh --record-id <person-id>
notes_trial/run.sh --self-check
```
