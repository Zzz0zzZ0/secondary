# Twenty → Hermes 二级线索跟进

从 Twenty CRM 只读获取联系人、公司背景和 Notes，由 Hermes 分类、安排跟进并提前生成草稿，供销售在 Review UI 中审阅。普通消息经人工批准后进入独立 Outbox，实际发送由外部邮件或 LinkedIn 消费者完成。

本 README 是接手、部署和日常维护入口，按 2026-09-11 的代码整理。文中的数量和时间参数是代码默认值；实际部署以本机配置及运行状态为准。

## 目录

- [系统组成与操作边界](#系统组成与操作边界)
- [接手现有项目](#接手现有项目)
- [新机器首次部署](#新机器首次部署)
- [配置与凭据](#配置与凭据)
- [启动、停止与重启](#启动停止与重启)
- [启动验收与日常维护](#启动验收与日常维护)
- [业务规则与审核操作](#业务规则与审核操作)
- [状态、文件与数据保存](#状态文件与数据保存)
- [备份与恢复](#备份与恢复)
- [升级、验证与推送](#升级验证与推送)
- [常见问题排查](#常见问题排查)
- [代码与进一步阅读](#代码与进一步阅读)

## 系统组成与操作边界

```text
Twenty PostgreSQL（只读）
    ↓ 联系人、推荐关系、Notes、公司及业务上下文
Poller → Hermes CLI → 本地 SQLite 排期与幂等状态
    ↓ 草稿 / 内部会话动作
Outbox PostgreSQL（sales_automation schema）
    ↕ Review UI：人工查看、修改、重生成、审阅和批准
    ↓ 普通消息批准且到期后创建投递任务
Outbox API → 外部渠道消费者 → 邮件 / LinkedIn 平台
                 └→ 回报发送结果
```

| 组件 | 入口 / 默认端口 | 职责 |
|---|---|---|
| Poller | `bin/hermes-poller` | CRM 全量对账、分类、排期、Notes 分析、草稿生成及状态维护 |
| Review UI | `bin/start-review-ui` / `8000` | 前端与 Review API，连接同一个 Outbox 数据库 |
| Outbox API | `bin/start-outbox-service` / `8010` | 带 Token 的投递任务领取和结果回报接口 |
| Hermes CLI | `HERMES_COMMAND` | 外部安装的模型运行环境，执行仓库内 Skill |
| 外部发送消费者 | 本仓库不包含 | 持有渠道账号，通过 Outbox 协议实际发送并回报 |

必须保留的边界：

- Twenty 查询使用只读事务，不写回 CRM；本地分类、排期、审阅和投递状态分别写入 SQLite 与独立 Outbox PostgreSQL。
- **生成、标记已审阅、批准、实际发送是四个不同动作。** 普通消息人工批准会产生外部消费者可领取的任务；不要把“系统不直接发信”理解为批准没有发送后果。
- Notes 直接回复目前仅供审阅，禁止批准进入 Outbox；内部待办和转介绍进入“会话动作”，标记已处理也不代表已发信。
- Review API 没有内置登录鉴权。两个启动脚本默认监听 `0.0.0.0`，首次配置应改为 `127.0.0.1`；需要远程访问时使用受控隧道或带鉴权的反向代理。公开源码不意味着服务可以直接暴露到公网。
- 本仓库不包含客户数据、真实运行配置、数据库备份或 Hermes 账号凭据。交接这些内容必须使用私密渠道。

## 接手现有项目

已有机器正在运行时，先检查，**不要重新 configure、初始化状态或再启动一个 Poller**。

1. 确认项目绝对路径、当前 Git 提交、运行用户、三个服务及外部消费者的负责人。
2. 私下接收 `config/local.env`、`config/outbox-service.env`、数据库访问方式和 Hermes CLI 的模型配置；确认钥匙串条目属于运行服务的同一 macOS 用户。
3. 检查 [启动验收](#启动验收与日常维护)，确认数据库、排期状态及待审页面对应同一套环境。
4. 升级或迁移前执行 [备份](#备份与恢复)。现有 `outputs/poller/` 与 Outbox 数据库必须一并保留。
5. 确认外部发送消费者是否正在运行、是否有 `sending` / `unknown` 任务，以及谁负责核对实际发送结果。

部分任务记录包含输入和报告的绝对路径。迁移优先保留原项目路径和运行用户；换路径时既要重新安装 LaunchAgent，也要检查历史任务的文件引用。仅复制数据库到新路径不等于完成迁移。

## 新机器首次部署

### 1. 环境要求

正式后台部署面向 macOS。需要 Git、Bash、Python 3 与 venv/pip、Node.js/npm、PostgreSQL 客户端、`jq`、Ruby、`openssl`、`curl`；安装后台服务还需要 Xcode Command Line Tools 提供的 C 编译器和 macOS 的 `codesign` / `launchctl`。

- 新环境建议 Python 3.11 或以上；现有 Python 3.9 环境也经过测试。`setup` 使用当前 PATH 中的 `python3`，不会自动选择版本。
- 前端使用 Vite 7，Node 要求 `^20.19.0 || >=22.12.0`，新环境可选 Node 22.12 或以上的受支持版本。
- CRM 脚本目前直接使用 **`/opt/homebrew/opt/libpq/bin/psql`**。这是 Apple Silicon Homebrew 路径；Intel Mac / Linux 迁移需要检查并调整脚本，单独修改 PATH 不足以解决。LaunchAgent 只支持 macOS。
- 准备可访问的 Twenty 数据库、真实 `workspace_...` schema，以及单独可写的 Outbox 数据库和账号。`migrate` 创建/升级表结构，**不会创建 PostgreSQL 数据库或角色**。
- Hermes CLI 需要另行安装并完成模型提供方认证；本项目 `setup` 只安装 Python 依赖。后台用户必须能够无交互调用 Hermes。确认所用 CLI 支持 `--oneshot`、`--toolsets` 和 `--usage-file`；模型由 Hermes 自身配置决定，仓库不会自动锁定模型。

### 2. 获取代码并安装依赖

以下命令均从项目根目录执行。已有 checkout 不要再次 clone 覆盖。

```bash
git clone https://github.com/Zzz0zzZ0/secondary.git twenty-hermes
cd twenty-hermes
./bin/twenty-hermes setup
npm --prefix review_web ci
npm --prefix review_web run build
```

Python 依赖安装到 `.venv/`，前端构建输出到 `review_web/dist/`。这两个目录都不随 Git 分发。

### 3. 配置两套数据库和 Hermes

首次部署可交互配置：

```bash
./bin/twenty-hermes configure
```

该命令会重写 `config/local.env`。**现有安装不要用它修改单个配置项**，应先备份文件再直接编辑。也可参考 [`config/local.env.example`](config/local.env.example) 手动创建本地配置；示例中的主机、schema 和 Token 都是占位符，不能直接投入运行。

至少核对：

- `TWENTY_DB_HOST/PORT/NAME/USER/PASSWORD/SSLMODE`、`TWENTY_WORKSPACE_SCHEMA`；schema 必须为真实值，符合 `workspace_[a-z0-9]+`，不能保留示例 `workspace_replace_me`。
- `OUTBOX_DB_HOST/PORT/NAME/USER/SSLMODE` 及密码来源；不要指向 Twenty 工作库。后台运行可将 `OUTBOX_DB_PASSWORD` 保存到受保护的 `local.env`，或在“钥匙串访问”中保存服务名为 `OUTBOX_KEYCHAIN_SERVICE`、账号为 `OUTBOX_DB_USER` 的密码项，并确认后台用户可读取。
- `HERMES_COMMAND` 指向可执行的 Hermes CLI，默认 `$HOME/.local/bin/hermes`。
- 在 `config/local.env` 中设置 `REVIEW_UI_HOST=127.0.0.1`；需要后台无交互启动时提前配置 CRM 密码。

生成独立服务 Token：

```bash
./scripts/setup_outbox_service_config.sh
chmod 600 config/local.env config/outbox-service.env
```

生成器创建随机 Token 和 `config/outbox-service.env`，文件已存在时会拒绝覆盖。接着编辑该文件，将 `OUTBOX_API_HOST` 改为 `127.0.0.1`。生成器不配置数据库，默认继承 `local.env`；若自行复制 [`outbox-service.env.example`](config/outbox-service.env.example)，必须核对其中的数据库覆盖项。

### 4. 检查连接并创建 Outbox 表

```bash
./bin/twenty-hermes check
./bin/twenty-hermes migrate
```

`check` 只读验证 Twenty；`migrate` 对独立 Outbox 库执行 [`migrations/`](migrations/) 中的迁移。缺少密码时部分交互命令会提示输入，这不代表后台服务已经获得密码。

完成后按照下一节启动。新安装默认把既有 Notes 登记为基线，只自动处理后续新 Note；不要为了“补全历史”删除状态库。

## 配置与凭据

启动脚本通过 Bash `source` 加载配置，因此文件是可执行的可信本地配置，不接受不可信来源。包含空格或特殊字符的值应正确引用。

### 加载顺序和密码来源

| 进程 / 命令 | 实际加载规则 |
|---|---|
| `twenty-hermes`、Poller、Review、运行报告 | 先读取 `config/local.env`；文件中的同名赋值会覆盖调用时的环境变量 |
| Outbox 服务 | 先 `local.env`，再 `outbox-service.env`，后者覆盖前者 |
| Outbox 数据库密码 | 可配置连接 URL / `OUTBOX_DB_PASSWORD`，或通过 `OUTBOX_KEYCHAIN_SERVICE` 与数据库用户名查找 macOS 钥匙串；交互命令可能再提示输入 |
| CRM 数据库密码 | 使用 `TWENTY_DB_PASSWORD`；Review 启动缺少该值时会交互询问并追加写入 `local.env`，然后设为 `600` 权限 |

因此，仅在命令前写 `REVIEW_UI_HOST=127.0.0.1` 不一定能覆盖配置文件。长期设置应写入对应文件，修改后重启相关服务。示例文件“不写 CRM 密码”的注释表达配置建议，**当前 Review 启动脚本确实可能保存密码**；接手时应按含凭据文件保护它。

Review、Poller 和 Outbox API 必须连接同一个 Outbox 库。仅 Outbox 服务读取第二份配置；库连接不一致会导致“批准了但消费者看不到”。Outbox 领取前还需要核验 CRM/Notes，因此也需要可用的 CRM 连接与同一套本地调度状态。

Token 由 `OUTBOX_CONSUMER_TOKENS_JSON` 定义渠道/provider 权限，`OUTBOX_PRODUCER_TOKEN` 用于生产者接口。不要使用示例 Token；轮换时同步更新外部消费者。不要将密码放入命令行参数、截图或公开日志。

### 常用调度参数

| 配置项 | 默认值 | 用途 / 注意事项 |
|---|---|---|
| `TWENTY_BUSINESS_TIMEZONE` | `Asia/Shanghai` | 业务日期解释；内部时间以 UTC 保存 |
| `HERMES_SECONDARY_FULL_SCAN_HOURS` | `6` | CRM 全量对账周期，不限制创建日期 |
| `HERMES_SECONDARY_SETTLE_HOURS` / `...SETTLE_JITTER_HOURS` | `48` / `24` | 新线索沉淀及分散处理时间 |
| `HERMES_SECONDARY_SCAN_BATCH_SIZE` | `500` | CRM 每批读取数量 |
| `HERMES_CLASSIFICATION_BATCH_SIZE` | `20` | 每轮分类数量 |
| `HERMES_CLASSIFICATION_BACKLOG_DELAY_MINUTES` | `10` | 分类积压的下一轮处理间隔 |
| `HERMES_CLASSIFICATION_MIN_CONFIDENCE` | `0.65` | 低置信度转人工判断 |
| `HERMES_CLASSIFICATION_TIMEOUT_SECONDS` | `600` | 分类调用超时 |
| `HERMES_MESSAGE_WORKERS` | `1` | 消息生成并发，支持 `1..4`；先确认模型限流和机器资源 |
| `HERMES_NOTES_ENABLED` | `true` | 正式 Notes 流程开关 |
| `HERMES_NOTES_SCAN_MINUTES` / `...SCAN_LIMIT` / `...BATCH_SIZE` | `10` / `500` / `3` | Notes 扫描周期、扫描范围及每轮处理量 |
| `HERMES_NOTES_MAX_ATTEMPTS` / `HERMES_POLL_MAX_ATTEMPTS` | `3` / `3` | 自动重试上限 |
| `HERMES_NOTES_PROCESS_EXISTING` | `false` | 只影响首次 Notes 基线建立；不是既有部署的通用历史重放开关 |
| `HERMES_REMOTE_RETRY_MINUTES` | `10` | 远端连接恢复重试间隔 |
| `OUTBOX_CONTACT_COOLDOWN_HOURS` | `24` | 联系人投递冷却时间，不能代替业务跟进间隔 |
| `REVIEW_UI_HOST` / `REVIEW_UI_PORT` | `0.0.0.0` / `8000` | 建议将 host 改为回环地址 |
| `OUTBOX_API_HOST` / `OUTBOX_API_PORT` | `0.0.0.0` / `8010` | 建议将 host 改为回环地址 |

`OUTBOX_AUTO_IMPORT` 控制部分生成路径的草稿入库，不是全局发送开关。Review 启动时将其设为 false，而调度器有自己的正式入库路径。需要暂停实际投递时，应协调停止外部消费者，并核对已经领取的任务。

## 启动、停止与重启

### 前台调试

确认三个服务未在后台运行，然后分别在三个终端的项目根目录执行：

```bash
# 终端 1
./bin/start-outbox-service
```

```bash
# 终端 2
./bin/hermes-poller run
```

```bash
# 终端 3
./bin/start-review-ui
```

访问 [Review UI](http://127.0.0.1:8000)。前台用 `Ctrl-C` 停止，并等待子进程退出；状态和日志仍保留。

Review 缺少构建产物或检测到主要前端源文件更新时会构建，缺少 `node_modules` 时会安装依赖。更新依赖锁文件后仍应显式执行 `npm ci` 和 `npm run build`，避免沿用旧依赖。`REVIEW_UI_REBUILD=true` 可请求强制构建，`REVIEW_UI_OPEN_BROWSER=false` 可关闭自动打开浏览器。

### macOS 常驻服务

先完成配置、依赖和数据库迁移，再执行：

```bash
./bin/twenty-hermes mac install
./bin/twenty-hermes mac status
```

`install` 会安装并立即启动/重载三个服务，不是预览操作。它创建并注册 `~/Applications/Twenty Hermes Background.app`，Bundle ID 为 `com.aceler.twenty-hermes.background`，并写入以下用户级 LaunchAgent：

- `~/Library/LaunchAgents/com.aceler.twenty-hermes.outbox.plist`
- `~/Library/LaunchAgents/com.aceler.twenty-hermes.review.plist`
- `~/Library/LaunchAgents/com.aceler.twenty-hermes.poller.plist`

服务随该用户登录启动，异常退出由 launchd 重启；它们不是无人登录时也运行的系统级服务。日志位于 `outputs/macos-service/`。项目路径写入 plist，换路径或更新后台 launcher 后需要重新安装。

macOS 提示后台应用访问本地网络时应允许，并在按应用分流的 VPN 中配置该应用。Poller 使用 `caffeinate -is`；是否能合盖运行仍受电源及 macOS 闭合显示器条件限制，不能依赖该命令绕过系统睡眠规则。

### 暂停维护与重新启动

维护前先协调外部消费者停止领取，并核对正在发送的任务。不要只 `kill` 服务 PID：KeepAlive 会重新拉起它。下面的停止命令保留 plist 和后台应用，可重复执行，跳过已卸载的服务：

```bash
.venv/bin/python - <<'PY'
import os
import subprocess
from app.macos_service import _wait_until_unloaded

domain = f"gui/{os.getuid()}"
for service in ("poller", "review", "outbox"):
    label = f"com.aceler.twenty-hermes.{service}"
    active = subprocess.run(
        ["launchctl", "print", f"{domain}/{label}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0
    if active:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=True)
        _wait_until_unloaded(domain, label)
PY
```

继续维护前确认手动启动的 Poller、Review、Outbox 及正在执行的 Hermes 子进程也已退出。停止后 `mac status` 可能显示未安装/未加载；保留的 plist 仍可用于恢复。

维护完成后，在三个服务均已停止的情况下重新加载：

```bash
.venv/bin/python - <<'PY'
import os
import subprocess
from pathlib import Path

domain = f"gui/{os.getuid()}"
for service in ("outbox", "review", "poller"):
    plist = Path.home() / "Library/LaunchAgents" / f"com.aceler.twenty-hermes.{service}.plist"
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
PY
./bin/twenty-hermes mac status
```

必须等待旧服务完成卸载再 bootstrap，否则可能遇到 macOS `Bootstrap failed: 5`。若尚未安装、plist 缺失或路径发生变化，使用 `mac install`。

`./bin/twenty-hermes mac uninstall` 用于正式移除常驻服务，会删除 plist 和后台应用；不会删除数据库、配置和运行产物。日常暂停使用上述停止步骤即可。

## 启动验收与日常维护

### 只读检查

```bash
./bin/twenty-hermes mac status
curl --fail --silent --show-error http://127.0.0.1:8000/api/health
curl --fail --silent --show-error http://127.0.0.1:8010/health
curl --fail --silent --show-error http://127.0.0.1:8010/ready
./bin/hermes-poller status --limit 5
./bin/hermes-poller queue --limit 20
```

验收应同时满足：服务没有重启循环；两个 HTTP 服务可访问；Poller 最近一次远端检查成功、运行模式为 `online`；扫描时间持续推进；Review 中能读取对应队列和草稿。`local_only` 表示远端连接不可用时保留本地运行，不能当作全链路正常。

Review `/api/health` 的 `send_enabled: false` 表示该服务不直接执行渠道发送，不代表 Outbox 队列为空或外部消费者停机。`/health` 只反映 HTTP 进程响应，Outbox `/ready` 才会查询其数据库；这两者均不能单独证明 CRM 可用，需结合 Poller 状态及日志。

日志和端口排查：

```bash
tail -n 80 outputs/macos-service/poller.err.log
tail -n 80 outputs/macos-service/review.err.log
tail -n 80 outputs/macos-service/outbox.err.log
lsof -nP -iTCP:8000 -sTCP:LISTEN
lsof -nP -iTCP:8010 -sTCP:LISTEN
```

正常运行信息也可能在同目录的 `*.out.log`。日志可能带联系人上下文或原始模型错误，向外分享前脱敏。

### 日常工作安排

| 频率 | 操作 |
|---|---|
| 每日开始工作 | 检查服务、运行模式、最近扫描时间、失败任务和 `needs_contact`；先处理客户新回复与严重逾期 |
| 每日审阅 | 检查收件人、渠道、Notes 历史、客户语言正文和完整中文对照；提前草稿可先标记已审阅，到期再批准 |
| 每日按需 | UI 点击“生成当日报告”，或执行 `./bin/hermes-runtime-report`；按上海时区汇总到触发时刻 |
| 每周 / 重要变更前 | 备份 SQLite、Outbox 及本地配置；检查磁盘、日志增长、模型用量和外部消费者异常 |
| 变更配置或升级后 | 重启受影响服务，完成只读验收，检查新生成结果及实际模型记录；无需为验收批准真实消息 |

报告写入 `outputs/runtime-reports/`，同日再次生成会更新当日归档。它只读数据库，但会写本地报告；不会自动发送或提交。

### 命令用途与副作用

| 命令 | 用途 |
|---|---|
| `hermes-poller status` / `queue` | 查看现有状态和队列 |
| `hermes-poller scan` | CRM 对账，会更新本地排期、转出状态并清理不再有效的待处理项 |
| `hermes-poller classify --limit 20` | 执行分类，调用 Hermes 并写入状态 |
| `hermes-poller dispatch --limit 20` | 处理进入生成窗口的任务，可能生成并入库草稿 |
| `hermes-poller once` | 执行一轮完整调度，会修改状态并可能生成草稿 |
| `hermes-poller run` | 正式常驻调度 |
| `twenty-hermes discover` / `export` | 只读 CRM，输出含业务数据的本地文件 |
| `twenty-hermes products` | 对比 CRM 产品目录与生成 Skill，输出本地差异报告，不修改目录 |
| `twenty-hermes run` | 旧的一次性 CRM → Hermes 流水线，可能入库；不代替正式 Poller 的维护流程 |
| `twenty-hermes report [RUN_DIR]` / `open` | 生成或打开一次性流水线的报告 |

手动执行 `scan`、`classify`、`dispatch`、`once` 前先停常驻 Poller，并等待在途任务退出。`run` 有全局锁，但不是所有手动维护入口都使用同一个全局锁，不能依靠锁保证任意组合都安全。

`hermes-poller signal <message_version_id> approved|sent|rejected|superseded` 属于内部状态同步/修复入口。没有真实投递证据时不要手动标记 `sent`；修复前需核对两套状态和外部平台。不要直接改数据库状态来“加速生成”。

## 业务规则与审核操作

### 候选范围、渠道与发送身份

- 主流程读取 `person.lifeCycle` 为 `QUALIFIED` / `NO_DEMAND` 的联系人；`leadtype` 不是主筛选条件，`SMALL_QUANTITY` 可影响订量不够分类。
- 明确存在有效推荐关系的 `NO_REPLY` 被推荐联系人可扩展进入；询盘、客户、无效或删除记录按范围规则转出。具体产品/报价证据不会在这里自动写回 CRM 或升级生命周期。
- 本地四类为暂无需求、未知需求、被推荐、订量不够。CRM 单纯 `updatedAt` 变化不会重置业务排期。
- **`source` 含 `agent`（大小写不敏感）一律走 LinkedIn；`CRMGEN_JIN`（CRM跟进）走 Email。** 指定渠道缺少有效地址进入 `needs_contact`，不自动换渠道。其余来源结合 Notes、既有偏好与联系方式判断。
- CRM 导出、普通跟进、Notes 回复和重生成共享渠道规则。Notes 原始历史渠道不改写；新增来源元数据本身不应让已处理会话再次生成。
- 审核页销售归属来自 CRM 创建人；回信署名和发件身份优先沿用实际邮件往来的使用名，缺失时回退 CRM 创建人映射。两者独立保存，详见 [身份映射](skill/generate-secondary-lead-message/references/sender-identity-map.md)。交接时核对映射与外部消费者的真实账号授权。

### 排期与提前准备

首次分类以 `lastFollowUp`、缺失时以 `createdAt` 为时间参考，默认沉淀 48 小时并加 0–24 小时抖动。已经发过消息或资料的联系人按已有上下文继续普通跟进，不重复生成首次开发信；明确业务日期和有效发信历史参与下一次排期。

| 已确认发送的跟进次数 | 暂无需求 | 未知需求 | 被推荐 | 订量不够 |
|---|---|---|---|---|
| 0 | 30–40 天 | 3–7 天 | 1–3 天 | 60–90 天 |
| 1 | 30–40 天 | 30–45 天 | 30–45 天 | 60–90 天 |
| 2 | 60–90 天 | 60–90 天 | 60–90 天 | 60–90 天 |
| 3 及以上 | 90–120 天 | 90–120 天 | 90–120 天 | 90–120 天 |

以上是默认分类间隔，不覆盖明确的业务限制。区间内日期按联系人和阶段稳定分散。只有确认实际发送才增加跟进次数；判定当前无需发信也会按规则继续排期，不代表永久停止跟进。

每人只提前准备下一轮：通常提前 **3 天**，实际跟进间隔超过 30 天时提前 **7 天**。`next_action_at` 保留计划跟进日期，失败重试另存 `generation_retry_at`。进入生成窗口不等于可以提前投递。

### 推荐与被推荐必须分开

CRM `recommendedById` 及反向关联都进入上下文，结构化关系优先于模型猜测，忽略已删除、自关联关系。关联联系人不要求与当前联系人处于同一生命周期。

推荐人不生成感谢信或普通跟进，旧 `recommender_thanks` 草稿也禁止批准。系统沿明确关系转交到被推荐人的独立联系人 ID，使用其身份、地址、渠道、历史和排期；不能把关联地址覆盖到推荐人的收件人字段。已有待审/待发送任务不会因转交重复建立。

生成前检查被推荐人完整 Notes：已有我方发信则继续跟进，有未处理来信则优先进入 Notes 会话流程。Notes 读取失败、日期不明、曾成功发送但缺少对应历史，或推荐方向不明确时转人工核对，不能直接当首次联系。

### Notes 与内部会话动作

默认每 10 分钟扫描最近 500 位有邮件 Notes 的联系人，每轮分析 3 条。首次启动建立已有 Notes 基线；后续按最新 Note 与语义状态去重，包括 `no_action` 和等待客户回复。修改 `HERMES_NOTES_PROCESS_EXISTING` 不能替代对已初始化数据库的专门历史回放方案。

Hermes 先独立判断直接回复、内部待办、转介绍、无需发送或人工判断，再由第二次调用生成需要的文案，生成步骤不能改变已确定动作。Notes 直接回复保持审阅模式；内部待办提供销售处理草稿，附件等操作由销售人工完成。会话动作“已处理 / 忽略”不创建投递。

普通跟进也会带入已有 Notes。普通联系人标题以“邮件沟通记录”开头的旧 Notes 不参与判断，但被推荐人仍读取这类历史，避免重复联系。旧格式取正文的明确邮件日期；新格式必须满足可信来源、方向及标题时间条件，引用正文的普通 `Date:` 不用于排序。详细格式见 [Notes 说明](notes_trial/README.md)。

### 审核页使用方法

| 提示 | 含义 / 操作 |
|---|---|
| 蓝色“提前审阅” | 尚未到期，可编辑、重生成、标记已审阅；不能批准投递 |
| 黄色“即将到期” | 距计划时间不超过 24 小时，仍需等到实际到期 |
| 橙色“已到期” | 计划时间已到，可在检查上下文后批准普通消息 |
| 红色“严重逾期” | 已逾期至少 48 小时，优先处理 |
| 绿色客户回复 / 灰色无排期 | 结合消息类型处理；颜色不会解除 Notes 审阅限制 |

“已审阅”独立于时间颜色，记录审核人和时间；修改或重生成会清除旧标记。时间标签每 30 秒及返回页面时更新，保留当前编辑内容。列表最多加载 1000 条，按优先级和计划日期排列。

页面采用内存缓存：保存不重建编辑器，批准/拒绝后移除对应条目并继续下一条；切换记录和筛选保留未保存内容。主动读取列表时按服务器版本失效旧缓存。**整页刷新或关闭页面会丢失未保存编辑**，缓存不写浏览器持久存储。

标记已审阅、重新生成入库、批准及领取投递等关键环节核对 CRM/Notes 新鲜度。上下文变化会阻止沿用旧稿；领取时发现变化会取消旧投递并保留文案退回待审。重新生成会备份原稿和人工编辑，同时防止覆盖生成期间的新编辑。客户新回复优先走会话处理，不能跳过冲突提示强行发送。

## 状态、文件与数据保存

| 位置 | 内容 / 维护要求 |
|---|---|
| `outputs/poller/poller.sqlite3` | 本地线索、排期、跟进次数、Notes 幂等及任务状态；核心持久数据 |
| `outputs/poller/` | 任务输入、分类、运行引用和报告；不要只保留 sqlite 文件而丢掉关联文件 |
| `outputs/runs/` | 一次性生成的输入、输出、模型用量与校验结果 |
| `outputs/review-regenerations/` | 重生成记录及 `previous-draft.json` 原稿备份 |
| `outputs/runtime-reports/` | 每日手动运行报告 |
| `outputs/macos-service/` | 三个后台服务的标准输出及错误日志 |
| `reports/latest.html` | 最近一次流水线 HTML 报告 |
| `config/local.env`、`config/outbox-service.env` | 本地连接、参数和秘密；权限 `600`，不提交 |
| PostgreSQL `sales_automation` | `message_version`、`message_approval`、`conversation_action`、`delivery_outbox`、`delivery_attempt` 等 |

SQLite 与 PostgreSQL 分担状态，二者之间没有跨数据库事务；备份、恢复和人工修复必须同时考虑。不要删除 `poller.sqlite3` 来解决堆积，它会丢失去重和历史。SQLite 使用 WAL，运行中不能直接丢弃 `-wal` / `-shm` 文件。保持各进程使用同一 `outputs/poller/`；自定义状态路径迁移需检查所有调用入口，不能只改一个环境变量。

| 状态 / 显示分组 | 含义 |
|---|---|
| `pending_review` | 正式草稿待审，可能尚未到跟进日期 |
| `needs_review` / `needs_contact` / `failed` | 看板“需人工处理”；分别核对判断、联系方式或失败原因 |
| `paused` / `converted` | “暂停与已转出”；转出也可能源于删除或不再符合范围，不等于成交 |
| `queued → sending → sent` | Outbox 正常投递过程 |
| `retry_wait` | 保留的等待重试状态；领取逻辑接受已到可用时间的记录，当前 `fail` API 不会自动把任务转入此状态 |
| `unknown` | 保留的结果不确定状态；当前 API 不自动生成此状态，看到历史记录时先查外部平台 |
| `failed` / `cancelled` | 消费者报告失败或任务被取消；`failed` 也可能来自消费者无法确认发送结果，不能当作从未发出 |

当前消费者接口仅以 `complete` 标记 `sent`，或以 `fail` 统一标记 `failed`，不根据渠道 HTTP 错误码自动安排退避重试。超时、断线等不确定结果按消费者协议回报失败后，必须先核对平台事实再人工处理；旧文档中的自动退避表不代表当前实现。Worker 中断留下的 `sending` 不会自动重置。**Outbox 的 claim 接口会领取并改变任务状态，不能用它做健康检查。** 删除的 CRM 联系人或失效推荐关系会在对账中撤下相关待处理任务，历史发送记录仍保留；外部平台已接受的发送不能由本地清理撤回。

## 备份与恢复

### 一致性备份

建议每周以及升级、迁移、批量状态维护前备份。以下为停机备份，需安排维护窗口：先停止外部消费者领取并核实在途发送，再按前文停止三个服务，确认没有其他手动任务或数据库写入者。

在同一个终端、项目根目录执行下面两段。备份在仓库外，包含客户数据和凭据，目录权限设为 `700`，后续传输及保存应加密。

```bash
umask 077
HERMES_BACKUP_DIR="$HOME/twenty-hermes-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$HERMES_BACKUP_DIR"
chmod 700 "$HERMES_BACKUP_DIR"
git rev-parse HEAD > "$HERMES_BACKUP_DIR/git-commit.txt"
git status --short > "$HERMES_BACKUP_DIR/git-status.txt"
tar --exclude='*.sock' --exclude='*.lock' -czf "$HERMES_BACKUP_DIR/local-state.tar.gz" config outputs reports
```

使用与服务端 PostgreSQL 大版本兼容的 `pg_dump` 备份 Outbox。下面从 `local.env` 读取连接字段，`-W` 交互询问密码，不将密码写入命令行。若部署只使用 `OUTBOX_DATABASE_URL`，先安全取得其连接字段再调整命令；不要把带密码的 URL 粘到终端历史。

```bash
(
  set -a
  source config/local.env
  set +a
  export PGSSLMODE="${OUTBOX_DB_SSLMODE:-prefer}"
  /opt/homebrew/opt/libpq/bin/pg_dump \
    --host="$OUTBOX_DB_HOST" --port="${OUTBOX_DB_PORT:-5432}" \
    --username="$OUTBOX_DB_USER" --dbname="$OUTBOX_DB_NAME" \
    --schema=sales_automation --format=custom --no-owner --password \
    --file="$HERMES_BACKUP_DIR/outbox.dump"
)
tar -tzf "$HERMES_BACKUP_DIR/local-state.tar.gz" > "$HERMES_BACKUP_DIR/archive-contents.txt"
/opt/homebrew/opt/libpq/bin/pg_restore --list "$HERMES_BACKUP_DIR/outbox.dump" > "$HERMES_BACKUP_DIR/outbox-contents.txt"
```

命令必须全部成功，且确认 dump 指向三服务实际共用的 Outbox 库，才能认为备份齐全。若有未提交代码，另行私密保存补丁/文件；仅记录 Git SHA 无法恢复未提交内容。钥匙串、Hermes 自身认证、外部消费者配置不在该归档内，需要独立安全交接。备份列表可读不代表已经完成恢复演练。

### 恢复顺序

1. 停止旧机器及目标机器的消费者和三个服务，保留目标当前数据备份。核对备份之后是否发生过实际发送；若有，先补齐投递事实，避免恢复旧去重状态导致重发。
2. 在目标机器准备与 `git-commit.txt` 一致的代码，优先使用原绝对路径；解压 `local-state.tar.gz` 到该项目根目录前，确认不会覆盖需要保留的数据。
3. 让数据库管理员创建独立、空的恢复数据库。用 `pg_restore` 恢复 `outbox.dump`，先做隔离验证；不要直接对生产库使用 `--clean`。
4. 修正本地连接配置，确保三个服务指向恢复后的同一个库；重新配置钥匙串、Hermes 认证、外部发送账号及文件权限。
5. 重建 `.venv`、执行 `npm ci` 和前端构建。相同版本恢复先核验结构，跨版本升级再按迁移说明执行 `migrate`。
6. 检查 SQLite 完整性、待审数量及抽样上下文、跟进日期、Notes 去重记录与 Outbox 投递历史。换路径还需核对历史输入/报告引用。
7. 重新安装/启动服务并做只读验收，最后再恢复外部消费者领取任务。

恢复 PostgreSQL 的命令模板如下；先在当前终端设置目标库的 `HERMES_RESTORE_HOST`、`HERMES_RESTORE_PORT`、`HERMES_RESTORE_USER`、`HERMES_RESTORE_DB` 和备份目录，均使用隔离恢复环境的值；若目标要求 TLS，同时按其要求设置 `PGSSLMODE` 和证书：

```bash
/opt/homebrew/opt/libpq/bin/pg_restore \
  --host="$HERMES_RESTORE_HOST" --port="$HERMES_RESTORE_PORT" \
  --username="$HERMES_RESTORE_USER" --dbname="$HERMES_RESTORE_DB" \
  --no-owner --no-privileges --exit-on-error --password \
  "$HERMES_BACKUP_DIR/outbox.dump"
```

停止写入时可检查 SQLite：

```bash
.venv/bin/python - <<'PY'
import sqlite3
with sqlite3.connect("file:outputs/poller/poller.sqlite3?mode=ro", uri=True) as db:
    result = db.execute("PRAGMA integrity_check").fetchall()
    assert result == [("ok",)], result
    print("SQLite integrity: ok")
PY
```

## 升级、验证与推送

更新前记录当前提交并备份。确认工作区修改归属，停止写入服务后再更新代码；不要在运行中的 checkout 直接切换业务代码或执行数据库迁移。

无本地分叉时可使用 `git pull --ff-only`；分叉应先人工处理，不能强推或重置覆盖工作。Python 依赖变化时重新执行 `setup`，前端依赖变化时执行 `npm ci`，迁移文件变化时先备份再执行 `migrate`。更新后台 launcher 或项目路径后用 `mac install`，普通配置修改按暂停/启动步骤重启即可。

### 开发验证

从项目根目录运行：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
review_web/node_modules/.bin/tsc --noEmit -p review_web/tsconfig.json
npm --prefix review_web run build
cc -fsyntax-only macos/background_launcher.c
git diff --check
```

浏览器回归需要已安装的 Playwright 和 Chrome。仓库未把 Playwright 声明为前端依赖；使用开发环境已有安装，通过 `NODE_PATH` 指向其 `node_modules`。先启动可访问的 Review 静态页面，再执行：

```bash
# 先设置 NODE_PATH；非默认地址时另设 REVIEW_TEST_URL
node review_web/tests/review-cache.cjs
node review_web/tests/advance-review.cjs
```

两个测试默认访问 `http://127.0.0.1:8000`，模拟所有 `/api/` 请求，不执行真实审批或发送；截图输出到本地 `outputs/`。测试环境成功不代替上线后的只读健康检查。

### 提交与回退

公开仓库只提交代码、测试、示例配置和不含客户数据的说明。推送前检查 `git status --short`、`git diff --cached --stat` 及暂存内容，特别注意真实 `.env`、Token、数据库、原始 CRM/Notes、模型输入输出和截图。使用明确文件路径暂存，不用 `git add -f` 绕过忽略规则。

默认远端为 [Zzz0zzZ0/secondary](https://github.com/Zzz0zzZ0/secondary)。先确认本地分支和目标分支；功能分支可推送后走 PR，明确需要更新主分支时使用正常 fast-forward 推送，不强推。完成后核对远端提交 SHA。

代码回退优先使用可审查的 revert 或经过验证的旧版本。**代码回退不等于数据库回退**：先检查迁移兼容性及新版本已产生的消息/发送记录，不能把旧数据库直接覆盖到已发生新投递的环境。

## 常见问题排查

| 现象 | 优先检查与处理 |
|---|---|
| 网页打不开 / 端口占用 | `mac status`、Review 日志、`lsof`；确认 host/port 配置，避免前台后台同时启动 |
| HTTP 正常但不生成 | Poller 运行模式、最近扫描、沉淀期、分类积压、计划日期与提前窗口；“联系人总数”不等于“现在应生成数” |
| 邮箱草稿明显少于领英 | 核对 `source` 路由；带 `agent` 必须走领英，CRM跟进走邮箱。再检查地址缺失、Notes 待处理、已有草稿和未进入生成窗口 |
| `local_only` / `No route to host` | 比较终端 `check`、Poller 状态和后台错误日志；终端能连不代表 LaunchAgent 可连。检查后台应用本地网络权限及 VPN 按应用路由 |
| Hermes 失败或长期堆积 | 查看任务错误、超时、实际模型认证和限流；确认后台用户可无交互调用 CLI。修复原因后再人工重排，不能用提高并发掩盖失败 |
| 部分任务未生成 | 查看 `needs_contact` / `needs_review` / `failed`、已有待审/投递、推荐身份及跟进窗口，逐项区分正常等待与失败 |
| 推荐人仍出现旧草稿 | 核对 CRM 关系及最新对账；旧推荐人感谢稿禁止批准。被推荐人必须在自己的队列中检查 Notes 后生成 |
| 联系人被删除 | 等待/执行受控 CRM 对账，核对本地 `converted`、待审/排队清理和关联关系；历史发送保留，已接受的外部发送不能撤回 |
| 未来草稿不能批准 | 正常保护；先标记已审阅，到计划时间后再批准。不要改数据库日期绕过限制 |
| Notes 消息不能批准 | 当前产品边界：仅审阅；内部待办需要销售处理，不进入 Outbox |
| 保存/批准提示上下文变化 | CRM 或 Notes 已更新；先读新上下文并重生成，不能重用旧稿强行批准 |
| UI 操作后内容丢失 | 正常局部操作保留编辑；整页刷新会清空内存缓存。核对是否主动刷新、服务器版本变化或页面运行旧构建 |
| 批准后消费者看不到任务 | 核对两份配置最终的数据库连接、渠道/provider Token 权限、任务状态及外部消费者日志 |
| `sending` / `unknown` 卡住 | 先查实际渠道平台是否已接收/发送，再按投递协议人工处理；禁止直接重置重发 |
| LaunchAgent `Bootstrap failed: 5` | 等待旧服务彻底卸载，检查 plist 路径、脚本权限和错误日志；不要连续重复安装制造竞争 |
| Poller 反复退出，提示 `Incorrect number of bindings supplied`（4 个占位符、5 个参数） | 旧版本恢复中断分类的 SQL 参数错误；更新到包含该修复的版本后重启 Poller，未完成分类会自动重新排队，无需删除或手改状态库 |

## 代码与进一步阅读

| 目录 / 文档 | 用途 |
|---|---|
| [`app/`](app/) | CLI、调度、Outbox 持久化及报告 |
| [`review_api/`](review_api/) | 人工审核与前端服务 API |
| [`app/secondary/`](app/secondary/) | 二级线索业务模型、渠道、推荐、生成与状态逻辑 |
| [`outbox_api/`](outbox_api/) | 外部消费者 HTTP 服务 |
| [`review_web/`](review_web/) | TypeScript / Vite 审核页面与浏览器测试 |
| [`skill/`](skill/) | 分类与生成 Skill、产品事实、输出结构及身份映射 |
| [`tests/`](tests/) | Python 回归测试 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 模块边界和数据流 |
| [`docs/HERMES_POLLING.md`](docs/HERMES_POLLING.md) | 调度和业务验证细节 |
| [`docs/HERMES_JUDGMENT_STANDARD.md`](docs/HERMES_JUDGMENT_STANDARD.md) | 判断标准 |
| [`docs/OUTBOX_SENDER_QUICKSTART_CN.md`](docs/OUTBOX_SENDER_QUICKSTART_CN.md) | 发送消费者快速接入 |
| [`docs/OUTBOX_DOWNSTREAM_INTEGRATION_GUIDE_CN.md`](docs/OUTBOX_DOWNSTREAM_INTEGRATION_GUIDE_CN.md) | 幂等、领取、回报和异常处理协议 |
| [`docs/OUTBOX_SERVICE.md`](docs/OUTBOX_SERVICE.md) | 独立 Outbox 服务部署；其中容器示例不等于当前完整三服务部署，需另核对 CRM 新鲜度校验和共享本地状态 |
| [`notes_trial/README.md`](notes_trial/README.md) | Notes 格式、诊断和历史实验说明；正式 Notes 已接入 Poller |
| [`tertiary_trial/README.md`](tertiary_trial/README.md) | 已搁置的隔离三级线索实验，不被正式服务导入或调度 |

带日期的旧交接和方案文档用于追溯历史，可能描述旧流程。运行操作以本 README 和当前入口代码为准；修改业务规则、启动方式、配置或状态含义时应同步更新对应文档。

### 给使用 Skill 的接手助手

故障定位建议使用 `diagnosing-bugs`，代码修改后使用 `review-gate` 核验行为，交接下一阶段工作时使用 `handoff` 并引用现有文档。它们是可选工作方式，不是项目运行依赖；交接中只记录脱敏信息。
