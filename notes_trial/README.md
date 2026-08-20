# Notes 二级线索实验

这个原型回答一个问题：联系人 Notes 中的真实邮件往来，能否可靠判断当前由销售回复、
由客户回复、暂不联系或必须人工确认，并只为“销售确实需要回复”的记录生成草稿。

它与二级线索主调度器隔离，只读 Twenty CRM，不写 CRM、Outbox 或生产 SQLite，
也没有批准或发送能力。二级线索没有首次触达路径；没有 Notes、最后一封是发信、
邮件日期不可靠时都不会生成消息。

交互检查 5 条记录：

```bash
notes_trial/run.sh --limit 5
```

小批量运行并将本地报告写入已忽略的 `outputs/notes-experiment/latest.json`：

```bash
notes_trial/run.sh --batch --limit 5
```

只验证 Notes 读取、时间顺序和回复侧，不调用 Hermes：

```bash
notes_trial/run.sh --batch --no-hermes --limit 5
```

指定一个联系人或运行内置自检：

```bash
notes_trial/run.sh --record-id <person-id>
notes_trial/run.sh --self-check
```
