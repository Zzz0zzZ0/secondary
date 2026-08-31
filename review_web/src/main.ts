import "./styles.css";

type Json = Record<string, any>;
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
type ReviewPriorityKind =
  | "customer_reply"
  | "overdue_48h"
  | "overdue"
  | "due_24h"
  | "scheduled"
  | "unscheduled";
type ReviewPriority = {
  kind: ReviewPriorityKind;
  label: string;
  rank: number;
  at: string | null;
  timing: string;
};
type ConversationAction = {
  id: string;
  run_id: string;
  lead_id: string;
  action_type: "internal_task" | "manual_review";
  crm_snapshot: Json;
  analysis: Json;
  status: "pending" | "resolved" | "dismissed";
  handled_by: string | null;
  resolution_note: string | null;
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
  notes_owned?: number;
};
type OutboxDelivery = {
  id: string;
  message_version_id: string;
  channel: "email" | "linkedin";
  provider: string;
  recipient: string;
  sender_account_ref: string | null;
  payload: Json;
  status: string;
  attempt_count: number;
  max_attempts: number;
  available_at: string;
  sent_at: string | null;
  last_error_code: string | null;
  last_error_message: string | null;
  created_at: string;
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
type RuntimeReportSummary = {
  date: string;
  generated_at: string;
  overall_status: "正常" | "有风险" | "异常";
  complete: boolean;
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
  notes_follow_up: {
    latest_note_id: string;
    structural_state: string;
    status: string;
    decision: string | null;
    publication_type: string | null;
    publication_id: string | null;
    note_count: number;
    notes: Json[];
    updated_at: string;
  } | null;
};

const reviewMessagesButton = document.querySelector<HTMLButtonElement>(
  "#review-messages-button",
)!;
const reviewPriorityFilter = document.querySelector<HTMLSelectElement>(
  "#review-priority-filter",
)!;
const reviewPriorityStats = document.querySelector<HTMLElement>(
  "#review-priority-stats",
)!;
const reviewSearch = document.querySelector<HTMLInputElement>("#review-search")!;
const reviewClearFilters = document.querySelector<HTMLButtonElement>(
  "#review-clear-filters",
)!;
const reviewChannelFilter = document.querySelector<HTMLSelectElement>(
  "#review-channel-filter",
)!;
const reviewSalesFilter = document.querySelector<HTMLSelectElement>(
  "#review-sales-filter",
)!;
const reviewRouteFilter = document.querySelector<HTMLSelectElement>(
  "#review-route-filter",
)!;
let pendingReviewAll: ReviewMessage[] = [];
let visibleReviewMessages: ReviewMessage[] = [];
const conversationActionsButton = document.querySelector<HTMLButtonElement>(
  "#conversation-actions-button",
)!;
const outboxQueueButton = document.querySelector<HTMLButtonElement>(
  "#outbox-queue-button",
)!;
const pollQueueButton = document.querySelector<HTMLButtonElement>("#poll-queue-button")!;
const statusBox = document.querySelector<HTMLElement>("#status")!;
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
const runtimeReportStatus = document.querySelector<HTMLElement>("#runtime-report-status")!;
const runtimeReportDate = document.querySelector<HTMLSelectElement>("#runtime-report-date")!;
const runtimeReportJson = document.querySelector<HTMLAnchorElement>("#runtime-report-json")!;
const runtimeReportMarkdown = document.querySelector<HTMLAnchorElement>("#runtime-report-markdown")!;
const runtimeReportGenerate = document.querySelector<HTMLButtonElement>("#runtime-report-generate")!;
const runtimeReportRefresh = document.querySelector<HTMLButtonElement>("#runtime-report-refresh")!;
const runtimeReportContent = document.querySelector<HTMLElement>("#runtime-report-content")!;

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

function emailHistoryText(history: unknown): string {
  if (!Array.isArray(history)) return "";
  return history.map((note, index) => {
    const direction = note.direction === "FA" ? "销售发信" : "客户来信";
    const time = note.email_at ? formatDateTime(note.email_at) : "邮件时间未知";
    return `${index + 1}. ${direction} · ${time} · ${text(note.subject)}\n${text(note.body)}`;
  }).join("\n\n──────────\n\n");
}

const warningLabels: Record<string, string> = {
  CONTACT_CHANNEL_MISSING: "未在CRM检测到邮箱或者领英",
  SELECTED_CHANNEL_MISSING: "缺少所选渠道地址",
  COMPANY_CONTEXT_MISSING: "公司背调缺失",
  COMPANY_CONTEXT_THIN: "公司背调信息不足",
  NON_CATALOG_PRODUCT_REQUIRES_PRODUCTION_PARTNER_CONFIRMATION: "非目录产品需要向生产合作方确认",
  NON_CATALOG_PRODUCT_REQUIRES_FACTORY_CONFIRMATION: "非目录产品需要向生产合作方确认（旧记录）",
  CONTACT_NAME_LOW_CONFIDENCE: "联系人姓名可信度较低",
  INQUIRY_CONTEXT_MISSING: "未关联到具体询盘正文",
  INTERNAL_CONFIRMATION_REQUIRED: "需要内部确认",
  PRICE_FIELDS_INTENTIONALLY_EXCLUDED: "报价字段已按规则排除",
  RECENT_NO_CURRENT_DEMAND: "近期已确认暂无需求",
  FOLLOW_UP_TIMING_UNVERIFIED: "跟进时间尚未确认",
  SALES_FOLLOW_UP_TIME_UNVERIFIED: "销售已回复，但跟进时间尚未确认",
  CRM_EVIDENCE_REQUIRES_MANUAL_CONFIRMATION: "CRM证据不足，必须人工确认",
  DOCUMENT_AVAILABILITY_UNVERIFIED: "资料是否可提供尚未确认",
  AVAILABILITY_REQUIRES_MANUAL_CONFIRMATION: "资料或产品可用性需人工确认",
  CUSTOMER_REFERENCE_DISCLOSURE_REQUIRES_APPROVAL: "客户案例披露需要审批",
  REFERRAL_CONTEXT_MISSING: "缺少推荐关系信息",
  SALES_ENGLISH_NAME_MISSING: "销售英文姓名缺失",
  SALES_NAME_TRANSLITERATED: "销售姓名已按拼音转换",
  SALES_NAME_TRANSLITERATION_UNCERTAIN: "销售姓名拼音需人工核对",
  DO_NOT_CONTACT: "客户明确要求不要联系",
  NOTES_EXPERIMENT_REQUIRES_MANUAL_REVIEW: "历史 Notes 实验消息，仅供人工审阅",
  NOTES_REVIEW_ONLY_REQUIRES_MANUAL_REVIEW: "Notes 邮件消息，仅供人工审阅",
};
const messageRouteLabels: Record<string, string> = {
  email_reply: "客户邮件待回复",
  recommender_thanks: "推荐人待感谢",
  referred_intro: "被推荐人待联系",
  qualification: "需求待确认",
  conversation_follow_up: "已沟通待跟进",
  referral_review: "推荐关系待核对",
  default: "按原分类处理",
};
const conversationActionLabels: Record<string, string> = {
  reply: "直接回复",
  internal_task: "内部待办",
  referral: "转介绍感谢",
  no_action: "无需发送",
  manual_review: "人工判断",
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
const notesStatusLabels: Record<string, string> = {
  pending: "待分析",
  processing: "分析中",
  retry_wait: "等待重试",
  completed: "已处理",
  baseline: "历史基线",
  skipped: "等待客户",
  failed: "处理失败",
  superseded: "已被新邮件替代",
};
const outboxStatusLabels: Record<string, string> = {
  queued: "待投递",
  sending: "投递中",
  sent: "已发送",
  retry_wait: "等待重试",
  failed: "发送失败",
  unknown: "结果未知",
  cancelled: "已取消",
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
  manual_retry_queued: "手动重新排期",
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

function reviewScheduleAt(message: ReviewMessage): string | null {
  const snapshot = message.crm_snapshot || {};
  const contact = snapshot.contact || {};
  const lead = snapshot.lead || {};
  const value =
    contact.nextFollowUp ||
    contact.next_follow_up_at ||
    lead.nextFollowUp ||
    lead.next_follow_up_at ||
    snapshot.follow_up_schedule?.effective_next_follow_up_at ||
    snapshot.secondary_lead_schedule?.next_action_at ||
    lead.next_eligible_follow_up_at;
  return value ? String(value) : null;
}

function reviewPriority(message: ReviewMessage, now = Date.now()): ReviewPriority {
  const snapshot = message.crm_snapshot || {};
  if (snapshot.message_route === "email_reply") {
    const at =
      snapshot.review_context?.crm_email_evidence?.email_at ||
      snapshot.source_version?.activity_at ||
      message.created_at;
    return {
      kind: "customer_reply",
      label: "客户来信待回复",
      rank: 0,
      at: String(at),
      timing: `客户来信 ${formatDateTime(at)}`,
    };
  }

  const at = reviewScheduleAt(message);
  const dueAt = at ? new Date(at).getTime() : Number.NaN;
  if (!at || Number.isNaN(dueAt)) {
    return {
      kind: "unscheduled",
      label: "待排期",
      rank: 5,
      at: null,
      timing: "尚未排期",
    };
  }
  if (dueAt <= now - 48 * 60 * 60 * 1000) {
    return {
      kind: "overdue_48h",
      label: "严重逾期",
      rank: 1,
      at,
      timing: `计划 ${formatDateTime(at)}`,
    };
  }
  if (dueAt <= now) {
    return {
      kind: "overdue",
      label: "已到期",
      rank: 2,
      at,
      timing: `计划 ${formatDateTime(at)}`,
    };
  }
  if (dueAt <= now + 24 * 60 * 60 * 1000) {
    return {
      kind: "due_24h",
      label: "24 小时内",
      rank: 3,
      at,
      timing: `计划 ${formatDateTime(at)}`,
    };
  }
  return {
    kind: "scheduled",
    label: "后续排期",
    rank: 4,
    at,
    timing: `计划 ${formatDateTime(at)}`,
  };
}

function renderReviewPriorityStats(records: ReviewMessage[]): void {
  const priorities = records.map((message) => reviewPriority(message).kind);
  const stats = [
    ["全部待处理", records.length],
    ["客户来信待回复", priorities.filter((kind) => kind === "customer_reply").length],
    [
      "逾期跟进",
      priorities.filter((kind) => kind === "overdue_48h" || kind === "overdue").length,
    ],
    ["24 小时内", priorities.filter((kind) => kind === "due_24h").length],
  ];
  reviewPriorityStats.replaceChildren(
    ...stats.map(([label, value]) => {
      const node = document.createElement("div");
      node.className = "priority-stat";
      const strong = document.createElement("strong");
      const span = document.createElement("span");
      strong.textContent = String(value);
      span.textContent = String(label);
      node.append(strong, span);
      return node;
    }),
  );
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
    {
      label: "需人工处理",
      statuses: ["needs_review", "needs_contact", "failed"],
      tone: "attention",
    },
    { label: "已排期", statuses: ["scheduled"] },
    {
      label: "生成与审阅",
      statuses: ["generating", "waiting_review"],
      reviewMessages: true,
    },
    { label: "等待投递", statuses: ["waiting_delivery"] },
    {
      label: "暂停与已转出",
      statuses: ["paused", "converted"],
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

async function loadRuntimeReport(reportDate: string) {
  runtimeReportStatus.textContent = `正在读取 ${reportDate} 运行报告…`;
  const response = await fetch(`/api/runtime-reports/${reportDate}/markdown`);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || "运行报告读取失败");
  }
  runtimeReportContent.textContent = await response.text();
  runtimeReportJson.href = `/api/runtime-reports/${reportDate}?download=true`;
  runtimeReportMarkdown.href = `/api/runtime-reports/${reportDate}/markdown?download=true`;
  runtimeReportJson.classList.remove("disabled");
  runtimeReportMarkdown.classList.remove("disabled");
}

async function loadRuntimeReports() {
  runtimeReportRefresh.disabled = true;
  runtimeReportDate.disabled = true;
  runtimeReportStatus.textContent = "正在读取已归档报告…";
  try {
    const result = await request<{ records: RuntimeReportSummary[] }>(
      "/api/runtime-reports",
    );
    runtimeReportDate.replaceChildren(
      ...result.records.map((report) => {
        const option = document.createElement("option");
        option.value = report.date;
        option.textContent = `${report.date} · ${report.overall_status}${
          report.complete ? "" : " · 数据不完整"
        }`;
        return option;
      }),
    );
    if (!result.records.length) {
      runtimeReportContent.textContent = "暂无运行报告";
      runtimeReportStatus.textContent = "尚未手动生成报告";
      return;
    }
    runtimeReportDate.disabled = false;
    await loadRuntimeReport(runtimeReportDate.value);
    const latest = result.records[0];
    runtimeReportStatus.textContent =
      `${latest.date} · ${latest.overall_status} · ` +
      `${latest.complete ? "数据完整" : "数据不完整"} · 只读展示`;
  } catch (error) {
    runtimeReportStatus.textContent =
      error instanceof Error ? `报告读取失败：${error.message}` : "报告读取失败";
  } finally {
    runtimeReportRefresh.disabled = false;
  }
}

function warningText(value: unknown): string {
  if (!Array.isArray(value)) return text(value);
  return value.map((item) => warningLabels[String(item)] || String(item)).join("；") || "—";
}

function setStatus(message: string, kind = "") {
  statusBox.textContent = message;
  statusBox.className = `status ${kind}`.trim();
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

function block(
  title: string,
  content: unknown,
  options: { collapsible?: boolean } = {},
): HTMLElement {
  const section = document.createElement("section");
  const pre = document.createElement("pre");
  pre.textContent = text(content);
  if (options.collapsible) {
    const disclosure = document.createElement("details");
    const summary = document.createElement("summary");
    section.className = "evidence-disclosure";
    summary.textContent = title;
    disclosure.append(summary, pre);
    section.append(disclosure);
    return section;
  }
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.append(heading, pre);
  return section;
}

function selectReviewMessage(messageId: string, focusListItem = false): void {
  const message = visibleReviewMessages.find((item) => item.id === messageId);
  const button = recordList.querySelector<HTMLButtonElement>(
    `[data-message-id="${CSS.escape(messageId)}"]`,
  );
  if (!message || !button) return;
  recordList.querySelectorAll<HTMLButtonElement>(".record-item").forEach((item) => {
    const active = item === button;
    item.classList.toggle("active", active);
    item.setAttribute("aria-current", active ? "true" : "false");
  });
  renderReviewMessageDetail(message);
  button.scrollIntoView({ block: "nearest" });
  if (focusListItem) button.focus();
}

function selectNextReviewMessage(messageId: string): void {
  const index = visibleReviewMessages.findIndex((item) => item.id === messageId);
  const next = visibleReviewMessages[index + 1];
  if (!next) {
    setStatus("已到当前筛选结果的最后一条", "success");
    return;
  }
  selectReviewMessage(next.id, true);
  setStatus(`已进入下一条（${index + 2}/${visibleReviewMessages.length}）`, "success");
}

function renderReviewMessageDetail(message: ReviewMessage) {
  const snapshot = message.crm_snapshot || {};
  const lead = snapshot.lead || {};
  const company = snapshot.company || {};
  const contact = snapshot.contact || {};
  const sales = snapshot.sales || {};
  const output = message.effective_output || {};
  const content = output.content || {};
  const priority = reviewPriority(message);

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
    ["处理优先级", priority.label],
    ["优先依据", priority.timing],
    ["处理路由", messageRouteLabels[snapshot.message_route] || snapshot.message_route],
    [
      "会话动作",
      conversationActionLabels[snapshot.conversation_action] || snapshot.conversation_action,
    ],
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
  );
  if (snapshot.review_context?.crm_email_evidence) {
    crmPanel.append(
      block("邮件往来证据", snapshot.review_context.crm_email_evidence.evidence_quote),
    );
  }
  if (snapshot.review_context?.crm_email_history) {
    crmPanel.append(
      block(
        "完整邮件上下文",
        emailHistoryText(snapshot.review_context.crm_email_history),
        { collapsible: true },
      ),
    );
  }
  crmPanel.append(
    block("Hermes 判断理由", output.reason),
    block("内部提醒", warningText(output.warnings)),
  );
  const notesReviewOnly =
    snapshot.notes_review_only === true || snapshot.notes_experiment === true;
  const approvalBlocked =
    snapshot.message_route === "referral_review" || notesReviewOnly;
  if (snapshot.message_route === "referral_review") {
    crmPanel.append(
      block(
        "人工动作",
        "推荐关系方向未确认。请先核对 CRM 原文；当前消息禁止批准发送。",
      ),
    );
  } else if (notesReviewOnly) {
    crmPanel.append(
      block(
        "人工动作",
        "Notes 邮件已由 Poller 正式接入。可修改、重新生成或拒绝；当前仍为人工审阅模式，禁止批准发送。",
      ),
    );
  }

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
  const nextButton = document.createElement("button");
  saveButton.type = regenerateButton.type = approveButton.type = rejectButton.type =
    nextButton.type = "button";
  saveButton.textContent = "保存修改";
  regenerateButton.textContent = "让 Hermes 重新生成";
  approveButton.textContent = "批准并进入 Outbox";
  rejectButton.textContent = "拒绝";
  nextButton.textContent = "下一条";
  saveButton.className = "secondary";
  regenerateButton.className = "regenerate";
  approveButton.className = "approve";
  rejectButton.className = "reject";
  nextButton.className = "next";
  actions.append(saveButton, regenerateButton, approveButton, rejectButton, nextButton);
  if (approvalBlocked) {
    approveButton.disabled = true;
    approveButton.title = notesReviewOnly
      ? "Notes 人工审阅消息禁止批准发送"
      : "推荐关系未核对，禁止批准发送";
  }

  const buttons = [
    saveButton,
    regenerateButton,
    approveButton,
    rejectButton,
  ];
  nextButton.addEventListener("click", () => selectNextReviewMessage(message.id));
  const setBusy = (busy: boolean) => {
    buttons.forEach((button) => {
      button.disabled = busy || (
        button === approveButton && approvalBlocked
      );
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
    if (!window.confirm("页面不会直接发送。确认批准该消息并创建 Outbox 投递任务？")) return;
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
    block("中文对照（仅供审阅）", content.body_zh, { collapsible: true }),
  );
  detail.replaceChildren(crmPanel, reviewPanel);
}

function renderReviewMessages(records: ReviewMessage[]) {
  visibleReviewMessages = records;
  recordList.replaceChildren();
  detail.replaceChildren();
  recordListTitle.textContent = "待处理发信";
  recordCount.textContent = `${records.length} 条`;
  records.forEach((message, index) => {
    const company = message.crm_snapshot?.company?.name;
    const contact = message.crm_snapshot?.contact?.name;
    const route = message.crm_snapshot?.message_route || "default";
    const priority = reviewPriority(message);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "record-item";
    button.dataset.priorityKind = priority.kind;
    button.dataset.messageId = message.id;
    button.setAttribute("aria-current", "false");
    const title = document.createElement("strong");
    const subtitle = document.createElement("span");
    title.textContent = text(company, "未知公司");
    subtitle.textContent = `${text(contact, "未知联系人")} · ${
      messageRouteLabels[route] || route
    } · ${priority.timing}`;
    button.append(
      title,
      subtitle,
      badge(priority.label, `priority-${priority.kind}`),
    );
    button.addEventListener("click", () => selectReviewMessage(message.id));
    recordList.append(button);
    if (index === 0) button.click();
  });
  if (!records.length) {
    detail.append(block("待审消息", "当前没有等待人工审阅的正式消息"));
  }
  workspace.classList.remove("hidden");
}

function renderConversationActionDetail(action: ConversationAction) {
  const snapshot = action.crm_snapshot || {};
  const evidence = snapshot.review_context?.crm_email_evidence || {};
  const salesAction = action.analysis?.sales_action?.action;
  const panel = document.createElement("div");
  panel.className = "panel";
  const heading = document.createElement("div");
  heading.className = "panel-heading";
  const title = document.createElement("h2");
  title.textContent = "会话动作依据";
  heading.append(
    title,
    badge(conversationActionLabels[action.action_type] || action.action_type, action.action_type),
  );
  const fields = document.createElement("dl");
  [
    ["公司", snapshot.company?.name],
    ["联系人", snapshot.contact?.name],
    ["销售", snapshot.sales?.name],
    ["销售邮箱", snapshot.conversation_sender_identity?.account],
    ["动作", conversationActionLabels[action.action_type] || action.action_type],
    ["生成时间", formatDateTime(action.created_at)],
  ].forEach(([key, value]) => fields.append(field(String(key), value)));
  panel.append(
    heading,
    fields,
    block(
      action.action_type === "internal_task" ? "销售需要执行" : "需要人工判断",
      salesAction || action.analysis?.reason,
    ),
  );
  if (salesAction) {
    panel.append(block("Hermes 判断理由", action.analysis?.reason));
  }
  panel.append(
    block("关键证据", evidence.evidence_quote),
    block(
      "Notes 邮件上下文",
      emailHistoryText(snapshot.review_context?.crm_email_history) || evidence.note_text,
      { collapsible: true },
    ),
  );

  const reviewPanel = document.createElement("div");
  reviewPanel.className = "panel output-panel";
  const reviewHeading = document.createElement("div");
  reviewHeading.className = "panel-heading";
  const reviewTitle = document.createElement("h2");
  reviewTitle.textContent = "人工处理";
  reviewHeading.append(reviewTitle, badge("待处理", "generated"));
  const form = document.createElement("form");
  form.className = "review-editor";
  const reviewerLabel = document.createElement("label");
  const reviewerInput = document.createElement("input");
  reviewerLabel.textContent = "处理人";
  reviewerInput.value = "review-ui";
  reviewerInput.maxLength = 200;
  reviewerLabel.append(reviewerInput);
  const noteLabel = document.createElement("label");
  const noteInput = document.createElement("textarea");
  noteLabel.textContent = "处理备注（可选）";
  noteInput.maxLength = 2000;
  noteInput.rows = 3;
  noteLabel.append(noteInput);
  const actions = document.createElement("div");
  actions.className = "review-actions";
  const resolveButton = document.createElement("button");
  const dismissButton = document.createElement("button");
  resolveButton.type = dismissButton.type = "button";
  resolveButton.textContent = "标记已处理";
  dismissButton.textContent = "忽略";
  resolveButton.className = "approve";
  dismissButton.className = "reject";
  actions.append(resolveButton, dismissButton);
  const decide = async (decision: "resolved" | "dismissed") => {
    resolveButton.disabled = dismissButton.disabled = true;
    setStatus(decision === "resolved" ? "正在标记已处理…" : "正在忽略该动作…", "running");
    try {
      await request(
        `/api/review/actions/${encodeURIComponent(action.id)}/decision`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            decision,
            reviewer: reviewerInput.value,
            note: noteInput.value || null,
          }),
        },
      );
      setStatus(decision === "resolved" ? "会话动作已处理" : "会话动作已忽略", "success");
      await loadConversationActions();
    } catch (error) {
      resolveButton.disabled = dismissButton.disabled = false;
      showError(error);
    }
  };
  resolveButton.addEventListener("click", () => decide("resolved"));
  dismissButton.addEventListener("click", () => decide("dismissed"));
  form.append(reviewerLabel, noteLabel, actions);
  reviewPanel.append(
    reviewHeading,
    block(
      "安全边界",
      "此队列只记录给销售的内部动作，不生成客户邮件，也没有进入 Outbox 的入口。完成准备或核实后，再由销售决定如何回复客户。",
    ),
    form,
  );
  detail.replaceChildren(panel, reviewPanel);
}

function renderConversationActions(records: ConversationAction[]) {
  recordList.replaceChildren();
  detail.replaceChildren();
  recordListTitle.textContent = "会话动作";
  recordCount.textContent = `${records.length} 条`;
  records.forEach((action, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "record-item";
    const title = document.createElement("strong");
    const subtitle = document.createElement("span");
    title.textContent = text(action.crm_snapshot?.company?.name, "未知公司");
    subtitle.textContent = `${text(action.crm_snapshot?.contact?.name, "未知联系人")} · ${text(action.crm_snapshot?.sales?.name, "未知销售")}`;
    button.append(
      title,
      subtitle,
      badge(conversationActionLabels[action.action_type] || action.action_type, action.action_type),
    );
    button.addEventListener("click", () => {
      recordList.querySelectorAll(".active").forEach((node) => node.classList.remove("active"));
      button.classList.add("active");
      renderConversationActionDetail(action);
    });
    recordList.append(button);
    if (index === 0) button.click();
  });
  if (!records.length) {
    detail.append(block("会话动作", "当前没有等待人工处理的会话动作"));
  }
  workspace.classList.remove("hidden");
}

function renderOutboxDeliveryDetail(delivery: OutboxDelivery) {
  const payload = delivery.payload || {};
  const deliveryPanel = document.createElement("div");
  deliveryPanel.className = "panel";
  const heading = document.createElement("div");
  heading.className = "panel-heading";
  const title = document.createElement("h2");
  title.textContent = "Outbox 投递信息";
  heading.append(
    title,
    badge(outboxStatusLabels[delivery.status] || delivery.status, delivery.status),
  );
  const fields = document.createElement("dl");
  [
    ["渠道", delivery.channel === "email" ? "邮件" : "LinkedIn"],
    ["收件地址", delivery.recipient],
    ["销售发信账号", delivery.sender_account_ref],
    ["已尝试次数", `${delivery.attempt_count} / ${delivery.max_attempts}`],
    ["可领取时间", formatDateTime(delivery.available_at)],
    ["发送时间", formatDateTime(delivery.sent_at)],
    ["创建时间", formatDateTime(delivery.created_at)],
  ].forEach(([key, value]) => fields.append(field(String(key), value)));
  deliveryPanel.append(heading, fields);

  const contentPanel = document.createElement("div");
  contentPanel.className = "panel output-panel";
  const contentHeading = document.createElement("div");
  contentHeading.className = "panel-heading";
  const contentTitle = document.createElement("h2");
  contentTitle.textContent = "待发消息内容";
  contentHeading.append(contentTitle);
  contentPanel.append(contentHeading);
  if (payload.subject) contentPanel.append(block("邮件主题", payload.subject));
  contentPanel.append(block("客户可见正文", payload.body));
  if (delivery.last_error_code || delivery.last_error_message) {
    contentPanel.append(
      block(
        "最近失败原因",
        [delivery.last_error_code, delivery.last_error_message]
          .filter(Boolean)
          .join("："),
      ),
    );
  }
  detail.replaceChildren(deliveryPanel, contentPanel);
}

function renderOutboxDeliveries(records: OutboxDelivery[]) {
  recordList.replaceChildren();
  detail.replaceChildren();
  recordListTitle.textContent = "Outbox 投递队列";
  recordCount.textContent = `${records.length} 条`;
  records.forEach((delivery, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "record-item";
    const title = document.createElement("strong");
    const subtitle = document.createElement("span");
    title.textContent = delivery.recipient;
    subtitle.textContent = `${delivery.channel === "email" ? "邮件" : "LinkedIn"} · ${formatDateTime(delivery.created_at)}`;
    button.append(
      title,
      subtitle,
      badge(outboxStatusLabels[delivery.status] || delivery.status, delivery.status),
    );
    button.addEventListener("click", () => {
      recordList.querySelectorAll(".active").forEach((node) => node.classList.remove("active"));
      button.classList.add("active");
      renderOutboxDeliveryDetail(delivery);
    });
    recordList.append(button);
    if (index === 0) button.click();
  });
  if (!records.length) {
    detail.append(block("Outbox 投递队列", "当前没有已批准的投递任务"));
  }
  workspace.classList.remove("hidden");
}

function renderQueueDetail(record: QueueRecord, leadDetail?: LeadDetail) {
  const snapshot = leadDetail?.state.crm_snapshot || {};
  const lead = snapshot.lead || {};
  const company = snapshot.company || {};
  const contact = snapshot.contact || {};
  const sales = snapshot.sales || {};
  const notesFollowUp = leadDetail?.notes_follow_up;
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
    ["销售人员", sales.name],
    ["联系人 ID", record.lead_id],
    ["分类", leadTypeLabels[record.lead_type || ""] || "待分类"],
    ["置信度", record.classification_confidence],
    ["最后跟进", lead.last_follow_up_at],
    ["沉淀到期", formatDateTime(record.classification_due_at)],
    ["下次动作", formatDateTime(record.next_action_at)],
    ["成功发送次数", record.follow_up_count],
    [
      "Notes 读取",
      notesFollowUp
        ? `${notesFollowUp.note_count} 条 · ${notesStatusLabels[notesFollowUp.status] || notesFollowUp.status}`
        : "未读取",
    ],
    ["Outbox 消息 ID", record.latest_message_version_id],
    ["状态更新时间", formatDateTime(record.updated_at)],
  ].forEach(([key, value]) => fields.append(field(String(key), value)));
  schedulePanel.append(heading, fields);
  if (lead.internal_note) {
    schedulePanel.append(block("CRM 内文本", lead.internal_note));
  }
  if (notesFollowUp?.notes?.length) {
    schedulePanel.append(
      block("CRM Notes 邮件上下文", emailHistoryText(notesFollowUp.notes)),
    );
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
  const canRetry = record.status === "needs_contact" || record.status === "failed";
  if (canRetry) {
    const retrySection = document.createElement("section");
    retrySection.className = "classification-review";
    const retryHeading = document.createElement("h3");
    const retryHint = document.createElement("p");
    const retryButton = document.createElement("button");
    retryHeading.textContent = record.status === "needs_contact" ? "联系人已补充？" : "失败记录处理";
    retryHint.textContent = record.status === "needs_contact"
      ? "请先在 CRM 补充有效邮箱或 LinkedIn，再重新排期；调度器在线后会重新读取 CRM。"
      : "确认失败原因已处理后，可手动重新排期生成消息。";
    retryHint.className = "muted";
    retryButton.type = "button";
    retryButton.textContent = record.status === "needs_contact" ? "重新排期重试" : "重新生成消息";
    retryButton.addEventListener("click", async () => {
      retryButton.disabled = true;
      setStatus("正在重新排期…", "running");
      try {
        const result = await request<{
          lead_id: string;
          status: string;
          next_action_at: string;
        }>(`/api/polling/leads/${encodeURIComponent(record.lead_id)}/retry`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ actor: "review-ui" }),
        });
        record.status = result.status;
        record.next_action_at = result.next_action_at;
        record.last_error = null;
        const refreshed = await request<LeadDetail>(
          `/api/polling/leads/${encodeURIComponent(record.lead_id)}`,
        );
        renderQueueDetail(record, refreshed);
        const active = recordList.querySelector<HTMLElement>(".record-item.active");
        const statusBadge = active?.querySelector<HTMLElement>(".badge");
        if (statusBadge) {
          statusBadge.textContent = queueStatusLabels[result.status] || result.status;
          statusBadge.className = `badge ${result.status}`;
        }
        setStatus("已重新排期，等待调度器处理", "success");
        loadDashboard().catch(() => undefined);
      } catch (error) {
        retryButton.disabled = false;
        showError(error);
      }
    });
    retrySection.append(retryHeading, retryHint, retryButton);
    reasonPanel.append(retrySection);
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

function showError(error: unknown) {
  setStatus(error instanceof Error ? error.message : "发生未知错误", "error");
}

async function loadQueue(statuses: string[] = []) {
  pollQueueButton.disabled = true;
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
  workspace.classList.add("hidden");
  setStatus("正在读取 Outbox 前的待审消息…", "running");
  try {
    const result = await request<{ records: ReviewMessage[] }>(
      "/api/review/messages?status=pending_review&limit=200",
    );
    pendingReviewAll = result.records;
    renderReviewPriorityStats(pendingReviewAll);
    refreshSalesFilterOptions(pendingReviewAll);
    refreshRouteFilterOptions(pendingReviewAll);
    applyReviewFilters();
    const filtered = filterReviewMessages(pendingReviewAll);
    const total = pendingReviewAll.length;
    if (!total) {
      setStatus("当前没有等待人工审阅的正式消息", "");
    } else if (filtered.length === total) {
      setStatus(`已加载 ${total} 条待审消息`, "success");
    } else {
      setStatus(
        `已加载 ${total} 条待审消息，过滤后显示 ${filtered.length} 条`,
        "success",
      );
    }
  } finally {
    reviewMessagesButton.disabled = false;
  }
}

async function loadConversationActions() {
  conversationActionsButton.disabled = true;
  workspace.classList.add("hidden");
  setStatus("正在读取会话动作…", "running");
  try {
    const result = await request<{ records: ConversationAction[] }>(
      "/api/review/actions?status=pending&limit=200",
    );
    renderConversationActions(result.records);
    setStatus(
      result.records.length
        ? `已加载 ${result.records.length} 条待处理会话动作`
        : "当前没有等待人工处理的会话动作",
      result.records.length ? "success" : "",
    );
  } finally {
    conversationActionsButton.disabled = false;
  }
}

function filterReviewMessages(records: ReviewMessage[]): ReviewMessage[] {
  const search = reviewSearch.value.trim().toLocaleLowerCase();
  const priorityKind = reviewPriorityFilter.value;
  const channel = reviewChannelFilter.value;
  const sales = reviewSalesFilter.value;
  const route = reviewRouteFilter.value;
  const now = Date.now();
  return records
    .filter((message) => {
      if (search) {
        const snapshot = message.crm_snapshot || {};
        const haystack = [
          snapshot.company?.name,
          snapshot.contact?.name,
          message.recipient_original,
          snapshot.sales?.name,
          messageRouteLabels[snapshot.message_route] || snapshot.message_route,
        ].map((value) => text(value, "").toLocaleLowerCase()).join(" ");
        if (!haystack.includes(search)) return false;
      }
      if (priorityKind && reviewPriority(message, now).kind !== priorityKind) return false;
      if (channel && message.channel !== channel) return false;
      if (sales) {
        const name = (message.crm_snapshot?.sales || {}).name;
        if (name !== sales) return false;
      }
      if (route && (message.crm_snapshot?.message_route || "default") !== route) {
        return false;
      }
      return true;
    })
    .sort((left, right) => {
      const leftPriority = reviewPriority(left, now);
      const rightPriority = reviewPriority(right, now);
      if (leftPriority.rank !== rightPriority.rank) {
        return leftPriority.rank - rightPriority.rank;
      }
      const leftAt = leftPriority.at ? new Date(leftPriority.at).getTime() : Number.MAX_VALUE;
      const rightAt = rightPriority.at ? new Date(rightPriority.at).getTime() : Number.MAX_VALUE;
      if (leftAt !== rightAt) return leftAt - rightAt;
      return new Date(left.created_at).getTime() - new Date(right.created_at).getTime();
    });
}

function refreshSalesFilterOptions(records: ReviewMessage[]): void {
  const current = reviewSalesFilter.value;
  const names = new Set<string>();
  for (const message of records) {
    const name = (message.crm_snapshot?.sales || {}).name;
    if (name) names.add(String(name));
  }
  const sorted = [...names].sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
  reviewSalesFilter.replaceChildren();
  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent = "全部";
  reviewSalesFilter.append(allOption);
  for (const name of sorted) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    reviewSalesFilter.append(option);
  }
  if (current && sorted.includes(current)) {
    reviewSalesFilter.value = current;
  } else {
    reviewSalesFilter.value = "";
  }
}

function refreshRouteFilterOptions(records: ReviewMessage[]): void {
  const current = reviewRouteFilter.value;
  const routes = new Set<string>();
  records.forEach((message) => {
    routes.add(String(message.crm_snapshot?.message_route || "default"));
  });
  reviewRouteFilter.replaceChildren();
  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent = "全部";
  reviewRouteFilter.append(allOption);
  [...routes].sort().forEach((route) => {
    const option = document.createElement("option");
    option.value = route;
    option.textContent = messageRouteLabels[route] || route;
    reviewRouteFilter.append(option);
  });
  reviewRouteFilter.value = current && routes.has(current) ? current : "";
}

function applyReviewFilters(): void {
  const filtered = filterReviewMessages(pendingReviewAll);
  renderReviewMessages(filtered);
}

async function loadOutboxDeliveries() {
  outboxQueueButton.disabled = true;
  workspace.classList.add("hidden");
  setStatus("正在读取 Outbox 投递队列…", "running");
  try {
    const result = await request<{ records: OutboxDelivery[] }>(
      "/api/outbox/deliveries?limit=500",
    );
    renderOutboxDeliveries(result.records);
    setStatus(
      result.records.length
        ? `已加载 ${result.records.length} 条 Outbox 投递任务（只读）`
        : "当前没有已批准的投递任务",
      result.records.length ? "success" : "",
    );
  } finally {
    outboxQueueButton.disabled = false;
  }
}

reviewMessagesButton.addEventListener("click", async () => {
  try {
    await loadReviewMessages();
  } catch (error) {
    showError(error);
  }
});

conversationActionsButton.addEventListener("click", async () => {
  try {
    await loadConversationActions();
  } catch (error) {
    showError(error);
  }
});

function handleReviewFilterChange(): void {
  if (!pendingReviewAll.length) return;
  applyReviewFilters();
  const total = pendingReviewAll.length;
  const filtered = filterReviewMessages(pendingReviewAll);
  setStatus(
    filtered.length === total
      ? `已加载 ${total} 条待审消息`
      : `已加载 ${total} 条待审消息，过滤后显示 ${filtered.length} 条`,
    filtered.length ? "success" : "",
  );
}

reviewPriorityFilter.addEventListener("change", handleReviewFilterChange);
reviewChannelFilter.addEventListener("change", handleReviewFilterChange);
reviewSalesFilter.addEventListener("change", handleReviewFilterChange);
reviewRouteFilter.addEventListener("change", handleReviewFilterChange);
reviewSearch.addEventListener("input", handleReviewFilterChange);
reviewClearFilters.addEventListener("click", () => {
  reviewSearch.value = "";
  reviewPriorityFilter.value = "";
  reviewChannelFilter.value = "";
  reviewSalesFilter.value = "";
  reviewRouteFilter.value = "";
  handleReviewFilterChange();
  reviewSearch.focus();
});

recordList.addEventListener("keydown", (event) => {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const buttons = [...recordList.querySelectorAll<HTMLButtonElement>(".record-item")];
  if (!buttons.length) return;
  const active = recordList.querySelector<HTMLButtonElement>(".record-item.active");
  const current = Math.max(0, buttons.indexOf(active || buttons[0]));
  const offset = event.key === "ArrowDown" ? 1 : -1;
  const target = buttons[Math.min(buttons.length - 1, Math.max(0, current + offset))];
  event.preventDefault();
  target.click();
  target.focus();
});

document.querySelectorAll<HTMLButtonElement>("button[data-focused-view]").forEach((button) => {
  button.addEventListener("click", () => {
    const view = button.dataset.focusedView || "review";
    document.documentElement.dataset.focusedView = view;
    document.querySelectorAll<HTMLButtonElement>("button[data-focused-view]").forEach((item) => {
      item.setAttribute("aria-pressed", String(item === button));
    });
  });
});

outboxQueueButton.addEventListener("click", async () => {
  try {
    await loadOutboxDeliveries();
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

runtimeReportRefresh.addEventListener("click", () => {
  loadRuntimeReports().catch(() => undefined);
});

runtimeReportGenerate.addEventListener("click", async () => {
  runtimeReportGenerate.disabled = true;
  runtimeReportStatus.textContent = "正在生成今日 00:00 至当前时刻的运行报告…";
  try {
    const report = await request<RuntimeReportSummary>(
      "/api/runtime-reports/generate",
      { method: "POST" },
    );
    await loadRuntimeReports();
    runtimeReportStatus.textContent =
      `${report.date} · ${report.overall_status} · ` +
      `${report.complete ? "数据完整" : "数据不完整"} · 已手动生成`;
  } catch (error) {
    runtimeReportStatus.textContent =
      error instanceof Error ? `报告生成失败：${error.message}` : "报告生成失败";
  } finally {
    runtimeReportGenerate.disabled = false;
  }
});

runtimeReportDate.addEventListener("change", () => {
  loadRuntimeReport(runtimeReportDate.value)
    .then(() => {
      runtimeReportStatus.textContent = `${runtimeReportDate.value} · 只读展示`;
    })
    .catch((error) => {
      runtimeReportStatus.textContent =
        error instanceof Error ? `报告读取失败：${error.message}` : "报告读取失败";
    });
});

loadReviewMessages().catch(showError);
loadDashboard().catch(() => undefined);
loadRuntimeReports().catch(() => undefined);
