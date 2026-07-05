"use strict";

const CONFIG = window.DROBO_CONFIG || { pollInterval: 15 };
const FAST_MS = 3000;                 // live cards / per-core / top
const SLOW_MS = 15000;                // history charts
const $ = (id) => document.getElementById(id);

const COL = { ok: "#57c23a", warn: "#d8a020", crit: "#ef4b45", blue: "#2ea6ff", cache: "#d98a3a", free: "#4b5a68" };
let histHours = 1;

function usageColor(pct) {
  return pct >= 90 ? COL.crit : pct >= 70 ? COL.warn : COL.ok;
}
function setPill(el, sev, label) {
  const ok = ["ok", "warning", "critical", "info", "unknown"].includes(sev);
  el.className = "pill sev-" + (ok ? sev : "unknown");
  const l = el.querySelector(".pill-label");
  if (l) l.textContent = label;
}
function fmtDuration(sec) {
  sec = Math.floor(Number(sec) || 0);
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d > 0) return d + "d " + h + "h";
  if (h > 0) return h + "h " + m + "m";
  return m + "m";
}
const STATE_LABEL = { ok: "live over SSH", auth_failed: "SSH auth failed", unreachable: "device unreachable", idle: "connecting…", disabled: "disabled" };

function renderLive(d) {
  const st = d.state;
  setPill($("hw-pill"), st === "ok" ? "ok" : st === "auth_failed" || st === "unreachable" ? "critical" : "unknown",
    STATE_LABEL[st] || st);

  if (st !== "ok" && !d.latest) {
    const b = $("banner");
    if (!d.enabled) {
      b.className = "banner warning";
      b.textContent = "Hardware monitor is disabled — no SSH credentials (DROBO_USERNAME / DROBO_PASSWORD) in .env.";
    } else if (st === "auth_failed") {
      b.className = "banner critical";
      b.textContent = "SSH authentication failed — fix DROBO_PASSWORD in drobo-dashboard/.env.";
    } else if (st === "unreachable") {
      b.className = "banner critical";
      b.textContent = "Can't reach the Drobo over SSH right now" + (d.last_error ? " (" + d.last_error + ")" : "") + ".";
    } else {
      b.className = "banner info";
      b.textContent = "Connecting to the Drobo and collecting the first samples…";
    }
    return;
  }
  $("banner").className = "banner hidden";

  const info = d.info || {};
  const s = d.latest;
  if (s) {
    // CPU
    if (s.cpu_pct != null) {
      $("cpu-val").textContent = s.cpu_pct.toFixed(0);
      $("cpu-meter").style.width = Math.min(100, s.cpu_pct) + "%";
      $("cpu-meter").style.background = usageColor(s.cpu_pct);
    }
    $("cpu-iowait").textContent = s.iowait_pct != null ? s.iowait_pct.toFixed(0) + "%" : "–";

    // Memory
    if (s.mem_used_pct != null) {
      $("mem-val").textContent = s.mem_used_pct.toFixed(0);
      $("mem-meter").style.width = Math.min(100, s.mem_used_pct) + "%";
      $("mem-meter").style.background = usageColor(s.mem_used_pct);
    }
    $("mem-detail").textContent = Charts.fmtBytes(s.mem_used) + " of " + Charts.fmtBytes(s.mem_total);

    // Load (color by per-core ratio)
    if (s.load1 != null) $("load-val").textContent = s.load1.toFixed(2);
    if (s.load5 != null) $("load5").textContent = s.load5.toFixed(2);
    if (s.load15 != null) $("load15").textContent = s.load15.toFixed(2);
    const cores = info.cores || 0;
    const note = $("load-note");
    if (cores && s.load1 != null) {
      const per = s.load1 / cores;
      const sev = per < 0.7 ? "ok" : per < 1.5 ? "warning" : "critical";
      const word = per < 0.7 ? "comfortable" : per < 1.5 ? "busy" : "heavily loaded";
      note.className = "load-note " + sev;
      note.textContent = `${per.toFixed(1)}× per core — ${word}`;
    } else {
      note.className = "load-note";
      note.textContent = "";
    }

    // Uptime / processes
    if (s.uptime_sec != null) $("uptime-val").textContent = fmtDuration(s.uptime_sec);
    $("procs-val").textContent = s.procs_total != null ? s.procs_total : "–";

    // Per-core bars
    if (Array.isArray(s.per_core) && s.per_core.length) {
      $("cores").innerHTML = s.per_core.map((v, i) =>
        `<div class="core-row"><span class="core-label">core ${i}</span>` +
        `<span class="core-track"><span class="core-fill" style="width:${Math.min(100, v)}%;background:${usageColor(v)}"></span></span>` +
        `<span class="core-val">${v.toFixed(0)}%</span></div>`).join("");
    }

    // Memory breakdown bar + legend
    const memSegs = [
      { value: s.mem_used, color: usageColor(s.mem_used_pct || 0), label: "Used " + Charts.fmtBytes(s.mem_used) },
      { value: s.mem_cache, color: COL.cache, label: "Cache/buffers " + Charts.fmtBytes(s.mem_cache) },
      { value: s.mem_free, color: COL.free, label: "Free " + Charts.fmtBytes(s.mem_free) },
    ];
    Charts.stackedBar($("mem-bar"), memSegs);
    const mt = s.mem_total || 1;
    const legRow = (c, lbl, val, pct) =>
      `<div class="legend-row"><span class="swatch" style="background:${c}"></span>` +
      `<span class="lg-label">${lbl}</span><span class="lg-val">${Charts.fmtBytes(val)}</span>` +
      `<span class="lg-pct">${(100 * val / mt).toFixed(0)}%</span></div>`;
    $("mem-legend").innerHTML =
      legRow(usageColor(s.mem_used_pct || 0), "Used by processes", s.mem_used) +
      legRow(COL.cache, "Cache / buffers", s.mem_cache) +
      legRow(COL.free, "Free", s.mem_free) +
      (s.swap_total ? legRow(COL.blue, "Swap used", s.swap_used) : "");

    // Disk now
    if (s.disk_r_bps != null) {
      $("disk-now").textContent = "↓ " + Charts.fmtBps(s.disk_r_bps) + " read · ↑ " + Charts.fmtBps(s.disk_w_bps) + " write";
    }
  }

  // System info
  $("sys-model").textContent = info.cpu_model || "–";
  $("sys-cores").textContent = info.cores ? info.cores + " cores" : "–";
  $("sys-kernel").textContent = info.kernel || "–";
  $("sys-mem").textContent = info.mem_total_bytes ? Charts.fmtBytes(info.mem_total_bytes) : (s ? Charts.fmtBytes(s.mem_total) : "–");
  $("sys-swap").textContent = s && s.swap_total ? Charts.fmtBytes(s.swap_used) + " / " + Charts.fmtBytes(s.swap_total) : "–";
  $("core-count").textContent = info.cores ? info.cores + " cores" : "";

  // Top processes
  const top = d.top || [];
  $("proc-body").innerHTML = top.length
    ? top.map((p) =>
        `<tr><td class="num">${p.pid}</td><td>${Charts.esc(p.user)}</td>` +
        `<td class="num">${p.cpu_pct.toFixed(1)}</td><td class="num">${p.mem_pct.toFixed(1)}</td>` +
        `<td><span class="cmd">${Charts.esc(p.cmd)}</span></td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">No process data.</td></tr>`;

  $("freshness").textContent = "updated " + new Date().toLocaleTimeString();
}

async function tickLive() {
  try {
    const d = await (await fetch("/api/hardware", { cache: "no-store" })).json();
    renderLive(d);
  } catch (e) {
    setPill($("hw-pill"), "unknown", "error");
  }
}

async function tickHistory() {
  try {
    const h = await (await fetch("/api/history/hardware?hours=" + histHours, { cache: "no-store" })).json();
    const series = h.series || [];
    Charts.area($("cpu-chart"), [
      { values: series.filter((p) => p.cpu_pct != null).map((p) => ({ t: p.ts, v: p.cpu_pct })), color: COL.ok, label: "cpu" },
      { values: series.filter((p) => p.iowait_pct != null).map((p) => ({ t: p.ts, v: p.iowait_pct })), color: COL.warn, label: "iowait", fill: false },
    ], { height: 170, yMax: 100, fmtY: (v) => v.toFixed(0) + "%", emptyText: "Collecting CPU history…" });

    Charts.area($("mem-chart"), [
      { values: series.filter((p) => p.mem_total).map((p) => ({ t: p.ts, v: 100 * p.mem_used / p.mem_total })), color: COL.blue, label: "mem" },
    ], { height: 150, yMax: 100, fmtY: (v) => v.toFixed(0) + "%", emptyText: "Collecting memory history…" });

    Charts.area($("disk-chart"), [
      { values: series.filter((p) => p.disk_r_bps != null).map((p) => ({ t: p.ts, v: p.disk_r_bps })), color: COL.blue, label: "read" },
      { values: series.filter((p) => p.disk_w_bps != null).map((p) => ({ t: p.ts, v: p.disk_w_bps })), color: COL.ok, label: "write" },
    ], { height: 170, fmtY: Charts.fmtBps, emptyText: "Collecting disk I/O history…" });
  } catch (e) {
    /* keep last-rendered charts */
  }
}

document.querySelectorAll(".range-btns .btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".range-btns .btn").forEach((x) => x.classList.remove("active"));
    btn.classList.add("active");
    histHours = Number(btn.dataset.hours) || 1;
    tickHistory();
  });
});

tickLive();
tickHistory();
setInterval(tickLive, FAST_MS);
setInterval(tickHistory, SLOW_MS);
