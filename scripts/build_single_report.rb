#!/usr/bin/env ruby
# frozen_string_literal: true

require "cgi"
require "fileutils"
require "json"
require "time"

run_dir = File.expand_path(ARGV[0].to_s)
abort "Usage: build_single_report.rb RUN_DIR [OUTPUT_HTML]" if ARGV[0].to_s.empty?
abort "Run directory not found: #{run_dir}" unless Dir.exist?(run_dir)

output_path = File.expand_path(ARGV[1] || File.join(run_dir, "report.html"))

def read_json(path)
  return nil unless File.file?(path)

  JSON.parse(File.read(path))
rescue JSON::ParserError
  nil
end

def h(value)
  CGI.escapeHTML(value.to_s)
end

def text_block(value)
  value.to_s.empty? ? '<span class="muted">—</span>' : h(value)
end

summary = read_json(File.join(run_dir, "summary.json")) || {}
summary_records = Array(summary["records"]).to_h { |item| [item["lead_id"], item] }

records = Dir.glob(File.join(run_dir, "records", "*"))
             .select { |path| File.directory?(path) }
             .sort
             .map do |record_dir|
  input = read_json(File.join(record_dir, "input.json")) || {}
  generated = read_json(File.join(record_dir, "generated.json"))
  review = read_json(File.join(record_dir, "review.json"))
  usage = read_json(File.join(record_dir, "usage.json")) || {}
  lead_id = input.dig("lead", "id") || File.basename(record_dir).sub(/^\d+-/, "")
  result = summary_records[lead_id] || {}

  {
    dir: record_dir,
    lead_id: lead_id,
    input: input,
    generated: generated,
    review: review,
    usage: usage,
    pipeline_status: result["pipeline_status"] || (generated ? "valid" : "invalid"),
    raw: File.file?(File.join(record_dir, "hermes_raw.txt")) ? File.read(File.join(record_dir, "hermes_raw.txt")) : ""
  }
end

decision_counts = records.group_by { |record| record.dig(:generated, "decision") || "invalid" }
                         .transform_values(&:length)

cards = records.map do |record|
  input = record[:input]
  generated = record[:generated]
  review = record[:review]
  edited = review && review["edited_output"]
  display_output = edited || generated
  contact = input["contact"] || {}
  company = input["company"] || {}
  lead = input["lead"] || {}
  warnings = Array(display_output && display_output["warnings"])
  decision = display_output && display_output["decision"] || "invalid"
  status_class = record[:pipeline_status] == "valid" ? "ok" : "bad"
  decision_class = case decision
                   when "generated" then "ok"
                   when "no_message" then "hold"
                   else "bad"
                   end
  warning_html = if warnings.empty?
                   '<span class="muted">None</span>'
                 else
                   warnings.map { |warning| %(<span class="chip">#{h(warning)}</span>) }.join
                 end

  output_html = if display_output
                  subject = display_output.dig("content", "subject")
                  body = display_output.dig("content", "body")
                  <<~HTML
                    <div class="message">
                      #{subject ? %(<div class="subject"><strong>Subject:</strong> #{h(subject)}</div>) : ""}
                      <pre>#{text_block(body)}</pre>
                    </div>
                    <dl>
                      <dt>Message goal</dt><dd>#{text_block(display_output["message_goal"])}</dd>
                      <dt>Reason</dt><dd>#{text_block(display_output["reason"])}</dd>
                      <dt>Warnings</dt><dd>#{warning_html}</dd>
                    </dl>
                  HTML
                else
                  %(<div class="error-box"><strong>No validated output.</strong><pre>#{h(record[:raw])}</pre></div>)
                end

  edit_note = if edited
                '<span class="badge edited">Human edited</span>'
              elsif review
                '<span class="badge hold">Pending review</span>'
              else
                '<span class="badge bad">Not in review queue</span>'
              end

  <<~HTML
    <article class="card">
      <header class="card-header">
        <div>
          <h2>#{h(contact["name"] || "Unknown contact")}</h2>
          <p>#{h(company["name"] || "Unknown company")} · #{h(lead["type"] || "unknown")}</p>
        </div>
        <div class="badges">
          <span class="badge #{status_class}">#{h(record[:pipeline_status])}</span>
          <span class="badge #{decision_class}">#{h(decision)}</span>
          #{edit_note}
        </div>
      </header>

      <div class="meta-grid">
        <div><span>Lead ID</span><code>#{h(record[:lead_id])}</code></div>
        <div><span>Channel</span><strong>#{h(input.dig("output", "type") || "—")}</strong></div>
        <div><span>CRM status</span><strong>#{h(lead["status"] || "—")}</strong></div>
        <div><span>Sales</span><strong>#{h(input.dig("sales", "name") || "—")}</strong></div>
      </div>

      <section>
        <h3>Customer-facing result</h3>
        #{output_html}
      </section>

      <details>
        <summary>CRM input snapshot</summary>
        <dl>
          <dt>Contact</dt><dd>#{h(contact["name"])} · #{h(contact["job_title"])} · #{h(contact["email"] || contact["linkedin_url"])}</dd>
          <dt>Company website</dt><dd>#{text_block(company["website"])}</dd>
          <dt>CRM 内文本</dt><dd class="prewrap">#{text_block(input.dig("review_context", "crm_internal_note") || lead["internal_note"])}</dd>
          <dt>Company research</dt><dd class="prewrap">#{text_block(company["research_text"])}</dd>
        </dl>
      </details>

      <details>
        <summary>Generation metadata and raw output</summary>
        <dl>
          <dt>Model</dt><dd>#{h(record[:usage]["model"] || "—")}</dd>
          <dt>Provider</dt><dd>#{h(record[:usage]["provider"] || "—")}</dd>
          <dt>Total tokens</dt><dd>#{h(record[:usage]["total_tokens"] || "—")}</dd>
          <dt>Estimated cost</dt><dd>#{h(record[:usage]["estimated_cost_usd"] || "—")}</dd>
        </dl>
        <pre>#{h(record[:raw])}</pre>
      </details>
    </article>
  HTML
end.join("\n")

html = <<~HTML
  <!doctype html>
  <html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Twenty → Hermes Message Report</title>
    <style>
      :root { color-scheme: light; --ink:#18212b; --muted:#647181; --line:#dfe5ea; --bg:#f3f6f8; --card:#fff; --accent:#145c52; --ok:#16725f; --hold:#986b12; --bad:#a13b3b; }
      * { box-sizing:border-box; }
      body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
      main { max-width:1120px; margin:0 auto; padding:40px 24px 80px; }
      .hero { background:linear-gradient(135deg,#113f3a,#176b60); color:white; border-radius:18px; padding:28px 32px; box-shadow:0 14px 34px rgba(17,63,58,.16); }
      .hero h1 { margin:0 0 6px; font-size:26px; }
      .hero p { margin:3px 0; opacity:.86; }
      .stats { display:grid; grid-template-columns:repeat(5,minmax(110px,1fr)); gap:12px; margin:18px 0 28px; }
      .stat { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
      .stat strong { display:block; font-size:23px; }
      .stat span,.muted { color:var(--muted); }
      .notice { margin:18px 0; padding:12px 15px; border-left:4px solid var(--hold); background:#fff8e8; border-radius:8px; }
      .card { background:var(--card); border:1px solid var(--line); border-radius:16px; margin:18px 0; padding:22px; box-shadow:0 5px 18px rgba(27,43,56,.05); }
      .card-header { display:flex; justify-content:space-between; gap:20px; align-items:flex-start; border-bottom:1px solid var(--line); padding-bottom:14px; }
      .card h2 { margin:0; font-size:20px; }.card h3 { font-size:14px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin-top:22px; }
      .card-header p { margin:4px 0 0; color:var(--muted); }
      .badges { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:6px; }
      .badge,.chip { display:inline-block; border-radius:999px; padding:3px 9px; font-size:12px; background:#edf1f4; margin:2px 4px 2px 0; }
      .badge.ok { color:var(--ok); background:#e6f5f0; }.badge.hold { color:var(--hold); background:#fff3d9; }.badge.bad { color:var(--bad); background:#fdeaea; }.badge.edited { color:#3c52a0; background:#e9edff; }
      .meta-grid { display:grid; grid-template-columns:2fr 1fr 1fr 1fr; gap:12px; margin:16px 0; }
      .meta-grid div { background:#f7f9fa; border-radius:9px; padding:10px; min-width:0; }.meta-grid span { display:block; color:var(--muted); font-size:12px; }.meta-grid code { display:block; overflow:hidden; text-overflow:ellipsis; }
      .message { border-left:4px solid var(--accent); background:#f4faf8; border-radius:8px; padding:15px 17px; }
      .subject { margin-bottom:10px; } pre,.prewrap { white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word; }
      pre { margin:0; font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }
      dl { display:grid; grid-template-columns:130px 1fr; gap:8px 14px; } dt { color:var(--muted); } dd { margin:0; min-width:0; }
      details { margin-top:14px; border-top:1px solid var(--line); padding-top:12px; } summary { cursor:pointer; font-weight:600; }
      details pre { margin-top:12px; max-height:360px; overflow:auto; background:#151c23; color:#dce6ee; padding:14px; border-radius:8px; }
      .error-box { border-left:4px solid var(--bad); background:#fff4f4; padding:14px; border-radius:8px; }.error-box pre { margin-top:8px; }
      footer { color:var(--muted); text-align:center; margin-top:30px; }
      @media (max-width:760px) { .stats { grid-template-columns:repeat(2,1fr); }.meta-grid { grid-template-columns:1fr 1fr; }.card-header { flex-direction:column; }.badges { justify-content:flex-start; } dl { grid-template-columns:1fr; gap:2px; } }
    </style>
  </head>
  <body><main>
    <section class="hero">
      <h1>Twenty → Hermes Message Report</h1>
      <p>Run: #{h(summary["run_id"] || File.basename(run_dir))}</p>
      <p>Generated locally at #{h(Time.now.iso8601)}</p>
    </section>
    <div class="notice"><strong>Review-only:</strong> this report contains customer and CRM information. No message shown here has been sent or written back to Twenty.</div>
    <section class="stats">
      <div class="stat"><strong>#{records.length}</strong><span>Total</span></div>
      <div class="stat"><strong>#{records.count { |r| r[:pipeline_status] == "valid" }}</strong><span>Validated</span></div>
      <div class="stat"><strong>#{decision_counts.fetch("generated", 0)}</strong><span>Generated</span></div>
      <div class="stat"><strong>#{decision_counts.fetch("no_message", 0)}</strong><span>No message</span></div>
      <div class="stat"><strong>#{records.count { |r| r[:pipeline_status] != "valid" }}</strong><span>Invalid</span></div>
    </section>
    #{cards}
    <footer>Local test artifact · Aceler International · Human review required</footer>
  </main></body></html>
HTML

FileUtils.mkdir_p(File.dirname(output_path))
File.write(output_path, html)
puts "Report written to #{output_path}"
