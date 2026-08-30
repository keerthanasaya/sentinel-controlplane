const $ = (s) => document.querySelector(s);
let filterUC = "";

async function j(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString();
}

async function loadHealth() {
  try {
    const h = await j("/health");
    $("#modepill").textContent = `verifier: ${h.verifier || "deterministic"}`;
  } catch (e) {}
}

async function loadMetrics() {
  const m = await j("/api/metrics");
  const a = m.by_action || {};
  const chain = m.chain || {};
  const tiles = [
    ["decisions", m.total_decisions, ""],
    ["pass", a.pass || 0, ""],
    ["annotated", a.annotate || 0, ""],
    ["repaired", a.repair || 0, ""],
    ["blocked", a.block || 0, ""],
    ["+latency p50 / p95", `${m.added_latency_ms.p50}`, `/ ${m.added_latency_ms.p95} ms`],
    ["est. cost avoided", `$${m.est_cost_avoided_usd}`, ""],
    ["false-positive rate", m.false_positive_rate == null ? "—" : `${(m.false_positive_rate * 100).toFixed(0)}%`, ""],
    ["audit chain", chain.ok ? "intact" : "BROKEN", `${chain.count} recs`],
  ];
  $("#tiles").innerHTML = tiles
    .map(
      ([k, v, s]) =>
        `<div class="tile"><div class="k">${k}</div><div class="v">${v} <small>${s}</small></div></div>`
    )
    .join("");
}

async function loadDecisions() {
  const rows = await j("/api/decisions?limit=60" + (filterUC ? `&use_case=${filterUC}` : ""));
  const ucSel = $("#ucFilter");
  const seen = new Set([...ucSel.options].map((o) => o.value));
  rows.forEach((d) => {
    if (!seen.has(d.use_case)) {
      seen.add(d.use_case);
      const o = document.createElement("option");
      o.value = o.textContent = d.use_case;
      ucSel.appendChild(o);
    }
  });
  $("#rows").innerHTML = rows
    .map((d) => {
      const hot = d.flags.some((f) => ["high", "critical"].includes(f.severity));
      return `<tr onclick="openDrawer(${d.id})">
        <td>${d.id}</td>
        <td>${d.use_case}</td>
        <td><span class="tier">${d.tier}</span></td>
        <td><span class="badge ${d.action}">${d.action}</span></td>
        <td class="flagcount ${hot ? "hot" : ""}">${d.flags.length || "–"}</td>
        <td>${(d.telemetry.sentinel_overhead_ms ?? 0).toFixed(0)} ms</td>
        <td>${fmtTime(d.created_at)}</td>
      </tr>`;
    })
    .join("");
}

async function loadQueue() {
  const q = await j("/api/review-queue?status=open");
  if (!q.length) {
    $("#queue").innerHTML = `<div class="empty">No responses awaiting review.</div>`;
    return;
  }
  $("#queue").innerHTML = q
    .map(
      (it) => `<div class="qitem">
      <div class="reason">${it.reason}</div>
      <div class="meta">#${it.id} · ${it.use_case} · decision ${it.decision_id}</div>
      <button class="uphold" onclick="resolve(${it.id}, 'upheld')">Uphold block (correct catch)</button>
      <button class="override" onclick="resolve(${it.id}, 'overridden')">Override (false positive)</button>
    </div>`
    )
    .join("");
}

async function resolve(id, status) {
  const res = await j(`/api/review-queue/${id}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status, reviewer: "operator", note: "resolved from dashboard" }),
  });
  if (res.threshold_adjustments && res.threshold_adjustments.length) {
    const t = res.threshold_adjustments[0];
    alert(`Feedback loop: ${t.use_case}.${t.threshold} ${t.from} → ${t.to}`);
  }
  refresh();
}

async function openDrawer(id) {
  const d = await j(`/api/decisions/${id}`);
  const lanes = d.lane_results
    .map((l) => {
      const flags = l.flags
        .map(
          (f) =>
            `<div class="flag ${f.severity}"><b>${f.code}</b> — ${f.message}</div>`
        )
        .join("");
      return `<div class="lane"><h4>${l.lane} · risk ${l.score} · ${l.latency_ms} ms${
        l.timed_out ? " · TIMED OUT" : ""
      }</h4>${flags || '<span class="empty">clean</span>'}
      <details><summary>evidence</summary><pre>${JSON.stringify(l.evidence, null, 2)}</pre></details></div>`;
    })
    .join("");
  $("#drawerBody").innerHTML = `
    <h3>Decision ${d.id} · <span class="badge ${d.action}">${d.action}</span></h3>
    <div class="meta">${d.use_case} · tier ${d.tier} · policy ${d.policy_version} · ${fmtTime(d.created_at)}</div>
    ${d.annotations.map((a) => `<div class="ann">${a}</div>`).join("")}
    <div class="k">Original model output</div><div class="txtblock">${esc(d.original_text) || "—"}</div>
    <div class="k">Released to user</div><div class="txtblock">${esc(d.final_text) || "<i>withheld</i>"}</div>
    <div class="k">Lanes</div>${lanes}
    <div class="k">Telemetry</div><pre>${JSON.stringify(d.telemetry, null, 2)}</pre>
    <div class="k">Audit record (hash-chained)</div>
    <div class="hash">prev: ${d.prev_hash}<br/>this: ${d.hash}</div>
    <div class="k">Report a miss</div>
    <button class="override" onclick="reportFN(${d.id})">This should have been flagged (false negative)</button>
  `;
  $("#drawer").classList.add("open");
}
function esc(s) {
  return (s || "").replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
}
function closeDrawer() {
  $("#drawer").classList.remove("open");
}
async function reportFN(id) {
  const r = await j("/api/feedback/false-negative", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision_id: id, note: "flagged by operator" }),
  });
  alert(`Thresholds tightened: ${JSON.stringify(r.threshold_adjustments)}`);
  closeDrawer();
  refresh();
}

$("#ucFilter").addEventListener("change", (e) => {
  filterUC = e.target.value;
  loadDecisions();
});

function refresh() {
  loadMetrics().catch(() => {});
  loadDecisions().catch(() => {});
  loadQueue().catch(() => {});
}
loadHealth();
refresh();
setInterval(refresh, 2500);
