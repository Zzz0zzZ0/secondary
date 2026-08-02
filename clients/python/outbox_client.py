import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class OutboxClient:
    def __init__(self, base_url, token, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, path, payload):
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Outbox API returned HTTP {exc.code}: {detail}") from exc

    def claim(self, worker_id, channels, providers, max_items=1, wait_seconds=20):
        return self._post(
            "/v1/deliveries/claim",
            {
                "worker_id": worker_id,
                "channels": channels,
                "providers": providers,
                "max_items": max_items,
                "wait_seconds": wait_seconds,
            },
        )["items"]

    def heartbeat(self, item, worker_id):
        return self._post(
            f"/v1/deliveries/{item['delivery_id']}/heartbeat",
            {"worker_id": worker_id, "lease_token": item["lease_token"]},
        )

    def complete(self, item, worker_id, provider_message_id=None, provider_thread_id=None):
        return self._post(
            f"/v1/deliveries/{item['delivery_id']}/complete",
            {
                "worker_id": worker_id,
                "lease_token": item["lease_token"],
                "provider_message_id": provider_message_id,
                "provider_thread_id": provider_thread_id,
            },
        )

    def fail(self, item, worker_id, result, error_code, error_message=""):
        return self._post(
            f"/v1/deliveries/{item['delivery_id']}/fail",
            {
                "worker_id": worker_id,
                "lease_token": item["lease_token"],
                "result": result,
                "error_code": error_code,
                "error_message": error_message,
            },
        )
