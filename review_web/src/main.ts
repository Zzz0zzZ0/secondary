import "./styles.css";

type Json = Record<string, any>;
type RunState = {
  run_id: string;
  status: "running" | "completed" | "failed";
  total: number;
  completed: number;
  valid: number;
  invalid: number;
  error?: string;
};
type RecordResult = {
  lead_id: string;
  pipeline_status: string;
  input: Json;
  output: Json | null;
  candidate_output?: Json | null;
  validation_errors?: string[];
  usage: Json;
  raw_output: string;
};
type ReviewMessage = {
  id: string;
  run_id: string;
  lead_id: string;
  channel: "email" | "linkedin";
  version: number;
  recipient_original: string;
  crm_snapshot: Json;
  original_output: Json;
  edited_output: Json | null;
  effective_output: Json;
  review_status: "pending_review" | "approved" | "rejected";
  created_at: string;
  updated_at: string;
};
type QueueRecord = {
  lead_id: string;
  lead_type: string | null;
  classification_confidence: number | null;
  classification_reason: string | null;
  status: string;
  classification_due_at: string | null;
  next_action_at: string | null;
  follow_up_count: number;
  latest_message_version_id: string | null;
  last_error: string | null;
  updated_at: string;
};
type SchedulerRun = {
  id: string;
  trigger: string;
  status: "running" | "completed" | "failed";
  mode: "online" | "local_only";
  started_at: string;
  completed_at: string | null;
  scan_kind: "full" | "15d" | "120d" | null;
  scan: Record<string, number> | null;
  classification_count: number;
  classification_statuses: Record<string, number>;
  dispatch_count: number;
  dispatch_statuses: Record<string, number>;
  usage: {
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    estimated_cost_usd: number;
  };
  error: string | null;
};
type ReviewConfiguration = {
  enabled: boolean;
  mode: "manual" | "automatic";
  auto_reviewer: string | null;
};
type RuntimeStatus = {
  mode: "online" | "local_only" | "unknown";
  last_checked_at: string | null;
  retry_at: string | null;
  last_error: string | null;
};
const scanKindLabels: Record<string, string> = {
  full: "全量扫描",
  "15d": "旧版 15 天扫描",
  "120d": "旧版 120 天校准",
};
type DashboardData = {
  generated_at: string;
  review: ReviewConfiguration;
  runtime: RuntimeStatus;
  counts: Record<string, number>;
  lead_types: Record<string, number>;
  due: { classifications: number; actions: number };
  scheduler: Record<string, string>;
  next_wakeup_at: string;
  upcoming: Array<{
    date: string;
    kind: "classification" | "action";
    count: number;
  }>;
  runs: SchedulerRun[];
  recent_events: Array<{
    lead_id: string;
    event_type: string;
    event_at: string;
    details: Json;
    lead_type: string | null;
    status: string | null;
  }>;
};
type LeadDetail = {
  state: QueueRecord & {
    source_updated_at: string;
    crm_snapshot: Json;
    last_classified_at: string | null;
    last_generated_at: string | null;
    last_seen_at: string;
  };
  events: Array<{
    id: number;
    event_type: string;
    event_at: string;
    details: Json;
  }>;
  classifications: Array<{
    lead_type: string;
    confidence: number;
    information_completeness: number | null;
    reason: string;
    evidence: string[];
    policy_version: string;
    classified_at: string;
  }>;
};

const form = document.querySelector<HTMLFormElement>("#run-form")!;
const limitInput = document.querySelector<HTMLInputElement>("#limit")!;
const sortInput = document.querySelector<HTMLSelectElement>("#sort")!;
const startButton = document.querySelector<HTMLButtonElement>("#start-button")!;
const reviewMessagesButton = document.querySelector<HTMLButtonElement>(
  "#review-messages-button",
)!;
const pollQueueButton = document.querySelector<HTMLButtonElement>("#poll-queue-button")!;
const statusBox = document.querySelector<HTMLElement>("#status")!;
const summary = document.querySelector<HTMLElement>("#summary")!;
const workspace = document.querySelector<HTMLElement>("#workspace")!;
const recordList = document.querySelector<HTMLElement>("#record-list")!;
const recordCount = document.querySelector<HTMLElement>("#record-count")!;
const recordListTitle = document.querySelector<HTMLElement>("#record-list-title")!;
const detail = document.querySelector<HTMLElement>("#detail")!;
const dashboardRefresh = document.querySelector<HTMLButtonElement>("#dashboard-refresh")!;
const dashboardUpdated = document.querySelector<HTMLElement>("#dashboard-updated")!;
const runtimeMode = document.querySelector<HTMLElement>("#runtime-mode")!;
const dashboardStats = document.querySelector<HTMLElement>("#dashboard-stats")!;
const processFlow = document.querySelector<HTMLElement>("#process-flow")!;
const schedulerTimes = document.querySelector<HTMLElement>("#scheduler-times")!;
const upcomingChart = document.querySelector<HTMLElement>("#upcoming-chart")!;
const upcomingTotal = document.querySelector<HTMLElement>("#upcoming-total")!;
const runHistory = document.querySelector<HTMLElement>("#run-history")!;
const recentEvents = document.querySelector<HTMLElement>("#recent-events")!;
const reviewModeToggle = document.querySelector<HTMLButtonElement>("#review-mode-toggle")!;
const reviewModeDescription = document.querySelector<HTMLElement>("#review-mode-description")!;
let currentReviewEnabled: boolean | null = null;

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "请求失败");
  return data as T;
}

function text(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (Array.isArray(value)) return value.join("、") || fallback;
  return String(value);
}

const decisionLabels: Record<string, string> = {
  generated: "已生成",
  no_message: "不发送",
  cannot_generate: "无法生成",
  invalid: "非法输出",
  processing: "处理中",
};
const languageLabels: Record<string, string> = {
  English: "英语",
  Chinese: "中文",
  Japanese: "日语",
  French: "法语",
  German: "德语",
  Spanish: "西班牙语",
  Italian: "意大利语",
  Portuguese: "葡萄牙语",
  Russian: "俄语",
};
const warningLabels: Record<string, string> = {
  CONTACT_CHANNEL_MISSING: "未在CRM检测到邮箱或者领英",
  SELECTED_CHANNEL_MISSING: "缺少所选渠道地址",
  COMPANY_CONTEXT_MISSING: "公司背调缺失",
  COMPANY_CONTEXT_THIN: "公司背调信息不足",
  NON_CATALOG_PRODUCT_REQUIRES_FACTORY_CONFIRMATION: "非目录产品需要向工厂确认",
  CONTACT_NAME_LOW_CONFIDENCE: "联系人姓名可信度较低",
  INQUIRY_CONTEXT_MISSING: "未关联到具体询盘正文",
  INTERNAL_CONFIRMATION_REQUIRED: "需要内部确认",
  PRICE_FIELDS_INTENTIONALLY_EXCLUDED: "报价字段已按规则排除",
  RECENT_NO_CURRENT_DEMAND: "近期已确认暂无需求",
  FOLLOW_UP_TIMING_UNVERIFIED: "跟进时间尚未确认",
  DOCUMENT_AVAILABILITY_UNVERIFIED: "资料是否可提供尚未确认",
  AVAILABILITY_REQUIRES_MANUAL_CONFIRMATION: "资料或产品可用性需人工确认",
  CUSTOMER_REFERENCE_DISCLOSURE_REQUIRES_APPROVAL: "客户案例披露需要审批",
  REFERRAL_CONTEXT_MISSING: "缺少推荐关系信息",
  SALES_ENGLISH_NAME_MISSING: "销售英文姓名缺失",
  SALES_NAME_TRANSLITERATED: "销售姓名已按拼音转换",
  SALES_NAME_TRANSLITERATION_UNCERTAIN: "销售姓名拼音需人工核对",
  DO_NOT_CONTACT: "客户明确要求不要联系",
};
const leadTypeLabels: Record<string, string> = {
  no_current_demand: "暂无需求",
  unknown_demand: "未知需求",
  referred: "（被）推荐",
  below_moq: "订量不够",
};
const queueStatusLabels: Record<string, string> = {
  settling: "沉淀中",
  classifying: "分类中",
  scheduled: "已排期",
  generating: "生成中",
  waiting_review: "等待审阅",
  waiting_delivery: "等待投递",
  needs_review: "需要人工确认",
  needs_contact: "缺少有效联系人",
  paused: "已暂停",
  converted: "已转出",
  failed: "失败",
};
const eventLabels: Record<string, string> = {
  discovered: "发现线索",
  source_changed: "业务内容变化",
  classified: "完成分类",
  classification_confirmed: "人工确认分类",
  classification_review_bypassed: "已跳过分类复核",
  policy_reclassification_queued: "新标准重新分类",
  classification_failed: "分类失败",
  classification_recovered: "恢复中断分类",
  message_generation_completed: "消息生成完成",
  message_generation_failed: "消息生成失败",
  automatic_approval_failed: "自动批准失败",
  left_secondary_lead_scope: "离开二级线索范围",
  outbox_approved: "Outbox 已批准",
  outbox_sent: "投递成功",
  outbox_rejected: "Outbox 已拒绝",
};

function formatDateTime(value: unknown): string {
  if (!value) return "—";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return text(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function countFor(counts: Record<string, number>, statuses: string[]): number {
  return statuses.reduce((sum, status) => sum + (counts[status] || 0), 0);
}

function queueStatusText(record: QueueRecord): string {
  if (
    record.status === "settling" &&
    record.classification_due_at &&
    new Date(record.classification_due_at).getTime() <= Date.now()
  ) {
    return "待分类";
  }
  return queueStatusLabels[record.status] || record.status;
}

function renderReviewMode(review: ReviewConfiguration) {
  currentReviewEnabled = review.enabled;
  reviewModeToggle.textContent = review.enabled
    ? "人工审阅：开启"
    : "人工审阅：关闭";
  reviewModeToggle.classList.toggle("automatic", !review.enabled);
  reviewModeToggle.disabled = false;
  reviewModeToggle.title = review.enabled
    ? "点击关闭人工审阅"
    : "点击开启人工审阅";
  reviewModeDescription.textContent = review.enabled
    ? "预览只读取已排期线索，不改排期、不进 Outbox。自动调度生成的正式消息仍等待人工批准。"
    : "预览只读取已排期线索，不改排期、不进 Outbox。自动调度生成的正式消息会自动批准，实际发送仍受投递安全开关控制。";
}

function renderRuntimeMode(runtime: RuntimeStatus) {
  runtimeMode.className = `runtime-mode ${runtime.mode}`;
  runtimeMode.removeAttribute("title");
  if (runtime.mode === "online") {
    runtimeMode.textContent = "在线模式：CRM 扫描、分类排期和消息生成均可运行";
    return;
  }
  if (runtime.mode === "local_only") {
    const retry = runtime.retry_at
      ? `；下次重连 ${formatDateTime(runtime.retry_at)}`
      : "";
    runtimeMode.textContent =
      `本地模式：继续处理已缓存记录的分类和排期，CRM 扫描及消息生成已暂停${retry}`;
    runtimeMode.title = runtime.last_error || "";
    return;
  }
  runtimeMode.textContent = "运行模式未知：等待常驻调度器完成首个周期";
}

function renderDashboard(data: DashboardData) {
  renderReviewMode(data.review);
  renderRuntimeMode(data.runtime);
  const total = Object.values(data.counts).reduce((sum, value) => sum + value, 0);
  const attention = countFor(data.counts, ["needs_review", "needs_contact", "failed"]);
  const stats = [
    ["当前线索", total],
    ["到期待分类", data.due.classifications],
    ["到期待生成", data.due.actions],
    ["需人工处理", attention],
  ];
  dashboardStats.replaceChildren(
    ...stats.map(([label, value]) => {
      const node = document.createElement("div");
      node.className = "dashboard-stat";
      const strong = document.createElement("strong");
      const span = document.createElement("span");
      strong.textContent = String(value);
      span.textContent = String(label);
      node.append(strong, span);
      return node;
    }),
  );

  const stages = [
    { label: "沉淀与分类", statuses: ["settling", "classifying"] },
    { label: "人工复核", statuses: ["needs_review"], tone: "attention" },
    { label: "已排期", statuses: ["scheduled"] },
    {
      label: "生成与审阅",
      statuses: ["generating", "waiting_review"],
      reviewMessages: true,
    },
    { label: "等待投递", statuses: ["waiting_delivery"] },
    {
      label: "暂停与异常",
      statuses: ["needs_contact", "paused", "converted", "failed"],
      tone: "error",
    },
  ];
  processFlow.replaceChildren(
    ...stages.map((stage) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `flow-stage ${stage.tone || ""}`.trim();
      const strong = document.createElement("strong");
      const span = document.createElement("span");
      strong.textContent = String(countFor(data.counts, stage.statuses));
      span.textContent = stage.label;
      button.append(strong, span);
      button.addEventListener("click", () => {
        if (stage.reviewMessages) {
          loadReviewMessages().catch(showError);
        } else {
          loadQueue(stage.statuses).catch(showError);
        }
      });
      return button;
    }),
  );

  const times = [
    ["下次全量二级线索扫描", data.scheduler.next_full_scan_at],
    ["下次系统唤醒", data.next_wakeup_at],
  ];
  if (data.scheduler.classification_backlog_resume_at) {
    times.push([
      "分类积压恢复",
      data.scheduler.classification_backlog_resume_at,
    ]);
  }
  schedulerTimes.replaceChildren(
    ...times.map(([label, value]) => {
      const node = document.createElement("div");
      node.className = "scheduler-time";
      const span = document.createElement("span");
      const strong = document.createElement("strong");
      span.textContent = label;
      strong.textContent = formatDateTime(value);
      node.append(span, strong);
      return node;
    }),
  );

  const byDate = new Map<string, { classification: number; action: number }>();
  data.upcoming.forEach((item) => {
    const current = byDate.get(item.date) || { classification: 0, action: 0 };
    current[item.kind] = item.count;
    byDate.set(item.date, current);
  });
  const upcomingRows = [...byDate.entries()];
  const maxValue = Math.max(
    1,
    ...upcomingRows.map(([, value]) => value.classification + value.action),
  );
  const totalUpcoming = upcomingRows.reduce(
    (sum, [, value]) => sum + value.classification + value.action,
    0,
  );
  upcomingTotal.textContent = `${totalUpcoming} 条`;
  if (!upcomingRows.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "未来 30 天暂无新到期任务";
    upcomingChart.replaceChildren(empty);
  } else {
    upcomingChart.replaceChildren(
      ...upcomingRows.map(([date, value]) => {
        const row = document.createElement("div");
        row.className = "upcoming-row";
        const label = document.createElement("span");
        const bars = document.createElement("div");
        const count = document.createElement("span");
        label.textContent = date.slice(5);
        bars.className = "upcoming-bars";
        count.className = "upcoming-count";
        if (value.classification) {
          const bar = document.createElement("div");
          bar.className = "upcoming-bar";
          bar.style.width = `${Math.max(3, (value.classification / maxValue) * 100)}%`;
          bar.setAttribute("aria-label", `待分类 ${value.classification} 条`);
          bars.append(bar);
        }
        if (value.action) {
          const bar = document.createElement("div");
          bar.className = "upcoming-bar action";
          bar.style.width = `${Math.max(3, (value.action / maxValue) * 100)}%`;
          bar.setAttribute("aria-label", `待生成 ${value.action} 条`);
          bars.append(bar);
        }
        count.textContent = String(value.classification + value.action);
        row.append(label, bars, count);
        return row;
      }),
    );
  }

  if (!data.runs.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "运行审计从本版本启用，下一次 once 或 run 后显示。";
    runHistory.replaceChildren(empty);
  } else {
    runHistory.replaceChildren(
      ...data.runs.map((run) => {
        const item = document.createElement("div");
        item.className = "run-item";
        const line = document.createElement("div");
        line.className = "run-line";
        const title = document.createElement("strong");
        const status = document.createElement("span");
        title.textContent = `${formatDateTime(run.started_at)} · ${
          run.scan_kind ? scanKindLabels[run.scan_kind] || run.scan_kind : "无扫描"
        } · ${run.mode === "local_only" ? "本地模式" : "在线模式"}`;
        status.className = `run-status ${run.status}`;
        status.textContent =
          run.status === "completed" ? "完成" : run.status === "failed" ? "失败" : "运行中";
        line.append(title, status);
        const summary = document.createElement("p");
        const seen = run.scan?.seen || 0;
        summary.textContent =
          `扫描 ${seen} · 分类 ${run.classification_count} · ` +
          `生成 ${run.dispatch_count} · Token ${run.usage.total_tokens || 0}`;
        item.append(line, summary);
        if (run.error) {
          const error = document.createElement("p");
          error.className = "text-destructive";
          error.textContent = run.error;
          item.append(error);
        }
        return item;
      }),
    );
  }

  if (!data.recent_events.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "暂无状态变化";
    recentEvents.replaceChildren(empty);
  } else {
    recentEvents.replaceChildren(
      ...data.recent_events.slice(0, 10).map((event) => {
        const item = document.createElement("div");
        item.className = "event-item";
        const line = document.createElement("div");
        line.className = "event-line";
        const title = document.createElement("strong");
        const time = document.createElement("span");
        title.textContent = eventLabels[event.event_type] || event.event_type;
        time.className = "muted";
        time.textContent = formatDateTime(event.event_at);
        line.append(title, time);
        const summary = document.createElement("p");
        summary.textContent =
          `${leadTypeLabels[event.lead_type || ""] || "待分类"} · ` +
          `${queueStatusLabels[event.status || ""] || event.status || "—"} · ` +
          event.lead_id;
        item.append(line, summary);
        return item;
      }),
    );
  }
  dashboardUpdated.textContent = `数据更新时间：${formatDateTime(data.generated_at)} · 只读展示`;
}

async function loadDashboard() {
  dashboardRefresh.disabled = true;
  dashboardUpdated.textContent = "正在读取本地调度状态…";
  try {
    const data = await request<DashboardData>(
      "/api/polling/dashboard?days=30&run_limit=10",
    );
    renderDashboard(data);
  } catch (error) {
    dashboardUpdated.textContent =
      error instanceof Error ? `看板读取失败：${error.message}` : "看板读取失败";
  } finally {
    dashboardRefresh.disabled = false;
  }
}

function decisionText(value: unknown): string {
  const key = text(value, "processing");
  return decisionLabels[key] || key;
}

function warningText(value: unknown): string {
  if (!Array.isArray(value)) return text(value);
  return value.map((item) => warningLabels[String(item)] || String(item)).join("；") || "—";
}

function setStatus(message: string, kind = "") {
  statusBox.textContent = message;
  statusBox.className = `status ${kind}`.trim();
}

function renderSummary(run: RunState) {
  const items = [
    ["总记录", run.total],
    ["已完成", run.completed],
    ["有效结果", run.valid],
    ["无效结果", run.invalid],
  ];
  summary.replaceChildren(
    ...items.map(([label, value]) => {
      const item = document.createElement("div");
      const strong = document.createElement("strong");
      const span = document.createElement("span");
      strong.textContent = String(value);
      span.textContent = String(label);
      item.append(strong, span);
      return item;
    }),
  );
  summary.classList.remove("hidden");
}

function badge(label: string, className = ""): HTMLElement {
  const node = document.createElement("span");
  node.className = `badge ${className}`.trim();
  node.textContent = label;
  return node;
}

function field(label: string, value: unknown): HTMLElement {
  const row = document.createElement("div");
  const dt = document.createElement("dt");
  const dd = document.createElement("dd");
  dt.textContent = label;
  dd.textContent = text(value);
  row.append(dt, dd);
  return row;
}

function block(title: string, content: unknown): HTMLElement {
  const section = document.createElement("section");
  const heading = document.createElement("h3");
  const pre = document.createElement("pre");
  heading.textContent = title;
  pre.textContent = text(content);
  section.append(heading, pre);
  return section;
}

function renderProducts(products: Json[]): HTMLElement {
  const section = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = `产品需求（${products.length}）`;
  section.append(heading);
  if (!products.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "此记录没有结构化产品需求";
    section.append(empty);
    return section;
  }
  products.forEach((product, index) => {
    const card = document.createElement("div");
    card.className = "product";
    const title = document.createElement("strong");
    title.textContent = text(product.product_name_en || product.product_name || product.name, `产品 ${index + 1}`);
    const dl = document.createElement("dl");
    [
      ["规格", product.specification],
      ["粒度", product.size],
      ["数量", product.quantity],
      ["用途", product.application],
      ["包装", product.packaging],
      ["目的地", product.destination],
      ["贸易条款", product.incoterm],
      ["备注", product.remark],
    ].forEach(([key, value]) => dl.append(field(String(key), value)));
    card.append(title, dl);
    section.append(card);
  });
  return section;
}

function renderDetail(record: RecordResult) {
  const input = record.input || {};
  const lead = input.lead || {};
  const schedule = input.secondary_lead_schedule || {};
  const company = input.company || {};
  const contact = input.contact || {};
  const sales = input.sales || {};
  const output = record.output;
  const candidate = record.candidate_output;
  const displayOutput = output || candidate;

  const crmPanel = document.createElement("div");
  crmPanel.className = "panel";
  const crmTitle = document.createElement("div");
  crmTitle.className = "panel-heading";
  const crmH2 = document.createElement("h2");
  crmH2.textContent = "CRM 输入依据";
  crmTitle.append(crmH2, badge(text(lead.type), "neutral"));
  const crmFields = document.createElement("dl");
  [
    ["公司", company.name],
    ["联系人", contact.name],
    ["职位", contact.job_title],
    ["联系地址", contact.email || contact.linkedin_url],
    ["销售", sales.name],
    ["生成渠道", input.output?.type === "email" ? "邮件" : "LinkedIn"],
    ["二级分类", schedule.lead_type_label],
    ["原排期", formatDateTime(schedule.next_action_at)],
    ["最近更新", lead.updated_at],
    ["最近跟进", lead.last_follow_up_at],
  ].forEach(([key, value]) => crmFields.append(field(String(key), value)));
  crmPanel.append(
    crmTitle,
    crmFields,
    renderProducts(Array.isArray(lead.product_demands) ? lead.product_demands : []),
    block(
      "CRM 内文本",
      input.review_context?.crm_internal_note || lead.internal_note,
    ),
    block("公司背调", company.research_text),
  );

  const outputPanel = document.createElement("div");
  outputPanel.className = "panel output-panel";
  const outputTitle = document.createElement("div");
  outputTitle.className = "panel-heading";
  const outputH2 = document.createElement("h2");
  outputH2.textContent = "Hermes 生成结果";
  const decision = output?.decision || (candidate ? "invalid" : record.pipeline_status);
  outputTitle.append(outputH2, badge(decisionText(decision), decision));
  outputPanel.append(outputTitle);

  if (!displayOutput) {
    outputPanel.append(block("处理说明", record.raw_output ? "Hermes返回无法解析，未通过系统校验。" : "尚在处理"));
  } else {
    const meta = document.createElement("dl");
    [
      ["语言", languageLabels[displayOutput.language] || displayOutput.language],
      ["消息目标", displayOutput.message_goal],
      ["需要补充", displayOutput.information_requested],
      ["内部提醒", warningText(displayOutput.warnings)],
      ["判断原因", displayOutput.reason],
    ].forEach(([key, value]) => meta.append(field(String(key), value)));
    outputPanel.append(meta);
    if (record.validation_errors?.length) {
      outputPanel.append(block("非法输出原因", record.validation_errors.join("；")));
    }
    if (displayOutput.content?.subject) outputPanel.append(block("邮件主题（客户语言）", displayOutput.content.subject));
    if (displayOutput.content?.subject_zh) outputPanel.append(block("邮件主题（中文对照）", displayOutput.content.subject_zh));
    if (displayOutput.content?.body) outputPanel.append(block(output ? "客户可见正文（目标语言）" : "非法候选正文（不可发送）", displayOutput.content.body));
    if (displayOutput.content?.body_zh) outputPanel.append(block("中文对照（仅供审阅）", displayOutput.content.body_zh));
  }
  detail.replaceChildren(crmPanel, outputPanel);
}

function renderRecords(records: RecordResult[]) {
  recordList.replaceChildren();
  detail.replaceChildren();
  recordListTitle.textContent = "生成结果";
  recordCount.textContent = `${records.length} 条`;
  records.forEach((record, index) => {
    const company = record.input?.company?.name;
    const contact = record.input?.contact?.name;
    const leadType = record.input?.lead?.type;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "record-item";
    const title = document.createElement("strong");
    const subtitle = document.createElement("span");
    title.textContent = text(company, "未知公司");
    subtitle.textContent = `${text(contact, "未知联系人")} · ${text(leadType, "未知类型")}`;
    const decision = record.output?.decision || (record.candidate_output ? "invalid" : record.pipeline_status);
    button.append(title, subtitle, badge(decisionText(decision), decision));
    button.addEventListener("click", () => {
      recordList.querySelectorAll(".active").forEach((node) => node.classList.remove("active"));
      button.classList.add("active");
      renderDetail(record);
    });
    recordList.append(button);
    if (index === 0) button.click();
  });
  workspace.classList.remove("hidden");
}

function renderReviewMessageDetail(message: ReviewMessage) {
  const snapshot = message.crm_snapshot || {};
  const lead = snapshot.lead || {};
  const company = snapshot.company || {};
  const contact = snapshot.contact || {};
  const sales = snapshot.sales || {};
  const output = message.effective_output || {};
  const content = output.content || {};

  const crmPanel = document.createElement("div");
  crmPanel.className = "panel";
  const crmHeading = document.createElement("div");
  crmHeading.className = "panel-heading";
  const crmTitle = document.createElement("h2");
  crmTitle.textContent = "待审消息依据";
  crmHeading.append(
    crmTitle,
    badge(
      message.edited_output ? "已有修改" : "Hermes 原稿",
      message.edited_output ? "generated" : "neutral",
    ),
  );
  const crmFields = document.createElement("dl");
  [
    ["公司", company.name],
    ["联系人", contact.name],
    ["收件地址", message.recipient_original],
    ["销售", sales.name],
    ["渠道", message.channel === "email" ? "邮件" : "LinkedIn"],
    ["消息版本", message.version],
    ["生成时间", formatDateTime(message.created_at)],
  ].forEach(([key, value]) => crmFields.append(field(String(key), value)));
  crmPanel.append(
    crmHeading,
    crmFields,
    block(
      "CRM 内文本",
      snapshot.review_context?.crm_internal_note || lead.internal_note,
    ),
    block("Hermes 判断理由", output.reason),
    block("内部提醒", warningText(output.warnings)),
  );

  const reviewPanel = document.createElement("div");
  reviewPanel.className = "panel output-panel";
  const reviewHeading = document.createElement("div");
  reviewHeading.className = "panel-heading";
  const reviewTitle = document.createElement("h2");
  reviewTitle.textContent = "人工审阅";
  reviewHeading.append(
    reviewTitle,
    badge("等待审阅", "generated"),
  );

  const editor = document.createElement("form");
  editor.className = "review-editor";
  const subjectLabel = document.createElement("label");
  const subjectInput = document.createElement("input");
  subjectLabel.textContent = "邮件主题";
  subjectInput.value = text(content.subject, "");
  subjectInput.maxLength = 500;
  subjectLabel.append(subjectInput);
  if (message.channel !== "email") subjectLabel.classList.add("hidden");

  const bodyLabel = document.createElement("label");
  const bodyInput = document.createElement("textarea");
  bodyLabel.textContent = "客户可见正文";
  bodyInput.value = text(content.body, "");
  bodyInput.maxLength = 20000;
  bodyInput.rows = 12;
  bodyLabel.append(bodyInput);

  const reviewerLabel = document.createElement("label");
  const reviewerInput = document.createElement("input");
  reviewerLabel.textContent = "审核人";
  reviewerInput.value = "review-ui";
  reviewerInput.maxLength = 200;
  reviewerLabel.append(reviewerInput);

  const noteLabel = document.createElement("label");
  const noteInput = document.createElement("textarea");
  noteLabel.textContent = "审核备注（可选）";
  noteInput.maxLength = 2000;
  noteInput.rows = 2;
  noteLabel.append(noteInput);

  const instructionLabel = document.createElement("label");
  const instructionInput = document.createElement("textarea");
  instructionLabel.textContent = "交给 Hermes 的新限制或调整方向";
  instructionInput.placeholder =
    "例如：缩短为 80 词以内；只询问规格和年用量；语气更直接。";
  instructionInput.maxLength = 4000;
  instructionInput.rows = 4;
  instructionLabel.append(instructionInput);

  const actions = document.createElement("div");
  actions.className = "review-actions";
  const saveButton = document.createElement("button");
  const regenerateButton = document.createElement("button");
  const approveButton = document.createElement("button");
  const rejectButton = document.createElement("button");
  saveButton.type = regenerateButton.type = approveButton.type = rejectButton.type = "button";
  saveButton.textContent = "保存修改";
  regenerateButton.textContent = "让 Hermes 重新生成";
  approveButton.textContent = "批准并进入 Outbox";
  rejectButton.textContent = "拒绝";
  saveButton.className = "secondary";
  regenerateButton.className = "regenerate";
  approveButton.className = "approve";
  rejectButton.className = "reject";
  actions.append(saveButton, regenerateButton, approveButton, rejectButton);

  const buttons = [
    saveButton,
    regenerateButton,
    approveButton,
    rejectButton,
  ];
  const setBusy = (busy: boolean) => {
    buttons.forEach((button) => {
      button.disabled = busy;
    });
  };
  const editPayload = () => ({
    subject: message.channel === "email" ? subjectInput.value : null,
    body: bodyInput.value,
  });

  saveButton.addEventListener("click", async () => {
    setBusy(true);
    setStatus("正在保存人工修改…", "running");
    try {
      const updated = await request<ReviewMessage>(
        `/api/review/messages/${encodeURIComponent(message.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(editPayload()),
        },
      );
      Object.assign(message, updated);
      renderReviewMessageDetail(message);
      setStatus("人工修改已保存，消息仍等待审阅", "success");
    } catch (error) {
      setBusy(false);
      showError(error);
    }
  });

  regenerateButton.addEventListener("click", async () => {
    const instruction = instructionInput.value.trim();
    if (!instruction) {
      setStatus("请先输入希望 Hermes 遵守的新限制或调整方向", "error");
      instructionInput.focus();
      return;
    }
    setBusy(true);
    setStatus("正在保存当前草稿并让 Hermes 重新生成…", "running");
    try {
      await request<ReviewMessage>(
        `/api/review/messages/${encodeURIComponent(message.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(editPayload()),
        },
      );
      const result = await request<{
        run_id: string;
        message: ReviewMessage;
      }>(
        `/api/review/messages/${encodeURIComponent(message.id)}/regenerate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            instruction,
            reviewer: reviewerInput.value,
          }),
        },
      );
      Object.assign(message, result.message);
      renderReviewMessageDetail(message);
      setStatus(
        `Hermes 已重新生成（${result.run_id}），结果仍等待人工审阅`,
        "success",
      );
    } catch (error) {
      setBusy(false);
      showError(error);
    }
  });

  approveButton.addEventListener("click", async () => {
    if (!window.confirm("确认批准该消息并创建 Outbox 投递任务？")) return;
    setBusy(true);
    setStatus("正在批准消息并写入 Outbox…", "running");
    try {
      await request(
        `/api/review/messages/${encodeURIComponent(message.id)}/approve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...editPayload(),
            reviewer: reviewerInput.value,
            note: noteInput.value || null,
          }),
        },
      );
      setStatus("消息已批准并进入 Outbox", "success");
      await loadReviewMessages();
      loadDashboard().catch(() => undefined);
    } catch (error) {
      setBusy(false);
      showError(error);
    }
  });

  rejectButton.addEventListener("click", async () => {
    if (!window.confirm("确认拒绝该消息？拒绝后不会进入 Outbox。")) return;
    setBusy(true);
    setStatus("正在拒绝消息…", "running");
    try {
      await request(
        `/api/review/messages/${encodeURIComponent(message.id)}/reject`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            reviewer: reviewerInput.value,
            note: noteInput.value || null,
          }),
        },
      );
      setStatus("消息已拒绝，未进入 Outbox", "success");
      await loadReviewMessages();
      loadDashboard().catch(() => undefined);
    } catch (error) {
      setBusy(false);
      showError(error);
    }
  });

  editor.append(
    subjectLabel,
    bodyLabel,
    reviewerLabel,
    noteLabel,
    instructionLabel,
    actions,
  );
  reviewPanel.append(
    reviewHeading,
    editor,
    block("中文对照（仅供审阅）", content.body_zh),
  );
  detail.replaceChildren(crmPanel, reviewPanel);
}

function renderReviewMessages(records: ReviewMessage[]) {
  recordList.replaceChildren();
  detail.replaceChildren();
  recordListTitle.textContent = "待发送消息";
  recordCount.textContent = `${records.length} 条`;
  records.forEach((message, index) => {
    const company = message.crm_snapshot?.company?.name;
    const contact = message.crm_snapshot?.contact?.name;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "record-item";
    const title = document.createElement("strong");
    const subtitle = document.createElement("span");
    title.textContent = text(company, "未知公司");
    subtitle.textContent = `${text(contact, "未知联系人")} · ${
      message.channel === "email" ? "邮件" : "LinkedIn"
    }`;
    button.append(
      title,
      subtitle,
      badge(message.edited_output ? "已修改" : "待审", "generated"),
    );
    button.addEventListener("click", () => {
      recordList
        .querySelectorAll(".active")
        .forEach((node) => node.classList.remove("active"));
      button.classList.add("active");
      renderReviewMessageDetail(message);
    });
    recordList.append(button);
    if (index === 0) button.click();
  });
  if (!records.length) {
    detail.append(block("待审消息", "当前没有等待人工审阅的正式消息"));
  }
  workspace.classList.remove("hidden");
}

function renderQueueDetail(record: QueueRecord, leadDetail?: LeadDetail) {
  const snapshot = leadDetail?.state.crm_snapshot || {};
  const lead = snapshot.lead || {};
  const company = snapshot.company || {};
  const contact = snapshot.contact || {};
  const schedulePanel = document.createElement("div");
  schedulePanel.className = "panel";
  const heading = document.createElement("div");
  heading.className = "panel-heading";
  const h2 = document.createElement("h2");
  h2.textContent = "二级线索调度";
  heading.append(h2, badge(queueStatusText(record), record.status));
  const fields = document.createElement("dl");
  [
    ["公司", company.name],
    ["联系人", contact.name],
    ["联系人 ID", record.lead_id],
    ["分类", leadTypeLabels[record.lead_type || ""] || "待分类"],
    ["置信度", record.classification_confidence],
    ["最后跟进", lead.last_follow_up_at],
    ["沉淀到期", formatDateTime(record.classification_due_at)],
    ["下次动作", formatDateTime(record.next_action_at)],
    ["成功发送次数", record.follow_up_count],
    ["Outbox 消息 ID", record.latest_message_version_id],
    ["状态更新时间", formatDateTime(record.updated_at)],
  ].forEach(([key, value]) => fields.append(field(String(key), value)));
  schedulePanel.append(heading, fields);
  if (lead.internal_note) {
    schedulePanel.append(block("CRM 内文本", lead.internal_note));
  }

  const reasonPanel = document.createElement("div");
  reasonPanel.className = "panel output-panel";
  const reasonHeading = document.createElement("div");
  reasonHeading.className = "panel-heading";
  const reasonTitle = document.createElement("h2");
  reasonTitle.textContent = "分类与处理轨迹";
  reasonHeading.append(reasonTitle);
  reasonPanel.append(reasonHeading);
  const canReviewClassification =
    record.status === "needs_review" &&
    leadDetail?.state.last_classified_at &&
    !leadDetail.state.last_generated_at &&
    !record.latest_message_version_id;
  if (canReviewClassification) {
    const reviewSection = document.createElement("section");
    reviewSection.className = "classification-review";
    const reviewHeading = document.createElement("h3");
    const reviewHint = document.createElement("p");
    const actions = document.createElement("div");
    reviewHeading.textContent = "人工确认分类";
    reviewHint.textContent =
      `Hermes 建议：${leadTypeLabels[record.lead_type || ""] || "待分类"}。` +
      "请选择最终分类，确认后系统会计算下一次动作时间。";
    reviewHint.className = "muted";
    actions.className = "classification-actions";
    Object.entries(leadTypeLabels).forEach(([leadType, label]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      if (leadType === record.lead_type) button.classList.add("suggested");
      button.addEventListener("click", async () => {
        const buttons = actions.querySelectorAll<HTMLButtonElement>("button");
        buttons.forEach((item) => {
          item.disabled = true;
        });
        setStatus(`正在确认分类：${label}…`, "running");
        try {
          const result = await request<{
            lead_id: string;
            lead_type: string;
            status: string;
            next_action_at: string;
          }>(
            `/api/polling/leads/${encodeURIComponent(record.lead_id)}/classification`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ lead_type: leadType }),
            },
          );
          record.lead_type = result.lead_type;
          record.status = result.status;
          record.next_action_at = result.next_action_at;
          record.last_error = null;
          const refreshed = await request<LeadDetail>(
            `/api/polling/leads/${encodeURIComponent(record.lead_id)}`,
          );
          renderQueueDetail(record, refreshed);
          const active = recordList.querySelector<HTMLElement>(".record-item.active");
          const title = active?.querySelector("strong");
          const statusBadge = active?.querySelector<HTMLElement>(".badge");
          if (title) title.textContent = leadTypeLabels[result.lead_type] || result.lead_type;
          if (statusBadge) {
            statusBadge.textContent = queueStatusLabels[result.status] || result.status;
            statusBadge.className = `badge ${result.status}`;
          }
          setStatus(
            `已确认分类：${leadTypeLabels[result.lead_type] || result.lead_type}，并完成排期`,
            "success",
          );
          loadDashboard().catch(() => undefined);
        } catch (error) {
          buttons.forEach((item) => {
            item.disabled = false;
          });
          showError(error);
        }
      });
      actions.append(button);
    });
    reviewSection.append(reviewHeading, reviewHint, actions);
    reasonPanel.append(reviewSection);
  }
  reasonPanel.append(
    block("分类理由", record.classification_reason),
    block("最近错误", record.last_error),
  );
  if (leadDetail?.classifications.length) {
    const latest = leadDetail.classifications[0];
    reasonPanel.append(
      block("分类证据", latest.evidence),
      block(
        "分类记录",
        `${leadTypeLabels[latest.lead_type] || latest.lead_type} · ` +
          `分类置信度 ${latest.confidence} · 信息完整度 ${
            latest.information_completeness ?? "—"
          } · ${formatDateTime(latest.classified_at)}`,
      ),
    );
  }
  if (leadDetail?.events.length) {
    const timeline = leadDetail.events
      .slice(0, 20)
      .map(
        (event) =>
          `${formatDateTime(event.event_at)}  ${
            eventLabels[event.event_type] || event.event_type
          }`,
      )
      .join("\n");
    reasonPanel.append(block("最近事件", timeline));
  } else if (!leadDetail) {
    reasonPanel.append(block("最近事件", "正在读取完整轨迹…"));
  }
  detail.replaceChildren(schedulePanel, reasonPanel);
}

function renderQueue(records: QueueRecord[]) {
  recordList.replaceChildren();
  detail.replaceChildren();
  recordListTitle.textContent = "二级线索队列";
  recordCount.textContent = `${records.length} 条`;
  records.forEach((record, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "record-item";
    const title = document.createElement("strong");
    const subtitle = document.createElement("span");
    title.textContent = leadTypeLabels[record.lead_type || ""] || "待分类";
    subtitle.textContent = record.lead_id;
    button.append(
      title,
      subtitle,
      badge(queueStatusText(record), record.status),
    );
    button.addEventListener("click", async () => {
      recordList.querySelectorAll(".active").forEach((node) => node.classList.remove("active"));
      button.classList.add("active");
      renderQueueDetail(record);
      try {
        const lead = await request<LeadDetail>(
          `/api/polling/leads/${encodeURIComponent(record.lead_id)}`,
        );
        if (button.classList.contains("active")) renderQueueDetail(record, lead);
      } catch (error) {
        if (button.classList.contains("active")) {
          setStatus(
            error instanceof Error ? `线索轨迹读取失败：${error.message}` : "线索轨迹读取失败",
            "error",
          );
        }
      }
    });
    recordList.append(button);
    if (index === 0) button.click();
  });
  workspace.classList.remove("hidden");
}

async function poll(runId: string) {
  const run = await request<RunState>(`/api/runs/${runId}`);
  renderSummary(run);
  if (run.status === "running") {
    setStatus(`正在生成：${run.completed} / ${run.total || "…"}`, "running");
    window.setTimeout(() => poll(runId).catch(showError), 1500);
    return;
  }
  startButton.disabled = false;
  if (run.status === "failed") {
    setStatus(run.error || "生成失败", "error");
  } else {
    setStatus(`生成完成：${run.valid} 条有效，${run.invalid} 条无效`, "success");
  }
  const result = await request<{ records: RecordResult[] }>(`/api/runs/${runId}/results`);
  renderRecords(result.records);
  loadDashboard().catch(() => undefined);
}

function showError(error: unknown) {
  startButton.disabled = false;
  setStatus(error instanceof Error ? error.message : "发生未知错误", "error");
}

async function loadQueue(statuses: string[] = []) {
  pollQueueButton.disabled = true;
  summary.classList.add("hidden");
  workspace.classList.add("hidden");
  setStatus("正在读取二级线索队列…", "running");
  const parameters = new URLSearchParams({ limit: "500" });
  if (statuses.length) parameters.set("status", statuses.join(","));
  try {
    const result = await request<{ records: QueueRecord[] }>(
      `/api/polling/queue?${parameters.toString()}`,
    );
    renderQueue(result.records);
    const scope = statuses.length
      ? statuses.map((status) => queueStatusLabels[status] || status).join("、")
      : "全部状态";
    setStatus(
      result.records.length
        ? `已加载 ${result.records.length} 条二级线索（${scope}）`
        : `当前没有符合条件的二级线索（${scope}）`,
      result.records.length ? "success" : "",
    );
  } finally {
    pollQueueButton.disabled = false;
  }
}

async function loadReviewMessages() {
  reviewMessagesButton.disabled = true;
  summary.classList.add("hidden");
  workspace.classList.add("hidden");
  setStatus("正在读取 Outbox 前的待审消息…", "running");
  try {
    const result = await request<{ records: ReviewMessage[] }>(
      "/api/review/messages?status=pending_review&limit=200",
    );
    renderReviewMessages(result.records);
    setStatus(
      result.records.length
        ? `已加载 ${result.records.length} 条待审消息`
        : "当前没有等待人工审阅的正式消息",
      result.records.length ? "success" : "",
    );
  } finally {
    reviewMessagesButton.disabled = false;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  startButton.disabled = true;
  summary.classList.add("hidden");
  workspace.classList.add("hidden");
  setStatus("正在从已排期队列准备消息预览…", "running");
  try {
    const run = await request<RunState>("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        limit: Number(limitInput.value),
        sort: sortInput.value,
      }),
    });
    await poll(run.run_id);
  } catch (error) {
    showError(error);
  }
});

reviewMessagesButton.addEventListener("click", async () => {
  try {
    await loadReviewMessages();
  } catch (error) {
    showError(error);
  }
});

pollQueueButton.addEventListener("click", async () => {
  try {
    await loadQueue();
  } catch (error) {
    showError(error);
  }
});

dashboardRefresh.addEventListener("click", () => {
  loadDashboard().catch(() => undefined);
});

reviewModeToggle.addEventListener("click", async () => {
  if (currentReviewEnabled === null) return;
  const enabled = !currentReviewEnabled;
  const action = enabled ? "开启" : "关闭";
  const consequence = enabled
    ? "后续低置信度分类和生成消息将等待人工处理。"
    : "低置信度分类将继续排期，待审阅消息会自动批准进入投递队列。";
  if (!window.confirm(`确认${action}人工审阅？\n${consequence}`)) return;
  reviewModeToggle.disabled = true;
  setStatus(`正在${action}人工审阅…`, "running");
  try {
    const result = await request<
      ReviewConfiguration & {
        scheduler_notified: boolean;
        changed_at: string;
      }
    >("/api/polling/review-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    renderReviewMode(result);
    setStatus(
      `${action}人工审阅已生效` +
        (result.scheduler_notified ? "，调度器已立即唤醒" : "，设置已保存"),
      "success",
    );
    loadDashboard().catch(() => undefined);
  } catch (error) {
    reviewModeToggle.disabled = false;
    showError(error);
  }
});

loadDashboard().catch(() => undefined);
