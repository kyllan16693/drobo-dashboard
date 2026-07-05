"use strict";

const CONFIG = window.DROBO_CONFIG || { host: "", pollInterval: 15 };
const REFRESH_MS = Math.max(5, Number(CONFIG.pollInterval) || 15) * 1000;
const DOT_COUNT = 10;

const SEVERITIES = ["ok", "warning", "critical", "info", "empty", "unknown"];
const LED_SEVERITIES = ["ok", "warning", "critical", "empty"];

const $ = (id) => document.getElementById(id);

function sevClass(sev) {
  return SEVERITIES.includes(sev) ? sev : "unknown";
}

// Map a slot severity to one of the four LED colours; anything unexpected
// falls back to the dim/empty look rather than a misleading green.
function ledClass(sev) {
  return LED_SEVERITIES.includes(sev) ? sev : "empty";
}

function setPill(el, sev, label) {
  el.className = "pill sev-" + sevClass(sev);
  const lbl = el.querySelector(".pill-label");
  if (lbl) lbl.textContent = label == null ? "" : label;
}

// Split "4.00 TB" -> { num: "4", unit: "TB" }; strips a trailing ".00".
function splitCap(human) {
  const m = String(human == null ? "" : human).trim().match(/^([\d.,]+)\s*(.*)$/);
  if (!m) return { num: String(human || "–"), unit: "" };
  const n = parseFloat(m[1]);
  const num = Number.isFinite(n) && Number.isInteger(n) ? String(n) : m[1];
  return { num, unit: m[2] };
}

function relTime(epochSeconds) {
  if (!epochSeconds) return "never";
  const secs = Math.max(0, Math.round(Date.now() / 1000 - epochSeconds));
  if (secs < 5) return "just now";
  if (secs < 60) return secs + "s ago";
  const mins = Math.round(secs / 60);
  if (mins < 60) return mins + "m ago";
  const hrs = Math.round(mins / 60);
  return hrs + "h ago";
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function render(snap) {
  const st = snap.status;
  const banner = $("banner");

  // No usable data: show the unreachable state and bail.
  if (!st) {
    setPill($("overall-pill"), "unknown", "No data");
    setPill($("si-health"), "unknown", "No data");
    banner.className = "banner critical";
    banner.textContent = "Cannot reach the Drobo at " + snap.host + ":" + snap.port +
      (snap.last_error ? " — " + snap.last_error : "");
    $("bays").innerHTML = '<div class="bays-placeholder muted">No drive data available.</div>';
    $("cache").className = "cache hidden";
    $("array-dots").innerHTML = "";
    $("array-caption").textContent = "–";
    $("freshness").innerHTML = '<span class="stale-tag">unreachable</span>';
    return;
  }

  // Header + overall status.
  $("device-name").textContent = st.name || "Drobo";
  $("device-model").textContent = st.model || "";
  setPill($("overall-pill"), st.status_severity, st.status_label);

  // Banner priority: data protection > stale > drive soft errors > hidden.
  const errDrives = st.slots.filter((s) => s.error_count > 0);
  if (st.data_protection_in_progress) {
    banner.className = "banner warning";
    banner.textContent = "Data protection in progress — do not remove any drives." +
      (st.relayout_count ? " (relayout blocks remaining: " + st.relayout_count + ")" : "");
  } else if (snap.stale) {
    banner.className = "banner warning";
    banner.textContent = "Showing last known-good data — the Drobo has not responded recently" +
      (snap.last_error ? " (" + snap.last_error + ")" : "") + ".";
  } else if (errDrives.length > 0) {
    const slots = errDrives.map((s) => "slot " + s.slot).join(", ");
    banner.className = "banner info";
    banner.textContent = errDrives.length + " drive" + (errDrives.length > 1 ? "s are" : " is") +
      " reporting soft errors (" + slots + ") — the Drobo still rates it healthy, but keep an eye on it.";
  } else {
    banner.className = "banner hidden";
  }

  // Drive bays (data bays only; the mSATA accelerator is rendered separately).
  const dataBays = st.slots.filter((s) => !s.is_accelerator);
  $("bays").innerHTML = dataBays.map(bayHtml).join("");

  // mSATA cache accelerator, highlighted in blue at the bottom.
  const accel = st.slots.find((s) => s.is_accelerator);
  const cacheEl = $("cache");
  if (accel && accel.present) {
    cacheEl.className = "cache";
    cacheEl.innerHTML = cacheHtml(accel);
  } else {
    cacheEl.className = "cache hidden";
    cacheEl.innerHTML = "";
  }

  // Array-fullness gauge: blue dots that fill proportionally to used_pct.
  renderArrayDots(st);

  // System Information panel.
  $("si-name").textContent = st.name || "–";
  $("si-model").textContent = st.model || "–";
  $("si-serial").textContent = st.device_serial || "–";
  setPill($("si-health"), st.status_severity, st.status_label);
  $("si-redundancy").textContent = st.data_protection_in_progress
    ? st.redundancy_label + " (rebuilding)"
    : st.redundancy_label;
  $("si-firmware").textContent = st.firmware_version || "–";
  $("si-firmware-date").textContent = st.firmware_release || "";
  $("si-droboapps").textContent = "DroboApps " + (st.droboapps_enabled ? "enabled" : "disabled");

  // Capacity bar.
  const pct = st.used_pct || 0;
  const fill = $("cap-fill");
  fill.style.width = Math.min(100, pct) + "%";
  let capColor = "var(--ok)";
  if (pct >= (st.red_threshold_pct || 95)) capColor = "var(--critical)";
  else if (pct >= (st.yellow_threshold_pct || 85)) capColor = "var(--warning)";
  fill.style.background = capColor;
  $("cap-yellow").style.left = (st.yellow_threshold_pct || 85) + "%";
  $("cap-red").style.left = (st.red_threshold_pct || 95) + "%";
  $("cap-used").textContent = st.used_human;
  $("cap-free").textContent = st.free_human;
  $("cap-total").textContent = st.total_human;
  $("cap-pct").textContent = pct.toFixed(1) + "% used · yellow at " +
    st.yellow_threshold_pct + "% · red at " + st.red_threshold_pct + "%";

  // Footer.
  const stale = snap.stale ? ' <span class="stale-tag">· stale</span>' : "";
  $("freshness").innerHTML = "updated " + relTime(snap.last_success) + stale;
}

function bayHtml(d) {
  // Use the device's own 0-based slot number so the bay label matches the
  // soft-error banner, the cache tile, and the /stats page (avoids pointing a
  // user at the wrong physical bay).
  const slotNo = "Slot " + Number(d.slot);
  if (!d.present) {
    return `<div class="bay empty">
      <span class="bay-index">${escapeHtml(slotNo)}</span>
      <div class="bay-main">
        <div class="bay-cap"><span class="num">Empty</span></div>
        <div class="bay-drive"><div class="bay-drive-name muted">No drive installed</div></div>
        <div class="bay-metric"></div>
        <div class="bay-metric"></div>
      </div>
      <div class="bay-led led-empty"></div>
    </div>`;
  }
  const cap = splitCap(d.capacity_human);
  const name = [d.vendor, d.model].filter(Boolean).join(" ") || "Drive";
  const typeClass = d.disk_type === "SSD" ? "ssd" : "";
  const led = "led-" + ledClass(d.status_severity);
  const hasTemp = Number(d.temperature) > 0;
  const tempVal = hasTemp ? escapeHtml(d.temperature + "\u00B0C") : "\u2014";
  const errWarn = Number(d.error_count) > 0 ? " err-warn" : "";
  return `<div class="bay">
    <span class="bay-index">${escapeHtml(slotNo)}</span>
    <div class="bay-main">
      <div class="bay-cap"><span class="num">${escapeHtml(cap.num)}</span><span class="unit">${escapeHtml(cap.unit)}</span></div>
      <div class="bay-drive">
        <div class="bay-drive-name" title="${escapeHtml(name)} · ${escapeHtml(d.serial)}">${escapeHtml(name)}</div>
        <div class="bay-drive-meta">
          <span class="badge ${typeClass}">${escapeHtml(d.disk_type)}</span>
          <span>${escapeHtml(d.status_label)}</span>
        </div>
      </div>
      <div class="bay-metric">
        <div class="m-label">Temp</div>
        <div class="m-value ${hasTemp ? "" : "na"}"${hasTemp ? "" : ' title="not reported by firmware"'}>${tempVal}</div>
      </div>
      <div class="bay-metric">
        <div class="m-label">Errors</div>
        <div class="m-value${errWarn}">${escapeHtml(String(d.error_count))}</div>
      </div>
    </div>
    <div class="bay-led ${led}"></div>
  </div>`;
}

function cacheHtml(a) {
  const cap = splitCap(a.capacity_human);
  const name = [a.vendor, a.model].filter(Boolean).join(" ") || "mSATA SSD";
  return `<div class="cache-icon">SSD</div>
    <div class="cache-body">
      <span class="cache-badge">mSATA Cache Accelerator</span>
      <div class="cache-name" title="${escapeHtml(name)} · ${escapeHtml(a.serial)}">${escapeHtml(name)}</div>
      <div class="cache-meta">Slot ${escapeHtml(String(a.slot))} · ${escapeHtml(a.status_label)} · FW ${escapeHtml(a.firmware)}</div>
    </div>
    <div class="cache-cap"><span class="num">${escapeHtml(cap.num)}</span><span class="unit">${escapeHtml(cap.unit)}</span></div>`;
}

function renderArrayDots(st) {
  const pct = st.used_pct || 0;
  let lit = Math.round((pct / 100) * DOT_COUNT);
  if (pct > 0 && lit === 0) lit = 1;
  lit = Math.max(0, Math.min(DOT_COUNT, lit));

  let tint = "";
  if (pct >= (st.red_threshold_pct || 95)) tint = " crit";
  else if (pct >= (st.yellow_threshold_pct || 85)) tint = " warn";

  let dots = "";
  for (let i = 0; i < DOT_COUNT; i++) {
    dots += `<span class="array-dot${i < lit ? " lit" + tint : ""}"></span>`;
  }
  $("array-dots").innerHTML = dots;
  $("array-caption").innerHTML = "<b>" + escapeHtml(st.used_human) + "</b> of <b>" +
    escapeHtml(st.total_human) + "</b> used · " + pct.toFixed(1) + "% full";
}

async function tick() {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    render(await res.json());
  } catch (err) {
    const banner = $("banner");
    banner.className = "banner critical";
    banner.textContent = "Dashboard server unreachable: " + err;
  }
}

// Live network throughput sparkline (SSH-sourced). Refreshed on its own faster
// cadence so it feels live; degrades quietly if SSH is unavailable.
const TP_MS = 4000;
const TP_STATE_LABEL = { ok: "live", auth_failed: "auth failed", unreachable: "offline", idle: "…", disabled: "off" };

async function tickThroughput() {
  const el = $("tp-mini-chart");
  if (!el || typeof Charts === "undefined") return;
  try {
    const d = await (await fetch("/api/throughput", { cache: "no-store" })).json();
    $("tp-mini-state").textContent = d.enabled ? (TP_STATE_LABEL[d.state] || d.state) : "off";
    const latest = d.latest;
    $("tp-mini-rx").textContent = latest ? Charts.fmtBps(latest.rx_bps) : "–";
    $("tp-mini-tx").textContent = latest ? Charts.fmtBps(latest.tx_bps) : "–";
    const samples = d.samples || [];
    const empty = d.state === "auth_failed"
      ? "SSH auth failed — check .env password"
      : (d.enabled ? "collecting…" : "throughput off (no SSH creds)");
    Charts.area(el, [
      { values: samples.map((s) => ({ t: s.ts, v: s.rx_bps })), color: "#2ea6ff", label: "rx" },
      { values: samples.map((s) => ({ t: s.ts, v: s.tx_bps })), color: "#57c23a", label: "tx" },
    ], { height: 74, fmtY: Charts.fmtBps, emptyText: empty });
  } catch (err) {
    /* leave last-rendered state */
  }
}

// Live system (CPU/RAM/load) mini-panel, SSH-sourced like throughput.
const SYS_MS = 4000;
function sysUsageColor(pct) {
  return pct >= 90 ? "var(--critical)" : pct >= 70 ? "var(--warning)" : "var(--ok)";
}
function fmtUptime(sec) {
  sec = Math.floor(Number(sec) || 0);
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d > 0) return d + "d " + h + "h";
  if (h > 0) return h + "h " + m + "m";
  return m + "m";
}
async function tickSystem() {
  const stateEl = $("sys-state");
  if (!stateEl) return;
  try {
    const d = await (await fetch("/api/hardware", { cache: "no-store" })).json();
    const s = d.latest;
    stateEl.textContent = d.enabled ? (TP_STATE_LABEL[d.state] || d.state) : "off";
    if (s) {
      if (s.cpu_pct != null) {
        $("sys-cpu").textContent = s.cpu_pct.toFixed(0) + "%";
        $("sys-cpu-bar").style.width = Math.min(100, s.cpu_pct) + "%";
        $("sys-cpu-bar").style.background = sysUsageColor(s.cpu_pct);
      }
      if (s.mem_used_pct != null) {
        $("sys-ram").textContent = s.mem_used_pct.toFixed(0) + "%";
        $("sys-ram-bar").style.width = Math.min(100, s.mem_used_pct) + "%";
        $("sys-ram-bar").style.background = sysUsageColor(s.mem_used_pct);
      }
      if (s.load1 != null) $("sys-load").textContent = s.load1.toFixed(2);
      if (s.uptime_sec != null) $("sys-uptime").textContent = fmtUptime(s.uptime_sec);
    }
  } catch (err) {
    /* leave last-rendered state */
  }
}

tick();
setInterval(tick, REFRESH_MS);
tickThroughput();
setInterval(tickThroughput, TP_MS);
tickSystem();
setInterval(tickSystem, SYS_MS);
