# Outbox 外部发信消费者接入手册

## 1. 接入边界

同事只通过 HTTP API 接入，不需要访问本项目源码、文件夹或数据库。消费者负责：

```text
领取已批准的投递任务 → 调用下游发信平台 → 回写成功或失败
```

- Review UI 使用 `8000` 端口，负责人工审核和批准；
- Outbox API 使用 `8010` 端口，供发信消费者调用；
- 只有人工批准的消息才会出现在消费者队列中。

## 2. 管理员需要提供的内容

管理员只需通过安全渠道给每个消费者以下三项：

```dotenv
OUTBOX_API_URL=http://192.168.112.178:8010
OUTBOX_CONSUMER_TOKENS_JSON='{"replace-email-consumer-token":["email:email"],"replace-linkedin-consumer-token":["linkedin:linkedin"]}'
OUTBOX_WORKER_ID=<由同事自行设置的稳定且唯一的名称>（这个自己写就行了，不重复就行）
```

| 用途 | channel | provider | Token scope |
|---|---|---|---|
| Email 发信（下游自选实现） | `email` | `email` | `email:email` |
| LinkedIn 发信 | `linkedin` | `linkedin` | `linkedin:linkedin` |

同一消费者实例长期保持不变，并行实例之间不要重复。

## 3. 第一次连接检查

同事在自己的电脑上执行：

```bash
curl "$OUTBOX_API_URL/health"
curl "$OUTBOX_API_URL/ready"
```

预期：

```json
{"status":"ok","service":"outbox-api","version":"1.0.0"}
{"status":"ready"}
```

浏览器可打开以下地址查看在线接口定义，无需项目源码：

```text
http://192.168.112.178:8010/docs
http://192.168.112.178:8010/openapi.json
```

如果同事无法打开 `/health`，先确认 Outbox API 已启动、双方网络互通，并且 `8010`
端口未被防火墙拦截。局域网地址变化时，管理员需要提供新的地址。

在消费者已经能够处理真实任务的前提下，测试领取：

```bash
curl -X POST "$OUTBOX_API_URL/v1/deliveries/claim" \
  -H "Authorization: Bearer $OUTBOX_CONSUMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "worker_id": "sales-email-worker-01",
    "channels": ["email"],
    "providers": ["email"],
    "max_items": 1,
    "wait_seconds": 0
  }'
```

没有任务时正常返回：

```json
{"items":[]}
```

`claim` 会真正领取并锁定任务。消费者尚未实现成功/失败回写前，不要领取正式队列中的
任务。

## 4. 领取结果结构

有任务时，`items` 中每项的主要结构如下：

```json
{
  "delivery_id": "任务ID",
  "message_version_id": "消息版本ID",
  "lease_token": "本次租约令牌",
  "lease_expires_at": "租约到期时间",
  "schema_version": 1,
  "idempotency_key": "幂等键",
  "channel": "email",
  "provider": "email",
  "recipient": "customer@example.com",
  "payload": {
    "to": "customer@example.com",
    "subject": "邮件主题",
    "body": "邮件正文"
  },
  "metadata": {}
}
```

消费者应持续循环领取；空队列不是错误，稍后继续请求即可。

## 5. 收到任务后必须校验（前置有校验，当然可以加一层保险）

Email 消费者至少检查：

- `channel == "email"`；
- `provider == "email"`；
- `recipient` 是有效邮箱；
- `payload.to` 与 `recipient` 完全一致；
- `payload.subject` 和 `payload.body` 非空；
- `delivery_id`、`lease_token`、`idempotency_key` 非空。

LinkedIn 消费者至少检查：

- `channel == "linkedin"`；
- `provider == "linkedin"`；
- `recipient` 是当前任务指定的 LinkedIn 地址；
- `payload.body` 非空。

## 6. 发信结果回写

任务领取后，后续请求必须继续使用原来的 `worker_id`、`delivery_id` 和
`lease_token`。

### 处理时间较长：续租

```bash
curl -X POST "$OUTBOX_API_URL/v1/deliveries/$DELIVERY_ID/heartbeat" \
  -H "Authorization: Bearer $OUTBOX_CONSUMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"worker_id\": \"$OUTBOX_WORKER_ID\",
    \"lease_token\": \"$LEASE_TOKEN\"
  }"
```

默认租约为 300 秒。处理可能超过租约时，应提前续租。

### 平台明确确认发送成功

```bash
curl -X POST "$OUTBOX_API_URL/v1/deliveries/$DELIVERY_ID/complete" \
  -H "Authorization: Bearer $OUTBOX_CONSUMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"worker_id\": \"$OUTBOX_WORKER_ID\",
    \"lease_token\": \"$LEASE_TOKEN\",
    \"provider_message_id\": \"平台返回的消息ID\",
    \"provider_thread_id\": \"平台返回的会话ID\"
  }"
```

平台没有会话 ID 时可将 `provider_thread_id` 设为 `null` 或省略。

### 发送失败

```bash
curl -X POST "$OUTBOX_API_URL/v1/deliveries/$DELIVERY_ID/fail" \
  -H "Authorization: Bearer $OUTBOX_CONSUMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"worker_id\": \"$OUTBOX_WORKER_ID\",
    \"lease_token\": \"$LEASE_TOKEN\",
    \"result\": \"retryable\",
    \"error_code\": \"provider_rate_limited\",
    \"error_message\": \"平台明确拒绝且未发送\"
  }"
```

| result | 什么时候使用 |
|---|---|
| `retryable` | 明确没有发送，并且属于限流、服务端错误等可重试问题 |
| `permanent` | 地址无效、任务内容非法等明确不应重试的问题 |
| `unknown` | 超时、断线等导致“可能已发送但无法确认”的情况 |

只要存在“可能已经发送”的可能，就必须使用 `unknown`，禁止自动重试。

## 7. 可复制到同事项目中的 Python HTTP 封装

以下代码只使用 Python 标准库，不依赖本仓库。它只封装 Outbox 协议；同事在自己的项目
中调用 Gmail API、SMTP、Microsoft Graph 或其他平台完成实际发送。

```python
import json
import os
import urllib.request

API_URL = os.environ["OUTBOX_API_URL"].rstrip("/")
TOKEN = os.environ["OUTBOX_CONSUMER_TOKEN"]
WORKER_ID = os.environ["OUTBOX_WORKER_ID"]


def post(path, body):
    request = urllib.request.Request(
        API_URL + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def claim():
    return post("/v1/deliveries/claim", {
        "worker_id": WORKER_ID,
        "channels": ["email"],
        "providers": ["email"],
        "max_items": 1,
        "wait_seconds": 20,
    })["items"]


def heartbeat(item):
    return post(f"/v1/deliveries/{item['delivery_id']}/heartbeat", {
        "worker_id": WORKER_ID,
        "lease_token": item["lease_token"],
    })


def complete(item, message_id, thread_id=None):
    return post(f"/v1/deliveries/{item['delivery_id']}/complete", {
        "worker_id": WORKER_ID,
        "lease_token": item["lease_token"],
        "provider_message_id": message_id,
        "provider_thread_id": thread_id,
    })


def fail(item, result, code, message):
    return post(f"/v1/deliveries/{item['delivery_id']}/fail", {
        "worker_id": WORKER_ID,
        "lease_token": item["lease_token"],
        "result": result,
        "error_code": code,
        "error_message": message,
    })
```

建议处理顺序：

```text
claim → 校验任务 → 按 idempotency_key 查重 → 调用发信平台
      → 明确成功：complete
      → 明确未发送：fail(retryable/permanent)
      → 无法确认是否发送：fail(unknown)
```

## 8. 幂等要求

消费者必须持久化 `idempotency_key` 和平台返回的消息 ID。

如果发信平台已经接受消息，但消费者在调用 `complete` 前崩溃，任务可能再次出现。此时
应根据 `idempotency_key` 找到原发送结果并完成回写，不能再次发送同一封消息。

## 9. 联调通过标准

- [ ] `/health` 返回 `ok`；
- [ ] `/ready` 返回 `ready`；
- [ ] 正确 Token 调用 `claim` 返回 `200`；
- [ ] 错误 Token 返回 `401`；
- [ ] 消费者只领取授权的 channel/provider；
- [ ] 能对成功任务调用 `complete`；
- [ ] 能分别回报 `retryable`、`permanent`、`unknown`；
- [ ] 长任务会调用 `heartbeat`；
- [ ] 已持久化 `idempotency_key`，不会重复发送；
- [ ] 消费者日志不会打印 Token 或完整客户正文。

## 10. 常见错误

| 状态码 | 处理 |
|---|---|
| `401` | Token 缺失或错误，联系管理员重新确认 |
| `403` | Token 没有该 channel/provider 权限 |
| `409` | 租约过期、Worker 不匹配或任务已被其他消费者处理 |
| `422` | 请求字段格式错误 |
| `503` | Outbox 服务鉴权未配置或数据库不可用 |
