# Outbox HTTP Service

The service exposes PostgreSQL Outbox deliveries without giving consumers database access.

For a step-by-step Chinese consumer integration guide, see
[`OUTBOX_DOWNSTREAM_INTEGRATION_GUIDE_CN.md`](OUTBOX_DOWNSTREAM_INTEGRATION_GUIDE_CN.md).

## Consumer protocol

1. `POST /v1/deliveries/claim`
2. `POST /v1/deliveries/{id}/complete` after confirmed provider acceptance
3. `POST /v1/deliveries/{id}/fail` when delivery fails

Both writeback requests only require `worker_id`; the delivery ID is in the URL.

Every consumer sends `Authorization: Bearer <token>`. Configure token scopes as:

```dotenv
OUTBOX_CONSUMER_TOKENS_JSON='{"email-secret":["email:email"],"linkedin-secret":["linkedin:linkedin"]}'
```

Producer and administrative endpoints use a separate `OUTBOX_PRODUCER_TOKEN`.

## Local startup

Apply migrations first:

```bash
./bin/twenty-hermes migrate
```

Generate local API tokens once, then run:

```bash
./scripts/setup_outbox_service_config.sh
./bin/start-outbox-service
```

Claimed tasks remain `sending` until the consumer reports `complete` or `fail`.
The heartbeat endpoint is retained as a no-op compatibility endpoint.

OpenAPI is available at `http://127.0.0.1:8010/docs`.

## Docker

Copy `config/outbox-service.env.example` to `config/outbox-docker.env`, replace the
database password and all API tokens, and run:

```bash
docker compose --env-file config/outbox-docker.env -f compose.outbox.yaml up -d --build
```

The default configuration connects containers to the existing host database on port `55432`.
The optional `standalone-db` profile starts a separate PostgreSQL instance on port `55433`;
when using it, set `OUTBOX_DB_HOST=outbox-postgres` and `OUTBOX_DB_PORT=5432`.

## Client libraries

- `clients/python/outbox_client.py`
- `clients/typescript/outbox-client.ts`

Both clients preserve compatibility with claim, heartbeat, complete, and fail
without external runtime dependencies.
