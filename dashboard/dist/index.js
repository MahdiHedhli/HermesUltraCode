/* HermesUltraCode dashboard plugin — a native tab in the Hermes web dashboard.
 * Hand-authored plain JS (no npm/Vite build): it uses the host's React via the plugin SDK
 * (window.__HERMES_PLUGIN_SDK__) and fetches the gate's read-only data from this plugin's
 * backend at /api/plugins/hermesultracode/. Mirrors the standalone dashboard's Live + Plan
 * + Audit views. */
(function () {
  "use strict";
  var SDK = window.__HERMES_PLUGIN_SDK__;
  var Plugins = window.__HERMES_PLUGINS__;
  if (!SDK || !Plugins || !SDK.React) return;

  var React = SDK.React;
  var e = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  // Require the SDK's authenticated fetch — never fall back to an unauthenticated request.
  var fetchJSON = (typeof SDK.fetchJSON === "function") ? SDK.fetchJSON : null;
  var API = "/api/plugins/hermesultracode";

  function cls(s) { return { className: s }; }
  function badge(text, kind) { return e("span", cls("uc-badge uc-" + (kind || "muted")), text); }
  function statusKind(s) {
    s = String(s || "").toLowerCase();
    if (/run/.test(s)) return "run";
    if (/(complete|^ok$|done|success|dispatch)/.test(s)) return "ok";
    if (/(err|fail|block|timeout|denied|interrupt)/.test(s)) return "bad";
    return "muted";
  }

  function logChips(log) {
    if (!log || !log.length) return null;
    return e("div", cls("uc-log"), log.slice(0, 8).map(function (x, i) {
      var bad = /err|fail|block|timeout/.test(String(x.status).toLowerCase());
      return e("span", { key: i, className: "uc-chip" + (bad ? " uc-bad" : "") }, e("code", null, x.tool));
    }));
  }

  function gateBox(g) {
    if (!g) return null;
    var dirs = (g && Array.isArray(g.added_directives)) ? g.added_directives : [];
    return e("div", cls("uc-gate"),
      e("div", cls("uc-gate-h"), "reviewer ", badge(g.tier, "tier"), " " + (g.verdict || "") + " → " + (g.decision || "")),
      dirs.length
        ? e("ul", cls("uc-dirs"), dirs.map(function (d, i) { return e("li", { key: i }, "＋ " + d); }))
        : e("div", cls("uc-muted"), "no tightening — passed clean"));
  }

  function agentCard(a, i) {
    return e("div", { key: i, className: "uc-card" },
      e("div", cls("uc-card-h"),
        e("span", cls("uc-id"), e("code", null, (a.subagent_id || "").slice(0, 8)), " ",
          e("span", cls("uc-role"), (a.role || "leaf") + (a.depth ? " ·d" + a.depth : ""))),
        badge(a.status, statusKind(a.status))),
      e("div", cls("uc-goal"), a.goal || "(no goal)"),
      e("div", cls("uc-meta"),
        e("span", null, "last ", e("code", null, a.last_tool || "—")),
        e("span", null, (a.tool_count || 0) + " tools"),
        e("span", null, a.elapsed_s != null ? a.elapsed_s + "s" : "—")),
      logChips(a.log),
      gateBox(a.gate));
  }

  function orchCard(o) {
    return e("div", cls("uc-card uc-orch"),
      e("div", cls("uc-card-h"),
        e("span", cls("uc-id"), "🧭 Orchestrator ", e("span", cls("uc-role"), o.backend || "")),
        badge(o.active ? "working" : "idle", o.active ? "run" : "muted")),
      e("div", cls("uc-meta"),
        e("span", null, "last ", e("code", null, o.last_tool || "—")),
        e("span", null, (o.tool_count || 0) + " tools"),
        e("span", null, o.elapsed_s != null ? o.elapsed_s + "s" : "—")),
      logChips(o.log));
  }

  function liveView(d) {
    var agents = d.active || [];
    return e("div", null,
      orchCard(d.orchestrator || {}),
      e("h3", cls("uc-h3"), "Agents", e("span", cls("uc-muted"), " · active subagents + the reviewer's tightening")),
      agents.length
        ? e("div", cls("uc-grid"), agents.map(agentCard))
        : e("div", cls("uc-muted"), "No active subagents — the orchestrator is working solo or idle. A multi-component build fans out here, each agent gate-reviewed."),
      e("h3", cls("uc-h3"), "Activity feed", e("span", cls("uc-muted"), " · newest first")),
      e("div", cls("uc-feed"), (d.feed || []).slice(0, 18).map(function (f, i) {
        var orch = f.subagent_id === "orchestrator";
        return e("div", { key: i, className: "uc-feed-row" },
          e("span", cls(orch ? "uc-who-orch" : "uc-who"), orch ? "🧭 orchestrator" : (f.subagent_id || "").slice(0, 8)),
          e("code", cls("uc-tool"), f.tool),
          badge(f.status, statusKind(f.status)),
          e("span", cls("uc-ms"), (f.duration_ms || 0) + "ms"));
      })));
  }

  function planView(d) {
    var plan = d.plan || { items: [], progress: {} };
    var p = plan.progress || { total: 0, done: 0, active: 0, todo: 0, pct: 0 };
    if (!p.total) return e("div", cls("uc-muted"), "No plan yet. The orchestrator's todo stages appear here the moment it plans the build.");
    return e("div", null,
      e("div", cls("uc-bar"), e("div", { className: "uc-bar-fill", style: { width: p.pct + "%" } })),
      e("div", cls("uc-counts"), e("b", null, p.pct + "% complete"), " ",
        badge(p.done + " done", "ok"), " ", badge(p.active + " active", "run"), " ", badge(p.todo + " to do", "muted")),
      e("ol", cls("uc-stages"), (plan.items || []).map(function (it, i) {
        return e("li", { key: i, className: "uc-stage uc-stage-" + (it.state || "todo") },
          e("span", cls("uc-dot")),
          e("span", cls("uc-stage-body"),
            e("span", cls("uc-stage-text"), it.content || ""),
            e("span", cls("uc-stage-state"), it.state || "todo")));
      })));
  }

  function auditView(rows) {
    var head = ["Time", "Tier", "Verdict", "Decision", "Rounds", "Fail-closed"];
    return e("table", cls("uc-table"),
      e("thead", null, e("tr", null, head.map(function (t, i) { return e("th", { key: i }, t); }))),
      e("tbody", null, (Array.isArray(rows) && rows.length)
        ? rows.map(function (r, i) {
            return e("tr", { key: i },
              e("td", null, r.ts), e("td", null, badge(r.tier, "tier")), e("td", null, r.verdict),
              e("td", null, badge(r.decision, /dispatch/.test(r.decision) ? "ok" : "bad")),
              e("td", null, r.round_count), e("td", null, r.fail_closed ? badge("yes", "bad") : "no"));
          })
        : e("tr", null, e("td", { colSpan: 6, className: "uc-muted" }, "no dispatches yet — configure a reviewer and run a delegate_task"))));
  }

  function hashView() {
    var h = (typeof location !== "undefined" ? location.hash || "" : "").replace(/^#/, "");
    return (h === "plan" || h === "audit") ? h : "live";
  }

  function UltraCode() {
    var t = useState(hashView()), view = t[0], setView = t[1];
    var dl = useState(null), live = dl[0], setLive = dl[1];
    var al = useState(null), aud = al[0], setAud = al[1];
    var er = useState(null), err = er[0], setErr = er[1];
    useEffect(function () {
      if (!fetchJSON) { setErr("Dashboard SDK unavailable — no authenticated fetch."); return; }
      var alive = true;
      function load() {
        fetchJSON(API + "/live")
          .then(function (r) { if (alive) { setLive(r); setErr(null); } })
          .catch(function () { if (alive) setErr("Can't reach the UltraCode backend (/api/plugins/hermesultracode)."); });
        if (view === "audit") {
          fetchJSON(API + "/audit?limit=60").then(function (r) { if (alive) setAud((r && r.rows) || []); }).catch(function () {});
        }
      }
      load();
      var id = setInterval(load, 4000);
      return function () { alive = false; clearInterval(id); };
    }, [view]);
    function go(v) { setView(v); try { location.hash = v; } catch (e) { /* no-op */ } }
    var d = live || {};
    var tabs = [["live", "Live"], ["plan", "Plan"], ["audit", "Audit"]];
    return e("div", cls("uc-root"),
      e("div", cls("uc-head"),
        e("div", cls("uc-title"), "HermesUltraCode ", e("span", cls("uc-muted"), "· pre-dispatch gate")),
        e("div", cls("uc-tabs"), tabs.map(function (x) {
          return e("button", { key: x[0], className: "uc-tab" + (view === x[0] ? " uc-active" : ""), onClick: function () { go(x[0]); } }, x[1]);
        }))),
      err ? e("div", cls("uc-err"), err) : null,
      view === "live" ? liveView(d) : view === "plan" ? planView(d) : auditView(aud));
  }

  Plugins.register("hermesultracode", UltraCode);
})();
