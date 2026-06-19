/* HermesUltraCode dashboard — minimal vanilla JS over the read API.
 * neckbeard: no framework (rung 1 — these read-only views don't need React yet).
 * Upgrade path: the React 19 + Vite + Tailwind SPA noted in web/README.md.
 * The session token is held only in memory and sent in the X-Gate-Session-Token
 * header; it is never written to storage or the URL. */
(function () {
  "use strict";
  let TOKEN = "";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(path) {
    const res = await fetch(path, { headers: { "X-Gate-Session-Token": TOKEN } });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  function setConnected(ok, label) {
    const el = $("#conn-status");
    el.textContent = label || (ok ? "connected" : "disconnected");
    el.className = "pill " + (ok ? "pill-ok" : "pill-warn");
  }

  function tierBadge(tier) {
    return `<span class="badge badge-${esc(tier)}">${esc(tier)}</span>`;
  }
  function decisionBadge(d) {
    const cls = d && d.indexOf("dispatched") === 0 ? "ok" : (d === "escalated" ? "esc" : "block");
    return `<span class="badge badge-${cls}">${esc(d)}</span>`;
  }
  function statusBadge(s) {
    s = String(s || "").toLowerCase();
    let cls = "badge-trivial";
    if (/run/.test(s)) cls = "badge-standard";
    else if (/(complete|^ok$|success|done|dispatch)/.test(s)) cls = "badge-ok";
    else if (/(err|fail|block|interrupt|timeout|denied)/.test(s)) cls = "badge-block";
    return `<span class="badge ${cls}">${esc(s || "—")}</span>`;
  }
  function fmtTime(ts) {
    if (!ts) return "";
    try { return new Date(ts * 1000).toLocaleTimeString(); } catch (e) { return ""; }
  }

  // ---- views ----
  async function loadLive() {
    const d = await api("/api/live");
    $("#live-active tbody").innerHTML = (d.active || []).map((s) =>
      `<tr><td>${esc(s.goal || "(no goal)")}</td>
       <td>${esc(s.role || "")}${s.depth ? " ·d" + esc(s.depth) : ""}</td>
       <td>${statusBadge(s.status)}</td><td><code>${esc(s.last_tool || "—")}</code></td>
       <td>${esc(s.tool_count == null ? 0 : s.tool_count)}</td>
       <td>${s.elapsed_s != null ? esc(s.elapsed_s) + "s" : "—"}</td></tr>`
    ).join("") || `<tr><td colspan="6" class="muted">no active subagents — start one with <code>/ultracode &lt;task&gt;</code></td></tr>`;

    $("#live-feed tbody").innerHTML = (d.feed || []).map((e) =>
      `<tr><td>${esc(fmtTime(e.ts))}</td>
       <td title="${esc(e.goal || "")}"><code>${esc((e.subagent_id || "").slice(0, 8))}</code></td>
       <td><code>${esc(e.tool)}</code></td><td>${statusBadge(e.status)}</td>
       <td>${esc(e.duration_ms || 0)}</td></tr>`
    ).join("") || `<tr><td colspan="5" class="muted">no tool activity yet</td></tr>`;

    $("#live-completed").innerHTML = (d.completed || []).map((c) =>
      `<div class="card" style="min-width:280px;max-width:520px">
         <div class="card-h">${statusBadge(c.status)} · ${esc(c.tool_count || 0)} tools · ${esc(Math.round((c.duration_ms || 0) / 1000))}s</div>
         <div class="card-b"><b>${esc(c.goal || "(no goal)")}</b>
         <div class="muted" style="margin-top:6px;white-space:pre-wrap">${esc((c.summary || "(no summary)").slice(0, 600))}</div></div>
       </div>`
    ).join("") || `<em class="muted">none yet</em>`;
  }

  async function loadQueue() {
    const d = await api("/api/queue");
    $("#queue-table tbody").innerHTML = (d.queue || []).map((r) =>
      `<tr><td>${esc(r.ts)}</td><td>${tierBadge(r.tier)}</td><td>${esc(r.verdict)}</td>
       <td>${decisionBadge(r.decision)}</td><td>${esc(r.round_count)}</td><td>${esc(r.n_directives)}</td>
       <td><a href="#" data-id="${esc(r.id)}" class="link-panel">open</a></td></tr>`
    ).join("") || `<tr><td colspan="7" class="muted">empty</td></tr>`;
    wirePanelLinks();
  }

  function auditQuery() {
    const f = $("#audit-filters");
    const p = new URLSearchParams();
    ["tier", "verdict", "from", "to"].forEach((k) => { if (f[k].value) p.set(k, f[k].value); });
    return p.toString();
  }

  async function loadAudit() {
    const q = auditQuery();
    const d = await api("/api/audit" + (q ? "?" + q : ""));
    $("#export-json").href = "/api/audit.json" + (q ? "?" + q : "");
    $("#export-csv").href = "/api/audit.csv" + (q ? "?" + q : "");
    $("#audit-table tbody").innerHTML = (d.rows || []).map((r) =>
      `<tr><td>${esc(r.ts)}</td><td>${tierBadge(r.tier)}</td><td>${esc(r.verdict)}</td>
       <td>${decisionBadge(r.decision)}</td><td>${esc(r.round_count)}</td>
       <td>${r.fail_closed ? '<span class="badge badge-block">yes</span>' : "no"}</td>
       <td>${esc(r.latency_ms)}ms</td><td>${esc(r.added_tokens)}</td>
       <td><a href="#" data-id="${esc(r.id)}" class="link-panel">open</a></td></tr>`
    ).join("") || `<tr><td colspan="9" class="muted">no rows</td></tr>`;
    wirePanelLinks();
  }

  async function loadNeckbeard() {
    const d = await api("/api/neckbeard");
    $("#debt-table tbody").innerHTML = (d.debt_ledger || []).map((it) =>
      `<tr><td><code>${esc(it.file)}</code></td><td>${esc(it.line)}</td><td>${esc(it.note)}</td></tr>`
    ).join("") || `<tr><td colspan="3" class="muted">no debt markers</td></tr>`;
    const pv = d.protected_set_violations || [];
    $("#protected-violations").innerHTML = pv.length
      ? pv.map((r) => `<div class="card"><div class="card-h">${decisionBadge(r.decision)} ${tierBadge(r.tier)}</div>
          <div class="card-b">${esc(r.rationale)}</div></div>`).join("")
      : '<em class="muted">none — the gate has blocked no protected-set violations</em>';
  }

  async function loadMetrics() {
    const m = await api("/api/metrics");
    const card = (label, val) => `<div class="card"><div class="card-h">${esc(label)}</div><div class="card-b big">${esc(val)}</div></div>`;
    $("#metric-cards").innerHTML = [
      card("Total dispatches", m.total_dispatches),
      card("Released", m.released),
      card("Blocked", m.blocked),
      card("Escalated", m.escalated),
      card("Fail-closed", m.fail_closed_count),
      card("Latency p50", m.gate_latency_ms_p50 + "ms"),
      card("Latency p95", m.gate_latency_ms_p95 + "ms"),
      card("Avg +tokens", m.avg_added_tokens_per_dispatch),
    ].join("");
    const b = m.benchmark;
    $("#bench-block").innerHTML = b
      ? kv({
          "first-pass success (gate ON)": pct(b.gate_on && b.gate_on.first_pass_success_rate),
          "first-pass success (gate OFF)": pct(b.gate_off && b.gate_off.first_pass_success_rate),
          "guideline-violation (gate ON)": pct(b.gate_on && b.gate_on.guideline_violation_rate),
          "guideline-violation (gate OFF)": pct(b.gate_off && b.gate_off.guideline_violation_rate),
          "added latency / dispatch": (b.added_latency_ms_per_dispatch ?? "—") + "ms",
          "added cost / dispatch": "$" + (b.added_cost_usd_per_dispatch ?? "—"),
        })
      : "<em>No benchmark results loaded. Run bench/harness.py and pass results to the read API.</em>";
  }

  function pct(x) { return x == null ? "—" : (Math.round(x * 1000) / 10) + "%"; }
  function kv(obj) {
    return Object.entries(obj).map(([k, v]) =>
      `<div class="kv-row"><span class="kv-k">${esc(k)}</span><span class="kv-v">${esc(v)}</span></div>`).join("");
  }

  async function openPanel(id) {
    try {
      const r = await api("/api/dispatch/" + encodeURIComponent(id));
      $("#gate-panel-body").innerHTML = `
        <h3>Gate panel ${decisionBadge(r.decision)} ${tierBadge(r.tier)}</h3>
        ${kv({ id: r.id, ts: r.ts, verdict: r.verdict, rounds: r.round_count,
               escalated: r.escalated, fail_closed: r.fail_closed, "reviewer model": r.reviewer_model,
               scope: r.scope_assessment, latency: r.latency_ms + "ms", "+tokens": r.added_tokens })}
        <h4>Rationale</h4><p>${esc(r.rationale)}</p>
        ${r.fail_closed_reason ? `<h4>Fail-closed reason</h4><p class="warn">${esc(r.fail_closed_reason)}</p>` : ""}
        <h4>Appended directives (the tighten)</h4>
        <ul>${(r.added_directives || []).map((d) => `<li>${esc(d)}</li>`).join("") || "<li class='muted'>none (no-op)</li>"}</ul>
        <h4>Base prompt (immutable)</h4><pre>${esc(r.base_prompt)}</pre>
        <h4>Dispatched prompt</h4><pre>${esc(r.dispatched_prompt || "(not dispatched)")}</pre>`;
      $("#gate-panel").showModal();
    } catch (e) { alert("Failed to load dispatch: " + e.message); }
  }

  function wirePanelLinks() {
    $$(".link-panel").forEach((a) => a.onclick = (ev) => { ev.preventDefault(); openPanel(a.dataset.id); });
  }

  const LOADERS = { live: loadLive, queue: loadQueue, audit: loadAudit, neckbeard: loadNeckbeard, metrics: loadMetrics };
  let current = "live";

  async function refresh() {
    if (!TOKEN) return;
    try {
      await LOADERS[current]();
      const fc = await api("/api/failclosed");
      const banner = $("#failclosed-banner");
      $("#failclosed-count").textContent = fc.fail_closed_count;
      banner.hidden = !(fc.fail_closed_count > 0);
      setConnected(true);
    } catch (e) {
      setConnected(false, "error: " + e.message);
    }
  }

  function showView(name) {
    current = name;
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
    $$(".view").forEach((v) => (v.hidden = v.id !== "view-" + name));
    refresh();
  }

  // ---- wiring ----
  document.addEventListener("DOMContentLoaded", () => {
    $("#save-token").onclick = () => { TOKEN = $("#token").value.trim(); refresh(); };
    $("#token").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#save-token").click(); });
    // One-click open: ?token=... in the URL (printed on load / by /ultracode) auto-connects.
    try {
      const urlToken = new URLSearchParams(location.search).get("token");
      if (urlToken) { $("#token").value = urlToken; TOKEN = urlToken.trim(); refresh(); }
    } catch (e) { /* no-op */ }
    $$(".tab").forEach((t) => t.onclick = () => showView(t.dataset.view));
    $("#audit-filters").onsubmit = (e) => { e.preventDefault(); loadAudit(); };
    $("#gate-panel-close").onclick = () => $("#gate-panel").close();
    setInterval(() => { if (current === "live") refresh(); }, 5000);
  });
})();
