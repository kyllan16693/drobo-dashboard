"use strict";

const CONFIG = window.DROBO_CONFIG || { pollInterval: 15 };
const REFRESH_MS = Math.max(5, Number(CONFIG.pollInterval) || 15) * 1000;
const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function relTime(sec) {
  if (!sec) return "–";
  const s = Math.max(0, Math.round(Date.now() / 1000 - sec));
  if (s < 60) return s + "s ago";
  const m = Math.round(s / 60);
  if (m < 60) return m + "m ago";
  const h = Math.round(m / 60);
  if (h < 48) return h + "h ago";
  return Math.round(h / 24) + "d ago";
}

function absTime(sec) {
  if (!sec) return "";
  return new Date(sec * 1000).toLocaleString();
}

function renderCurrent(current) {
  const tbody = $("current-table").querySelector("tbody");
  if (!current || !current.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted" style="padding:16px">No drive data (Drobo unreachable).</td></tr>`;
    $("cur-summary").textContent = "";
    return;
  }
  let withErr = 0;
  tbody.innerHTML = current.map((d) => {
    const n = Number(d.error_count) || 0;
    if (n > 0 && !d.is_accelerator) withErr++;
    const cls = n > 0 ? "warn" : "zero";
    const accel = d.is_accelerator ? '<span class="accel-badge">mSATA CACHE</span>' : "";
    const sev = ["ok", "warning", "critical", "info", "empty", "unknown"].includes(d.status_severity) ? d.status_severity : "unknown";
    return `<tr${d.is_accelerator ? ' class="accel-row"' : ""}>
      <td>Slot ${esc(d.slot)}</td>
      <td>${esc(d.make || "Drive")}${accel}</td>
      <td class="mono">${esc(d.serial || "–")}</td>
      <td class="muted">${esc(d.rotational_label || "–")}</td>
      <td><span class="pill sev-${sev}" style="font-size:12px;padding:3px 9px"><span class="dot"></span><span class="pill-label">${esc(d.status_label || "–")}</span></span></td>
      <td class="num"><span class="err-count ${cls}">${n}</span></td>
    </tr>`;
  }).join("");
  $("cur-summary").textContent = withErr > 0
    ? `${withErr} drive${withErr > 1 ? "s" : ""} with a non-zero count`
    : "all drives at zero";
}

function changeCell(ev) {
  if (ev.kind === "baseline") {
    return `<span class="change-base">baseline · ${esc(ev.new_count)} pre-existing</span>`;
  }
  if (ev.kind === "reset") {
    return `<span class="change-reset">reset ${esc(ev.prev_count)} → ${esc(ev.new_count)}</span>`;
  }
  return `<span class="change-up">+${esc(ev.delta)} (${esc(ev.prev_count)} → ${esc(ev.new_count)})</span>`;
}

function renderEvents(events, totals) {
  const tbody = $("events-table").querySelector("tbody");
  if (!events || !events.length) {
    const since = totals && totals.tracking_since ? " since " + absTime(totals.tracking_since) : "";
    tbody.innerHTML = `<tr><td colspan="6" class="muted" style="padding:16px">No error changes recorded${since}. New increases will appear here as they happen.</td></tr>`;
    return;
  }
  tbody.innerHTML = events.map((ev) => `<tr>
    <td title="${esc(absTime(ev.ts))}">${esc(relTime(ev.ts))}</td>
    <td>Slot ${esc(ev.slot)}</td>
    <td>${esc(ev.make || "–")}</td>
    <td>${changeCell(ev)}</td>
    <td class="num">${esc(ev.new_count)}</td>
    <td class="muted">${esc(ev.note || "")}</td>
  </tr>`).join("");
}

function renderExplainer(ref) {
  const e = ref.error_count;
  if (!e) return;
  $("explainer-summary").textContent = e.summary;
  $("explainer-detail").textContent = e.detail;
  $("explainer-guidance").innerHTML = (e.guidance || []).map((g) => `<li>${esc(g)}</li>`).join("");
}

function refRows(rows, cols) {
  return `<table class="dtable"><thead><tr>${cols.map((c) => `<th${c.num ? ' class="num"' : ""}>${esc(c.h)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td${c.num ? ' class="num"' : ""}>${c.render ? c.render(r) : esc(r[c.k])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function renderReference(ref) {
  const body = $("reference-body");
  const parts = [];

  parts.push(`<div class="ref-group"><h3>Rotational speed codes</h3>
    <p class="ref-note">${esc(ref.rotational_speed.rule)}</p>
    ${refRows(ref.rotational_speed.codes, [
      { h: "Code", k: "code", render: (r) => `<span class="code-chip">${esc(r.code)}</span>` },
      { h: "Decoded", k: "label" },
    ])}</div>`);

  parts.push(`<div class="ref-group"><h3>Slot status codes</h3>
    ${refRows(ref.slot_status, [
      { h: "Code", render: (r) => `<span class="code-chip">${esc(r.code)}</span> <span class="muted">${esc(r.hex)}</span>` },
      { h: "Meaning", k: "label" },
      { h: "Severity", render: (r) => `<span class="pill sev-${esc(r.severity)}" style="font-size:11px;padding:2px 8px"><span class="dot"></span><span class="pill-label">${esc(r.severity)}</span></span>` },
    ])}</div>`);

  parts.push(`<div class="ref-group"><h3>Disk-state &amp; type codes</h3>
    ${refRows(ref.disk_state, [
      { h: "State", render: (r) => `<span class="code-chip">${esc(r.code)}</span>` },
      { h: "Meaning", k: "label" },
    ])}</div>`);

  parts.push(`<details class="ref"><summary>S.M.A.R.T. attribute reference (${ref.smart.attributes.length})</summary>
    <p class="ref-note">${esc(ref.smart.note)}</p>
    ${refRows(ref.smart.attributes, [
      { h: "ID", num: true, k: "id" },
      { h: "Attribute", k: "name" },
      { h: "What it means", k: "meaning" },
      { h: "Rising is bad?", render: (r) => r.rising_is_bad ? '<span class="tag up">yes</span>' : '<span class="tag">info</span>' },
    ])}</details>`);

  body.innerHTML = parts.join("");
}

async function tick() {
  try {
    const [errRes, refRes] = await Promise.all([
      fetch("/api/history/errors", { cache: "no-store" }),
      fetch("/api/reference", { cache: "no-store" }),
    ]);
    const data = await errRes.json();
    const ref = await refRes.json();
    $("banner").className = "banner hidden";
    renderCurrent(data.current);
    renderEvents(data.events, data.totals);
    renderExplainer(ref);
    renderReference(ref);
    const t = data.totals || {};
    $("freshness").textContent = "updated " + relTime(Date.now() / 1000) +
      (t.tracking_since ? " · tracking since " + absTime(t.tracking_since) : "") +
      (t.errors_added_since_tracking ? " · " + t.errors_added_since_tracking + " new since then" : "");
  } catch (err) {
    const b = $("banner");
    b.className = "banner critical";
    b.textContent = "Could not load error data: " + err;
  }
}

tick();
setInterval(tick, REFRESH_MS);
