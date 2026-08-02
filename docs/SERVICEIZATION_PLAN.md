# Hermes 模块服务化计划

状态：**暂缓实施**

记录日期：2026-07-27

本文件仅记录后续方向。目前继续使用现有本地 Review、Poller 和 SQLite
工作方式；服务化不是当前审阅测试的前置条件。

## 目标形态

先建设“单体服务、多个进程角色”，暂不拆分微服务：

- `api`：提供 Review UI、结果查询和受控的下游接口；
- `poller`：按计划只读查询 Twenty CRM，并创建分析任务；
- `worker`：领取任务，运行 Hermes，校验并保存结果。

三个角色使用同一份代码和镜像，状态统一存放在 PostgreSQL。

## 数据与可靠性

- 将本地 `polling_cursor`、`analysis_job`、`analysis_result` 迁移到 PostgreSQL；
- 使用 `(source_updated_at, record_id)` 保存 CRM 增量游标；
- 对 `(lead_id, source_version)` 和 `analysis_result.job_id` 建立唯一约束；
- Worker 使用租约和 `FOR UPDATE SKIP LOCKED` 安全领取任务；
- 任务状态保持为 `pending → processing → completed/retry_wait/failed`；
- CRM 始终只读，分析结果不直接写回 CRM。

## 接口边界

- Review API 负责查看任务和分析结果；
- Outbox 下游接口使用独立鉴权和权限范围；
- 只有存在明确发送目标的消息才允许进入 Outbox；
- 推测邮箱必须标记 `email_inferred=true`，默认禁止直接进入 Outbox；
- `OUTBOX_ENABLED` 和真实邮件发送开关默认保持关闭。

## 建议实施顺序

1. 抽象 SQLite 数据访问层；
2. 增加 PostgreSQL schema 和迁移工具；
3. 拆分 `api`、`poller`、`worker` 启动入口；
4. 增加数据库租约、唯一约束和崩溃恢复；
5. 增加健康检查、结构化日志和运行指标；
6. 制作统一 Docker 镜像与 Docker Compose 配置；
7. 最后开放经过鉴权的 Outbox 下游接口。

## 恢复条件

只有在明确决定进入服务化阶段后才开始实施。开始前需要重新确认：

- 部署环境和 PostgreSQL 位置；
- Hermes 的运行方式和并发量；
- Review API 的访问控制；
- Outbox 是否仍只用于人工审核后的消息；
- 本地 SQLite 历史数据是否需要迁移。
