# 三级线索独立测试

`tertiary_trial/run.sh` 是一个与 Hermes 主流程隔离的只读审计工具。
它只读取 Twenty CRM 和 Outbox 的发送事实，生成测试队列 JSON；不会写 CRM、
不会写 Outbox，也不会触碰 `app/secondary_scheduler.py` 或生产 SQLite 状态。

该目录独立保存运行脚本、Python 实现、测试、说明和本地输出，不属于主流程。

```bash
tertiary_trial/run.sh --sample-limit 20
```

默认输出到 `tertiary_trial/outputs/latest.json`。结果包含全部记录的队列计数，
每个队列只保留有限样本，避免把完整 CRM 数据复制到报告。

队列含义：

- `uncontacted`：`NEW`、无回复、Outbox 无成功发送且有联系方式；
- `no_response_waiting`：有 CRM 跟进日期，但尚未到下一次跟进窗口；
- `no_response_due`：已到跟进窗口，但仍未收到回复；
- `reactivation_or_close`：超过 30 天无回复，需人工决定重新激活或关闭；
- `contact_fact_unknown`：没有 CRM 跟进日期，不能证明是否联系过；
- `contacted_without_crm_followup`：Outbox 已成功发送，但 CRM 没有对应跟进事实；
- `response_conflict`：CRM 已出现回复，禁止继续发送；
- `missing_channel`：没有可用邮箱或 LinkedIn。

如果 Outbox 不可读，脚本会把 `NEW` 记录降级为 `contact_fact_unknown`，不会把
“查不到发送记录”误判成“从未联系”。

独立运行测试：

```bash
.venv/bin/python -m unittest tertiary_trial.test_tertiary_trial
```
