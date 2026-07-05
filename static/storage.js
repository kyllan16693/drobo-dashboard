"use strict";

const CONFIG = window.DROBO_CONFIG || { pollInterval: 15 };
const SLOW_MS = Math.max(5, Number(CONFIG.pollInterval) || 15) * 1000;
const FAST_MS = 3000; // live throughput refresh
const $ = (id) => document.getElementById(id);

const COL = {
  ok: "#57c23a", warn: "#d8a020", crit: "#ef4b45",
  free: "#4b5a68", unalloc: "#2ea6ff", parity: "#d98a3a",
};
function sevColor(sev) {
  return sev === "critical" ? COL.crit : sev === "warning" ? COL.warn : COL.ok;
}

let showUnalloc = true;
let capHours = 168;
let lastBreakdown = null;

function setPill(el, sev, label) {
  const ok = ["ok", "warning", "critical", "info", "empty", "unknown"].includes(sev);
  el.className = "pill sev-" + (ok ? sev : "unknown");
  const l = el.querySelector(".pill-label");
  if (l) l.textContent = label;
}

function legendRow(color, label, valueHuman, pct) {
  return `<div class="legend-row">` +
    `<span class="swatch" style="background:${color}"></span>` +
    `<span class="lg-label">${Charts.esc(label)}</span>` +
    `<span class="lg-val">${Charts.esc(valueHuman)}</span>` +
    `<span class="lg-pct">${pct == null ? "" : pct.toFixed(1) + "%"}</span></div>`;
}

function renderPie(br) {
  const usedColor = sevColor(br.severity);
  const hasUnalloc = showUnalloc && br.unallocated_bytes > 0;
  const segs = [
    { value: br.used_bytes, color: usedColor, label: "Used " + br.used_human },
    { value: br.free_bytes, color: COL.free, label: "Free " + br.free_human },
  ];
  if (hasUnalloc) segs.push({ value: br.unallocated_bytes, color: COL.unalloc, label: "Unallocated " + br.unallocated_human });
  const total = segs.reduce((a, s) => a + s.value, 0) || 1;
  const totalHuman = hasUnalloc ? br.pie_total_human : br.protected_total_human;

  Charts.pie($("pie"), segs, { size: 200, edge: "#0f151b", edgeWidth: 2 });

  const pct = (v) => (v / total) * 100;
  let rows = `<div class="legend-row lg-total"><span class="lg-label">Total</span>` +
    `<span class="lg-val">${Charts.esc(totalHuman)}</span><span class="lg-pct"></span></div>`;
  rows += legendRow(usedColor, "Used", br.used_human, pct(br.used_bytes)) +
    legendRow(COL.free, "Free", br.free_human, pct(br.free_bytes));
  if (hasUnalloc) rows += legendRow(COL.unalloc, "Unallocated", br.unallocated_human, pct(br.unallocated_bytes));
  rows += `<div class="legend-row" style="border-top:1px solid var(--border-soft);padding-top:8px;margin-top:2px">` +
    `<span class="lg-label small">${br.used_pct.toFixed(1)}% full · yellow ${br.yellow_threshold_pct}% · red ${br.red_threshold_pct}%</span></div>`;
  $("pie-legend").innerHTML = rows;
}

function renderUsage(br) {
  const usedColor = sevColor(br.severity);
  const segs = [
    { value: br.parity_reserve_bytes, color: COL.parity, label: "Protection " + br.parity_reserve_human },
    { value: br.used_bytes, color: usedColor, label: "Used " + br.used_human },
    { value: br.free_bytes, color: COL.free, label: "Free " + br.free_human },
    { value: br.unallocated_bytes, color: COL.unalloc, label: "Unallocated " + br.unallocated_human },
  ];
  Charts.stackedBar($("usage-bar"), segs);
  const raw = br.raw_physical_bytes || 1;
  const pct = (v) => (v / raw) * 100;
  $("usage-legend").innerHTML =
    legendRow(COL.parity, "Used for protection (" + br.redundancy_label + ")", br.parity_reserve_human, pct(br.parity_reserve_bytes)) +
    legendRow(usedColor, "Used for data", br.used_human, pct(br.used_bytes)) +
    legendRow(COL.free, "Available for data", br.free_human, pct(br.free_bytes)) +
    legendRow(COL.unalloc, "Unallocated / reserved for expansion", br.unallocated_human, pct(br.unallocated_bytes));
  $("usage-note").textContent =
    `${br.data_bay_count} data bays · ${br.raw_physical_human} raw across the disks. ` +
    `"Used for protection" is capacity actively holding redundancy (${br.redundancy_label.toLowerCase()}). ` +
    `"Unallocated" is space on your larger drives that can't be protected while the smaller drives are much smaller — ` +
    `swap a small drive for a bigger one to unlock it.`;
}

function tpStateText(d) {
  if (!d.enabled) return d.last_error || "disabled";
  return ({ ok: "live over SSH", auth_failed: "SSH auth failed", unreachable: "device unreachable",
    idle: "connecting…", disabled: "disabled" })[d.state] || d.state;
}
function tpEmptyText(d) {
  if (d.state === "auth_failed") return "SSH auth failed — fix DROBO_PASSWORD in drobo-dashboard/.env to enable live throughput.";
  if (d.state === "unreachable") return "Drobo not reachable over SSH right now.";
  if (!d.enabled) return "Throughput disabled — no SSH credentials in .env.";
  return "Collecting throughput samples…";
}

async function tickThroughput() {
  try {
    const d = await (await fetch("/api/throughput", { cache: "no-store" })).json();
    $("tp-state").textContent = tpStateText(d);
    $("tp-iface").textContent = d.iface ? "iface " + d.iface : "";
    const latest = d.latest;
    $("tp-rx").textContent = latest ? Charts.fmtBps(latest.rx_bps) : "–";
    $("tp-tx").textContent = latest ? Charts.fmtBps(latest.tx_bps) : "–";
    const samples = d.samples || [];
    Charts.area($("tp-chart"), [
      { values: samples.map((s) => ({ t: s.ts, v: s.rx_bps })), color: COL.unalloc, label: "rx" },
      { values: samples.map((s) => ({ t: s.ts, v: s.tx_bps })), color: COL.ok, label: "tx" },
    ], { height: 150, fmtY: Charts.fmtBps, emptyText: tpEmptyText(d) });
  } catch (e) {
    $("tp-state").textContent = "error";
  }
}

function shortDay(iso) {
  const d = new Date(iso + "T00:00:00");
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

async function tickStorage() {
  try {
    const [s, h] = await Promise.all([
      fetch("/api/storage", { cache: "no-store" }).then((r) => r.json()),
      fetch("/api/history/capacity?hours=" + capHours + "&days=14", { cache: "no-store" }).then((r) => r.json()),
    ]);
    if (s.available) {
      lastBreakdown = s.breakdown;
      setPill($("cap-pill"), s.breakdown.severity, s.breakdown.used_pct.toFixed(1) + "% full");
      renderPie(s.breakdown);
      renderUsage(s.breakdown);
      if (s.stale) {
        $("banner").className = "banner warning";
        $("banner").textContent = "Showing last known-good capacity — the Drobo hasn't responded recently.";
      } else {
        $("banner").className = "banner hidden";
      }
    } else {
      setPill($("cap-pill"), "unknown", "no data");
      $("banner").className = "banner critical";
      $("banner").textContent = "Drobo unreachable" + (s.last_error ? ": " + s.last_error : "") + ".";
    }
    Charts.area($("cap-chart"), [
      { values: (h.series || []).map((p) => ({ t: p.ts, v: p.used })), color: COL.ok, label: "used" },
    ], { height: 170, fmtY: Charts.fmtBytes, emptyText: "No capacity history yet — samples accumulate as the dashboard runs." });

    Charts.bars($("written-chart"),
      (h.daily_written || []).map((d) => ({ label: shortDay(d.date), value: d.delta_bytes })),
      { fmtV: (v) => (v >= 0 ? "+" : "") + Charts.fmtBytes(v), posColor: COL.ok, negColor: COL.unalloc,
        emptyText: "Need a couple of days of samples to chart daily writes." });

    $("freshness").textContent = "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    const b = $("banner");
    b.className = "banner critical";
    b.textContent = "Could not load storage data: " + e;
  }
}

// Wiring
(() => {
  const saved = localStorage.getItem("drobo.showUnalloc");
  if (saved !== null) { showUnalloc = saved === "1"; $("toggle-unalloc").checked = showUnalloc; }
})();
$("toggle-unalloc").addEventListener("change", (e) => {
  showUnalloc = e.target.checked;
  localStorage.setItem("drobo.showUnalloc", showUnalloc ? "1" : "0");
  if (lastBreakdown) renderPie(lastBreakdown);
});
document.querySelectorAll(".range-btns .btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".range-btns .btn").forEach((x) => x.classList.remove("active"));
    btn.classList.add("active");
    capHours = Number(btn.dataset.hours) || 168;
    tickStorage();
  });
});

tickThroughput();
tickStorage();
setInterval(tickThroughput, FAST_MS);
setInterval(tickStorage, SLOW_MS);
