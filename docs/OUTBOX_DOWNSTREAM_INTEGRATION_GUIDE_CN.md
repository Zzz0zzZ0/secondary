# Outbox 下游消费者实现说明

下游消费者只通过 HTTP API 对接，不需要访问 Hermes 源码、数据库或审核系统。

```text
claim → 校验任务 → 按幂等键查重 → 调用发信平台 → complete / fail
```

## 配置

```dotenv
OUTBOX_API_URL=https://<outbox-api>
OUTBOX_CONSUMER_TOKEN=<管理员分配的单个 Token>
OUTBOX_WORKER_ID=<稳定且唯一的消费者名称>
```

- Email：`channels=["email"]`、`providers=["email"]`；
- LinkedIn：`channels=["linkedin"]`、`providers=["linkedin"]`；
- Token 只能保存在密钥管理或环境变量中，不能写入代码或日志。

## 领取任务

调用 `POST /v1/deliveries/claim`。空队列返回 `{ "items": [] }`，是正常结果。
`claim` 会锁定任务；未实现结果回写前不得领取正式队列。

每个任务至少使用以下字段：

| 字段 | 消费者要求 |
|---|---|
| `delivery_id`、`lease_token` | 原样保存，用于后续回写与续租 |
| `idempotency_key` | 持久化保存；同一键只允许实际发送一次 |
| `channel`、`provider` | 必须与当前消费者匹配 |
| `recipient`、`payload` | 唯一发送目标及内容；不得自行改写收件人 |
| `sender_account_ref` | Email 为 `email:<完整邮箱>`，据此选择本地发信账号；LinkedIn 暂为 `null` |

Email 还必须校验 `payload.to == recipient`，且主题、正文均非空。LinkedIn 必须校验正文非空。

## 结果回写

所有回写都带原来的 `worker_id` 和 `lease_token`，并使用领取到的 `delivery_id`：

| 情况 | 接口 | 要求 |
|---|---|---|
| 处理接近租约到期 | `POST /v1/deliveries/{id}/heartbeat` | 先续租，再继续处理 |
| 平台明确确认已发送 | `POST /v1/deliveries/{id}/complete` | 仅在确认成功后调用 |
| 明确未发送 | `POST /v1/deliveries/{id}/fail` | `result` 为 `retryable` 或 `permanent` |
| 可能已发送但无法确认 | `POST /v1/deliveries/{id}/fail` | `result=unknown`，禁止自动重试 |

平台消息 ID 可以随 `complete` 传入，但 Outbox 不保存它；消费者可自行留作排障记录。

## 必须遵守的失败处理

- 发送平台已接受、但消费者在 `complete` 前崩溃：按 `idempotency_key` 找到原发送结果，只回写 `complete`，不得再次发送；
- 网络超时、断线等无法判断是否已发送：回写 `unknown`；
- 租约过期或不匹配（`409`）：停止处理该任务，不能继续发送；
- `401` / `403`：检查 Token 与渠道权限；`422`：检查请求字段；`503`：稍后重试服务连通性。

服务检查：`GET /health`、`GET /ready`；在线接口定义：`/docs`、`/openapi.json`。
完整 HTTP 请求体、curl 示例和无依赖 Python 封装见
[OUTBOX_SENDER_QUICKSTART_CN.md](OUTBOX_SENDER_QUICKSTART_CN.md)。
