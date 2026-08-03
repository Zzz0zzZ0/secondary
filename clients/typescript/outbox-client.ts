export type Delivery = {
  delivery_id: string;
  message_version_id: string;
  lease_token: string;
  lease_expires_at: string;
  schema_version: "1.0";
  idempotency_key: string;
  channel: "email" | "linkedin";
  provider: string;
  recipient: string;
  sender_account_ref?: string | null;
  payload: Record<string, unknown>;
  metadata: { lead_id?: string; attempt_no: number };
};

export class OutboxClient {
  constructor(
    private readonly baseUrl: string,
    private readonly token: string,
  ) {}

  private async post<T>(path: string, body: unknown): Promise<T> {
    const response = await fetch(`${this.baseUrl.replace(/\/$/, "")}${path}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      throw new Error(`Outbox API ${response.status}: ${await response.text()}`);
    }
    return response.json() as Promise<T>;
  }

  async claim(
    workerId: string,
    channels: Array<"email" | "linkedin">,
    providers: string[],
    maxItems = 1,
    waitSeconds = 20,
  ): Promise<Delivery[]> {
    const result = await this.post<{ items: Delivery[] }>("/v1/deliveries/claim", {
      worker_id: workerId,
      channels,
      providers,
      max_items: maxItems,
      wait_seconds: waitSeconds,
    });
    return result.items;
  }

  heartbeat(item: Delivery, workerId: string) {
    return this.post(`/v1/deliveries/${item.delivery_id}/heartbeat`, {
      worker_id: workerId,
      lease_token: item.lease_token,
    });
  }

  complete(
    item: Delivery,
    workerId: string,
    providerMessageId?: string,
    providerThreadId?: string,
  ) {
    return this.post(`/v1/deliveries/${item.delivery_id}/complete`, {
      worker_id: workerId,
      lease_token: item.lease_token,
      provider_message_id: providerMessageId,
      provider_thread_id: providerThreadId,
    });
  }

  fail(
    item: Delivery,
    workerId: string,
    result: "retryable" | "permanent" | "unknown",
    errorCode: string,
    errorMessage = "",
  ) {
    return this.post(`/v1/deliveries/${item.delivery_id}/fail`, {
      worker_id: workerId,
      lease_token: item.lease_token,
      result,
      error_code: errorCode,
      error_message: errorMessage,
    });
  }
}
