# Twenty-Hermes 交接（2026-08-27）

> 用途：新 Codex 线程开始时先阅读本文，再以只读方式核对 Git、进程、API 和数据库现场。
> 本文是 2026-08-27 15:24 CST 的快照；队列数量、进程号和网络状态会变化，不得直接当作当前事实。

## 1. 接手边界

- 仓库：`twenty-hermes-poc/`（以当前检出目录为准）
- 唯一分支：`main`
- 快照提交：`ee90c0260863d7d97be5715754ed5beda17f4f26`
- 远端：`origin/main` 与本地一致；快照时工作区干净。
- Twenty CRM 只读；本模块不得写 CRM。
- Notes 客户草稿只允许人工审阅，禁止批准和发送。
- 普通二级线索消息只有人工批准后才能进入 Outbox；项目本身不直接发信。
- 业务语义必须由 Hermes 阅读完整上下文后给出结构化结论。禁止用正则、关键词、短语黑名单或标点计数判断需求、询盘、承诺、转介绍或文案是否合适。
- 不确定语义进入人工复核；本地代码只校验结构、枚举、逐字证据、身份和渠道等确定性边界。
- 用户要求 24 小时内不做团队知识库相关工作；新线程开始时先向用户确认该限制是否仍有效，未解除前不要读取、登记或更新团队知识库。
- 不提交 `outputs/`、SQLite、日志、`.env`、运行报告、客户原文或任何凭据。

## 2. 当前正式流程

```text
Twenty CRM（二级联系人，只读）
  ├─ 常规二级线索 → Hermes 分类 → 排期 → Hermes 生成 → 人工审核 → Outbox
  └─ Notes 邮件 → Hermes 语义判断 → 独立生成
                    ├─ reply/referral → 消息审核队列（禁止批准）
                    ├─ internal_task/manual_review → 会话动作队列
                    └─ no_action → 不发布
```

### 常规二级线索

- Poller 默认每 6 小时全量扫描，不依赖 `updatedAt` 增量窗口。
- 记录沉淀 48 小时并加入 0～24 小时确定性抖动后分类。
- CRM 结构化枚举 `SMALL_QUANTITY` 对模型分类有优先级，固定映射为 `below_moq`；不得继续推送当前产品，默认进入 60～90 天低频排期。
- 全量扫描成功结束后，会清理已不属于二级联系人范围的本地活动状态：拒绝旧待审草稿、取消待处理会话动作、结束未完成任务并标记 `converted`。扫描中断时不做缺失记录清理。
- `needs_review`、`needs_contact`、`failed` 在前端“需人工处理”中展示；`paused`、`converted` 单独进入“暂停与已转出”。
- 从已排期线索临时生成预览的旧逻辑和前端入口已经删除。

### Notes 邮件

- 正式适配器：`app/secondary/notes_followup.py`；解析和 Hermes 契约核心：`notes_trial/notes_trial.py`。
- 默认每 10 分钟扫描最近 500 位有邮件 Notes 的二级联系人，每轮最多分析 3 条。
- 标题以“邮件沟通记录”开头的旧版本 Notes 被忽略；只处理当前格式。
- 发件人与方向以 Note 内容为准。CRM `createdByName` 只决定销售归属；回信署名和发件账号优先使用邮件往来使用名，缺失时才使用 CRM 创建人映射。
- 必须读取完整邮件序列；不得因为部分记录无日期而只向 Hermes 提供一封邮件。
- 判断与生成是两次独立 Hermes 调用。第一阶段不能写正文，第二阶段不能修改动作。
- 资料、附件、报价、证书、KYC、规格或待核实公司事实通常形成 `internal_task`，生成带方括号占位符的销售处理草稿；附件仍由销售在邮箱中人工添加。
- 转介绍只感谢当前联系人，不询问其产品需求，也不承诺后续联系、报价或发送动作。
- Notes 使用 `notes_follow_up_state` 做策略版本、幂等、重试和发布状态管理；同一 Note 不应重复消耗 Hermes。

### 审核与 Outbox

- 客户消息存入 PostgreSQL `sales_automation.message_version`。
- 内部任务和不确定项存入 `sales_automation.conversation_action`。
- 常规消息人工批准与创建 `delivery_outbox` 在同一事务中完成。
- Notes 消息可修改、重新生成或拒绝，但审批接口必须拒绝 Notes 消息。
- Outbox 消费者只能领取、完成或失败；旧 heartbeat、租约 Token、自动重试和项目内 Gmail 发信路径已删除。

## 3. 业务知识现状

当前没有独立、可追加的业务记忆库，也没有接入团队业务知识库。业务知识分散为：

1. `skill/generate-secondary-lead-message/references/business-facts.md`
   - 产品范围、公司定位、关联公司说明；正式消息生成会将其注入 Hermes。
2. `skill/generate-secondary-lead-message/SKILL.md` 与 `references/message-policy.md`
   - 沟通经验、边界和示例；本质仍是静态提示词。
3. `notes_trial/notes_trial.py` 的 `_semantic_prompt` 与 `_draft_prompt`
   - Notes 专用经验目前直接写在两个 Prompt 中。
4. Twenty CRM
   - Notes、remark、leadType、createdBy、公司背调等单客户事实仍以 CRM 为源；处理时只复制必要快照到本地状态和审核数据库。

公司定位已统一为：Aceler 是贸易/供应链合作伙伴，依托长期生产合作方；不得称为工厂、制造商，不得声称自有产能，也不得把生产合作方称为“我们的工厂”。

## 4. 现场状态快照

核对时间：2026-08-27 15:24 CST。

- Poller：`online`，最近一次 CRM 检查无错误。
- Screen 会话：`twenty-hermes-poller`、`twenty-hermes-review`、`twenty-hermes-outbox` 均存在。
- Review UI/API：端口 `8000` 监听，`/api/health`、消息列表、会话动作列表均返回 HTTP 200。
- Outbox API：端口 `8010` 监听，`/health` 与 `/ready` 均返回 HTTP 200。
- 安全提醒：快照时 `8000` 和 `8010` 都监听所有网卡，而不是仅 `127.0.0.1`。这只代表局域网可达，不等于具备公网发布所需的认证、TLS、访问审计或限流；不得直接映射到公网。

本地 Poller 状态计数：

| 状态 | 数量 |
|---|---:|
| `converted` | 47 |
| `failed` | 9 |
| `needs_contact` | 13 |
| `needs_review` | 172 |
| `paused` | 47 |
| `scheduled` | 250 |
| `settling` | 50 |
| `waiting_review` | 180 |

Notes 状态：`baseline=16`、`completed=153`、`skipped=233`、`superseded=2`；没有 `pending`、`processing`、`retry_wait` 或 `failed`。审核数据库快照为 180 条待审消息、18 条待处理会话动作。

以上数字会被 Poller 持续更新；新线程必须重新读取，不要据此直接修改队列。

## 5. 已完成的清理与仓库状态

- 旧 Gmail 发信代码已删除。
- 旧批量刷新待审消息脚本及 README 入口已删除。
- 旧 Outbox 测试数据脚本和 `extract_candidate.py` 已删除。
- LinkedIn 上下文实验已由用户复制到其他目录，本仓库中的实现、脚本和专用测试已删除。
- `tertiary_trial/` 是已搁置、与主流程隔离的手动实验；不得被 Poller、Review UI 或 Outbox 导入。
- Git 历史已重写，文档中的旧消费者 Token 已从所有可达提交中移除；远端和本地只保留 `main`。其他旧克隆如仍存在，应重新克隆。
- 示例配置中的 Token、内网地址和工作区标识均使用占位符；真实本地配置只在被忽略的 `config/local.env` 或安全凭据存储中。

## 6. 最近验证

- 主测试：`.venv/bin/python -m unittest discover -s tests` → 107 项通过。
- 三级隔离测试：`.venv/bin/python -m unittest tertiary_trial.test_tertiary_trial` → 7 项通过。
- Python `py_compile`、Shell `bash -n`、`git diff --check` 通过。
- `review_web` 中 `npm run build` 通过。
- 历史重写后本地/远端 `main` 哈希一致，旧本地提交对象、reflog、`refs/original` 和远端 PR 引用均无残留。

## 7. 新线程建议顺序

1. 阅读本文、`README.md`、`docs/ARCHITECTURE.md` 和 `docs/HERMES_POLLING.md`。
2. 只读执行下列现场检查，不要先重启服务或重新处理队列：

   ```bash
   cd /path/to/twenty-hermes-poc
   git status -sb
   git log -1 --oneline --decorate
   ./bin/hermes-poller status
   curl -fsS http://127.0.0.1:8000/api/health
   curl -fsS http://127.0.0.1:8010/health
   curl -fsS http://127.0.0.1:8010/ready
   ```

3. 向用户报告当前 Git、Poller、Review 和 Outbox 实况，并说明与本文快照的差异。
4. 在继续生成客户消息前，保持 `business-facts.md` 的贸易/供应链合作伙伴定位。
5. 若要设计业务记忆层，先提出最小方案；不要把语义经验重新实现成代码正则，也不要在用户限制有效期内接入团队知识库。
6. 未经用户明确要求，不批量重生成、批准、发送、重启服务、清空队列、写 CRM、合并三级线索或恢复 LinkedIn 实验。

## 8. 新线程可直接使用的首条指令

```text
请先阅读 docs/HANDOFF_2026-08-27_TWENTY_HERMES.md，并以只读方式核对当前 Git、Poller、Review UI、Outbox API 和队列状态。不要调用团队知识库，不要重启服务，不要处理或重生成队列，不要写 CRM。先报告与交接快照的差异，再等待下一步指令。
```
