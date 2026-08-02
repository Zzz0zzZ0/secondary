# Outbox 下游接口检查

本文用于 Gmail、LinkedIn 等下游快速确认 Outbox API 是否可访问、鉴权是否正确，以及
能否正常领取任务。下游不需要数据库权限。

## 1. 准备配置

```dotenv
OUTBOX_API_URL=http://<服务器地址>:8010
OUTBOX_CONSUMER_TOKEN=<管理员分配的Token>
OUTBOX_WORKER_ID=<稳定且唯一的消费者名称>
```

| 下游 | channel | provider |
|---|---|---|
| Email（下游自选 Gmail API、SMTP、Graph 等实现） | `email` | `email` |
| LinkedIn | `linkedin` | `linkedin` |

Token 不得写入源码或日志。生产环境应使用 HTTPS。

## 2. 检查服务

```bash
curl "$OUTBOX_API_URL/health"
curl "$OUTBOX_API_URL/ready"
```

预期：

```json
{"status":"ok","service":"outbox-api","version":"1.0.0"}
{"status":"ready"}
```

- `/health` 失败：地址错误、网络不通或 API 未启动；
- `/ready` 返回 `503`：Outbox 数据库不可用。

OpenAPI 页面：

```text
http://<服务器地址>:8010/docs
```

## 3. 检查鉴权和领取接口

将 `channels` 和 `providers` 换成上表中对应的值：

```bash
curl -X POST "$OUTBOX_API_URL/v1/deliveries/claim" \
  -H "Authorization: Bearer $OUTBOX_CONSUMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_id": "gmail-worker-test-01",
    "channels": ["email"],
    "providers": ["email"],
    "max_items": 1,
    "wait_seconds": 0
  }'
```

空队列正常返回：

```json
{"items":[]}
```

这表示网络、API、Token 和权限均正常。

> `claim` 会真正领取并锁定任务，不是只读查询。仅在测试环境、已确认队列为空，或下游
> 已经能够继续处理任务时执行。

## 4. 有任务时检查

响应中的任务应包含：

```json
{
  "delivery_id": "任务ID",
  "lease_token": "租约令牌",
  "lease_expires_at": "租约到期时间",
  "idempotency_key": "幂等键",
  "channel": "email",
  "provider": "email",
  "recipient": "customer@example.com",
  "payload": {
    "to": "customer@example.com",
    "subject": "主题",
    "body": "正文"
  }
}
```

必须确认：

- `channel` 和 `provider` 与当前下游一致；
- `recipient` 非空且格式有效；
- `payload.to` 与 `recipient` 完全一致；
- Email 的主题和正文非空；
- LinkedIn 的正文非空、主题为空；
- `delivery_id`、`lease_token`、`idempotency_key` 非空。

`recipient` 是唯一发送目标，下游不得自行更换联系人或地址。

## 5. 领取后的接口

| 操作 | 接口 | 说明 |
|---|---|---|
| 续租 | `POST /v1/deliveries/{id}/heartbeat` | 处理时间接近租约期限 |
| 成功 | `POST /v1/deliveries/{id}/complete` | 平台明确确认发送成功 |
| 失败 | `POST /v1/deliveries/{id}/fail` | 回报失败结果 |

这三个接口都要提交原 `worker_id` 和 `lease_token`。

失败结果：

- `retryable`：明确未发送，可以重试；
- `permanent`：明确无法发送，不应重试；
- `unknown`：可能已发送但无法确认，禁止自动重试。

## 6. 常见状态码

| 状态码 | 含义 |
|---|---|
| `200` | 正常；`items: []` 表示暂无任务 |
| `401` | Token 错误 |
| `403` | Token 没有当前渠道权限 |
| `409` | 租约过期、不匹配或属于其他 Worker |
| `422` | 请求字段错误 |
| `503` | 服务配置或数据库不可用 |

## 7. 通过标准

- [ ] `/health` 返回 `ok`；
- [ ] `/ready` 返回 `ready`；
- [ ] claim 返回 `200`；
- [ ] 空队列返回 `{"items":[]}`；
- [ ] 有任务时 `recipient` 有效；
- [ ] `payload.to` 与 `recipient` 一致；
- [ ] 下游能够保存任务 ID、租约令牌和幂等键。

## 8. 外部消费者实现

外部同事不需要访问本项目代码。管理员提供 API 地址、单个 Consumer Token 和接口说明
即可。同事可以在自己的项目中使用任意语言和任意邮件平台实现消费者。

完整请求体、回写示例和不依赖本仓库的 Python HTTP 封装见
`docs/OUTBOX_SENDER_QUICKSTART_CN.md`。服务运行时也可直接访问：

```text
http://<服务器地址>:8010/docs
http://<服务器地址>:8010/openapi.json
```
